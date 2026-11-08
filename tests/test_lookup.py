"""Le schede delle opere pubblicate contro un finto web: Open Library per i libri, Crossref per gli
articoli, Wikipedia per tutte, una fonte che non risponde, i titoli fuori dai log."""

import json
import unittest
from urllib.parse import quote, urlencode

from sazed_agent.lookup import Lookup
from sazed_agent.reader import Reader
from tests.fake_web import FakeWeb, allow_all

TITLE, AUTHOR = "Il pozzo dell'ascensione", "Brandon Sanderson"


class LookupTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.web = FakeWeb()
        reader = Reader(transport=self.web.transport, allow=allow_all)
        self.addAsyncCleanup(reader.close)
        self.lookup = Lookup(reader)

    def json(self, url, data, status=200):
        self.web.route(url, status, json.dumps(data), {"content-type": "application/json"})

    def wikipedia(self, lang, words, page=None, extract=None, kind="standard", before=()):
        """Ciò che risponde Wikipedia a una ricerca: la voce trovata (dopo quelle in before) e il suo
        riassunto, o nessuna voce."""
        search = {"action": "query", "list": "search", "srsearch": words, "srlimit": 3, "srprop": "", "format": "json"}
        self.json(f"https://{lang}.wikipedia.org/w/api.php?{urlencode(search)}",
                  {"query": {"search": [{"title": t} for t in [*before, page] if t]}})
        if page:
            self.json(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(page.replace(' ', '_'), safe='')}",
                      {"type": kind, "title": page, "extract": extract})

    async def test_a_book_has_its_open_library_description_and_its_wikipedia_summary(self):
        query = {"title": TITLE, "limit": 1, "fields": "key,title,author_name,first_publish_year", "author": AUTHOR}
        self.json(f"https://openlibrary.org/search.json?{urlencode(query)}", {"docs": [
            {"key": "/works/OL123W", "title": "The Well of Ascension", "author_name": [AUTHOR],
             "first_publish_year": 2007}]})
        self.json("https://openlibrary.org/works/OL123W.json", {"description": {"value": "Vin and Elend <b>rule</b>."}})
        self.wikipedia("it", TITLE, "Mistborn: Il pozzo dell'ascensione", "Romanzo fantasy del 2007.",
                       before=[AUTHOR])  # la voce dell'autore si salta
        found = await self.lookup.find(TITLE, [AUTHOR], "book", "it")
        self.assertEqual(found, [
            {"source": "Open Library", "title": "The Well of Ascension", "text": "Vin and Elend rule .",
             "authors": [AUTHOR], "year": 2007},
            {"source": "Wikipedia (it)", "title": "Mistborn: Il pozzo dell'ascensione",
             "text": "Romanzo fantasy del 2007."}])

    async def test_an_article_has_its_crossref_abstract(self):
        query = {"query.bibliographic": "Notes on the Engine", "rows": 1, "select": "DOI,title,author,abstract,issued",
                 "query.author": "Ada Lovelace"}
        self.json(f"https://api.crossref.org/works?{urlencode(query)}", {"message": {"items": [{
            "title": ["Notes on the Engine"], "author": [{"given": "Ada", "family": "Lovelace"}],
            "issued": {"date-parts": [[1843, 9]]}, "abstract": "<jats:p>A machine that weaves numbers.</jats:p>"}]}})
        found = await self.lookup.find("Notes on the Engine", ["Ada Lovelace"], "article", "en")
        self.assertEqual(found, [{"source": "Crossref", "title": "Notes on the Engine",
                                  "text": "A machine that weaves numbers.", "authors": ["Ada Lovelace"], "year": 1843}])

    async def test_wikipedia_tries_title_and_author_then_english_and_skips_disambiguations(self):
        self.wikipedia("it", "Mistborn", "Mistborn", "Può riferirsi a:", kind="disambiguation")
        self.wikipedia("it", "Mistborn Sanderson")
        self.wikipedia("en", "Mistborn")
        self.wikipedia("en", "Mistborn Sanderson", "Mistborn: The Final Empire", "A 2006 fantasy novel.")
        found = await self.lookup.find("Mistborn", ["Sanderson"], "other", "it")
        self.assertEqual(found, [{"source": "Wikipedia (en)", "title": "Mistborn: The Final Empire",
                                  "text": "A 2006 fantasy novel."}])
        searched = [u for u in self.web.requests if "srsearch" in u]
        self.assertEqual(len(searched), 4)  # il solo titolo, poi titolo e autore, in italiano e in inglese

    async def test_a_source_that_fails_is_skipped_without_the_title_in_the_logs(self):
        query = {"title": TITLE, "limit": 1, "fields": "key,title,author_name,first_publish_year"}
        self.web.route(f"https://openlibrary.org/search.json?{urlencode(query)}", 503)
        with self.assertLogs("sazed_agent.lookup", "WARNING") as logs:
            self.assertEqual(await self.lookup.find(TITLE, [], "book", "../evil"), [])
        self.assertIn("open_library", logs.output[0])
        self.assertNotIn("pozzo", "\n".join(logs.output))
        self.assertTrue(all("://en.wikipedia.org/" in u for u in self.web.requests if "wikipedia" in u))

    async def test_without_a_title_nothing_is_asked(self):
        self.assertEqual(await self.lookup.find(None, [AUTHOR], "book", "it"), [])
        self.assertEqual(self.web.requests, [])

    async def test_the_catalogues_are_told_who_is_asking(self):
        agents = []

        def answer(request):
            agents.append(request.headers["user-agent"])
            return 200, b'{"query": {"search": []}}', {"content-type": "application/json"}
        for lang, words in (("it", "Mistborn"), ("en", "Mistborn")):
            search = {"action": "query", "list": "search", "srsearch": words, "srlimit": 3, "srprop": "",
                      "format": "json"}
            self.web.route(f"https://{lang}.wikipedia.org/w/api.php?{urlencode(search)}", body=answer)
        self.assertEqual(await self.lookup.find("Mistborn", [], "other", "it"), [])
        self.assertEqual(len(agents), 2)
        self.assertTrue(all(a.startswith("sazed-agent/") and "github.com" in a for a in agents))


if __name__ == "__main__":
    unittest.main()
