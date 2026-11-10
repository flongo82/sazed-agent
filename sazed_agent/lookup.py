"""Le schede delle opere pubblicate, da fonti libere e gratuite: quando di un libro o di un
articolo si conoscono titolo e autori, ma le prime pagine non dicono di che cosa parla.

- Open Library, per i libri: la descrizione dell'opera;
- Crossref, per gli articoli: l'abstract, quando l'editore lo deposita;
- Wikipedia, per le opere note, prima nella lingua dell'utente e poi in inglese: l'inizio della
  voce, solo se il suo titolo è quello dell'opera (una ricerca trova anche voci che non c'entrano).

Open Library conferma anche titolo, autori e anno di un libro di cui non ha la descrizione, e
ritrova un titolo sentito male (una nota vocale) fra le opere del suo autore; il titolo trovato
si cerca poi su Wikipedia.

Si chiede solo per opere pubblicate, mai per documenti personali (lo decide chi le descrive),
con poche richieste per fonte; una fonte che non risponde si salta. Le fonti chiedono a chi le
usa di presentarsi: lo User-Agent dice che cosa è sazed e dove trovarlo (Wikipedia, senza,
risponde 403). Open Library conosce i libri soprattutto con il titolo originale: per un libro
tradotto la scheda arriva di solito da Wikipedia. Le schede sono candidate:
chi le usa (Claude) le prende solo se parlano davvero della stessa opera. Titoli e autori non
vanno nei log, come i link dell'utente.
"""

import difflib
import logging
import re
import unicodedata
from urllib.parse import quote, urlencode

from .reader import ReadError

log = logging.getLogger("sazed_agent.lookup")

TEXT_MAX = 2_000                       # caratteri di una scheda
USER_AGENT = "sazed-agent/1.0 (+https://github.com/flongo82/sazed-agent; personal knowledge base)"
LANGUAGE = re.compile(r"[a-z]{2,3}")   # un codice ISO 639, l'unica parte variabile di un host di Wikipedia
WORK = re.compile(r"/works/OL\d+W")
SAME = 0.9          # due titoli quasi uguali (una lettera, un articolo): la stessa opera
ALIKE = 0.75        # fra le opere di un autore, un titolo sentito male e quello vero
AUTHOR_WORKS = 20   # quante opere di un autore si guardano per ritrovare un titolo sentito male
SUBTITLE = re.compile(r"\s*[:(\u2013\u2014]\s*|\s+-\s+")


