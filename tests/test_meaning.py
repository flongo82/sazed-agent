"""Il significato dei testi con un finto llama-server: socket UNIX con chiave, vettori normalizzati,
istruzione per le domande, priorità bassa, un server solo, errori; la scheda di un elemento."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy

from sazed_agent.coppermind import Item
from sazed_agent.meaning import CARD_MAX, Meanings, MeaningError, card

FAKE = Path(__file__).resolve().parent / "fake_llama_server.py"


class MeaningsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def meanings(self, plan=None, server=FAKE):
        model = self.dir / "modello.gguf"
        model.write_text(json.dumps(plan or {}))
        meanings = Meanings(model, threads=2, server=str(server))
        self.addAsyncCleanup(meanings.close)
        return meanings

    def calls(self):
        return json.loads((self.dir / "calls.json").read_text())

    async def test_texts_close_in_meaning_get_close_vectors(self):
        meanings = self.meanings()
        bread, home_bread, contract = await meanings.vectors(["pane fatto in casa", "il pane di casa",
                                                              "contratto di affitto"])
        self.assertAlmostEqual(float(numpy.linalg.norm(bread)), 1.0, places=5)
        self.assertGreater(float(bread @ home_bread), float(bread @ contract))
        [query] = await meanings.vectors(["come si fa il pane?"], query=True)
        self.assertEqual(query.dtype, numpy.float32)
        started, first, second = self.calls()
        self.assertTrue(second["texts"][0].startswith("Instruct: ") and second["texts"][0].endswith("Query:come si fa il pane?"))
        self.assertEqual(first["texts"][0], "pane fatto in casa")  # i documenti senza istruzione
        self.assertEqual((first["nice"], second["nice"]), (10, 10))
        args = started["args"]
        self.assertEqual((args[args.index("--pooling") + 1], args[args.index("-t") + 1]), ("last", "2"))
        self.assertIn("--api-key-file", args)
        self.assertNotIn("--api-key", args)  # la chiave non sta fra gli argomenti
        self.assertTrue(args[args.index("--host") + 1].endswith(".sock"))

    async def test_the_server_stops_with_the_agent(self):
        meanings = self.meanings()
        await meanings.vectors(["riso"])
        work = Path(meanings._work)
        process = meanings._process
        self.assertEqual(oct(work.stat().st_mode & 0o777), "0o700")
        await meanings.close()
        self.assertEqual((process.returncode is not None, work.exists()), (True, False))
        await meanings.vectors(["riso"])  # e riparte alla prossima richiesta
        self.assertEqual(len([c for c in self.calls() if "args" in c]), 2)

    async def test_the_server_rests_when_nothing_is_asked_and_wakes_on_the_next_request(self):
        meanings = self.meanings()
        meanings.idle = 0.3
        await meanings.vectors(["riso"])
        process = meanings._process
        await asyncio.sleep(0.15)
        await meanings.vectors(["pane"])  # una richiesta prima del riposo lo rimanda
        self.assertIs(meanings._process, process)
        await asyncio.sleep(0.6)
        self.assertEqual((process.returncode is not None, meanings._process, meanings._work), (True, None, None))
        await meanings.vectors(["riso"])  # e riparte
        self.assertEqual(len([c for c in self.calls() if "args" in c]), 2)

    async def test_a_server_that_does_not_start_or_fails_is_a_meaning_error(self):
        for plan, server in (({"exit": True}, FAKE), ({}, self.dir / "manca"), ({"fail": True}, FAKE)):
            with self.subTest(plan=plan, server=server.name), self.assertRaises(MeaningError):
                await self.meanings(plan, server).vectors(["riso"])
        self.assertEqual(await self.meanings().vectors([]), [])


class CardTest(unittest.TestCase):
    def test_a_card_says_what_an_item_is_and_how_it_begins(self):
        item = Item(1, 42, 10, "link", "https://www.youtube.com/watch?v=x", "youtube", "youtube.com", "recipe",
                    "Tiramisù", ["Iginio"], ["mascarpone", "caffè"], "Il dolce al cucchiaio.", "per sabato",
                    "parola " * 1000, "complete", None, None, None, None, {}, 0, 0.0)
        text = card(item)
        self.assertTrue(text.startswith("Title: Tiramisù\nAuthors: Iginio\nKind: recipe\nTopics: mascarpone, caffè\n"
                                        "Summary: Il dolce al cucchiaio.\nNote: per sabato\nFrom: youtube\nContent: parola"))
        self.assertLess(len(text), 1000)  # del testo letto solo l'inizio
        self.assertLessEqual(len(card(item.__class__(**dict(item.__dict__, summary="x" * 5000)))), CARD_MAX)


if __name__ == "__main__":
    unittest.main()
