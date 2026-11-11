"""La lettura delle fonti: dal link al testo che coppermind custodisce.

- link brevi (share.google, bit.ly, ...): si seguono i rimandi fino all'indirizzo vero, così
  lo stesso contenuto resta un elemento solo;
- YouTube, Instagram, TikTok: le anteprime pubbliche (oEmbed) danno titolo, autore, la
  copertina e, per Instagram e TikTok, la didascalia intera. La copertina conta quando la
  didascalia manca: spesso il titolo del video è scritto lì. Sottotitoli e audio arriveranno
  con yt-dlp e Whisper: per ora la lettura è parziale;
- pagine web: titolo, descrizione, dati strutturati (le ricette di schema.org con ingredienti e
  passi) e il testo principale.

Ogni indirizzo, anche dopo un rimando, deve portare a un host pubblico: niente rete di casa né
indirizzi locali. La connessione va proprio all'indirizzo controllato, non al nome: un DNS che
cambia risposta fra il controllo e la connessione (DNS rebinding) non porta altrove. Il nome resta
nell'header Host e, per https, nel nome del server TLS (SNI), su cui si verifica il certificato;
le connessioni non si riusano, perché una aperta per un nome non serva un altro. Si scaricano al massimo MAX_BYTES, e nulla resta su disco: la copertina
serve a descrivere e poi si dimentica. Testo e immagini lette sono dati: chi li usa (Claude) li
tratta come contenuto, mai come istruzioni.
"""

import asyncio
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .coppermind import parse_link

MAX_BYTES = 5_000_000
MAX_TEXT = 30_000
MAX_REDIRECTS = 8
MAX_IMAGE = 4_000_000  # l'API di Claude accetta immagini fino a 5 MB
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
USER_AGENT = "Mozilla/5.0 (compatible; sazed-agent/1.0)"

SHORTENERS = {"share.google", "search.app", "bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly",
              "buff.ly", "is.gd", "lnkd.in", "vm.tiktok.com", "vt.tiktok.com", "fb.me", "amzn.to",
              "amzn.eu", "dlvr.it", "rebrand.ly", "cutt.ly", "shorturl.at", "t.ly", "pin.it"}

# Le anteprime pubbliche delle piattaforme: indirizzo e se la didascalia è il contenuto.
OEMBED = {
    "youtube": ("https://www.youtube.com/oembed", False),
    "instagram": ("https://www.instagram.com/api/v1/oembed/", True),
    "tiktok": ("https://www.tiktok.com/oembed", True),
}


class ReadError(Exception):
    """Una lettura non riuscita.

    unavailable: la fonte non c'è più (404, 410); permanent: inutile riprovare (accesso
    negato, tipo di file che non si sa leggere); altrimenti si riprova più tardi.
    """

    def __init__(self, message, permanent=False, unavailable=False):
        super().__init__(message)
        self.permanent, self.unavailable = permanent or unavailable, unavailable


@dataclass(frozen=True)
class Extract:
    title: str | None
    author: str | None
    text: str | None     # tutto ciò che si è letto, con descrizione e dati strutturati
    complete: bool       # False quando manca una parte (l'audio di un video, per esempio)
    images: tuple = ()   # immagini viste alla fonte, come (tipo, byte): la copertina di un video
    screen_text: str | None = None   # le scritte sui fotogrammi di un video, lette in locale (tesseract)


async def public_address(host):
    """L'indirizzo a cui connettersi per host (IPv4 se c'è), se tutti i suoi indirizzi sono
    pubblici: niente rete locale né loopback. None altrimenti."""
    try:
        addresses = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError:
            raise ReadError(f"host sconosciuto: {host}") from None
        addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    if not addresses or not all(a.is_global for a in addresses):
        return None
    return str(next((a for a in addresses if a.version == 4), addresses[0]))


