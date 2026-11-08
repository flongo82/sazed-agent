"""La lettura dei documenti, con il vero poppler: di norma i dati del file e le prime pagine,
tutto il testo solo se richiesto; EPUB, DOCX, ODT e testo; scansioni, formati sconosciuti e
archivi che si srotolano oltre misura."""

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from sazed_agent import documents
from sazed_agent.documents import Documents
from sazed_agent.reader import ReadError
from tests.files import docx, epub, odt, pdf


class DocumentsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.documents = Documents()

    def file(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    async def test_a_pdf_is_glanced_at_its_first_pages_with_its_metadata(self):
        path = self.file("file", pdf(["On the Analytical Engine", "Abstract: a machine that weaves numbers",
                                      "Chapter three, never read"], title="Analytical Engine", author="Ada Lovelace"))
        got = await self.documents.read(path, "engine.pdf", "application/pdf")
        self.assertEqual((got.title, got.author, got.complete, got.images), ("Analytical Engine", "Ada Lovelace",
                                                                             False, ()))
        self.assertIn("Abstract: a machine that weaves numbers", got.text)
        self.assertNotIn("Chapter three", got.text)

    async def test_a_pdf_read_in_full_gives_all_its_text(self):
        path = self.file("file", pdf(["one", "two", "Chapter three, read at last"], title="Microsoft Word - bozza.docx"))
        got = await self.documents.read(path, full=True)
        self.assertEqual((got.title, got.complete), (None, True))  # il nome del file non è un titolo
        self.assertIn("Chapter three, read at last", got.text)

    async def test_a_short_pdf_is_read_whole_even_at_a_glance(self):
        got = await self.documents.read(self.file("file", pdf(["Ricevuta numero 42, pagata il 3 novembre"])))
        self.assertTrue(got.complete)

    async def test_a_scanned_pdf_shows_its_first_page_to_claude(self):
        path = self.file("file", pdf([], scan=True))
        got = await self.documents.read(path)
        [(kind, data)] = got.images
        self.assertEqual((kind, data[:2], got.complete), ("image/jpeg", b"\xff\xd8", False))
        with self.assertRaises(ReadError) as ctx:
            await self.documents.read(path, full=True)
        self.assertTrue(ctx.exception.permanent)

    async def test_an_epub_gives_its_metadata_and_its_chapters_in_order(self):
        epub(self.dir / "book", [("Prologo", "La cenere cadeva dal cielo."), ("Uno", "Vin guardava le brume.")])
        got = await self.documents.read(self.dir / "book", "pozzo.epub", "application/epub+zip")
        self.assertEqual((got.title, got.author, got.complete), ("Il pozzo dell'ascensione", "Brandon Sanderson", True))
        self.assertTrue(got.text.startswith("Description:\nIl secondo romanzo di Mistborn."))
        self.assertLess(got.text.index("La cenere"), got.text.index("Vin guardava"))

    async def test_a_long_epub_is_glanced_and_read_in_full_only_when_asked(self):
        chapters = [(f"Capitolo {i}", "parola " * 900 + f"fine del capitolo {i}") for i in range(5)]
        epub(self.dir / "book", chapters, creators=("Ada", "Bea"), description=None)
        glance = await self.documents.read(self.dir / "book")
        self.assertEqual((glance.author, glance.complete, "capitolo 4" in glance.text), ("Ada, Bea", False, False))
        self.assertLessEqual(len(glance.text), documents.GLANCE_CHARS + 10)
        whole = await self.documents.read(self.dir / "book", full=True)
        self.assertEqual((whole.complete, "fine del capitolo 4" in whole.text), (True, True))

    async def test_docx_odt_and_plain_text(self):
        docx(self.dir / "d", ["Contratto di locazione", "Tra Ada e Bea"], title="Contratto", creator="Studio Rossi")
        got = await self.documents.read(self.dir / "d", "contratto.docx")
        self.assertEqual((got.title, got.author, got.text), ("Contratto", "Studio Rossi",
                                                             "Text:\nContratto di locazione\nTra Ada e Bea"))
        odt(self.dir / "o", "Verbale", ["Presenti: Ada"], title="Verbale di condominio", creator="Bea")
        got = await self.documents.read(self.dir / "o")
        self.assertEqual((got.title, got.author, got.text), ("Verbale di condominio", "Bea",
                                                             "Text:\nVerbale\nPresenti: Ada fine"))
        got = await self.documents.read(self.file("t", "riso\r\n\r\n\r\n\r\nparmigiano  \n".encode()), "spesa.txt")
        self.assertEqual((got.text, got.complete), ("Text:\nriso\n\nparmigiano", True))

    async def test_what_cannot_be_read_says_so_for_good(self):
        with zipfile.ZipFile(self.dir / "z", "w") as z:
            z.writestr("foto.jpg", b"\xff\xd8")
        cases = [(self.file("bin", b"\x00\x01\x02 binario"), "programma.exe", "application/octet-stream"),
                 (self.dir / "z", "archivio.zip", "application/zip"),
                 (self.file("rotto", b"%PDF-1.4 rotto"), "rotto.pdf", "application/pdf")]
        for path, name, mime in cases:
            with self.subTest(name), self.assertRaises(ReadError) as ctx:
                await self.documents.read(path, name, mime)
            self.assertTrue(ctx.exception.permanent)

    async def test_an_archive_that_unrolls_beyond_measure_is_refused(self):
        docx(self.dir / "d", ["x" * 5000])
        with mock.patch.object(documents, "MEMBER_MAX", 1000), self.assertRaises(ReadError) as ctx:
            await self.documents.read(self.dir / "d")
        self.assertIn("oltre misura", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
