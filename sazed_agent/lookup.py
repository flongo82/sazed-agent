"""Le schede delle opere pubblicate, da fonti libere e gratuite: quando di un libro o di un
articolo si conoscono titolo e autori, ma le prime pagine non dicono di che cosa parla.

- Open Library, per i libri: la descrizione dell'opera;
- Crossref, per gli articoli: l'abstract, quando l'editore lo deposita;
- Wikipedia, per le opere note, prima nella lingua dell'utente e poi in inglese: l'inizio della
  voce.

Si chiede solo per opere pubblicate, mai per documenti personali (lo decide chi le descrive),
con poche richieste per fonte; una fonte che non risponde si salta. Le fonti chiedono a chi le
usa di presentarsi: lo User-Agent dice che cosa è sazed e dove trovarlo (Wikipedia, senza,
risponde 403). Open Library conosce i libri soprattutto con il titolo originale: per un libro
tradotto la scheda arriva di solito da Wikipedia. Le schede sono candidate:
chi le usa (Claude) le prende solo se parlano davvero della stessa opera. Titoli e autori non
vanno nei log, come i link dell'utente.
"""

import logging
import re
from urllib.parse import quote, urlencode

from .reader import ReadError

log = logging.getLogger("sazed_agent.lookup")

TEXT_MAX = 2_000                       # caratteri di una scheda
USER_AGENT = "sazed-agent/1.0 (+https://github.com/flongo82/sazed-agent; personal knowledge base)"
LANGUAGE = re.compile(r"[a-z]{2,3}")   # un codice ISO 639, l'unica parte variabile di un host di Wikipedia
WORK = re.compile(r"/works/OL\d+W")


class Lookup:
    def __init__(self, reader):
        self.reader = reader  # le richieste passano dal lettore: solo host pubblici, risposte limitate

    async def find(self, title, authors, kind, language):
        """Le schede trovate per un'opera (kind "book", "article" o altro), come dizionari con
        fonte, titolo, autori e testo; [] se nessuna fonte ne ha una."""
        if not title:
            return []
        author = authors[0] if authors else None
        sources = {"book": [self._open_library], "article": [self._crossref]}.get(kind, []) + [self._wikipedia]
        found = []
        for source in sources:
            try:
                card = await source(title, author, language)
            except ReadError as err:
                log.warning("scheda di %s non letta: %s", source.__name__.strip("_"), err)
                continue
            if card:
                found.append(card)
        return found

    async def _json(self, url):
        return await self.reader.get_json(url, USER_AGENT)

    async def _open_library(self, title, author, language):
        query = {"title": title, "limit": 1, "fields": "key,title,author_name,first_publish_year"}
        if author:
            query["author"] = author
        data = await self._json(f"https://openlibrary.org/search.json?{urlencode(query)}")
        doc = next(iter(data.get("docs") or ()), None)
        if not isinstance(doc, dict) or not WORK.fullmatch(str(doc.get("key"))):
            return None
        work = await self._json(f"https://openlibrary.org{doc['key']}.json")
        return _card("Open Library", doc.get("title"), doc.get("author_name"), doc.get("first_publish_year"),
                     work.get("description"))

    async def _crossref(self, title, author, language):
        query = {"query.bibliographic": title, "rows": 1, "select": "DOI,title,author,abstract,issued"}
        if author:
            query["query.author"] = author
        data = await self._json(f"https://api.crossref.org/works?{urlencode(query)}")
        item = next(iter((data.get("message") or {}).get("items") or ()), None)
        if not isinstance(item, dict):
            return None
        names = [" ".join(filter(None, (a.get("given"), a.get("family")))) for a in item.get("author") or ()
                 if isinstance(a, dict)]
        year = next(iter(next(iter((item.get("issued") or {}).get("date-parts") or ()), None) or ()), None)
        return _card("Crossref", next(iter(item.get("title") or ()), None), names, year, item.get("abstract"))

    async def _wikipedia(self, title, author, language):
        for lang in dict.fromkeys([language if LANGUAGE.fullmatch(language or "") else "en", "en"]):
            for words in dict.fromkeys([title, f"{title} {author}" if author else title]):
                search = {"action": "query", "list": "search", "srsearch": words, "srlimit": 3, "srprop": "",
                          "format": "json"}
                data = await self._json(f"https://{lang}.wikipedia.org/w/api.php?{urlencode(search)}")
                hits = [h.get("title") for h in (data.get("query") or {}).get("search") or () if isinstance(h, dict)]
                # la voce dell'autore non parla dell'opera: con titolo e autore spesso viene prima
                page = next((h for h in hits if isinstance(h, str) and h.casefold() != (author or "").casefold()), None)
                if not page:
                    continue
                summary = await self._json(
                    f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(page.replace(' ', '_'), safe='')}")
                if summary.get("type") != "disambiguation":
                    return _card(f"Wikipedia ({lang})", summary.get("title"), None, None, summary.get("extract"))
        return None


def _card(source, title, authors, year, text):
    """Una scheda, se ha un testo: le descrizioni arrivano anche come {"value": ...} o con tag
    (JATS di Crossref, HTML)."""
    text = text.get("value") if isinstance(text, dict) else text
    text = " ".join(re.sub(r"<[^>]+>", " ", text).split())[:TEXT_MAX] if isinstance(text, str) else ""
    if not text:
        return None
    card = {"source": source, "title": title if isinstance(title, str) else None, "text": text}
    if authors:
        card["authors"] = [a for a in authors if isinstance(a, str)][:5]
    if isinstance(year, int):
        card["year"] = year
    return card