class Lookup:
    def __init__(self, reader):
        self.reader = reader  # le richieste passano dal lettore: solo host pubblici, risposte limitate

    async def find(self, title, authors, kind, language):
        """Le schede trovate per un'opera (kind "book", "article" o altro), come dizionari con
        fonte, titolo, autori, anno e testo (una scheda di Open Library può non avere il testo:
        conferma titolo, autori e anno); [] se nessuna fonte ne ha una. Il titolo che una fonte
        trova si cerca anche nelle successive."""
        if not title:
            return []
        authors = [a for a in authors or () if isinstance(a, str) and a.strip()][:2]
        sources = {"book": [self._open_library], "article": [self._crossref]}.get(kind, []) + [self._wikipedia]
        titles, found = [title], []
        for source in sources:
            try:
                card = await source(titles, authors, language)
            except ReadError as err:
                log.warning("scheda di %s non letta: %s", source.__name__.strip("_"), err)
                continue
            if card:
                found.append(card)
                if card.get("title") and card["title"] not in titles:
                    titles.append(card["title"])
        return found

    async def _json(self, url):
        return await self.reader.get_json(url, USER_AGENT)

    async def _open_library(self, titles, authors, language):
        """Il libro con titolo e primo autore (Open Library conosce anche i titoli tradotti); se
        non c'è, con il solo titolo e uno degli autori fra i suoi (il nome sentito male); se non
        c'è ancora, fra le opere di ciascun autore il titolo più vicino (il titolo sentito male)."""
        title = titles[0]
        doc = next(iter(await self._books(title=title, author=authors[0] if authors else None, limit=1)), None)
        if doc is None and authors:
            doc = next((d for d in await self._books(title=title, limit=5)
                        if same_title(title, d.get("title")) and _written_by(d, authors)), None)
        for author in authors if doc is None else ():
            doc = _closest(title, await self._books(author=author, limit=AUTHOR_WORKS))
            if doc is not None:
                break
        if doc is None:
            return None
        work = await self._json(f"https://openlibrary.org{doc['key']}.json")
        return _card("Open Library", doc.get("title"), doc.get("author_name"), doc.get("first_publish_year"),
                     work.get("description"), text_needed=False)

    async def _books(self, limit, **query):
        """Le opere trovate su Open Library (con la loro chiave /works/), nell'ordine della ricerca."""
        query = {k: v for k, v in query.items() if v} | {"limit": limit,
                                                           "fields": "key,title,author_name,first_publish_year"}
        data = await self._json(f"https://openlibrary.org/search.json?{urlencode(query)}")
        return [d for d in data.get("docs") or () if isinstance(d, dict) and WORK.fullmatch(str(d.get("key")))]

    async def _crossref(self, titles, authors, language):
        title, author = titles[0], authors[0] if authors else None
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

    async def _wikipedia(self, titles, authors, language):
        """La voce dell'opera, nella lingua dell'utente o in inglese, cercata con ogni titolo noto,
        da solo e con il primo autore: solo una voce con il titolo dell'opera (la ricerca trova
        anche le voci dell'autore, o di tutt'altro)."""
        author = authors[0] if authors else None
        for lang in dict.fromkeys([language if LANGUAGE.fullmatch(language or "") else "en", "en"]):
            for words in dict.fromkeys(w for t in titles for w in (t, f"{t} {author}" if author else t)):
                search = {"action": "query", "list": "search", "srsearch": words, "srlimit": 3, "srprop": "",
                          "format": "json"}
                data = await self._json(f"https://{lang}.wikipedia.org/w/api.php?{urlencode(search)}")
                hits = [h.get("title") for h in (data.get("query") or {}).get("search") or () if isinstance(h, dict)]
                page = next((h for h in hits if isinstance(h, str) and any(same_title(t, h) for t in titles)), None)
                if not page:
                    continue
                summary = await self._json(
                    f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(page.replace(' ', '_'), safe='')}")
                if summary.get("type") != "disambiguation":
                    return _card(f"Wikipedia ({lang})", summary.get("title"), None, None, summary.get("extract"))
        return None


def same_title(wanted, found):
    """True se found è il titolo wanted: uguale a meno di maiuscole, accenti e punteggiatura,
    anche con qualcosa in più (un sottotitolo, la serie, «(romanzo)»), o quasi uguale. wanted vale
    anche senza il suo sottotitolo."""
    found = _words(found)
    for words in dict.fromkeys(tuple(_words(w)) for w in (wanted, SUBTITLE.split(wanted or "")[0])):
        n = len(words)
        if n and (any(tuple(found[i:i + n]) == words for i in range(len(found) - n + 1))
                  or _ratio(words, found) >= SAME):
            return True
    return False


def _closest(title, docs):
    """Fra le opere di un autore, quella con il titolo più vicino a title, se abbastanza vicino."""
    scored = [(_ratio(_words(title), _words(d.get("title"))), i) for i, d in enumerate(docs)]
    best = max(scored, default=(0, None))
    return docs[best[1]] if best[0] >= ALIKE else None


def _written_by(doc, authors):
    """True se fra gli autori dell'opera c'è uno di authors, anche con il nome un po' storpiato."""
    return any(_ratio(_words(a), _words(n)) >= ALIKE for a in authors for n in doc.get("author_name") or ()
               if isinstance(n, str))


def _words(text):
    """Le parole di un testo, senza maiuscole, accenti e punteggiatura."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text if isinstance(text, str) else "")
                   if not unicodedata.combining(c))
    return re.findall(r"\w+", text.casefold())


def _ratio(a, b):
    return difflib.SequenceMatcher(None, " ".join(a), " ".join(b)).ratio() if a and b else 0.0


def _card(source, title, authors, year, text, text_needed=True):
    """Una scheda, se ha un testo (o anche senza, con text_needed falso): le descrizioni arrivano
    anche come {"value": ...} o con tag (JATS di Crossref, HTML)."""
    text = text.get("value") if isinstance(text, dict) else text
    text = " ".join(re.sub(r"<[^>]+>", " ", text).split())[:TEXT_MAX] if isinstance(text, str) else ""
    if not text and text_needed:
        return None
    card = {"source": source, "title": title if isinstance(title, str) else None}
    if text:
        card["text"] = text
    if authors:
        card["authors"] = [a for a in authors if isinstance(a, str)][:5]
    if isinstance(year, int):
        card["year"] = year
    return card