class Reader:
    def __init__(self, timeout=20.0, transport=None, allow=public_address):
        self.allow = allow  # host -> l'indirizzo controllato a cui connettersi, o None
        self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport,
                                        headers={"user-agent": USER_AGENT},
                                        limits=httpx.Limits(max_keepalive_connections=0))

    async def close(self):
        await self.client.aclose()

    async def resolve(self, url):
        """L'indirizzo vero dietro un link breve; gli altri link restano come sono."""
        if (urlsplit(_absolute(url)).hostname or "").lower() not in SHORTENERS:
            return url
        final, _, _ = await self._get(_absolute(url), body=False)  # conta l'indirizzo, non la pagina
        return final

    async def read(self, url):
        """Ciò che si legge dietro un link, o ReadError."""
        url = _absolute(url)
        platform = parse_link(url).platform
        if platform in OEMBED:
            return await self._oembed(url, *OEMBED[platform])
        final, headers, body = await self._get(url)
        kind = headers.get("content-type", "").split(";")[0].strip().lower()
        if kind in ("text/html", "application/xhtml+xml", ""):
            return await asyncio.to_thread(page, _decode(body, headers), final)  # fino a 5 MB di HTML
        if kind == "text/plain":
            text = _decode(body, headers).strip()[:MAX_TEXT]
            return Extract(None, None, text or None, bool(text))
        raise ReadError(f"tipo di contenuto che non so leggere: {kind}", permanent=True)

    async def get_json(self, url, user_agent=None):
        """Un oggetto JSON da un servizio pubblico (le schede delle opere), o ReadError. Un
        servizio può chiedere di presentarsi con un User-Agent che dica chi chiama (user_agent)."""
        _, _, body = await self._get(url, headers={"user-agent": user_agent} if user_agent else None)
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ReadError("risposta JSON non valida")
        return data

    async def _oembed(self, url, endpoint, caption_is_content):
        _, _, body = await self._get(f"{endpoint}?{urlencode({'url': url, 'format': 'json'})}")
        try:
            data = json.loads(body)
        except ValueError:
            raise ReadError("anteprima non valida") from None
        title = data.get("title").strip() if isinstance(data.get("title"), str) else None
        author = _clean(data.get("author_name"))
        cover = await self._image(data.get("thumbnail_url"))
        if not title and not author and not cover:
            raise ReadError("anteprima vuota", permanent=True)
        if caption_is_content:  # la didascalia è il testo, a capo compresi: come titolo la prima riga
            return Extract(_first_line(title), author, title[:MAX_TEXT] if title else None, False, cover)
        return Extract(_clean(title), author, None, False, cover)

    async def _image(self, url):
        """Un'immagine da mostrare a Claude, come ((tipo, byte),); () se non c'è o non si legge:
        senza copertina la lettura va avanti lo stesso."""
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            return ()
        try:
            _, headers, body = await self._get(url)
        except ReadError:
            return ()
        kind = headers.get("content-type", "").split(";")[0].strip().lower()
        return ((kind, body),) if kind in IMAGE_TYPES and 0 < len(body) <= MAX_IMAGE else ()

    async def _get(self, url, body=True, headers=None):
        """GET seguendo i rimandi, uno per uno, solo verso host pubblici."""
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ReadError(f"indirizzo non leggibile: {parts.scheme}://", permanent=True)
            address = await self.allow(parts.hostname)
            if not address:
                raise ReadError(f"indirizzo non pubblico: {parts.hostname}", permanent=True)
            target, request_headers, extensions = _pinned(url, parts, address, headers)
            try:
                async with self.client.stream("GET", target, headers=request_headers,
                                              extensions=extensions) as response:
                    if response.is_redirect and "location" in response.headers:
                        url = urljoin(url, response.headers["location"])
                        continue
                    if not body:
                        return url, response.headers, b""
                    _check(response.status_code)
                    return url, response.headers, await _limited(response)
            except httpx.TimeoutException:
                raise ReadError("tempo scaduto") from None
            except httpx.HTTPError as err:
                raise ReadError(f"rete: {type(err).__name__}") from None
        raise ReadError("troppi rimandi", permanent=True)


