"""Claude contro un finto server dell'API: le tre richieste, le risposte controllate, gli
strumenti delle risposte, rifiuti ed errori."""

import shutil
import tempfile
import unittest
from pathlib import Path

from sazed_agent.claude import Claude, ClaudeError, Classification, Interpretation, view
from sazed_agent.coppermind import Coppermind, Draft
from sazed_agent.journal import Journal
from sazed_agent.reader import Extract
from tests.fake_claude import KEY, FakeClaude, description, interpretation, tool


class ClaudeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeClaude().start()
        self.addCleanup(self.fake.stop)
        self.claude = Claude(KEY, self.fake.api_base, "claude-haiku-5-5", timeout=5, max_retries=0)
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

    async def test_an_answer_can_search_and_read_items(self):
        self.memory.read_done(self.old.id, "complete", content="guanciale, uova, pecorino", title="Carbonara",
                              kind="recipe")
        turns = iter([tool("search_items", {"text": "carbonara", "kind": "recipe", "limit": 99}),
                      tool("get_item", {"id": self.old.id}), "ne hai una: https://example.com/x"])
        self.fake.on("answer", lambda messages: next(turns))
        text = await self.claude.answer(self.earlier, "che ricette di carbonara ho?", [self.link], "it",
                                        self.memory, "2026-11-06")
        self.assertEqual(text, "ne hai una: https://example.com/x")
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
        self.assertEqual({t["name"] for t in requests[0]["body"]["tools"]}, {"search_items", "get_item"})

    async def test_a_wrong_tool_input_is_an_error_for_claude_not_for_the_agent(self):
        turns = iter([tool("search_items", {"kind": "romanzo"}), tool("get_item", {"id": 999}), "niente."])
        self.fake.on("answer", lambda messages: next(turns))
        self.assertEqual(await self.claude.answer([], "romanzi?", [], "it", self.memory, "2026-11-06"), "niente.")
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
                                 "quando?", (3, 4))
        self.assertEqual(Interpretation.from_dict(meaning.to_dict()), meaning)
        self.assertEqual(Interpretation.from_dict({"intent": "save", "items": []}), Interpretation())  # di prima

    def test_without_claude_words_go_with_the_items_or_are_a_note(self):
        self.assertEqual(Interpretation.plain("buona", [1, 2]), Interpretation(comment="buona", comment_on=(1, 2)))
        self.assertEqual(Interpretation.plain("riso", []), Interpretation(note="riso"))
        self.assertEqual(Interpretation.plain(None, [1]), Interpretation())


if __name__ == "__main__":
    unittest.main()
