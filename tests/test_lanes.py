"""Le corsie: reazioni, salvataggio e risposta, /stato, tentativi, errori, ripresa dopo un arresto."""

import asyncio
import logging
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from sazed_agent import intake
from sazed_agent.claude import Claude
from sazed_agent.config import Reactions
from sazed_agent.coppermind import Coppermind, Draft, Item, Outcome
from sazed_agent.journal import Journal
from sazed_agent.lanes import Processor, acquired_text, lane_for
from sazed_agent.messages import messages_for
from sazed_agent.telegram import Telegram, TelegramError
from tests import fake_claude
from tests.fake_telegram import TOKEN, FakeTelegram, url_entities

SAVED_LINK_IT = "Ciao Ada, ho salvato il link. Il contenuto lo leggerò più avanti."
PI_DAY_2025 = 1_741_953_600  # 14 marzo 2025, mezzogiorno UTC


def update(update_id, text=None, lang="it", edited=False, **fields):
    """Un messaggio di Ada; senza testo né altri campi, un link diverso per ogni update_id."""
    if text is None and not fields:
        text = f"https://example.com/ricetta/{update_id}"
    msg = {"message_id": update_id * 10, "chat": {"id": 42, "type": "private"},
           "from": {"id": 42, "first_name": "Ada", "language_code": lang}, **fields}
    if text is not None:
        msg.update(text=text, entities=url_entities(text))
    return {"update_id": update_id, "edited_message" if edited else "message": msg}


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
        self.memory = Coppermind(self.journal.db)
        self.stop = asyncio.Event()

    def processor(self, token=TOKEN, delays=(0.05, 0.1), claude=None):
        return Processor(self.journal, Telegram(self.telegram.api_base, token, timeout=5), "Custode", "auto",
                         Reactions("👀", "👌", "😢"), delays, typing_interval=0.05, idle_wait=5.0,
                         config_pause=30.0, memory=self.memory, claude=claude)

    def with_claude(self, key=fake_claude.KEY):
        """Un finto Claude, e il processore che lo usa."""
        self.claude = fake_claude.FakeClaude().start()
        self.addCleanup(self.claude.stop)
        return self.processor(claude=Claude(key, self.claude.api_base, "claude-haiku-5-5", timeout=5, max_retries=0))

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

    def replies(self):
        return [prm["text"] for _, prm in self.telegram.ok_calls("sendMessage")]

    async def answer(self, *updates, processor=None):
        """Elabora i messaggi uno dopo l'altro e restituisce le risposte, nello stesso ordine."""
        p = await self.start(processor)
        for upd in updates:
            self.record(p, upd)
            await self.until(lambda: self.state(upd["update_id"]) == "done")
        return self.replies()

    async def test_a_link_is_saved_then_answered_with_reactions(self):
        p = await self.start()
        self.record(p, update(1))
        await self.until(lambda: self.state(1) == "done")
        methods = [m for m, _ in self.telegram.ok_calls()]
        self.assertEqual(methods, ["setMessageReaction", "sendChatAction", "sendMessage", "setMessageReaction"])
        reactions = [c[1]["reaction"][0]["emoji"] for c in self.telegram.ok_calls("setMessageReaction")]
        self.assertEqual(reactions, ["👀", "👌"])
        _, sent = self.telegram.ok_calls("sendMessage")[0]
        self.assertEqual(sent["text"], SAVED_LINK_IT)
        self.assertEqual(sent["reply_parameters"]["message_id"], 10)
        self.assertEqual(self.journal.get(1).reply_message_id, 9000)
        [item] = self.memory.of_message(42, 10)
        self.assertEqual((item.url, item.platform, item.extraction), ("https://example.com/ricetta/1", "web", "pending"))

    async def test_english_user_gets_english_texts(self):
        self.assertEqual(await self.answer(update(1, lang="en")), ["Hi Ada, I saved the link. I'll read its content later."])

    async def test_a_note_is_saved_as_written(self):
        replies = await self.answer(update(1, "riso carnaroli\nda comprare"))
        self.assertEqual(replies, ["Ciao Ada, ho salvato la tua nota."])
        self.assertEqual(self.memory.of_message(42, 10)[0].note, "riso carnaroli\nda comprare")

    async def test_platforms_and_several_links_are_named(self):
        replies = await self.answer(update(1, "https://youtu.be/abc?si=x"),
                                    update(2, "https://example.com/a e https://example.com/b"))
        self.assertEqual(replies, ["Ciao Ada, ho salvato il link di YouTube. Il contenuto lo leggerò più avanti.",
                                   "Ciao Ada, ho salvato i 2 link. Il contenuto lo leggerò più avanti."])

    async def test_a_link_sent_again_is_not_duplicated_and_says_since_when(self):
        first = update(1, "https://www.youtube.com/watch?v=abc")
        first["message"]["date"] = PI_DAY_2025
        replies = await self.answer(first, update(2, "https://youtu.be/abc rifallo con meno sale"))
        self.assertEqual(replies[1], "Ciao Ada, il link di YouTube era già tra ciò che custodisco, dal 14 marzo 2025.")
        [item] = self.memory.search(limit=None).items
        self.assertEqual((item.message_id, item.note), (10, "rifallo con meno sale"))

    async def test_a_photo_with_a_link_in_the_caption(self):
        caption = "tramonto https://example.com/scilla"
        replies = await self.answer(update(1, photo=[{"file_id": "p", "file_unique_id": "P"}], caption=caption,
                                           caption_entities=url_entities(caption)))
        self.assertEqual(replies, ["Ciao Ada, ho salvato la foto e il link. Il contenuto lo leggerò più avanti."])
        self.assertEqual([i.source for i in self.memory.of_message(42, 10)], ["photo", "link"])

    async def test_an_edited_message_updates_its_item(self):
        replies = await self.answer(update(1, "riso carnaroli"), update(2, "riso venere", edited=True, message_id=10))
        self.assertEqual(replies, ["Ciao Ada, ho salvato la tua nota.", "Ciao Ada, ho aggiornato la tua nota."])
        self.assertEqual([i.note for i in self.memory.search(limit=None).items], ["riso venere"])

    async def test_unknown_commands_and_stickers_are_answered_but_not_saved(self):
        replies = await self.answer(update(1, "/aiuto"), update(2, sticker={"file_id": "s", "file_unique_id": "S"}))
        self.assertEqual(replies, ["Ciao Ada, non conosco il comando /aiuto.",
                                   "Ciao Ada, questo tipo di messaggio non so ancora custodirlo."])
        self.assertEqual(self.memory.count(), 0)

    async def test_resume_after_saving_saves_nothing_twice_and_answers_the_same(self):
        upd = update(1)
        self.journal.record(1, upd, "acquisition")
        self.journal.advance(1, "read")
        with self.journal.transaction():  # arresto subito dopo il commit, prima della risposta
            self.memory.save(42, 10, 1, intake.read(upd).drafts)
            self.journal.advance(1, "saved")
        await self.start()
        await self.until(lambda: self.state(1) == "done")
        self.assertEqual(self.replies(), [SAVED_LINK_IT])
        self.assertEqual(self.memory.count(), 1)

    async def test_claude_describes_a_note_and_the_reply_names_it(self):
        p = self.with_claude()
        self.claude.answer(lambda m: {"intent": "save", "question": None, "items": [
            {"index": 0, "kind": "note", "title": "Spesa di sabato", "topics": ["Spesa", "riso"],
             "summary": "Cose da comprare."}]})
        replies = await self.answer(update(1, "riso carnaroli\nparmigiano"), processor=p)
        self.assertEqual(replies, ["Ciao Ada, ho salvato la tua nota «Spesa di sabato»."])
        self.assertEqual(self.claude.messages(), [{"language": "it", "words": "riso carnaroli\nparmigiano",
                                                   "items": [{"index": 0, "source": "note"}]}])
        [item] = self.memory.of_message(42, 10)
        self.assertEqual((item.kind, item.title, item.topics, item.summary),
                         ("note", "Spesa di sabato", ["spesa", "riso"], "Cose da comprare."))
        self.assertEqual(self.memory.search("sabato").total, 1)  # titolo e argomenti si cercano

    async def test_a_question_waits_for_its_answer_and_nothing_is_saved(self):
        p = self.with_claude()
        self.claude.answer(lambda m: {"intent": "question", "question": "Dove ho messo la carbonara?", "items": []})
        replies = await self.answer(update(1, "dove ho messo la carbonara?"), update(2, "where?", lang="en"),
                                    processor=p)
        self.assertEqual(replies, ["Ciao Ada, alle domande saprò rispondere presto: per ora non ho salvato nulla.",
                                   "Hi Ada, I'll be able to answer questions soon: for now I haven't saved anything."])
        self.assertEqual(self.memory.count(), 0)
        self.assertEqual(self.journal.get(1).interpretation["question"], "Dove ho messo la carbonara?")
        self.assertEqual([m["language"] for m in self.claude.messages()], ["it", "en"])

    async def test_a_link_with_a_question_is_saved_and_the_question_waits(self):
        p = self.with_claude()
        self.claude.answer(lambda m: {"intent": "question", "question": "La faccio stasera?", "items": [
            {"index": 0, "kind": "recipe", "title": "Carbonara", "topics": ["uova"], "summary": None}]})
        replies = await self.answer(update(1, "https://example.com/carbonara la faccio stasera?"), processor=p)
        self.assertEqual(replies, ["Ciao Ada, ho salvato il link «Carbonara». Il contenuto lo leggerò più avanti. "
                                   "Alla tua domanda saprò rispondere presto."])
        self.assertEqual([i.kind for i in self.memory.search(kind="recipe").items], ["recipe"])

    async def test_without_words_claude_is_not_asked(self):
        p = self.with_claude()
        await self.answer(update(1, photo=[{"file_id": "p", "file_unique_id": "P"}]), update(2, "/aiuto"),
                          update(3, sticker={"file_id": "s", "file_unique_id": "S"}), processor=p)
        self.assertEqual(self.claude.requests, [])
        self.assertEqual(self.memory.count(), 1)

    async def test_a_refusal_keeps_the_note_without_a_description(self):
        p = self.with_claude()
        self.claude.stop_with("refusal")
        self.assertEqual(await self.answer(update(1, "una nota qualunque"), processor=p),
                         ["Ciao Ada, ho salvato la tua nota."])
        self.assertIsNone(self.memory.of_message(42, 10)[0].kind)

    async def test_claude_down_is_retried(self):
        p = self.with_claude()
        self.claude.fail(500)
        self.assertEqual(await self.answer(update(1, "riso"), processor=p), ["Ciao Ada, ho salvato la tua nota."])
        self.assertEqual((self.journal.get(1).attempts, len(self.claude.requests)), (1, 2))

    async def test_a_rejected_claude_key_waits_without_using_attempts(self):
        p = await self.start(self.with_claude(key="sk-ant-api03-SBAGLIATA"))
        self.record(p, update(1, "riso"))
        await self.until(lambda: self.journal.get(1).last_error is not None)
        entry = self.journal.get(1)
        self.assertEqual((entry.state, entry.attempts, self.memory.count()), ("read", 0, 0))
        self.assertIn("chiave dell'API di Claude rifiutata", entry.last_error)
        self.assertNotIn("SBAGLIATA", entry.last_error)

    async def test_the_interpretation_is_asked_once_even_after_a_crash(self):
        upd = update(1, "https://example.com/zucca")
        self.journal.record(1, upd, "acquisition")
        self.journal.advance(1, "read")
        self.journal.set_interpretation(1, {"intent": "save", "question": None, "items": [
            {"index": 0, "kind": "recipe", "title": "Vellutata", "topics": ["zucca"], "summary": None}]})
        await self.start(self.with_claude())  # arresto dopo l'interpretazione, prima del salvataggio
        await self.until(lambda: self.state(1) == "done")
        self.assertEqual(self.claude.requests, [])
        self.assertEqual(self.replies(), ["Ciao Ada, ho salvato il link «Vellutata». Il contenuto lo leggerò più avanti."])
        self.assertEqual(self.memory.of_message(42, 10)[0].topics, ["zucca"])

    async def test_status_does_not_wait_behind_a_long_acquisition(self):
        self.telegram.slow(text_has("ho salvato"), 1.5)
        p = await self.start()
        self.record(p, update(1))
        await self.until(lambda: self.state(1) == "saved")
        self.record(p, update(2, "/stato"))
        await self.until(lambda: self.state(2) == "done")
        self.assertEqual(self.state(1), "saved")  # l'acquisizione è ancora in corso
        self.assertEqual(self.telegram.ok_calls("sendMessage")[0][1]["text"],
                         "Ciao Ada, l'agente Custode è collegato. Messaggi da processare: 1, in errore: 0. "
                         "Elementi custoditi: 1.")
        await self.until(lambda: self.state(1) == "done")

    async def test_transient_error_is_retried(self):
        self.telegram.fail("sendMessage", 500)
        p = await self.start()
        self.record(p, update(1))
        await self.until(lambda: self.state(1) == "done")
        self.assertEqual(self.journal.get(1).attempts, 1)
        self.assertEqual(len(self.telegram.ok_calls("sendMessage")), 1)

    async def test_retries_exhausted_the_item_fails_is_reported_and_others_go_on(self):
        self.telegram.rule(lambda m, prm: 500 if text_has("ho salvato")(m, prm) and prm["reply_parameters"]["message_id"] == 10 else None)
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
        self.telegram.rule(lambda m, prm: 400 if text_has("ho salvato")(m, prm) else None)
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
        self.assertEqual((entry.state, entry.attempts), ("saved", 0))  # salvato: manca solo la risposta
        self.assertEqual(self.memory.count(), 1)
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


