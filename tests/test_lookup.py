"""Le schede delle opere pubblicate contro un finto web: Open Library per i libri, Crossref per gli
articoli, Wikipedia per tutte, una fonte che non risponde, i titoli fuori dai log; le voci che non
sono dell'opera, i libri senza descrizione, titoli e autori sentiti male."""

import json
import unittest
from urllib.parse import quote, urlencode

from sazed_agent.lookup import Lookup, same_title
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

    def books(self, docs, limit=1, **query):
        """Ciò che risponde Open Library a una ricerca."""
        query = {k: v for k, v in query.items() if v} | {"limit": limit,
                                                           "fields": "key,title,author_name,first_publish_year"}
        self.json(f"https://openlibrary.org/search.json?{urlencode(query)}", {"docs": docs})

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
        self.books([{"key": "/works/OL123W", "title": "The Well of Ascension", "author_name": [AUTHOR],
                     "first_publish_year": 2007}], title=TITLE, author=AUTHOR)
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

    async def test_wikipedia_takes_only_a_page_about_the_work(self):
        self.wikipedia("it", "The Nerd Reich", before=["Jon Heder", "Gil Duran"])  # un attore, l'autore
        self.wikipedia("it", "The Nerd Reich Gil Duran", before=["Zachary Levi"])
        self.wikipedia("en", "The Nerd Reich", "The Nerd Reich", "A 2026 non-fiction book by Gil Durán.",
                       before=["Technolibertarianism"])
        found = await self.lookup.find("The Nerd Reich", ["Gil Duran"], "other", "it")
        self.assertEqual(found, [{"source": "Wikipedia (en)", "title": "The Nerd Reich",
                                  "text": "A 2026 non-fiction book by Gil Durán."}])
        self.assertFalse([u for u in self.web.requests if "summary/Jon" in u or "summary/Zachary" in u])

    async def test_a_misheard_title_is_found_among_its_authors_books_and_then_on_wikipedia(self):
        self.books([], title="The Nerd Rike", author="Gil Duran")
        self.books([], limit=5, title="The Nerd Rike")
        self.books([{"key": "/works/OL9W", "title": "Introducción a la investigación social", "author_name": ["Gil Durán Vidal"]},
                    {"key": "/works/OL7W", "title": "The Nerd Reich", "author_name": ["Gil Durán"],
                     "first_publish_year": 2026}], limit=20, author="Gil Duran")
        self.json("https://openlibrary.org/works/OL7W.json", {"title": "The Nerd Reich"})  # senza descrizione
        for words in ("The Nerd Rike", "The Nerd Rike Gil Duran"):
            self.wikipedia("en", words)  # Wikipedia, con il titolo sentito male, non trova nulla
        self.wikipedia("en", "The Nerd Reich", "The Nerd Reich", "A 2026 non-fiction book.")
        found = await self.lookup.find("The Nerd Rike", ["Gil Duran"], "book", "en")
        self.assertEqual(found, [
            {"source": "Open Library", "title": "The Nerd Reich", "authors": ["Gil Durán"], "year": 2026},
            {"source": "Wikipedia (en)", "title": "The Nerd Reich", "text": "A 2026 non-fiction book."}])

    async def test_a_misheard_author_is_found_among_the_books_with_that_title(self):
        self.books([], title="End Times Fascism", author="Rastra Taylor")
        self.books([{"key": "/works/OL5W", "title": "End Times Fascism", "author_name": ["Naomi Klein", "Astra Taylor"],
                     "first_publish_year": 2026}], limit=5, title="End Times Fascism")
        self.json("https://openlibrary.org/works/OL5W.json", {})
        self.wikipedia("en", "End Times Fascism", before=["Naomi Klein", "Astra Taylor"])  # nessuna voce sua
        self.wikipedia("en", "End Times Fascism Rastra Taylor")
        found = await self.lookup.find("End Times Fascism", ["Rastra Taylor"], "book", "en")
        self.assertEqual(found, [{"source": "Open Library", "title": "End Times Fascism",
                                  "authors": ["Naomi Klein", "Astra Taylor"], "year": 2026}])

    def test_titles_are_the_same_work_only_when_they_really_are(self):
        same = [("Il pozzo dell'ascensione", "Mistborn: Il pozzo dell'ascensione"), ("It", "It (novel)"),
                ("Il nome della rosa", "IL NOME DELLA ROSA!"), ("Cent'anni di solitudine", "Cent’anni di solitudine"),
                ("The Nerd Reich: Silicon Valley Fascism and the War on Democracy", "The Nerd Reich"),
                ("Gödel, Escher, Bach", "Godel Escher Bach")]
        different = [("Dune Messiah", "Dune"), ("It", "Italia"), ("End Times Fascism", "Astra Taylor"),
                     ("The Nerd Rike", "The Nerd Reich"), ("", "Dune"), ("Dune", None)]
        self.assertEqual([same_title(a, b) for a, b in same], [True] * len(same))
        self.assertEqual([same_title(a, b) for a, b in different], [False] * len(different))

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
