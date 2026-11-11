"""La lettura dei documenti mandati in chat: PDF, EPUB, DOCX, ODT e testo semplice.

Due profondità, come vuole l'utente:
- di norma solo ciò che dice che cos'è un documento, con pochi token: i dati del file (titolo,
  autori, la descrizione di un EPUB) e le prime pagine, dove stanno il titolo, gli autori e
  l'abstract di un articolo. Se le prime pagine non hanno testo (una scansione), un'immagine
  della prima, che Claude guarda;
- con full, solo quando l'utente lo chiede, tutto il testo.

I PDF si leggono con poppler (pdfinfo, pdftotext, pdftoppm); EPUB, DOCX e ODT sono archivi zip
di XML e HTML, letti con la libreria standard e con un limite a quanto se ne estrae: un archivio
può promettere poco e srotolarsi in gigabyte. Tutto avviene sul file già scaricato, in una
cartella temporanea che sparisce subito dopo. Il testo letto è un dato, mai istruzioni.
"""

import asyncio
import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

from .listener import run_program
from .reader import MAX_IMAGE, Extract, ReadError

GLANCE_PAGES = 2            # le pagine di un PDF che si guardano di norma
GLANCE_CHARS = 6_000        # il testo che si guarda di norma, da qualunque formato
FULL_CHARS = 3_000_000      # il testo che si custodisce di un documento letto tutto: un libro lungo
MEMBER_MAX = 50_000_000     # byte di un file dentro un archivio
ARCHIVE_MAX = 200_000_000   # byte che si estraggono da un archivio, in tutto
INFO_TIMEOUT = 30
SCAN_SIZE = 1400            # il lato lungo dell'immagine di una pagina senza testo
TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".csv")
BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "blockquote", "pre"}
ODT_MIME, EPUB_MIME = "application/vnd.oasis.opendocument.text", "application/epub+zip"


class Documents:
    def __init__(self, pdfinfo="pdfinfo", pdftotext="pdftotext", pdftoppm="pdftoppm"):
        self.pdfinfo, self.pdftotext, self.pdftoppm = pdfinfo, pdftotext, pdftoppm

    async def read(self, path, name=None, mime=None, full=False):
        """Ciò che si legge di un documento: di norma i dati del file e le prime pagine, con full
        tutto il testo. Completo se si è letto tutto il documento. ReadError permanente per un
        formato che non si sa leggere."""
        kind = _kind(Path(path), name, mime)
        if kind is None:
            raise ReadError(f"formato di documento che non so leggere ({mime or 'tipo sconosciuto'})", permanent=True)
        if kind == "pdf":
            return await self._pdf(Path(path), full)
        # archivi e XML si leggono in un altro thread: un libro grosso non ferma comandi e collegamento
        title, authors, description, text = await asyncio.to_thread(
            {"epub": _epub, "docx": _docx, "odt": _odt, "text": _text_file}[kind], Path(path),
            FULL_CHARS if full else GLANCE_CHARS)
        return _extract(title, authors, description, text, FULL_CHARS if full else GLANCE_CHARS)

    async def _pdf(self, path, full):
        out = await run_program([self.pdfinfo, "-enc", "UTF-8", str(path)], INFO_TIMEOUT, "PDF che non so leggere",
                                permanent=True)
        info = dict(line.partition(":")[::2] for line in out.decode("utf-8", "replace").splitlines() if ":" in line)
        info = {k.strip(): v.strip() for k, v in info.items()}
        pages = int(info["Pages"]) if info.get("Pages", "").isdigit() else None
        title, authors = _meta_title(info.get("Title")), _meta(info.get("Author"))
        last = [] if full else ["-f", "1", "-l", str(GLANCE_PAGES)]
        out = await run_program([self.pdftotext, "-enc", "UTF-8", *last, str(path), "-"],
                                60 + (pages or 0) // 2 if full else 60, "testo del PDF non estratto", permanent=True,
                                limit=4 * (FULL_CHARS if full else GLANCE_CHARS) + 4)  # oltre, non servirebbe
        text = _tidy(out.decode("utf-8", "replace"))
        if full:
            if not text:
                raise ReadError("PDF senza testo: di una scansione si legge solo la prima pagina", permanent=True)
            return _extract(title, authors, None, text, FULL_CHARS)
        if len(text) < 20:  # niente testo nelle prime pagine (al più un numero di pagina): una scansione
            return Extract(title, authors, text or None, False, await self._scan(path))
        whole = pages is not None and pages <= GLANCE_PAGES
        return _extract(title, authors, None, text, GLANCE_CHARS, whole)

    async def _scan(self, path):
        """La prima pagina come immagine JPEG, ((tipo, byte),); () se non si disegna."""
        with tempfile.TemporaryDirectory(prefix="sazed-") as work:
            out = Path(work) / "page"
            try:
                await run_program([self.pdftoppm, "-f", "1", "-l", "1", "-singlefile", "-jpeg", "-scale-to",
                                   str(SCAN_SIZE), str(path), str(out)], 60, "pagina non disegnata")
            except ReadError:
                return ()
            image = out.with_suffix(".jpg")
            data = image.read_bytes() if image.exists() else b""
        return (("image/jpeg", data),) if 0 < len(data) <= MAX_IMAGE else ()