def item(source, extraction="pending", platform=None, sent_at=PI_DAY_2025):
    return Item(id=1, chat_id=42, message_id=10, source=source, url=None, platform=platform, site=None,
                kind=None, title=None, topics=[], summary=None, note=None, content=None, extraction=extraction,
                place=None, latitude=None, longitude=None, taken_at=None, media={}, sent_at=sent_at, saved_at=0)


class AcquiredText(unittest.TestCase):
    it, en = messages_for("it"), messages_for("en")

    def text(self, t, *outcomes, edited=False, year=2026):
        reading = intake.Reading((Draft("note"),), None, None, edited)
        return acquired_text(t, reading, list(outcomes), year)

    def test_saved_and_already_kept_in_one_answer(self):
        self.assertEqual(self.text(self.it, Outcome(item("photo"), "saved"), Outcome(item("link", platform="tiktok"), "already")),
                         "ho salvato la foto. Il contenuto lo leggerò più avanti. "
                         "Il link di TikTok era già tra ciò che custodisco, dal 14 marzo 2025.")
        self.assertEqual(self.text(self.en, Outcome(item("voice"), "saved"), Outcome(item("link"), "already")),
                         "I saved the voice message. I'll read its content later. "
                         "The link was already among the things I keep, since 14 March 2025.")

    def test_this_year_needs_no_year(self):
        self.assertEqual(self.text(self.it, Outcome(item("document"), "already"), year=2025),
                         "il documento era già tra ciò che custodisco, dal 14 marzo.")

    def test_several_already_kept(self):
        self.assertEqual(self.text(self.it, Outcome(item("link"), "already"), Outcome(item("link"), "already")),
                         "2 di questi erano già tra ciò che custodisco.")

    def test_an_edited_note_needs_no_reading(self):
        self.assertEqual(self.text(self.it, Outcome(item("note", "complete"), "saved"), edited=True),
                         "ho aggiornato la tua nota.")


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