def _pinned(url, parts, address, headers):
    """La richiesta per url verso l'indirizzo già controllato: l'indirizzo al posto del nome, il
    nome nell'header Host e, per https, come nome del server TLS (SNI), su cui si verifica il
    certificato. Per un indirizzo scritto nel link, la richiesta resta com'è."""
    if address == parts.hostname:
        return url, headers, None
    try:
        host = parts.hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise ReadError(f"indirizzo non leggibile: {parts.hostname}", permanent=True) from None
    port = f":{parts.port}" if parts.port else ""
    target = urlunsplit(parts._replace(netloc=(f"[{address}]" if ":" in address else address) + port))
    return target, {**(headers or {}), "host": host + port}, ({"sni_hostname": host} if parts.scheme == "https" else None)


def _check(status):
    if status in (404, 410):
        raise ReadError(f"la fonte non c'è più (HTTP {status})", unavailable=True)
    if status in (401, 403, 451) or 400 <= status < 500 and status not in (408, 425, 429):
        raise ReadError(f"accesso negato (HTTP {status})", permanent=True)
    if status >= 400:
        raise ReadError(f"HTTP {status}")


async def _limited(response):
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data += chunk
        if len(data) >= MAX_BYTES:
            break
    return bytes(data[:MAX_BYTES])


def _absolute(url):
    url = url.strip()
    return url if "://" in url else "https://" + url


