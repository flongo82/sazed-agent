"""Il giro completo, tutto finto tranne l'agente: Worker → registro → ack → corsie → Telegram."""

import asyncio
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from sazed_agent.config import Reactions
from sazed_agent.journal import Journal
from sazed_agent.lanes import Processor, lane_for
from sazed_agent.link import WorkerLink
from sazed_agent.telegram import Telegram
from tests import fake_telegram, fake_worker


def update(update_id, text):
    return {"update_id": update_id,
            "message": {"message_id": update_id * 10, "chat": {"id": 42, "type": "private"},
                        "from": {"id": 42, "first_name": "Ada", "language_code": "it"}, "text": text,
                        "entities": fake_telegram.url_entities(text)}}


class Flow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.worker = await fake_worker.FakeWorker().start()
        self.addAsyncCleanup(self.worker.stop)
        self.telegram = fake_telegram.FakeTelegram().start()
        self.addCleanup(self.telegram.stop)
        self.stop = asyncio.Event()

    async def run_agent(self):
        journal = Journal(self.dir / "coppermind.db")
        processor = Processor(journal, Telegram(self.telegram.api_base, fake_telegram.TOKEN, timeout=5),
                              "Custode", "auto", Reactions("👍", "😢", "🤷"), (0.05,),
                              typing_interval=0.05, idle_wait=5.0)
        link = WorkerLink(self.worker.url, fake_worker.TOKEN, journal, lane_for, 0.5, 0.2,
                          on_recorded=processor.notify, backoff=(0.05,), jitter=lambda a, b: 1.0)
        stop = asyncio.Event()
        task = asyncio.ensure_future(asyncio.gather(link.run(stop), processor.run(stop)))
        return journal, stop, task

    async def until(self, condition, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return
            await asyncio.sleep(0.02)
        self.fail("condizione non raggiunta in tempo")

    async def test_backlog_and_live_messages_are_answered(self):
        await self.worker.push(update(1, "https://example.com/ricetta"))  # arrivato a agente spento
        journal, stop, task = await self.run_agent()
        await self.until(lambda: self.worker.acks == [1])
        await self.worker.push(update(2, "/stato"))
        await self.worker.push(update(3, "nota: riso carnaroli"))
        await self.until(lambda: all(journal.get(i) and journal.get(i).state == "done" for i in (1, 2, 3)))
        self.assertEqual(self.worker.acks, [1, 2, 3])
        self.assertEqual(self.worker.queue, [])
        texts = {p["reply_parameters"]["message_id"]: p["text"] for _, p in self.telegram.ok_calls("sendMessage")}
        self.assertEqual(list(texts), [20])  # link e nota hanno solo il 👍
        self.assertTrue(texts[20].startswith("Ciao Ada, l'agente Custode è collegato."))
        thumbs = {p["message_id"]: p["reaction"][0]["emoji"] for _, p in self.telegram.ok_calls("setMessageReaction")}
        self.assertEqual(thumbs, {10: "👍", 30: "👍"})
        stop.set()
        await asyncio.wait_for(task, 5)
        journal.close()

    async def test_restart_resumes_unfinished_work_without_new_deliveries(self):
        # Primo avvio: Telegram non risponde, il messaggio resta da elaborare nel registro.
        self.telegram.rule(lambda m, p: 500 if m == "sendMessage" else None)
        await self.worker.push(update(1, "/aiuto"))
        journal, stop, task = await self.run_agent()
        await self.until(lambda: self.worker.acks == [1] and journal.get(1).attempts >= 1)
        stop.set()
        await asyncio.wait_for(task, 5)
        journal.close()
        self.assertEqual(self.worker.queue, [])  # il Worker non lo ha più: sta nel registro
        # Secondo avvio, Telegram di nuovo raggiungibile: il lavoro riprende dal registro.
        self.telegram._rules.clear()
        journal, stop, task = await self.run_agent()
        await self.until(lambda: journal.get(1).state == "done")
        self.assertEqual(len(self.telegram.ok_calls("sendMessage")), 1)
        stop.set()
        await asyncio.wait_for(task, 5)
        journal.close()


if __name__ == "__main__":
    unittest.main()
