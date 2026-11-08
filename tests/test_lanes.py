"""Le corsie: 👍 subito, link e file custoditi, lettura dei link, il filo della conversazione,
risposte alle domande, /stato, tentativi, errori, ripresa dopo un arresto."""

import asyncio
import logging
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from sazed_agent import intake
from sazed_agent.claude import Claude, Interpretation
from sazed_agent.config import Reactions
from sazed_agent.coppermind import Coppermind, Draft
from sazed_agent.journal import Journal
from sazed_agent.lanes import Processor, lane_for
from sazed_agent.reader import Extract, Reader, ReadError
from sazed_agent.telegram import Telegram, TelegramError
from tests import fake_claude
from tests.fake_claude import interpretation, tool
from tests.fake_telegram import TOKEN, FakeTelegram, url_entities
from tests.fake_web import FakeWeb, allow_all

ARTICLE = ("<title>Torta di mele</title><article><p>Tre uova, zucchero, farina e mele: la torta della domenica, "
           "soffice e profumata di cannella.</p></article>")


def update(update_id, text=None, lang="it", edited=False, **fields):
    """Un messaggio di Ada; senza testo né altri campi, un link diverso per ogni update_id."""
    if text is None and not fields:
        text = f"https://example.com/ricetta/{update_id}"
    msg = {"message_id": update_id * 10, "chat": {"id": 42, "type": "private"},
           "from": {"id": 42, "first_name": "Ada", "language_code": lang}, **fields}
    if text is not None:
        msg.update(text=text, entities=url_entities(text))
    return {"update_id": update_id, "edited_message" if edited else "message": msg}


class FakeListener:
    """Un finto ascolto: annota i file che riceve (contenuto, se video, percorso) e ne dà una
    trascrizione fissa, con i fotogrammi indicati, dopo delay secondi."""

    def __init__(self, text="comprare il riso", frames=(), delay=0):
        self.text, self.frames, self.delay, self.heard = text, frames, delay, []

    async def listen(self, path, video=False):
        self.heard.append((Path(path).read_bytes(), video, Path(path)))
        await asyncio.sleep(self.delay)
        return Extract(None, None, self.text, True, self.frames)


class FakeVideos:
    """Una finta lettura dei video: aggiunge una trascrizione all'anteprima, o fallisce."""

    def __init__(self, text="Transcript:\nguanciale e uova", fail=None):
        self.text, self.fail, self.urls = text, fail, []

    async def read(self, url, preview):
        self.urls.append(url)
        if self.fail:
            raise ReadError(self.fail)
        return Extract(preview.title, preview.author, self.text, True, preview.images)


def text_has(fragment):
    return lambda method, params: method == "sendMessage" and fragment in params.get("text", "")


class LanesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.telegram = FakeTelegram().start()
        self.addCleanup(self.telegram.stop)
        self.journal = Journal(self.dir / "coppermind.db")
        self.addCleanup(self.journal.close)
        self.memory = Coppermind(self.journal.db)
        self.web = FakeWeb()
        self.stop = asyncio.Event()

    def processor(self, token=TOKEN, delays=(0.05, 0.1), claude=None, reads=False, read_wait=5.0, listener=None,
                  videos=None, listen_wait=5.0):
        reader = None
        if reads:
            reader = Reader(transport=self.web.transport, allow=allow_all)
            self.addAsyncCleanup(reader.close)
        return Processor(self.journal, Telegram(self.telegram.api_base, token, timeout=5), "Custode", "auto",
                         Reactions("👍", "😢", "🤷"), delays, typing_interval=0.05, idle_wait=5.0,
                         config_pause=30.0, memory=self.memory, claude=claude, reader=reader, read_wait=read_wait,
                         listener=listener, videos=videos, listen_wait=listen_wait)

    def with_claude(self, key=fake_claude.KEY, **options):
        """Un finto Claude, e il processore che lo usa."""
        self.claude = fake_claude.FakeClaude().start()
        self.addCleanup(self.claude.stop)
        return self.processor(claude=Claude(key, self.claude.api_base, "claude-haiku-5-5", timeout=5, max_retries=0),
                              **options)

    async def start(self, processor=None):
        processor = processor or self.processor()
        task = asyncio.create_task(processor.run(self.stop))

        async def finish():
            self.stop.set()
            await asyncio.wait_for(task, 10)
        self.addAsyncCleanup(finish)
        return processor

    def record(self, processor, upd):
        self.journal.record(upd["update_id"], upd, lane_for(upd))
        processor.notify()

    async def until(self, condition, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return
            await asyncio.sleep(0.02)
        self.fail("condizione non raggiunta in tempo")

    def state(self, update_id):
        return self.journal.get(update_id).state

    def replies(self):
        return [prm["text"] for _, prm in self.telegram.ok_calls("sendMessage")]

    def reactions(self, message_id=None):
        return [prm["reaction"][0]["emoji"] for _, prm in self.telegram.ok_calls("setMessageReaction")
                if message_id is None or prm["message_id"] == message_id]

    async def handle(self, *updates, processor=None):
        """Elabora i messaggi uno dopo l'altro e restituisce le risposte."""
        p = await self.start(processor)
        for upd in updates:
            self.record(p, upd)
            await self.until(lambda: self.state(upd["update_id"]) == "done")
        return self.replies()

    def items(self):
        return self.memory.search(limit=None).items[::-1]  # in ordine di arrivo

    # --- 👍 e nient'altro, per ciò che si custodisce ---

    async def test_a_link_gets_a_thumb_at_once_and_no_reply(self):
        self.assertEqual(await self.handle(update(1)), [])
        self.assertEqual([m for m, _ in self.telegram.ok_calls()], ["setMessageReaction"])
        self.assertEqual(self.reactions(10), ["👍"])
        [item] = self.memory.of_message(42, 10)
        self.assertEqual((item.url, item.platform, item.note, item.extraction),
                         ("https://example.com/ricetta/1", "web", None, "pending"))

    async def test_without_claude_words_go_with_the_link_or_are_a_note(self):
        replies = await self.handle(update(1, "https://example.com/pasta da provare"),
                                    update(2, "riso carnaroli\nda comprare"),
                                    update(3, voice={"file_id": "v", "file_unique_id": "V", "duration": 3}))
        self.assertEqual(replies, [])
        link, note, voice = self.items()
        self.assertEqual((link.source, link.note), ("link", "da provare"))
        self.assertEqual((note.source, note.note, note.extraction), ("note", "riso carnaroli\nda comprare", "complete"))
        self.assertEqual(voice.source, "voice")
        self.assertEqual(self.reactions(), ["👍", "👍", "👍"])

    async def test_a_link_sent_again_stays_one_item_and_keeps_the_new_words(self):
        await self.handle(update(1, "https://www.youtube.com/watch?v=abc"), update(2, "https://youtu.be/abc meno sale"))
        [item] = self.items()
        self.assertEqual((item.message_id, item.note), (10, "meno sale"))

    async def test_a_photo_with_a_link_in_the_caption(self):
        caption = "tramonto https://example.com/scilla"
        await self.handle(update(1, photo=[{"file_id": "p", "file_unique_id": "P"}], caption=caption,
                                 caption_entities=url_entities(caption)))
        self.assertEqual([(i.source, i.note) for i in self.memory.of_message(42, 10)],
                         [("photo", "tramonto"), ("link", "tramonto")])

    async def test_an_edited_message_updates_its_item_without_a_reply(self):
        replies = await self.handle(update(1, "riso carnaroli"), update(2, "riso venere", edited=True, message_id=10))
        self.assertEqual(replies, [])
        self.assertEqual([i.note for i in self.items()], ["riso venere"])

    async def test_unknown_commands_and_stickers_are_answered_but_not_saved(self):
        replies = await self.handle(update(1, "/aiuto"), update(2, sticker={"file_id": "s", "file_unique_id": "S"}))
        self.assertEqual(replies, ["Ciao Ada, non conosco il comando /aiuto.",
                                   "Ciao Ada, questo tipo di messaggio non so ancora custodirlo."])
        self.assertEqual(self.memory.count(), 0)
        self.assertEqual(self.telegram.ok_calls("sendMessage")[0][1]["reply_parameters"]["message_id"], 10)

    # --- la lettura ---

    async def test_a_short_link_is_kept_at_its_true_address(self):
        self.web.redirect("https://share.google/abc", "https://www.google.com/share.google?q=abc")
        self.web.redirect("https://www.google.com/share.google?q=abc", "https://example.com/torta")
        self.web.page("https://example.com/torta", ARTICLE)
        p = self.processor(reads=True)
        await self.handle(update(1, "https://share.google/abc"), update(2, "https://example.com/torta"), processor=p)
        [item] = self.items()
        self.assertEqual((item.url, item.message_id), ("https://example.com/torta", 10))
        self.assertEqual(self.journal.get(1).links, {"https://share.google/abc": "https://example.com/torta"})

    async def test_a_short_link_left_as_it_was_is_moved_when_read(self):
        self.web.redirect("https://share.google/abc", "https://example.com/torta")
        self.web.page("https://example.com/torta", ARTICLE)
        # salvato prima che l'ingresso risolvesse i link brevi
        [out] = self.memory.save(42, 5, 1, [Draft("link", url="https://share.google/abc")])
        await self.start(self.processor(reads=True))
        await self.until(lambda: self.memory.get(out.item.id).extraction == "complete")
        self.assertEqual(self.memory.get(out.item.id).url, "https://example.com/torta")

    async def test_a_link_is_read_and_described_from_its_content(self):
        self.web.page("https://example.com/torta", ARTICLE)
        p = self.with_claude(reads=True)
        self.claude.on("describe", lambda m: fake_claude.description("recipe", "Torta di mele", ["mele", "uova"],
                                                                     "Una torta soffice."))
        await self.handle(update(1, "https://example.com/torta"), processor=p)
        await self.until(lambda: self.items()[0].extraction == "complete")
        item = self.items()[0]
        self.assertEqual((item.kind, item.title, item.topics, item.summary),
                         ("recipe", "Torta di mele", ["mele", "uova"], "Una torta soffice."))
        self.assertIn("profumata di cannella", item.content)
        self.assertEqual(self.claude.messages("describe")[0]["language"], "it")
        self.assertEqual(self.claude.of("interpret"), [])  # un link senza parole non chiede nulla
        self.assertEqual((self.replies(), self.reactions()), ([], ["👍"]))

    async def test_a_reel_without_caption_is_described_from_its_cover(self):
        reel, cover = "https://www.instagram.com/reel/XYZ/", "https://cdn.example.com/cover.jpg"
        self.web.oembed("instagram", reel, {"title": "", "author_name": "pasticcere", "thumbnail_url": cover})
        self.web.route(cover, 200, b"\xff\xd8jpeg", {"content-type": "image/jpeg"})
        p = self.with_claude(reads=True)
        self.claude.on("describe", lambda m: fake_claude.description("recipe", "Torta di mele definitiva", ["mele"]))
        await self.handle(update(1, reel), processor=p)
        await self.until(lambda: self.items()[0].extraction == "partial")
        item = self.items()[0]
        self.assertEqual((item.kind, item.title, item.content), ("recipe", "Torta di mele definitiva", None))
        self.assertEqual(len(fake_claude.images(self.claude.of("describe")[0])), 1)
        self.assertEqual(self.reactions(), ["👍"])

    async def test_a_youtube_video_adds_what_yt_dlp_reads_to_its_preview(self):
        url = "https://www.youtube.com/watch?v=abc"
        self.web.oembed("youtube", url, {"title": "Carbonara perfetta", "author_name": "Chef"})
        self.web.page("https://example.com/torta", ARTICLE)
        videos = FakeVideos()
        await self.handle(update(1, url), update(2, "https://example.com/torta"),
                          processor=self.processor(reads=True, videos=videos))
        await self.until(lambda: all(i.extraction == "complete" for i in self.items()))
        video = self.items()[0]
        self.assertEqual((video.title, video.content), ("Carbonara perfetta", "Transcript:\nguanciale e uova"))
        self.assertEqual(videos.urls, [url])  # solo i video, non le pagine

    async def test_when_yt_dlp_fails_the_preview_stays(self):
        url = "https://www.youtube.com/watch?v=abc"
        self.web.oembed("youtube", url, {"title": "Carbonara perfetta", "author_name": "Chef"})
        with self.assertLogs("sazed_agent.lanes", "WARNING") as logs:
            await self.handle(update(1, url), processor=self.processor(reads=True, videos=FakeVideos(fail="bloccato")))
            await self.until(lambda: self.items()[0].extraction == "partial")
        self.assertEqual((self.items()[0].title, self.reactions()), ("Carbonara perfetta", ["👍"]))
        self.assertIn("del video resta l'anteprima (bloccato)", "\n".join(logs.output))
        self.assertNotIn("abc", "\n".join(logs.output))

    async def test_a_voice_message_is_heard_and_described(self):
        self.telegram.file("v", b"OggS voce")
        listener = FakeListener("Comprare il riso e il parmigiano.")
        p = self.with_claude(listener=listener)
        self.claude.on("describe", lambda m: fake_claude.description("note", "Spesa", ["riso", "parmigiano"]))
        self.claude.on("interpret", lambda m: interpretation(note=m["current"]["spoken"]))
        await self.handle(update(1, voice={"file_id": "v", "file_unique_id": "V", "duration": 3}), processor=p)
        [item] = self.items()  # il vocale stesso è la nota: nessuna copia delle sue parole
        self.assertEqual((item.kind, item.title, item.content), ("note", "Spesa", "Comprare il riso e il parmigiano."))
        [(data, video, path)] = listener.heard
        self.assertEqual((data, video, path.exists()), (b"OggS voce", False, False))  # la cartella non c'è più
        [message] = self.claude.messages("describe")
        self.assertEqual((message["source"], message["content"]), ("voice", "Comprare il riso e il parmigiano."))
        [message] = self.claude.messages("interpret")  # le parole dette, appena trascritte
        self.assertEqual((message["current"]["words"], message["current"]["spoken"]),
                         (None, "Comprare il riso e il parmigiano."))
        self.assertEqual((self.reactions(), self.replies()), (["👍"], []))

    async def test_a_question_asked_in_a_voice_message_is_answered_and_not_kept(self):
        self.telegram.file("v", b"OggS domanda")
        p = self.with_claude(listener=FakeListener("Che ricette di carbonara ho?"))
        self.claude.on("interpret", lambda m: interpretation(
            question=m["current"].get("spoken") or m["current"]["words"], keep_spoken=False))
        self.claude.on("answer", lambda messages: "ne hai una.")
        replies = await self.handle(update(1, voice={"file_id": "v", "file_unique_id": "V", "duration": 2}),
                                    update(2, "e con la panna?"), processor=p)
        self.assertEqual(replies, ["Ciao Ada, ne hai una."] * 2)
        first, second = self.claude.messages("answer")
        self.assertEqual((first["question"], first["spoken"]), ("Che ricette di carbonara ho?", True))
        self.assertEqual(second["conversation"], [{"message": 10, "minutes_ago": 0,  # il filo ricorda le parole
                                                   "spoken": "Che ricette di carbonara ho?", "sazed": "ne hai una."}])
        self.assertEqual(self.memory.count(), 0)  # una domanda non si custodisce, detta o scritta
        self.assertEqual(self.reactions(10), ["👍"])

    async def test_a_comment_spoken_right_after_a_link_joins_it(self):
        self.telegram.file("v", b"OggS commento")
        p = self.with_claude(listener=FakeListener("Questa la facciamo sabato."))
        self.claude.on("interpret", lambda m: interpretation(
            comment=m["current"]["spoken"], comment_on=[m["conversation"][-1]["items"][0]["id"]], keep_spoken=False))
        replies = await self.handle(update(1, "https://example.com/torta"),
                                    update(2, voice={"file_id": "v", "file_unique_id": "V"}), processor=p)
        [link] = self.items()
        self.assertEqual((replies, link.url, link.note), ([], "https://example.com/torta", "Questa la facciamo sabato."))

    async def test_a_forwarded_voice_message_is_content_not_words(self):
        self.telegram.file("v", b"OggS altrui")
        p = self.with_claude(listener=FakeListener("Mi mandi la ricetta?"))
        origin = {"type": "hidden_user", "date": 1_790_000_000, "sender_user_name": "Bea"}
        await self.handle(update(1, voice={"file_id": "v", "file_unique_id": "V"}, forward_origin=origin), processor=p)
        await self.until(lambda: self.items()[0].extraction == "complete")
        self.assertEqual((self.replies(), self.claude.of("interpret")), ([], []))

    async def test_the_words_of_a_voice_message_do_not_wait_forever(self):
        self.telegram.file("v", b"OggS lungo")
        p = self.with_claude(listener=FakeListener(delay=1.0), listen_wait=0.2)
        await self.handle(update(1, voice={"file_id": "v", "file_unique_id": "V"}), processor=p)
        self.assertEqual(self.items()[0].extraction, "pending")  # ancora in ascolto
        await self.until(lambda: self.items()[0].extraction == "complete", timeout=3)
        self.assertEqual(self.claude.of("interpret"), [])  # resta un vocale, come una nota

    async def test_a_voice_message_waiting_for_its_words_is_listened_to_first(self):
        p = self.with_claude(reads=True, listener=FakeListener())
        self.memory.save(42, 10, 0, [Draft("link", url="https://example.com/prima")])
        voice = update(2, voice={"file_id": "v", "file_unique_id": "V"})
        self.journal.record(2, voice, "acquisition")
        self.journal.advance(2, "read")
        self.memory.save(42, 20, 0, [Draft("voice", media={"file_id": "v", "file_unique_id": "V"})])
        self.assertEqual(self.memory.to_read(p.readable, first=p._awaited()).source, "voice")

    async def test_a_video_is_heard_and_seen(self):
        self.telegram.file("m", b"mp4")
        listener = FakeListener("tre uova e farina", frames=(("image/jpeg", b"\xff\xd8a"), ("image/jpeg", b"\xff\xd8b")))
        p = self.with_claude(listener=listener)
        await self.handle(update(1, video={"file_id": "m", "file_unique_id": "M", "duration": 30}), processor=p)
        await self.until(lambda: self.items()[0].extraction == "complete")
        self.assertTrue(listener.heard[0][1])
        self.assertEqual(len(fake_claude.images(self.claude.of("describe")[0])), 2)

    async def test_a_photo_is_looked_at_by_claude(self):
        self.telegram.file("p", b"\xff\xd8foto")
        p = self.with_claude()
        self.claude.on("describe", lambda m: fake_claude.description("photo", "Tramonto sul mare", ["mare"]))
        await self.handle(update(1, photo=[{"file_id": "piccola", "file_unique_id": "p1"},
                                           {"file_id": "p", "file_unique_id": "P"}]), processor=p)
        await self.until(lambda: self.items()[0].extraction == "complete")
        self.assertEqual(self.items()[0].title, "Tramonto sul mare")
        self.assertEqual(self.telegram.downloads, ["p"])  # la più grande
        [image] = fake_claude.images(self.claude.of("describe")[0])
        self.assertEqual(image["source"]["data"], "/9hmb3Rv")

    async def test_a_file_too_big_or_gone_gets_a_shrug(self):
        p = self.with_claude(listener=FakeListener())
        await self.handle(update(1, video={"file_id": "grande", "file_unique_id": "G", "file_size": 25_000_000}),
                          update(2, voice={"file_id": "sparito", "file_unique_id": "S"}), processor=p)
        await self.until(lambda: self.reactions(10) == ["👍", "🤷"] and self.reactions(20) == ["👍", "🤷"])
        big, gone = self.items()
        self.assertEqual((big.extraction, gone.extraction), ("failed", "failed"))
        self.assertIn("20 MB", big.read_error)
        self.assertEqual(self.telegram.downloads, [])
        self.assertEqual([prm["file_id"] for m, prm, _ in self.telegram.calls if m == "getFile"], ["sparito"])

    async def test_without_whisper_recordings_wait_and_photos_need_claude(self):
        p = await self.start(self.processor(reads=True))
        self.assertEqual(p.readable, ("link",))
        self.assertEqual(self.with_claude(reads=True, listener=FakeListener()).readable,
                         ("link", "photo", "voice", "audio", "video", "video_note"))

    async def test_an_unreadable_link_gets_a_shrug(self):
        self.web.route("https://example.com/via", 410)
        p = self.processor(reads=True)
        await self.handle(update(1, "https://example.com/via"), processor=p)
        await self.until(lambda: self.reactions(10) == ["👍", "🤷"])
        item = self.items()[0]
        self.assertEqual((item.extraction, item.read_error), ("unavailable", "la fonte non c'è più (HTTP 410)"))
        self.assertEqual(self.replies(), [])

    async def test_a_reading_that_keeps_failing_is_retried_then_given_up(self):
        self.web.route("https://example.com/giu", 503)
        p = self.processor(reads=True)
        await self.handle(update(1, "https://example.com/giu"), processor=p)
        await self.until(lambda: self.reactions(10) == ["👍", "🤷"])
        self.assertEqual(self.items()[0].extraction, "failed")
        self.assertEqual(self.web.requests.count("https://example.com/giu"), 3)  # due attese, tre tentativi

    # --- il filo della conversazione ---

    async def test_a_comment_right_after_a_link_joins_it(self):
        p = self.with_claude()
        self.claude.on("interpret", lambda m: interpretation(
            comment=m["current"]["words"], comment_on=[m["conversation"][-1]["items"][0]["id"]]))
        replies = await self.handle(update(1, "https://example.com/torta"), update(2, "da fare domenica"), processor=p)
        self.assertEqual(replies, [])
        [link] = self.items()
        self.assertEqual(link.note, "da fare domenica")
        [message] = self.claude.messages("interpret")
        [earlier] = message["conversation"]
        self.assertEqual((earlier["message"], earlier["items"][0]["url"], message["current"]["items"]),
                         (10, "https://example.com/torta", []))

    async def test_a_link_with_a_question_is_answered_after_it_is_read(self):
        self.web.page("https://example.com/torta", ARTICLE)
        self.web.slow("https://example.com/torta", 0.3)
        p = self.with_claude(reads=True)
        self.claude.on("interpret", lambda m: interpretation(
            question="quante uova servono?", about=[m["current"]["items"][0]["id"]]))
        self.claude.on("answer", lambda messages: "tre uova.")
        replies = await self.handle(update(1, "https://example.com/torta quante uova servono?"), processor=p)
        self.assertEqual(replies, ["Ciao Ada, tre uova."])
        [question] = self.claude.messages("answer")
        [about] = question["items"]
        self.assertEqual((about["reading"], question["question"]), ("complete", "quante uova servono?"))
        self.assertIn("Tre uova", about["content"])
        self.assertIsNone(self.items()[0].note)  # una domanda non è una nota
        self.assertEqual(self.journal.get(1).reply_text, "tre uova.")
        self.assertEqual(self.reactions(10), ["👍"])
        self.assertTrue(self.telegram.ok_calls("sendChatAction"))

    async def test_a_question_right_after_a_link_is_about_it(self):
        self.web.page("https://example.com/torta", ARTICLE)
        self.web.slow("https://example.com/torta", 0.3)
        p = self.with_claude(reads=True)
        self.claude.on("interpret", lambda m: interpretation(
            question="di cosa parla?", about=[m["conversation"][-1]["items"][0]["id"]]))
        self.claude.on("answer", lambda messages: "di una torta di mele.")
        await self.handle(update(1, "https://example.com/torta"), update(2, "di cosa parla?"), processor=p)
        self.assertEqual(self.replies(), ["Ciao Ada, di una torta di mele."])
        self.assertEqual(self.claude.messages("answer")[0]["items"][0]["reading"], "complete")
        self.assertEqual(self.telegram.ok_calls("sendMessage")[0][1]["reply_parameters"]["message_id"], 20)

    async def test_the_answer_comes_back_in_the_thread(self):
        p = self.with_claude()
        self.claude.on("interpret", lambda m: interpretation(question=m["current"]["words"]))
        self.claude.on("answer", lambda messages: "ne hai due.")
        await self.handle(update(1, "quante ricette ho?"), update(2, "e di dolci?"), processor=p)
        conversation = self.claude.messages("answer")[1]["conversation"]
        self.assertEqual(conversation, [{"message": 10, "minutes_ago": 0, "words": "quante ricette ho?",
                                         "sazed": "ne hai due."}])

    async def test_a_reply_to_an_old_message_brings_it_into_the_thread(self):
        p = await self.start(self.with_claude())
        self.record(p, update(1, "https://example.com/vecchio"))
        await self.until(lambda: self.state(1) == "done")
        self.journal.db.execute("UPDATE inbox SET received_at = received_at - 86400 WHERE update_id = 1")
        self.claude.on("interpret", lambda m: interpretation(question="è buona?"))
        for upd in (update(2, "è buona?", reply_to_message={"message_id": 10}), update(3, "e questa?")):
            self.record(p, upd)
            await self.until(lambda: self.state(upd["update_id"]) == "done")
        second, third = self.claude.messages("interpret")
        self.assertEqual(([m["message"] for m in second["conversation"]], second["current"]["reply_to"]), ([10], 10))
        self.assertEqual([m["message"] for m in third["conversation"]], [20])  # un giorno fa: fuori dal filo

    async def test_a_question_does_not_wait_forever_for_a_reading(self):
        self.web.route("https://example.com/giu", 503)
        p = self.with_claude(reads=True, delays=(30,), read_wait=0.3)
        self.claude.on("interpret", lambda m: interpretation(question="di cosa parla?",
                                                             about=[m["current"]["items"][0]["id"]]))
        await self.handle(update(1, "https://example.com/giu di cosa parla?"), processor=p)
        self.assertEqual(self.claude.messages("answer")[0]["items"][0]["reading"], "pending")

    async def test_questions_do_not_wait_behind_a_slow_reading(self):
        self.web.page("https://example.com/lenta", ARTICLE)
        self.web.slow("https://example.com/lenta", 2)
        p = await self.start(self.with_claude(reads=True))
        self.claude.on("interpret", lambda m: interpretation(question=m["current"]["words"]))
        self.record(p, update(1, "https://example.com/lenta"))
        await self.until(lambda: self.state(1) == "done")
        self.record(p, update(2, "quante ricette ho?"))
        self.record(p, update(3, "/stato"))
        await self.until(lambda: self.state(2) == "done" and self.state(3) == "done", timeout=1.5)
        self.assertEqual(self.items()[0].extraction, "pending")  # la lettura è ancora in corso
        self.assertEqual(len(self.replies()), 2)  # lo stato e la risposta, mentre la lettura continua

    async def test_a_question_about_a_voice_message_is_answered_at_once(self):
        p = self.with_claude(reads=True)
        self.claude.on("interpret", lambda m: interpretation(question="cosa dice?",
                                                             about=[m["current"]["items"][0]["id"]]))
        await self.handle(update(1, voice={"file_id": "v", "file_unique_id": "V"}, caption="cosa dice?"),
                          processor=p)
        self.assertEqual(self.replies(), ["Ciao Ada, ecco la risposta."])

    async def test_without_words_claude_is_not_asked(self):
        p = self.with_claude()
        await self.handle(update(1, photo=[{"file_id": "p", "file_unique_id": "P"}]), update(2, "/aiuto"),
                          update(3, sticker={"file_id": "s", "file_unique_id": "S"}), processor=p)
        self.assertEqual(self.claude.requests, [])
        self.assertEqual(self.memory.count(), 1)

    async def test_claude_describes_a_note(self):
        p = self.with_claude()
        self.claude.on("interpret", lambda m: interpretation(note=m["current"]["words"], kind="note",
                                                             title="Spesa di sabato", topics=["Spesa", "riso"]))
        await self.handle(update(1, "riso carnaroli\nparmigiano"), processor=p)
        [item] = self.items()
        self.assertEqual((item.note, item.kind, item.title, item.topics),
                         ("riso carnaroli\nparmigiano", "note", "Spesa di sabato", ["spesa", "riso"]))

    async def test_just_saying_thanks_keeps_nothing(self):
        p = self.with_claude()
        self.claude.on("interpret", lambda m: interpretation())
        self.assertEqual(await self.handle(update(1, "grazie!"), processor=p), [])
        self.assertEqual((self.memory.count(), self.reactions()), (0, ["👍"]))

    # --- Claude che manca o sbaglia ---

    async def test_claude_down_is_retried(self):
        p = self.with_claude()
        self.claude.fail(500)
        await self.handle(update(1, "riso"), processor=p)
        self.assertEqual((self.journal.get(1).attempts, len(self.claude.requests), self.memory.count()), (1, 2, 1))

    async def test_a_rejected_claude_key_waits_without_using_attempts_and_the_link_is_already_safe(self):
        p = await self.start(self.with_claude(key="sk-ant-api03-SBAGLIATA"))
        self.record(p, update(1, "https://example.com/torta buona"))
        await self.until(lambda: self.journal.get(1).last_error is not None)
        entry = self.journal.get(1)
        self.assertEqual((entry.state, entry.attempts), ("read", 0))
        self.assertEqual([i.url for i in self.items()], ["https://example.com/torta"])
        self.assertIn("chiave dell'API di Claude rifiutata", entry.last_error)
        self.assertNotIn("SBAGLIATA", entry.last_error)

    async def test_no_answer_from_claude_says_so(self):
        p = self.with_claude()
        self.claude.on("interpret", lambda m: interpretation(question="?"))
        self.claude.on("answer", lambda messages: tool("search_items", {}))
        with self.assertLogs("sazed_agent.claude", "WARNING"):
            replies = await self.handle(update(1, "boh?"), processor=p)
        self.assertEqual(replies, ["Ciao Ada, non sono riuscito a rispondere a questa domanda: riprova tra poco."])

    # --- ripresa dopo un arresto ---

    async def test_the_interpretation_is_asked_once_even_after_a_crash(self):
        upd = update(1, "riso e zucca")
        self.journal.record(1, upd, "acquisition")
        self.journal.advance(1, "read")
        self.journal.set_interpretation(1, Interpretation(note="riso e zucca").to_dict())
        await self.start(self.with_claude())  # arresto dopo l'interpretazione, prima del salvataggio
        await self.until(lambda: self.state(1) == "done")
        self.assertEqual(self.claude.requests, [])
        self.assertEqual(self.items()[0].note, "riso e zucca")

    async def test_resume_after_the_first_save_saves_nothing_twice(self):
        upd = update(1)
        self.journal.record(1, upd, "acquisition")
        with self.journal.transaction():  # arresto subito dopo aver custodito il link
            self.memory.save(42, 10, 1, intake.read(upd).drafts)
            self.journal.set_links(1, {})
            self.journal.advance(1, "read")
        await self.start()
        await self.until(lambda: self.state(1) == "done")
        self.assertEqual((self.memory.count(), self.reactions()), (1, []))

    async def test_resume_after_saving_a_note_sends_nothing(self):
        self.journal.record(1, update(1, "riso"), "acquisition")
        with self.journal.transaction():  # arresto fra il salvataggio e la fine
            self.memory.save(42, 10, 1, [Draft("note", note="riso")])
            self.journal.set_interpretation(1, Interpretation(note="riso").to_dict())
            self.journal.advance(1, "saved")
        await self.start()
        await self.until(lambda: self.state(1) == "done")
        self.assertEqual((self.replies(), self.memory.count()), ([], 1))

    async def test_resume_after_a_crash_never_resends_a_recorded_reply(self):
        # Arresto dopo l'invio della risposta e la sua annotazione, prima del cambio di stato.
        self.journal.record(1, update(1, "/aiuto"), "acquisition")
        self.journal.advance(1, "saved")
        self.journal.set_reply(1, 555, "non conosco il comando /aiuto.")
        self.journal.record(2, update(2, "/aiuto"), "acquisition")
        self.journal.advance(2, "answered", reply_message_id=556)
        await self.start()
        await self.until(lambda: self.state(1) == "done" and self.state(2) == "done")
        self.assertEqual(self.telegram.ok_calls("sendMessage"), [])
        self.assertEqual(self.journal.get(1).reply_message_id, 555)

    # --- comandi, tentativi, errori ---

    async def test_status_counts_what_is_kept(self):
        p = await self.start()
        self.record(p, update(1))
        await self.until(lambda: self.state(1) == "done")
        self.record(p, update(2, "/stato", lang="en"))
        await self.until(lambda: self.state(2) == "done")
        self.assertEqual(self.replies(), ["Hi Ada, the Custode agent is connected. Messages to process: 0, "
                                          "failed: 0. Items kept: 1."])

    async def test_transient_error_is_retried(self):
        self.telegram.fail("sendMessage", 500)
        await self.handle(update(1, "/aiuto"))
        self.assertEqual(self.journal.get(1).attempts, 1)
        self.assertEqual(len(self.telegram.ok_calls("sendMessage")), 1)

    async def test_retries_exhausted_the_item_fails_is_reported_and_others_go_on(self):
        self.telegram.rule(lambda m, prm: 500 if text_has("non conosco")(m, prm)
                           and prm["reply_parameters"]["message_id"] == 10 else None)
        p = await self.start()
        self.record(p, update(1, "/aiuto"))
        self.record(p, update(2, "/aiuto"))
        await self.until(lambda: self.state(2) == "done" and len(self.reactions(10)) == 2)  # avviso, poi 😢
        self.assertEqual((self.state(1), self.journal.get(1).attempts), ("failed", 3))  # due attese configurate, tre tentativi
        notices = [text for text in self.replies() if "riuscito" in text]
        self.assertEqual(notices, ["Ciao Ada, non sono riuscito a elaborare questo messaggio dopo 3 tentativi: "
                                   "lo tengo da parte."])
        self.assertEqual(self.reactions(10), ["👍", "😢"])

    async def test_permanent_error_fails_at_once(self):
        self.telegram.rule(lambda m, prm: 400 if text_has("non conosco")(m, prm) else None)
        p = await self.start()
        self.record(p, update(1, "/aiuto"))
        await self.until(lambda: self.state(1) == "failed")
        entry = self.journal.get(1)
        self.assertEqual(entry.attempts, 1)
        self.assertIn("errore finto 400", entry.last_error)
        self.assertIn("dopo 1 tentativo:", self.replies()[0])

    async def test_rate_limit_waits_as_long_as_telegram_asks(self):
        self.telegram.fail("sendMessage", 429)
        p = await self.start(self.processor(delays=(0.05,)))
        self.record(p, update(1, "/aiuto"))
        await self.until(lambda: self.journal.get(1).attempts == 1)
        self.assertGreater(self.journal.get(1).next_attempt_at - time.time(), 0.8)  # retry_after = 1
        await self.until(lambda: self.state(1) == "done", timeout=4)

    async def test_refused_reaction_does_not_block_the_message(self):
        self.telegram.rule(lambda m, prm: 400 if m == "setMessageReaction" else None)
        await self.handle(update(1))
        self.assertEqual(self.memory.count(), 1)

    async def test_rejected_token_postpones_without_using_attempts(self):
        p = await self.start(self.processor(token="999:SBAGLIATO"))
        self.record(p, update(1, "/aiuto"))
        await self.until(lambda: self.journal.get(1).last_error is not None)
        entry = self.journal.get(1)
        self.assertEqual((entry.state, entry.attempts), ("saved", 0))  # manca solo la risposta
        self.assertIn("token del bot rifiutato", entry.last_error)
        self.assertGreater(entry.next_attempt_at - time.time(), 25)

    async def test_wakes_up_at_once_when_notified(self):
        p = await self.start()  # idle_wait è 5 s: senza l'avviso aspetterebbe
        await asyncio.sleep(0.2)
        started = time.monotonic()
        self.record(p, update(1))
        await self.until(lambda: self.state(1) == "done")
        self.assertLess(time.monotonic() - started, 2)

    async def test_each_completed_item_is_logged_without_its_text(self):
        with self.assertLogs("sazed_agent", level="INFO") as logs:
            p = await self.start()
            self.record(p, update(1, text="la ricetta segreta della nonna"))
            self.record(p, update(2, text="/aiuto"))
            self.record(p, update(3, text="/stato"))
            await self.until(lambda: all(self.state(n) == "done" for n in (1, 2, 3)))
        joined = "\n".join(logs.output)
        self.assertIn("elemento 1 completato, senza bisogno di risposta", joined)
        self.assertIn("elemento 2 completato: risposta inviata", joined)
        self.assertIn("elemento 3 completato: stato inviato", joined)
        self.assertNotIn("ricetta segreta", joined)  # il contenuto dei messaggi non va nei log

    async def test_token_never_appears_in_logs(self):
        self.telegram.fail("sendMessage", 500, 400)
        self.telegram.fail("setMessageReaction", 400)
        with self.assertLogs("sazed_agent", level="INFO") as logs:
            p = await self.start(self.processor(delays=(0.05,)))
            self.record(p, update(1, "/aiuto"))
            await self.until(lambda: self.state(1) == "failed")
            await asyncio.sleep(0.2)
        joined = "\n".join(logs.output)
        self.assertIn("errore finto 500", joined)  # gli errori ci sono, il token no
        self.assertNotIn(TOKEN, joined)
        self.assertNotIn("TEST-TOKEN", joined)


class TelegramErrors(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeTelegram().start()
        self.addCleanup(self.fake.stop)
        self.telegram = Telegram(self.fake.api_base, TOKEN, timeout=5)

    async def error(self, status):
        self.fake.fail("sendMessage", status)
        with self.assertRaises(TelegramError) as ctx:
            await self.telegram.send_message(42, "x")
        return ctx.exception

    async def test_classification(self):
        self.assertTrue((await self.error(400)).permanent)
        self.assertTrue((await self.error(403)).permanent)
        err = await self.error(429)
        self.assertEqual((err.permanent, err.retry_after), (False, 1))
        err = await self.error(500)
        self.assertFalse(err.permanent or err.config_error)

    async def test_rejected_token_is_a_configuration_error(self):
        telegram = Telegram(self.fake.api_base, "1:SBAGLIATO", timeout=5)
        with self.assertRaises(TelegramError) as ctx:
            await telegram.send_message(42, "x")
        self.assertTrue(ctx.exception.config_error)
        self.assertNotIn("SBAGLIATO", str(ctx.exception))

    async def test_unreachable_telegram_is_transient_and_hides_the_token(self):
        telegram = Telegram("http://127.0.0.1:9", TOKEN, timeout=2)  # porta chiusa
        with self.assertRaises(TelegramError) as ctx:
            await telegram.send_message(42, "x")
        self.assertFalse(ctx.exception.permanent or ctx.exception.config_error)
        self.assertNotIn(TOKEN, str(ctx.exception))

    async def test_files_are_downloaded_within_their_limit(self):
        self.fake.file("v", b"OggS" * 100)
        destination = Path(tempfile.mkdtemp()) / "file"
        self.addCleanup(shutil.rmtree, destination.parent)
        await self.telegram.download(await self.telegram.get_file("v"), destination)
        self.assertEqual(destination.read_bytes(), b"OggS" * 100)
        with self.assertRaisesRegex(TelegramError, "oltre 100 byte") as ctx:
            await self.telegram.download("files/v", destination, limit=100)
        self.assertTrue(ctx.exception.permanent)
        with self.assertRaises(TelegramError) as ctx:
            await Telegram(self.fake.api_base, "1:SBAGLIATO", timeout=5).download("files/v", destination)
        self.assertTrue(ctx.exception.config_error)
        self.assertNotIn("SBAGLIATO", str(ctx.exception))
        with self.assertRaises(TelegramError) as ctx:  # un percorso scaduto: si chiede di nuovo a getFile
            await self.telegram.download("files/scaduto", destination)
        self.assertFalse(ctx.exception.permanent or ctx.exception.config_error)

    async def test_message_id_is_returned(self):
        self.assertEqual(await self.telegram.send_message(42, "x", reply_to=7), 9000)
        _, params = self.fake.ok_calls("sendMessage")[0]
        self.assertEqual(params["reply_parameters"], {"message_id": 7, "allow_sending_without_reply": True})


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