def _decode(body, headers):
    charset = re.search(r"charset=[\"']?([\w-]+)", headers.get("content-type", ""))
    if not charset:
        charset = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    name = charset.group(1) if charset else "utf-8"
    name = name.decode("ascii", "replace") if isinstance(name, bytes) else name
    try:
        return body.decode(name, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _clean(text):
    text = " ".join(str(text).split()) if isinstance(text, str) and text.strip() else None
    return text or None


def _first_line(text):
    if not text:
        return None
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line[:120].rstrip() or None


def page(html, url=None):
    """Ciò che si legge in una pagina HTML: titolo, autore, e il testo con descrizione e dati
    strutturati davanti."""
    parser = _Page()
    try:
        parser.feed(html)
        parser.close()
    except AssertionError:  # HTML troppo rotto per il parser: si tiene ciò che ha letto
        pass
    meta, data = parser.meta, _structured(parser.jsonld)
    title = _clean(meta.get("og:title")) or _clean(data.get("title")) or _clean(" ".join(parser.title))
    author = _clean(meta.get("author")) or _clean(data.get("author")) or _clean(meta.get("og:site_name"))
    description = _clean(meta.get("og:description")) or _clean(meta.get("description")) or _clean(
        data.get("description"))
    parts = [p for p in (description, data.get("text"), parser.text()) if p]
    text = "\n\n".join(dict.fromkeys(parts))[:MAX_TEXT] or None
    if not title and not text:
        raise ReadError("nella pagina non c'è testo da leggere", permanent=True)
    return Extract(title, author, text, bool(parser.text()) or bool(data.get("text")))


class _Page(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "nav", "footer", "aside", "form",
            "button", "select", "iframe", "dialog"}
    BLOCKS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "td", "th",
              "figcaption", "dd", "dt", "div", "section", "article", "main", "br", "tr", "ul", "ol"}
    HEADINGS = {"h1", "h2", "h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.jsonld = [], {}, []
        self.blocks = []           # (testo, dentro article/main, è un titolo)
        self._skip = {}            # tag da saltare aperti, per nome
        self._main = 0
        self._buffer, self._heading = [], False
        self._in_title = self._in_jsonld = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key and attrs.get("content") and key not in self.meta:
                self.meta[key] = attrs["content"]
            return
        if tag == "script" and (attrs.get("type") or "").lower() == "application/ld+json":
            self._in_jsonld = True
            self.jsonld.append("")
            return
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip[tag] = self._skip.get(tag, 0) + 1
        if tag in ("article", "main"):
            self._main += 1
        if tag in self.BLOCKS:
            self._flush()
            self._heading = tag in self.HEADINGS

    def handle_endtag(self, tag):
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            return
        if tag == "title":
            self._in_title = False
        if self._skip.get(tag):
            self._skip[tag] -= 1
        if tag in self.BLOCKS:
            self._flush()
        if tag in ("article", "main") and self._main:
            self._main -= 1

    def handle_data(self, data):
        if self._in_jsonld:
            self.jsonld[-1] += data
        elif self._in_title:
            self.title.append(data)
        elif not any(self._skip.values()):
            self._buffer.append(data)

    def close(self):
        super().close()
        self._flush()

    def _flush(self):
        text = " ".join("".join(self._buffer).split())
        self._buffer = []
        if text:
            self.blocks.append((text, self._main > 0, self._heading))

    def text(self):
        """Il testo principale: i blocchi dentro article o main, se ce n'è abbastanza, altrimenti
        tutti; via le righe corte (menu, pulsanti) tranne i titoli, e i doppioni."""
        main = [b for b in self.blocks if b[1]]
        blocks = main if sum(len(b[0]) for b in main) >= 500 else self.blocks
        kept = [text for text, _, heading in blocks if heading or len(text) >= 40]
        return "\n".join(dict.fromkeys(kept))[:MAX_TEXT] or None


def _structured(scripts):
    """Dai dati JSON-LD della pagina: titolo, autore, descrizione e, per una ricetta,
    ingredienti e passi."""
    found = {}
    for node in _nodes(scripts):
        types = node.get("@type")
        types = {types} if isinstance(types, str) else set(types or ())
        if "Recipe" in types and "text" not in found:
            lines = [f"Recipe: {_clean(node.get('name')) or ''}".rstrip()]
            ingredients = [_clean(i) for i in _list(node.get("recipeIngredient")) if _clean(i)]
            if ingredients:
                lines += ["Ingredients:"] + [f"- {i}" for i in ingredients]
            steps = [s for s in _steps(node.get("recipeInstructions")) if s]
            if steps:
                lines += ["Steps:"] + [f"{n}. {s}" for n, s in enumerate(steps, 1)]
            for key, label in (("recipeYield", "Yield"), ("totalTime", "Total time")):
                value = _clean(", ".join(map(str, _list(node.get(key)))))
                if value:
                    lines.append(f"{label}: {value}")
            found["text"] = "\n".join(lines)
            found.setdefault("title", node.get("name"))
            found.setdefault("description", node.get("description"))
        if types & {"Article", "NewsArticle", "BlogPosting", "Recipe", "VideoObject", "WebPage"}:
            found.setdefault("title", node.get("headline") or node.get("name"))
            found.setdefault("description", node.get("description"))
            author = node.get("author")
            author = author[0] if isinstance(author, list) and author else author
            if isinstance(author, dict):
                author = author.get("name")
            if isinstance(author, str):
                found.setdefault("author", author)
    return {k: v for k, v in found.items() if isinstance(v, str) and v.strip()}


def _nodes(scripts):
    for script in scripts:
        try:
            data = json.loads(script, strict=False)
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop(0)
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                stack.extend(v for k, v in node.items() if k == "@graph" or k == "mainEntity")


def _list(value):
    return value if isinstance(value, list) else [] if value is None else [value]


def _steps(value):
    for step in _list(value):
        if isinstance(step, str):
            yield from (_clean(s) for s in step.splitlines())
        elif isinstance(step, dict):
            if step.get("itemListElement"):
                yield from _steps(step["itemListElement"])
            else:
                yield _clean(step.get("text") or step.get("name"))
