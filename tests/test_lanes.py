"""Le corsie: reazioni, risposta provvisoria, /stato, tentativi, errori, ripresa dopo un arresto."""

import asyncio
import logging
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from sazed_agent.config import Reactions
from sazed_agent.journal import Journal
from sazed_agent.lanes import Processor, lane_for
from sazed_agent.telegram import Telegram, TelegramError
from tests.fake_telegram import TOKEN, FakeTelegram

RECEIVED_IT = "Ciao Ada, ho ricevuto il tuo messaggio: per ora lo custodisco, presto saprò leggerlo."


def update(update_id, text="https://example.com/ricetta", lang="it"):
    return {"update_id": update_id,
            "message": {"message_id": update_id * 10, "chat": {"id": 42, "type": "private"},
                        "from": {"id": 42, "first_name": "Ada", "language_code": lang}, "text": text}}


def text_has(fragment):
    return lambda method, params: method == "sendMessage" and fragment in params.get("text", "")


class LanesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.telegram = FakeTelegram().start()
        self.addCleanup(self.telegram.stop)
        self.journal = Journal(self.dir / "coppermind.db")
        self.addCleanup(self.journal.close)
        self.stop = asyncio.Event()

    def processor(self, token=TOKEN, delays=(0.05, 0.1)):
        return Processor(self.journal, Telegram(self.telegram.api_base, token, timeout=5), "Custode", "auto",
                         Reactions("👀", "👌", "😢"), delays, typing_interval=0.05, idle_wait=5.0,
                         config_pause=30.0)

    async def start(self, processor=None):
        processor = processor or self.processor()
        task = asyncio.create_task(processor.run(self.stop))

        async def finish():
            self.stop.set()
            await asyncio.wait_for(task, 10)
        self.addAsyncCleanup(finish)
        return processor

    def record(self, processor, upd):
        self.journal.record(upd["update_id"], upd, lane_for(upd))
        processor.notify()

    async def until(self, condition, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return
            await asyncio.sleep(0.02)
        self.fail("condizione non raggiunta in tempo")

    def state(self, update_id):
        return self.journal.get(update_id).state

    async def test_message_gets_reactions_and_a_provisional_reply(self):
        p = await self.start()
        self.record(p, update(1))
        await self.until(lambda: self.state(1) == "done")
        methods = [m for m, _ in self.telegram.ok_calls()]
        self.assertEqual(methods, ["setMessageReaction", "sendChatAction", "sendMessage", "setMessageReaction"])
        reactions = [c[1]["reaction"][0]["emoji"] for c in self.telegram.ok_calls("setMessageReaction")]
        self.assertEqual(reactions, ["👀", "👌"])
        _, sent = self.telegram.ok_calls("sendMessage")[0]
        self.assertEqual(sent["text"], RECEIVED_IT)
        self.assertEqual(sent["reply_parameters"]["message_id"], 10)
        self.assertEqual(self.journal.get(1).reply_message_id, 9000)

    async def test_english_user_gets_english_texts(self):
        p = await self.start()
        self.record(p, update(1, lang="en"))
        await self.until(lambda: self.state(1) == "done")
        self.assertEqual(self.telegram.ok_calls("sendMessage")[0][1]["text"],
                         "Hi Ada, I received your message: for now I'm keeping it safe, soon I'll be able to read it.")

    async def test_status_does_not_wait_behind_a_long_acquisition(self):
        self.telegram.slow(text_has("custodisco"), 1.5)
        p = await self.start()
        self.record(p, update(1))
        await self.until(lambda: self.state(1) == "read")
        self.record(p, update(2, "/stato"))
        await self.until(lambda: self.state(2) == "done")
        self.assertEqual(self.state(1), "read")  # l'acquisizione è ancora in corso
        self.assertEqual(self.telegram.ok_calls("sendMessage")[0][1]["text"],
                         "Ciao Ada, l'agente Custode è collegato. Messaggi da processare: 1, in errore: 0.")
        await self.until(lambda: self.state(1) == "done")

    async def test_transient_error_is_retried(self):
        self.telegram.fail("sendMessage", 500)
        p = await self.start()
        self.record(p, update(1))
        await self.until(lambda: self.state(1) == "done")
        self.assertEqual(self.journal.get(1).attempts, 1)
        self.assertEqual(len(self.telegram.ok_calls("sendMessage")), 1)

    async def test_retries_exhausted_the_item_fails_is_reported_and_others_go_on(self):
        self.telegram.rule(lambda m, prm: 500 if text_has("custodisco")(m, prm) and prm["reply_parameters"]["message_id"] == 10 else None)
        p = await self.start()
        self.record(p, update(1))
        self.record(p, update(2))
        await self.until(lambda: self.state(1) == "failed" and self.state(2) == "done")
        self.assertEqual(self.journal.get(1).attempts, 3)  # due attese configurate, tre tentativi
        notices = [prm["text"] for _, prm in self.telegram.ok_calls("sendMessage") if "riuscito" in prm["text"]]
        self.assertEqual(notices, ["Ciao Ada, non sono riuscito a elaborare questo messaggio dopo 3 tentativi: "
                                   "lo tengo da parte."])
        reactions = [prm["reaction"][0]["emoji"] for _, prm in self.telegram.ok_calls("setMessageReaction")
                     if prm["message_id"] == 10]
        self.assertEqual(reactions, ["👀", "😢"])

    async def test_permanent_error_fails_at_once(self):
        self.telegram.rule(lambda m, prm: 400 if text_has("custodisco")(m, prm) else None)
        p = await self.start()
        self.record(p, update(1))
        await self.until(lambda: self.state(1) == "failed")
        entry = self.journal.get(1)
        self.assertEqual(entry.attempts, 1)
        self.assertIn("errore finto 400", entry.last_error)
        self.assertIn("dopo 1 tentativo:", [prm["text"] for _, prm in self.telegram.ok_calls("sendMessage")][0])

    async def test_rate_limit_waits_as_long_as_telegram_asks(self):
        self.telegram.fail("sendMessage", 429)
        p = await self.start(self.processor(delays=(0.05,)))
        self.record(p, update(1))
        await self.until(lambda: self.journal.get(1).attempts == 1)
        self.assertGreater(self.journal.get(1).next_attempt_at - time.time(), 0.8)  # retry_after = 1
        await self.until(lambda: self.state(1) == "done", timeout=4)

    async def test_resume_after_a_crash_never_resends_a_recorded_reply(self):
        # Arresto dopo l'invio della risposta e la sua annotazione, prima del cambio di stato.
        self.journal.record(1, update(1), "acquisition")
        self.journal.advance(1, "read")
        self.journal.set_reply(1, 555)
        # Arresto dopo la risposta, prima della reazione finale.
        self.journal.record(2, update(2), "acquisition")
        self.journal.advance(2, "answered", reply_message_id=556)
        await self.start()
        await self.until(lambda: self.state(1) == "done" and self.state(2) == "done")
        self.assertEqual(self.telegram.ok_calls("sendMessage"), [])
        self.assertEqual(self.journal.get(1).reply_message_id, 555)

    async def test_refused_reaction_does_not_block_the_message(self):
        self.telegram.rule(lambda m, prm: 400 if m == "setMessageReaction" else None)
        p = await self.start()
        self.record(p, update(1))
        await self.until(lambda: self.state(1) == "done")
        self.assertEqual(len(self.telegram.ok_calls("sendMessage")), 1)

    async def test_rejected_token_postpones_without_using_attempts(self):
        p = await self.start(self.processor(token="999:SBAGLIATO"))
        self.record(p, update(1))
        await self.until(lambda: self.journal.get(1).last_error is not None)
        entry = self.journal.get(1)
        self.assertEqual((entry.state, entry.attempts), ("read", 0))
        self.assertIn("token del bot rifiutato", entry.last_error)
        self.assertGreater(entry.next_attempt_at - time.time(), 25)

    async def test_wakes_up_at_once_when_notified(self):
        p = await self.start()  # idle_wait è 5 s: senza l'avviso aspetterebbe
        await asyncio.sleep(0.2)
        started = time.monotonic()
        self.record(p, update(1))
        await self.until(lambda: self.state(1) == "done")
        self.assertLess(time.monotonic() - started, 2)

    async def test_each_completed_item_is_logged_without_its_text(self):
        with self.assertLogs("sazed_agent", level="INFO") as logs:
            p = await self.start()
            self.record(p, update(1, text="la ricetta segreta della nonna"))
            self.record(p, update(2, text="/stato"))
            await self.until(lambda: self.state(1) == "done" and self.state(2) == "done")
        joined = "\n".join(logs.output)
        self.assertIn("elemento 1 completato: risposta inviata", joined)
        self.assertIn("elemento 2 completato: stato inviato", joined)
        self.assertNotIn("ricetta segreta", joined)  # il contenuto dei messaggi non va nei log

    async def test_token_never_appears_in_logs(self):
        self.telegram.fail("sendMessage", 500, 400)
        self.telegram.fail("setMessageReaction", 400)
        with self.assertLogs("sazed_agent", level="INFO") as logs:
            p = await self.start(self.processor(delays=(0.05,)))
            self.record(p, update(1))
            await self.until(lambda: self.state(1) == "failed")
            await asyncio.sleep(0.2)
        joined = "\n".join(logs.output)
        self.assertIn("errore finto 500", joined)  # gli errori ci sono, il token no
        self.assertNotIn(TOKEN, joined)
        self.assertNotIn("TEST-TOKEN", joined)


class TelegramErrors(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeTelegram().start()
        self.addCleanup(self.fake.stop)
        self.telegram = Telegram(self.fake.api_base, TOKEN, timeout=5)

    async def error(self, status):
        self.fake.fail("sendMessage", status)
        with self.assertRaises(TelegramError) as ctx:
            await self.telegram.send_message(42, "x")
        return ctx.exception

    async def test_classification(self):
        self.assertTrue((await self.error(400)).permanent)
        self.assertTrue((await self.error(403)).permanent)
        err = await self.error(429)
        self.assertEqual((err.permanent, err.retry_after), (False, 1))
        err = await self.error(500)
        self.assertFalse(err.permanent or err.config_error)

    async def test_rejected_token_is_a_configuration_error(self):
        telegram = Telegram(self.fake.api_base, "1:SBAGLIATO", timeout=5)
        with self.assertRaises(TelegramError) as ctx:
            await telegram.send_message(42, "x")
        self.assertTrue(ctx.exception.config_error)
        self.assertNotIn("SBAGLIATO", str(ctx.exception))

    async def test_unreachable_telegram_is_transient_and_hides_the_token(self):
        telegram = Telegram("http://127.0.0.1:9", TOKEN, timeout=2)  # porta chiusa
        with self.assertRaises(TelegramError) as ctx:
            await telegram.send_message(42, "x")
        self.assertFalse(ctx.exception.permanent or ctx.exception.config_error)
        self.assertNotIn(TOKEN, str(ctx.exception))

    async def test_message_id_is_returned(self):
        self.assertEqual(await self.telegram.send_message(42, "x", reply_to=7), 9000)
        _, params = self.fake.ok_calls("sendMessage")[0]
        self.assertEqual(params["reply_parameters"], {"message_id": 7, "allow_sending_without_reply": True})


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
