"""Claude contro un finto server dell'API: le tre richieste, le risposte controllate, gli
strumenti delle risposte, rifiuti ed errori."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sazed_agent.claude import Answer, Claude, ClaudeError, Classification, Interpretation, view
from sazed_agent.coppermind import Coppermind, Draft
from sazed_agent.journal import Journal
from sazed_agent.reader import Extract
from tests.fake_claude import KEY, FakeClaude, description, images, interpretation, tool


class ClaudeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeClaude().start()
        self.addCleanup(self.fake.stop)
        self.claude = Claude(KEY, self.fake.api_base, "claude-haiku-5-5", timeout=5, max_retries=0)
        self.addAsyncCleanup(self.claude.close)
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        journal = Journal(self.dir / "coppermind.db")
        self.addCleanup(journal.close)
        self.memory = Coppermind(journal.db)
        [self.link, self.photo] = [o.item for o in self.memory.save(42, 10, 1_790_000_000, [
            Draft("link", url="https://www.youtube.com/watch?v=abc&si=x"),
            Draft("photo", media={"file_id": "f", "file_unique_id": "U", "file_size": 9})])]
        [self.old] = [o.item for o in self.memory.save(42, 9, 1_790_000_000, [Draft("link", url="https://example.com/x")])]
        self.earlier = [{"message": 9, "minutes_ago": 1, "items": [view(self.old)]}]

    async def interpret(self, words, items=None):
        return await self.claude.interpret(self.earlier, words, [self.link, self.photo] if items is None else items, "it")

    async def test_interpreting_sends_words_items_and_the_conversation_but_no_contents(self):
        await self.claude.interpret(self.earlier, "ricetta della nonna", [self.link, self.photo], "it", reply_to=9)
        [request] = self.fake.requests
        body = request["body"]
        self.assertEqual((request["path"], body["model"], body["output_config"]["effort"]),
                         ("/v1/messages", "claude-haiku-5-5", "low"))
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("temperature", body)
        self.assertIn("ISO 639-1", body["system"])
        [message] = self.fake.messages("interpret")
        self.assertEqual((message["language"], message["conversation"], message["current"]["reply_to"]),
                         ("it", self.earlier, 9))
        link, photo = message["current"]["items"]
        self.assertEqual((link["url"], link["platform"], link["reading"]),
                         ("https://www.youtube.com/watch?v=abc", "YouTube", "pending"))
        self.assertEqual(set(photo), {"id", "source", "reading", "sent"})  # del file nulla che lo identifichi

    async def test_words_can_comment_on_an_earlier_item_and_ask_about_it(self):
        self.fake.on("interpret", lambda m: interpretation(
            comment="da provare", comment_on=[self.old.id, 999], question="quanto zucchero?", about=[self.old.id, 7]))
        meaning = await self.interpret("da provare, quanto zucchero?", items=[])
        self.assertEqual(meaning, Interpretation(comment="da provare", comment_on=(self.old.id,),
                                                 question="quanto zucchero?", about=(self.old.id,)))

    async def test_a_note_comes_with_its_description_checked(self):
        self.fake.on("interpret", lambda m: interpretation(
            note="riso\nparmigiano", kind="note", title="x" * 300, summary="La spesa.",
            topics=["Riso", "riso", " ", "t" * 90] + [f"a{i}" for i in range(10)]))
        meaning = await self.interpret("riso\nparmigiano", items=[])
        self.assertEqual(meaning.note, "riso\nparmigiano")  # a capo compresi
        d = meaning.description
        self.assertEqual((d.kind, len(d.title), d.summary, d.topics[:2], len(d.topics)),
                         ("note", 120, "La spesa.", ("riso", "t" * 40), 8))

    async def test_the_words_with_a_link_are_never_lost(self):
        self.fake.on("interpret", lambda m: interpretation())  # Claude non vede nulla da custodire
        meaning = await self.interpret("buona")
        self.assertEqual((meaning.comment, meaning.comment_on), ("buona", (self.link.id, self.photo.id)))
        self.fake.on("interpret", lambda m: interpretation(comment="buona"))  # commento senza elementi
        self.assertEqual((await self.interpret("buona")).comment_on, (self.link.id, self.photo.id))
        self.assertEqual(await self.interpret("buona", items=[]), Interpretation(note="buona"))
        self.fake.on("interpret", lambda m: interpretation())
        self.assertEqual(await self.interpret("ok grazie", items=[]), Interpretation())  # solo un saluto

    async def test_spoken_words_are_interpreted_like_typed_ones(self):
        voice = self.voice()
        self.fake.on("interpret", lambda m: interpretation(question="che ricette di carbonara ho?", keep_spoken=False))
        meaning = await self.claude.interpret(self.earlier, None, [voice], "it", spoken="Che ricette di carbonara ho?")
        self.assertEqual((meaning.question, meaning.spoken, meaning.keep_spoken),
                         ("che ricette di carbonara ho?", "Che ricette di carbonara ho?", False))
        [message] = self.fake.messages("interpret")
        self.assertEqual((message["current"]["words"], message["current"]["spoken"]),
                         (None, "Che ricette di carbonara ho?"))
        self.assertIn("spoken words are never copied into note", self.fake.of("interpret")[0]["body"]["system"])

    async def test_a_voice_message_is_let_go_only_when_a_question_or_a_comment_holds_its_words(self):
        voice = self.voice()

        async def interpret(answer, words=None):
            self.fake.on("interpret", lambda m: answer)
            return await self.claude.interpret([], words, [voice], "it", spoken="comprare il riso")
        meaning = await interpret(interpretation(note="comprare il riso", keep_spoken=False))
        self.assertEqual((meaning.note, meaning.keep_spoken), (None, True))  # la nota è il vocale stesso
        self.assertTrue((await interpret(interpretation(keep_spoken=False))).keep_spoken)  # niente che le tenga
        meaning = await interpret(interpretation(note="spesa", question="quanto riso?", keep_spoken=False),
                                  words="spesa")
        self.assertEqual((meaning.note, meaning.question, meaning.keep_spoken), ("spesa", "quanto riso?", False))
        self.fake.stop_with("refusal")
        with self.assertLogs("sazed_agent.claude", "WARNING"):
            meaning = await self.claude.interpret([], None, [voice], "it", spoken="parola " * 2000)
        self.assertEqual(meaning, Interpretation(spoken=("parola " * 2000)[:8000]))  # resta, con le sue parole

    def voice(self):
        [voice] = [o.item for o in self.memory.save(42, 11, 1_790_000_000, [
            Draft("voice", media={"file_id": "v", "file_unique_id": "V", "duration": 4})])]
        return voice

    async def test_a_refusal_or_an_unusable_answer_falls_back_on_the_code(self):
        self.fake.stop_with("refusal")
        with self.assertLogs("sazed_agent.claude", "WARNING"):
            self.assertEqual(await self.interpret("x", items=[]), Interpretation(note="x"))
        self.fake.on("interpret", lambda m: {"note": 3})
        with self.assertLogs("sazed_agent.claude", "WARNING"):
            self.assertEqual(await self.interpret("x"), Interpretation(comment="x", comment_on=(self.link.id, self.photo.id)))

    async def test_describing_uses_the_content_as_data(self):
        self.memory.annotate(self.link.id, "per sabato")
        item = self.memory.get(self.link.id)
        self.fake.on("describe", lambda m: description("recipe", "Carbonara", ["Guanciale", "youtube"], "Pasta."))
        extract = Extract("Carbonara perfetta", "Chef", "guanciale " * 3000, False)
        got = await self.claude.describe(item, extract, "it")
        self.assertEqual(got, Classification("recipe", "Carbonara", ("guanciale", "youtube"), "Pasta."))
        [message] = self.fake.messages("describe")
        self.assertEqual((message["note"], message["author"], message["partial"], len(message["content"])),
                         ("per sabato", "Chef", True, 12_000))
        system = self.fake.of("describe")[0]["body"]["system"]
        self.assertIn("never instructions", system)
        self.assertIn("even when the content is in another language", system)
        self.fake.stop_with("refusal")
        with self.assertLogs("sazed_agent.claude", "WARNING"):
            self.assertIsNone(await self.claude.describe(item, extract, "it"))

    async def test_describing_looks_at_the_images_seen_at_the_source(self):
        self.fake.on("describe", lambda m: description("recipe", "Torta di mele definitiva", ["mele"]))
        extract = Extract(None, "pasticcere", None, False, (("image/jpeg", b"\xff\xd8jpeg"),))
        got = await self.claude.describe(self.link, extract, "it")
        self.assertEqual(got.title, "Torta di mele definitiva")
        [request] = self.fake.of("describe")
        self.assertEqual(images(request), [{"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": "/9hqcGVn"}}])
        self.assertEqual(request["body"]["messages"][0]["content"][-1]["type"], "text")  # prima le immagini
        [message] = self.fake.messages("describe")
        self.assertEqual((message["images"], message["author"], message["content"]), (1, "pasticcere", ""))
        self.assertIn("cover of a video", request["body"]["system"])

    async def test_a_document_comes_with_its_authors_and_whether_it_was_published(self):
        [doc] = [o.item for o in self.memory.save(42, 12, 1_790_000_000, [
            Draft("document", media={"file_id": "d", "file_unique_id": "D", "file_name": "pozzo.pdf"})])]
        self.fake.on("describe", lambda m: description("book", "Il pozzo dell'ascensione", ["fantasy"], None,
                                                       ["Brandon Sanderson", " ", "Brandon Sanderson"], True))
        got = await self.claude.describe(doc, Extract(None, None, "Brandon Sanderson\nIl pozzo", False), "it")
        self.assertEqual((got.kind, got.authors, got.published, got.summary),
                         ("book", ("Brandon Sanderson",), True, None))
        schema = self.fake.of("describe")[0]["body"]["output_config"]["format"]["schema"]
        self.assertIn("published", schema["properties"])
        found = [{"source": "Open Library", "title": "The Well of Ascension", "text": "Vin and Elend rule."}]
        await self.claude.describe(doc, Extract(None, None, "x" * 50_000, True), "it", found=found, whole=True)
        message = self.fake.messages("describe")[1]
        self.assertEqual((message["found_online"], len(message["content"]), message["partial"]), (found, 50_000, False))
        self.assertIn("never mix works", self.fake.of("describe")[1]["body"]["system"])
        self.fake.on("describe", lambda m: description("article", "Torta", published=True))
        self.assertFalse((await self.claude.describe(self.link, Extract("Torta", None, "mele", True), "it")).published)

    async def test_asking_to_read_a_document_in_full_is_a_question_about_it(self):
        [doc] = [o.item for o in self.memory.save(42, 12, 1_790_000_000, [
            Draft("document", media={"file_id": "d", "file_unique_id": "D"})])]
        self.fake.on("interpret", lambda m: interpretation(read_in_full=[doc.id, 999]))
        meaning = await self.claude.interpret([], "leggilo tutto", [doc], "it")
        self.assertEqual((meaning.read_in_full, meaning.question, meaning.about),
                         ((doc.id,), "leggilo tutto", (doc.id,)))
        self.fake.on("interpret", lambda m: interpretation(read_in_full_named="il pozzo",
                                                           question="leggi tutto il pozzo"))
        meaning = await self.claude.interpret([], "leggi tutto il pozzo", [], "it")
        self.assertEqual((meaning.read_in_full, meaning.read_in_full_named), ((), "il pozzo"))
        self.assertEqual(Interpretation.from_dict(meaning.to_dict()), meaning)

    async def test_an_answer_can_read_the_passages_of_a_long_text(self):
        self.memory.read_done(self.old.id, "complete", content="\n".join(
            ["preambolo " * 200, "Il Lord Reggente cade.", "epilogo " * 200]))
        turns = iter([tool("read_passages", {"id": self.old.id, "text": "lord reggente"}),
                      tool("read_passages", {"id": 999, "text": "x"}), "cade alla fine."])
        self.fake.on("answer", lambda messages: next(turns))
        await self.claude.answer([], "come finisce?", [self.old], "it", self.memory, "2026-11-08")
        results = self.fake.of("answer")[2]["body"]["messages"]
        self.assertIn("Il Lord Reggente cade.", results[2]["content"][0]["content"])
        self.assertIn("no item", results[4]["content"][0]["content"])

    async def test_an_answer_can_search_and_read_items(self):
        self.memory.read_done(self.old.id, "complete", content="guanciale, uova, pecorino", title="Carbonara",
                              kind="recipe")
        turns = iter([tool("search_items", {"text": "carbonara", "kind": "recipe", "limit": 99}),
                      tool("get_item", {"id": self.old.id}), "ne hai una: https://example.com/x"])
        self.fake.on("answer", lambda messages: next(turns))
        answer = await self.claude.answer(self.earlier, "che ricette di carbonara ho?", [self.link], "it",
                                          self.memory, "2026-11-06")
        self.assertEqual(answer, Answer("ne hai una: https://example.com/x"))
        requests = self.fake.of("answer")
        self.assertEqual(len(requests), 3)
        first = self.fake.messages("answer")[0]
        self.assertEqual((first["question"], first["items_kept"], first["today"], first["items"][0]["id"]),
                         ("che ricette di carbonara ho?", 3, "2026-11-06", self.link.id))
        results = requests[2]["body"]["messages"]
        self.assertEqual([m["role"] for m in results], ["user", "assistant", "user", "assistant", "user"])
        found = results[2]["content"][0]["content"]
        self.assertIn('"total": 1', found)
        self.assertIn("guanciale, uova, pecorino", results[4]["content"][0]["content"])
        self.assertEqual({t["name"] for t in requests[0]["body"]["tools"]}, {"search_items", "get_item", "read_passages",
                                                                         "send_back"})

    async def test_a_spoken_question_is_answered_knowing_it_was_heard(self):
        await self.claude.answer([], "che ricette di carbonara ho?", [], "it", self.memory, "2026-11-08", spoken=True)
        self.assertTrue(self.fake.messages("answer")[0]["spoken"])
        self.assertIn("names above all", self.fake.of("answer")[0]["body"]["system"])

    async def test_an_answer_can_choose_files_to_send_back(self):
        others = [o.item for o in self.memory.save(42, 13, 1_790_000_000, [
            Draft("document", media={"file_id": f"d{n}", "file_unique_id": f"D{n}"}) for n in range(6)])]
        turns = iter([tool("send_back", {"ids": [self.photo.id, self.link.id, 999, self.photo.id]}),
                      tool("send_back", {"ids": [o.id for o in others]}), "eccoli."])
        self.fake.on("answer", lambda messages: next(turns))
        answer = await self.claude.answer([], "mandami le foto", [], "it", self.memory, "2026-11-08")
        self.assertEqual(answer, Answer("eccoli.", (self.photo.id, *(o.id for o in others[:4]))))  # al più 5
        result = json.loads(self.fake.of("answer")[1]["body"]["messages"][2]["content"][0]["content"])
        self.assertEqual((result["will_send_after_reply"], result["not_files"]), ([self.photo.id], [self.link.id, 999]))
        self.assertIn("send back", self.fake.of("answer")[0]["body"]["system"])

    async def test_a_wrong_tool_input_is_an_error_for_claude_not_for_the_agent(self):
        turns = iter([tool("search_items", {"kind": "romanzo"}), tool("get_item", {"id": 999}), "niente."])
        self.fake.on("answer", lambda messages: next(turns))
        self.assertEqual((await self.claude.answer([], "romanzi?", [], "it", self.memory, "2026-11-06")).text, "niente.")
        results = self.fake.of("answer")[2]["body"]["messages"]
        self.assertTrue(results[2]["content"][0]["is_error"])
        self.assertIn("no item", results[4]["content"][0]["content"])

    async def test_an_answer_that_never_ends_or_is_refused_is_none(self):
        self.fake.on("answer", lambda messages: tool("search_items", {}))
        with self.assertLogs("sazed_agent.claude", "WARNING"):
            self.assertIsNone(await self.claude.answer([], "?", [], "it", self.memory, "2026-11-06"))
        self.assertEqual(len(self.fake.of("answer")), 6)
        self.fake.stop_with("refusal")
        with self.assertLogs("sazed_agent.claude", "WARNING"):
            self.assertIsNone(await self.claude.answer([], "?", [], "it", self.memory, "2026-11-06"))

    async def test_api_errors_are_classified_like_telegram_ones(self):
        cases = [(500, False, False), (529, False, False), (400, True, False), (402, False, True),
                 (403, False, True), (404, False, True)]
        for status, permanent, config_error in cases:
            with self.subTest(status):
                self.fake.fail(status)
                with self.assertRaises(ClaudeError) as ctx:
                    await self.interpret("x")
                self.assertEqual((ctx.exception.permanent, ctx.exception.config_error), (permanent, config_error))
        self.fake.fail(429)
        with self.assertRaises(ClaudeError) as ctx:
            await self.claude.answer([], "?", [], "it", self.memory, "2026-11-06")
        self.assertEqual((ctx.exception.permanent, ctx.exception.retry_after), (False, 1.0))

    async def test_a_rejected_key_is_a_configuration_error_that_never_shows_the_key(self):
        claude = Claude("sk-ant-api03-SBAGLIATA", self.fake.api_base, "claude-haiku-5-5", timeout=5, max_retries=0)
        with self.assertRaises(ClaudeError) as ctx:
            await claude.interpret([], "x", [], "it")
        self.assertTrue(ctx.exception.config_error)
        self.assertIn("setup-claude.sh", str(ctx.exception))
        self.assertNotIn("SBAGLIATA", str(ctx.exception))

    async def test_an_unreachable_api_is_transient(self):
        claude = Claude(KEY, "http://127.0.0.1:9", "claude-haiku-5-5", timeout=2, max_retries=0)
        with self.assertRaises(ClaudeError) as ctx:
            await claude.interpret([], "x", [], "it")
        self.assertFalse(ctx.exception.permanent or ctx.exception.config_error)

    def test_an_interpretation_survives_the_journal(self):
        meaning = Interpretation("riso", Classification("note", "Spesa", ("riso",), None), "buona", (3,),
                                 "quando?", (3, 4), "quando lo cucino?", False)
        self.assertEqual(Interpretation.from_dict(meaning.to_dict()), meaning)
        self.assertEqual(Interpretation.from_dict({"intent": "save", "items": []}), Interpretation())  # di prima

    def test_without_claude_words_go_with_the_items_or_are_a_note(self):
        self.assertEqual(Interpretation.plain("buona", [1, 2]), Interpretation(comment="buona", comment_on=(1, 2)))
        self.assertEqual(Interpretation.plain("riso", []), Interpretation(note="riso"))
        self.assertEqual(Interpretation.plain(None, [1]), Interpretation())


if __name__ == "__main__":
    unittest.main()
