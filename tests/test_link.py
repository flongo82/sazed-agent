"""Connessione con il Worker: registro prima dell'ack, doppioni, guasti, riconnessioni."""

import asyncio
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from sazed_agent.journal import Journal
from sazed_agent.lanes import lane_for
from sazed_agent.link import Replaced, WorkerLink, agent_url
from tests.fake_worker import TOKEN, FakeWorker


def update(update_id, text="ciao"):
    return {"update_id": update_id,
            "message": {"message_id": update_id + 1, "chat": {"id": 42, "type": "private"},
                        "from": {"id": 42, "first_name": "Ada", "language_code": "it"}, "text": text}}


class RecordingJournal:
    """Il vero registro, che annota ogni scrittura riuscita negli eventi del finto Worker
    (così i test vedono l'ordine fra registrazione e ack) e può fallire a comando."""

    def __init__(self, journal, events, fail_times=0):
        self.journal, self.events, self.fail_times = journal, events, fail_times

    def record(self, queue_id, update, lane):
        if self.fail_times:
            self.fail_times -= 1
            raise OSError("disco pieno")
        new = self.journal.record(queue_id, update, lane)
        self.events.append(("record", queue_id))
        return new


class LinkTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.worker = await FakeWorker().start()
        self.addAsyncCleanup(self.worker.stop)
        self.journal = Journal(self.dir / "coppermind.db")
        self.addCleanup(self.journal.close)
        self.stop = asyncio.Event()
        self.recorded = 0

    def make_link(self, token=TOKEN, fail_times=0, wall=time.time):
        def on_recorded():
            self.recorded += 1
        journal = RecordingJournal(self.journal, self.worker.events, fail_times)
        return WorkerLink(self.worker.url, token, journal, lane_for, ping_interval=0.5,
                          pong_timeout=0.2, on_recorded=on_recorded, backoff=(0.05, 0.1, 0.2),
                          wall=wall, jitter=lambda a, b: 1.0)

    async def start(self, link):
        task = asyncio.create_task(link.run(self.stop))

        async def finish():
            self.stop.set()
            await asyncio.wait_for(task, 5)
        self.addAsyncCleanup(finish)
        return task

    async def until(self, condition, timeout=4.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return
            await asyncio.sleep(0.02)
        self.fail("condizione non raggiunta in tempo")

    async def test_backlog_is_recorded_before_each_ack(self):
        await self.worker.push(update(100))
        await self.worker.push(update(101, "/stato"))
        link = self.make_link()
        await self.start(link)
        await self.until(lambda: self.worker.acks == [1, 2])
        events = self.worker.events
        for queue_id in (1, 2):  # ogni ack arriva dopo la registrazione del suo elemento
            self.assertLess(events.index(("record", queue_id)), events.index(("ack", queue_id)), events)
        self.assertEqual((self.journal.get(100).lane, self.journal.get(101).lane), ("acquisition", "commands"))
        await self.until(lambda: link.worker_pending == 0)
        self.assertEqual(self.recorded, 2)

    async def test_live_message_is_recorded_at_once(self):
        await self.start(self.make_link())
        await self.until(lambda: self.worker.hellos == 1)
        await self.worker.push(update(200))
        await self.until(lambda: self.worker.acks == [1])
        self.assertEqual(self.journal.get(200).state, "received")

    async def test_no_ack_when_the_journal_cannot_write(self):
        await self.worker.push(update(300))
        await self.start(self.make_link(fail_times=1))
        await self.until(lambda: self.worker.acks == [1])
        self.assertEqual(self.worker.events, [("record", 1), ("ack", 1)])  # nessun ack prima
        self.assertEqual(self.worker.connections, 2)

    async def test_duplicates_are_recorded_once_and_acked_twice(self):
        await self.worker.push(update(400))
        await self.worker.push(update(400))  # lo stesso update riconsegnato con un altro id
        await self.start(self.make_link())
        await self.until(lambda: self.worker.acks == [1, 2])
        self.assertEqual(self.recorded, 1)
        self.assertEqual(self.journal.counts()["received"], 1)

    async def test_crash_between_record_and_ack(self):
        # La connessione cade subito dopo l'invio: il registro ha l'elemento, l'ack si perde.
        self.worker.close_after_update = True
        await self.worker.push(update(500))
        await self.start(self.make_link())
        await self.until(lambda: self.journal.get(500) is not None)
        self.worker.close_after_update = False
        await self.until(lambda: self.worker.acks == [1])
        self.assertEqual(self.recorded, 1)  # riconsegnato, riconosciuto, non rielaborato
        self.assertEqual(self.journal.get(500).state, "received")
        self.assertGreaterEqual(self.worker.connections, 2)

    async def test_wrong_token_retries_with_growing_delays(self):
        link = self.make_link(token="sbagliato")
        await self.start(link)
        await self.until(lambda: len(link.delays) >= 4)
        self.assertEqual(link.delays[:4], [0.05, 0.1, 0.2, 0.2])
        self.assertGreaterEqual(self.worker.rejected, 4)
        self.assertEqual(self.worker.connections, 0)

    async def test_reconnects_after_a_drop_and_asks_for_the_backlog_again(self):
        link = self.make_link()
        await self.start(link)
        await self.until(lambda: self.worker.hellos == 1 and link._healthy)
        await self.worker.drop()
        await self.until(lambda: self.worker.hellos == 2)
        self.assertEqual(link.delays, [0.05])  # la sessione andava bene: attesa più breve

    async def test_hung_connection_detected_by_missing_pong(self):
        self.worker.answer_pings = False
        await self.start(self.make_link())
        await self.until(lambda: self.worker.connections >= 2, timeout=3)

    async def test_suspend_triggers_an_immediate_reconnect(self):
        offset = 0.0
        link = self.make_link(wall=lambda: time.time() + offset)
        await self.start(link)
        await self.until(lambda: self.worker.hellos == 1)
        offset = 3600.0  # il computer è rimasto sospeso un'ora
        await self.until(lambda: self.worker.connections >= 2)
        self.assertEqual(link.delays, [])

    async def test_replaced_by_another_agent_stops_instead_of_reconnecting(self):
        link = self.make_link()
        task = asyncio.create_task(link.run(self.stop))
        await self.until(lambda: self.worker.hellos == 1)
        with self.assertLogs("sazed_agent.link", level="ERROR") as logs:
            await self.worker.replace()
            with self.assertRaises(Replaced):
                await asyncio.wait_for(task, 2)
        self.assertIn("un altro agente", logs.output[0])
        await asyncio.sleep(0.3)  # oltre la prima attesa prima di riprovare
        self.assertEqual(self.worker.connections, 1)

    async def test_frames_that_are_not_objects_are_ignored(self):
        link = self.make_link()
        await self.start(link)
        await self.until(lambda: self.worker.hellos == 1)
        for frame in ("null", "[]", "42", '"testo"', "{non è json"):
            await self.worker.send_raw(frame)
        queue_id = await self.worker.push(update(7))
        await self.until(lambda: queue_id in self.worker.acks)
        self.assertEqual(self.worker.connections, 1)  # nessuna riconnessione: la connessione regge

    async def test_stop_ends_the_link_promptly(self):
        link = self.make_link()
        task = asyncio.create_task(link.run(self.stop))
        await self.until(lambda: link.connected.is_set())
        self.stop.set()
        await asyncio.wait_for(task, 2)
        self.assertFalse(link.connected.is_set())


class Helpers(unittest.TestCase):
    def test_agent_url(self):
        self.assertEqual(agent_url("https://w.example"), "wss://w.example/agent")
        self.assertEqual(agent_url("https://w.example/"), "wss://w.example/agent")
        self.assertEqual(agent_url("http://127.0.0.1:8799"), "ws://127.0.0.1:8799/agent")
        with self.assertRaises(ValueError):
            agent_url("w.example")

    def test_lane_for(self):
        cases = {"/stato": "commands", "/status@sazed_bot": "commands", "/STATO adesso": "commands",
                 "ciao": "acquisition", "/start": "acquisition", "https://example.com": "acquisition"}
        for text, lane in cases.items():
            self.assertEqual(lane_for(update(1, text)), lane, text)
        self.assertEqual(lane_for({"update_id": 1, "message": {"photo": []}}), "acquisition")


if __name__ == "__main__":
    unittest.main()
