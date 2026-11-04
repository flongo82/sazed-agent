"""Claude contro un finto server dell'API: richiesta, risposta controllata, rifiuti ed errori."""

import unittest

from sazed_agent.claude import Claude, ClaudeError, Classification, Interpretation
from sazed_agent.coppermind import Draft
from tests.fake_claude import KEY, FakeClaude

LINK = Draft("link", url="https://www.youtube.com/watch?v=abc&si=x", note="ricetta della nonna")


class ClaudeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeClaude().start()
        self.addCleanup(self.fake.stop)
        self.claude = Claude(KEY, self.fake.api_base, "claude-haiku-5-5", timeout=5, max_retries=0)

    async def test_the_request_carries_words_items_and_language_but_no_contents(self):
        await self.claude.interpret("ricetta della nonna", [LINK, Draft("photo", media={"file_id": "f",
                                    "file_unique_id": "U", "file_size": 9})], "it")
        [request] = self.fake.requests
        body = request["body"]
        self.assertEqual((request["path"], body["model"], body["output_config"]["effort"]),
                         ("/v1/messages", "claude-haiku-5-5", "low"))
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("temperature", body)
        self.assertIn("ISO 639-1", body["system"])
        self.assertEqual(self.fake.messages(), [{"language": "it", "words": "ricetta della nonna", "items": [
            {"index": 0, "source": "link", "url": "https://www.youtube.com/watch?v=abc", "platform": "YouTube",
             "site": "youtube.com"},
            {"index": 1, "source": "photo"}]}])  # del file nulla che lo identifichi

    async def test_the_answer_is_checked_before_use(self):
        self.fake.answer(lambda m: {"intent": "both", "question": "  la faccio\nstasera? ", "items": [
            {"index": 0, "kind": "recipe", "title": "x" * 300, "summary": "Una carbonara.",
             "topics": ["Guanciale", "uova", "guanciale", " ", "t" * 90] + [f"a{i}" for i in range(10)]},
            {"index": 0, "kind": "video", "title": "doppione", "summary": None, "topics": []},
            {"index": 7, "kind": "recipe", "title": "fuori elenco", "summary": None, "topics": []}]})
        meaning = await self.claude.interpret("ricetta", [LINK], "it")
        self.assertEqual((meaning.intent, meaning.question), ("both", "la faccio stasera?"))
        [item] = meaning.items
        self.assertEqual((item.index, item.kind, len(item.title), item.summary), (0, "recipe", 120, "Una carbonara."))
        self.assertEqual(item.topics[:3], ("guanciale", "uova", "t" * 40))
        self.assertEqual(len(item.topics), 8)

    async def test_links_and_files_are_never_only_a_question(self):
        self.fake.answer(lambda m: {"intent": "question", "question": "che ne pensi?", "items": []})
        self.assertEqual((await self.claude.interpret("che ne pensi?", [LINK], "it")).intent, "both")
        meaning = await self.claude.interpret("dove ho messo la ricetta?", [Draft("note", note="x")], "it")
        self.assertEqual((meaning.intent, meaning.saves, meaning.asks), ("question", False, True))

    async def test_a_refusal_or_an_unusable_answer_saves_without_a_description(self):
        self.fake.stop_with("refusal")
        with self.assertLogs("sazed_agent.claude", "WARNING"):
            self.assertEqual(await self.claude.interpret("x", [LINK], "it"), Interpretation())
        self.fake.answer(lambda m: {"intent": "forse", "question": None, "items": []})
        with self.assertLogs("sazed_agent.claude", "WARNING"):
            self.assertEqual(await self.claude.interpret("x", [LINK], "it"), Interpretation())

    async def test_api_errors_are_classified_like_telegram_ones(self):
        cases = [(500, False, False), (529, False, False), (400, True, False), (402, False, True),
                 (403, False, True), (404, False, True)]
        for status, permanent, config_error in cases:
            with self.subTest(status):
                self.fake.fail(status)
                with self.assertRaises(ClaudeError) as ctx:
                    await self.claude.interpret("x", [LINK], "it")
                self.assertEqual((ctx.exception.permanent, ctx.exception.config_error), (permanent, config_error))
        self.fake.fail(429)
        with self.assertRaises(ClaudeError) as ctx:
            await self.claude.interpret("x", [LINK], "it")
        self.assertEqual((ctx.exception.permanent, ctx.exception.retry_after), (False, 1.0))

    async def test_a_rejected_key_is_a_configuration_error_that_never_shows_the_key(self):
        claude = Claude("sk-ant-api03-SBAGLIATA", self.fake.api_base, "claude-haiku-5-5", timeout=5, max_retries=0)
        with self.assertRaises(ClaudeError) as ctx:
            await claude.interpret("x", [LINK], "it")
        self.assertTrue(ctx.exception.config_error)
        self.assertIn("setup-claude.sh", str(ctx.exception))
        self.assertNotIn("SBAGLIATA", str(ctx.exception))

    async def test_an_unreachable_api_is_transient(self):
        claude = Claude(KEY, "http://127.0.0.1:9", "claude-haiku-5-5", timeout=2, max_retries=0)
        with self.assertRaises(ClaudeError) as ctx:
            await claude.interpret("x", [LINK], "it")
        self.assertFalse(ctx.exception.permanent or ctx.exception.config_error)

    def test_an_interpretation_survives_the_journal(self):
        meaning = Interpretation("both", "quando?", (Classification(0, "recipe", "Carbonara", ("uova",), "Buona."),))
        self.assertEqual(Interpretation.from_dict(meaning.to_dict()), meaning)


if __name__ == "__main__":
    unittest.main()