def _extract(title, authors, description, text, limit, whole=True):
    """L'Extract di un documento: la descrizione del file e il testo, fino a limit caratteri.
    Completo se il testo c'è tutto (whole: le pagine lette sono tutte quelle del documento)."""
    text = text or ""
    parts = [f"Description:\n{description}" if description else None, f"Text:\n{text[:limit]}" if text else None]
    return Extract(title, authors, "\n\n".join(p for p in parts if p) or None,
                   bool(text) and whole and len(text) <= limit)


def _kind(path, name, mime):
    """Il formato di un documento dai suoi primi byte (e, per il testo, dal nome o dal tipo)."""
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
                if "mimetype" in names:
                    with zf.open("mimetype") as f:
                        declared = f.read(100).decode("ascii", "replace").strip()
                    if declared in (EPUB_MIME, ODT_MIME):
                        return "epub" if declared == EPUB_MIME else "odt"
                return "docx" if "word/document.xml" in names else None
        except (zipfile.BadZipFile, OSError, RuntimeError):
            return None
    if (mime or "").startswith("text/") or Path(name or "").suffix.lower() in TEXT_SUFFIXES:
        return "text"
    return None


class _Archive:
    """Un archivio zip da cui si estrae al più MEMBER_MAX byte per file e ARCHIVE_MAX in tutto."""

    def __init__(self, path):
        try:
            self.zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError):
            raise ReadError("archivio rovinato", permanent=True) from None
        self.total = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.zf.close()

    def read(self, name):
        """Un file dell'archivio, o None se non c'è."""
        try:
            with self.zf.open(name) as f:
                data = f.read(MEMBER_MAX + 1)
        except KeyError:
            return None
        except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as err:
            raise ReadError(f"archivio rovinato ({type(err).__name__})", permanent=True) from None
        self.total += len(data)
        if len(data) > MEMBER_MAX or self.total > ARCHIVE_MAX:
            raise ReadError("archivio che si srotola oltre misura", permanent=True)
        return data

    def xml(self, name):
        data = self.read(name)
        if data is None:
            return None
        try:
            return ET.fromstring(data)
        except ET.ParseError:
            raise ReadError(f"XML rovinato in {name}", permanent=True) from None


