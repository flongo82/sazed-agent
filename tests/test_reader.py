"""Il lettore contro un finto web: link brevi, anteprime delle piattaforme, pagine, errori."""

import unittest

from sazed_agent.reader import MAX_BYTES, Reader, ReadError, page, public_address
from tests.fake_web import FakeWeb, allow_all

RECIPE = """<html><head><title>Torta di mele | Cucina</title>
<meta property="og:title" content="Torta di mele della nonna">
<meta name="description" content="Soffice, con la cannella.">
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [
 {"@type": "WebSite", "name": "Cucina"},
 {"@type": "Recipe", "name": "Torta di mele", "recipeIngredient": ["3 uova", "250 g farina", "2 mele"],
  "recipeInstructions": [{"@type": "HowToStep", "text": "Sbatti le uova."},
                         {"@type": "HowToSection", "itemListElement": [{"@type": "HowToStep", "text": "Inforna."}]}],
  "author": {"@type": "Person", "name": "Nonna Pina"}}]}</script>
<script>var tracking = "da non leggere";</script></head>
<body><nav><ul><li>Home</li><li>Ricette dolci e salate, tutte le categorie del sito</li></ul></nav>
<article><h1>Torta di mele</h1><p>Questa torta di mele è la ricetta di famiglia, tramandata da tre generazioni.
<p>Il segreto è un pizzico di cannella e le mele tagliate sottili, messe a raggiera sopra l'impasto.</p>
</article><footer><p>Tutti i diritti riservati, nessuna parte può essere copiata.</p></footer></body></html>"""


class PageTest(unittest.TestCase):
    def test_a_recipe_page_gives_title_author_structured_data_and_main_text(self):
        extract = page(RECIPE)
        self.assertEqual((extract.title, extract.author, extract.complete),
                         ("Torta di mele della nonna", "Nonna Pina", True))
        text = extract.text
        self.assertTrue(text.startswith("Soffice, con la cannella.\n\nRecipe: Torta di mele\nIngredients:\n- 3 uova"))
        self.assertIn("Steps:\n1. Sbatti le uova.\n2. Inforna.", text)
        self.assertIn("tramandata da tre generazioni", text)
        self.assertIn("messe a raggiera", text)
        for left_out in ("da non leggere", "tutte le categorie", "diritti riservati"):
            self.assertNotIn(left_out, text)  # script, menu e piè di pagina restano fuori

    def test_without_article_all_long_blocks_count(self):
        extract = page("<title>Note</title><div>Un paragrafo abbastanza lungo da essere testo vero.</div><p>ok</p>")
        self.assertEqual((extract.title, extract.text), ("Note", "Un paragrafo abbastanza lungo da essere testo vero."))

    def test_a_page_with_nothing_to_read(self):
        with self.assertRaises(ReadError) as ctx:
            page("<html><body><script>app()</script></body></html>")
        self.assertTrue(ctx.exception.permanent)

    def test_only_a_title_is_a_partial_reading(self):
        extract = page("<title>Instagram</title><body><div id=root></div>")
        self.assertEqual((extract.title, extract.text, extract.complete), ("Instagram", None, False))


class ReaderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.web = FakeWeb()
        self.reader = Reader(transport=self.web.transport, allow=allow_all)
        self.addAsyncCleanup(self.reader.close)

    async def test_a_short_link_leads_to_the_true_address(self):
        self.web.redirect("https://share.google/abc", "https://www.google.com/share.google?q=abc")
        self.web.redirect("https://www.google.com/share.google?q=abc", "https://www.example.com/articolo", 301)
        self.assertEqual(await self.reader.resolve("https://share.google/abc"), "https://www.example.com/articolo")
        self.assertEqual(await self.reader.resolve("https://example.com/x"), "https://example.com/x")
        self.assertEqual(len(self.web.requests), 3)  # senza leggere la pagina; un link qualunque non si tocca

    async def test_a_page_is_read_after_its_redirects(self):
        self.web.redirect("https://example.com/r", "/ricetta")
        self.web.page("https://example.com/ricetta", RECIPE)
        extract = await self.reader.read("https://example.com/r")
        self.assertEqual(extract.title, "Torta di mele della nonna")

    async def test_the_charset_comes_from_the_page_when_the_server_does_not_say(self):
        html = '<meta charset="iso-8859-1"><title>Caffè</title><p>Un caffè ristretto, perché così è più buono.'
        self.web.route("https://example.com/c", body=html.encode("iso-8859-1"), headers={"content-type": "text/html"})
        self.assertEqual((await self.reader.read("https://example.com/c")).title, "Caffè")

    async def test_instagram_gives_its_caption_through_the_public_preview(self):
        url = "https://www.instagram.com/reel/ABC/"
        self.web.oembed("instagram", url, {"title": "RICETTA QUA SOTTO\nTorta di mele: 3 uova, 160 g zucchero",
                                           "author_name": "pasticcere"})
        extract = await self.reader.read(url)
        self.assertEqual((extract.title, extract.author, extract.complete), ("RICETTA QUA SOTTO", "pasticcere", False))
        self.assertIn("160 g zucchero", extract.text)

    async def test_a_reel_without_caption_gives_its_cover(self):
        url = "https://www.instagram.com/reel/XYZ/"
        cover = "https://cdn.example.com/cover.jpg"
        self.web.oembed("instagram", url, {"title": "", "author_name": "pasticcere", "thumbnail_url": cover})
        self.web.route(cover, 200, b"\xff\xd8jpeg", {"content-type": "image/jpeg"})
        extract = await self.reader.read(url)
        self.assertEqual((extract.title, extract.author, extract.text, extract.images),
                         (None, "pasticcere", None, (("image/jpeg", b"\xff\xd8jpeg"),)))

    async def test_a_cover_that_cannot_be_seen_is_left_out(self):
        url = "https://www.instagram.com/reel/XYZ/"
        cases = [("https://cdn.example.com/via.jpg", None), ("https://cdn.example.com/pagina", "text/html"),
                 ("javascript:alert(1)", None), (7, None)]
        for cover, kind in cases:
            with self.subTest(cover):
                self.web.oembed("instagram", url, {"title": "Torta", "thumbnail_url": cover})
                if kind:
                    self.web.route(cover, 200, b"<html>", {"content-type": kind})
                extract = await self.reader.read(url)
                self.assertEqual((extract.title, extract.images), ("Torta", ()))
        self.web.oembed("instagram", url, {"title": "", "thumbnail_url": "https://cdn.example.com/via.jpg"})
        with self.assertRaisesRegex(ReadError, "anteprima vuota"):
            await self.reader.read(url)

    async def test_youtube_gives_title_and_channel(self):
        url = "https://www.youtube.com/watch?v=abc"
        self.web.oembed("youtube", url, {"title": "Carbonara perfetta", "author_name": "Chef"})
        extract = await self.reader.read(url)
        self.assertEqual((extract.title, extract.author, extract.text, extract.complete),
                         ("Carbonara perfetta", "Chef", None, False))

    async def test_errors_say_whether_to_try_again(self):
        self.web.route("https://example.com/via", 410)
        self.web.route("https://example.com/vietato", 403)
        self.web.route("https://example.com/giu", 503)
        self.web.route("https://example.com/doc.pdf", body=b"%PDF", headers={"content-type": "application/pdf"})
        cases = [("https://example.com/via", True, True), ("https://example.com/vietato", True, False),
                 ("https://example.com/giu", False, False), ("https://example.com/doc.pdf", True, False),
                 ("https://www.youtube.com/watch?v=nessuno", True, True)]
        for url, permanent, unavailable in cases:
            with self.subTest(url):
                with self.assertRaises(ReadError) as ctx:
                    await self.reader.read(url)
                self.assertEqual((ctx.exception.permanent, ctx.exception.unavailable), (permanent, unavailable))

    async def test_endless_redirects_stop(self):
        self.web.redirect("https://example.com/a", "https://example.com/b")
        self.web.redirect("https://example.com/b", "https://example.com/a")
        with self.assertRaisesRegex(ReadError, "troppi rimandi"):
            await self.reader.read("https://example.com/a")

    async def test_big_pages_are_cut(self):
        self.web.page("https://example.com/big", "<title>Grande</title><p>" + "parola " * (MAX_BYTES // 5))
        extract = await self.reader.read("https://example.com/big")
        self.assertEqual(extract.title, "Grande")

    async def test_no_address_inside_the_home_network(self):
        reader = Reader(transport=self.web.transport)  # con il controllo vero degli indirizzi
        self.addAsyncCleanup(reader.close)
        self.web.redirect("https://example.com/r", "http://192.168.1.1/admin")
        for url in ("http://127.0.0.1:8080/", "http://[::1]/", "http://10.0.0.5/x"):
            with self.subTest(url), self.assertRaisesRegex(ReadError, "non pubblico"):
                await reader.read(url)
        reader.allow = lambda host: public_address(host) if host != "example.com" else _same(host)
        with self.assertRaisesRegex(ReadError, "non pubblico"):
            await reader.read("https://example.com/r")  # nemmeno dopo un rimando
        self.assertEqual(self.web.requests, ["https://example.com/r"])
        reel = "https://www.instagram.com/reel/XYZ/"
        self.web.oembed("instagram", reel, {"title": "Torta", "thumbnail_url": "http://192.168.1.1/c.jpg"})
        reader.allow = lambda host: public_address(host) if host != "www.instagram.com" else _same(host)
        self.assertEqual((await reader.read(reel)).images, ())  # nemmeno la copertina
        self.assertNotIn("http://192.168.1.1/c.jpg", self.web.requests)

    async def test_the_connection_goes_to_the_address_that_was_checked(self):
        seen = []

        def answer(request):
            seen.append((str(request.url), request.headers["host"], request.extensions.get("sni_hostname")))
            return 200, b"<title>Fissato</title>", {"content-type": "text/html"}
        self.web.route("https://93.184.216.34/pagina?x=1", body=answer)
        self.web.route("http://[2606:2800:220:1::1]:8080/altra", body=answer)

        async def checked(host):  # il DNS, controllato una volta: la connessione non lo richiede
            return {"example.com": "93.184.216.34", "example.org": "2606:2800:220:1::1"}.get(host)
        self.reader.allow = checked
        self.assertEqual((await self.reader.read("https://example.com/pagina?x=1")).title, "Fissato")
        await self.reader.read("http://example.org:8080/altra")
        self.assertEqual(seen, [("https://93.184.216.34/pagina?x=1", "example.com", "example.com"),
                                ("http://[2606:2800:220:1::1]:8080/altra", "example.org:8080", None)])

    async def test_a_public_name_is_reached_at_its_ipv4_address_and_a_private_one_not_at_all(self):
        self.assertEqual(await public_address("8.8.8.8"), "8.8.8.8")
        self.assertIsNone(await public_address("192.168.1.1"))
        self.assertIsNone(await public_address("localhost"))  # risolto senza rete: 127.0.0.1 e ::1

    async def test_only_web_addresses(self):
        with self.assertRaisesRegex(ReadError, "non leggibile"):
            await self.reader.read("ftp://example.com/file")


async def _same(host):
    return host


if __name__ == "__main__":
    unittest.main()