def _epub(path, limit):
    with _Archive(path) as archive:
        container = archive.xml("META-INF/container.xml")
        root = next((e.get("full-path") for e in container.iter() if _local(e.tag) == "rootfile"),
                    None) if container is not None else None
        package = archive.xml(root) if root else None
        if package is None:
            raise ReadError("EPUB senza indice", permanent=True)
        title, authors, description = _dublin_core(package)
        manifest = {e.get("id"): (e.get("href"), e.get("media-type") or "") for e in package.iter()
                    if _local(e.tag) == "item"}
        texts, size = [], 0
        for ref in (e.get("idref") for e in package.iter() if _local(e.tag) == "itemref"):
            href, kind = manifest.get(ref, (None, ""))
            if not href or "html" not in kind:
                continue
            data = archive.read(posixpath.normpath(posixpath.join(posixpath.dirname(root), unquote(href))))
            text = _html_text(data.decode("utf-8", "replace")) if data else ""
            if text:
                texts.append(text)
                size += len(text)
            if size > limit:  # basta così: quello che resta non si legge
                break
    return title, authors, _html_text(description) if description else None, "\n\n".join(texts)


def _docx(path, limit):
    with _Archive(path) as archive:
        core = archive.xml("docProps/core.xml")
        title, authors, _ = _dublin_core(core) if core is not None else (None, None, None)
        document = archive.xml("word/document.xml")
        if document is None:
            raise ReadError("DOCX senza testo", permanent=True)
        paragraphs = []
        for p in document.iter():
            if _local(p.tag) != "p":
                continue
            paragraphs.append("".join(e.text or "" if _local(e.tag) == "t" else "\t" if _local(e.tag) == "tab"
                                      else "\n" if _local(e.tag) in ("br", "cr") else "" for e in p.iter()))
            if sum(map(len, paragraphs)) > limit:
                break
    return title, authors, None, _tidy("\n".join(paragraphs))


def _odt(path, limit):
    with _Archive(path) as archive:
        meta = archive.xml("meta.xml")
        title, authors, _ = _dublin_core(meta) if meta is not None else (None, None, None)
        if meta is not None and not authors:
            authors = _meta(next((e.text for e in meta.iter() if _local(e.tag) == "initial-creator"), None))
        content = archive.xml("content.xml")
        if content is None:
            raise ReadError("ODT senza testo", permanent=True)
        paragraphs = []
        for e in content.iter():
            if _local(e.tag) in ("p", "h"):
                paragraphs.append("".join(e.itertext()))
                if sum(map(len, paragraphs)) > limit:
                    break
    return title, authors, None, _tidy("\n".join(paragraphs))


def _text_file(path, limit):
    with open(path, "rb") as f:
        data = f.read(limit * 4 + 4)
    return None, None, None, _tidy(data.decode("utf-8-sig", "replace"))


def _dublin_core(root):
    """Titolo, autori e descrizione dai metadati Dublin Core (EPUB, DOCX, ODT)."""
    def texts(name):
        return [" ".join(e.text.split()) for e in root.iter() if _local(e.tag) == name and e.text and e.text.strip()]
    titles, creators, descriptions = texts("title"), texts("creator"), texts("description")
    return (_meta_title(titles[0]) if titles else None, _meta(", ".join(creators)),
            descriptions[0] if descriptions else None)


def _meta(value):
    value = " ".join((value or "").split())
    return value or None


def _meta_title(value):
    """Il titolo dei dati del file, se è un titolo e non il nome del file o di un modello."""
    value = _meta(value)
    if not value or len(value) < 3 or value.lower() in ("untitled", "senza titolo", "title", "titolo"):
        return None
    if value.startswith("Microsoft Word - ") or re.search(r"\.(docx?|odt|pdf|tex|dvi|rtf|indd)$", value, re.I):
        return None
    return value


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _tidy(text):
    """Il testo senza salti pagina né spazi in fondo alle righe, con al più una riga vuota di fila."""
    lines = (line.rstrip() for line in text.replace("\f", "\n").replace("\r", "").split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _html_text(markup):
    parser = _Text()
    parser.feed(markup)
    parser.close()
    return _tidy("\n".join(line.strip() for line in "".join(parser.parts).split("\n")))


class _Text(HTMLParser):
    """Il testo di una pagina XHTML di un EPUB: a capo dopo i blocchi, niente script né stili."""

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(re.sub(r"\s+", " ", data))
