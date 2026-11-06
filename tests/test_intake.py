"""Lettura dei messaggi Telegram: parole, link dalle entità (in UTF-16), file, comandi, modifiche,
risposte a un messaggio di prima."""

import unittest

from sazed_agent.coppermind import Draft
from sazed_agent.intake import read
from tests.fake_telegram import url_entities


def update(edited=False, **fields):
    msg = {"message_id": 10, "date": 1_790_000_000, "chat": {"id": 42, "type": "private"},
           "from": {"id": 42, "first_name": "Ada"}, **fields}
    return {"update_id": 1, "edited_message" if edited else "message": msg}


def text(value, **fields):
    return update(text=value, entities=url_entities(value), **fields)


class Texts(unittest.TestCase):
    def test_a_text_without_links_is_only_words_as_written(self):
        reading = read(text("riso carnaroli\n  da comprare "))
        self.assertEqual((reading.drafts, reading.words), ((), "riso carnaroli\n  da comprare"))
        self.assertEqual((reading.command, reading.sent_at, reading.edited, reading.reply_to),
                         (None, 1_790_000_000, False, None))

    def test_a_reply_says_to_which_message(self):
        reading = read(text("e questa?", reply_to_message={"message_id": 7, "text": "https://example.com"}))
        self.assertEqual(reading.reply_to, 7)

    def test_a_link_alone_has_no_note(self):
        self.assertEqual(read(text("https://example.com/r")).drafts, (Draft("link", url="https://example.com/r"),))

    def test_the_words_around_a_link_stay_apart_even_after_an_emoji(self):
        reading = read(text("🍝 da provare https://example.com/pasta stasera"))
        self.assertEqual((reading.drafts, reading.words),
                         ((Draft("link", url="https://example.com/pasta"),), "🍝 da provare stasera"))

    def test_every_link_is_an_item(self):
        reading = read(text("due versioni: https://a.example/1\nhttps://b.example/2 quale è meglio?"))
        self.assertEqual(reading.drafts, (Draft("link", url="https://a.example/1"), Draft("link", url="https://b.example/2")))
        self.assertEqual(reading.words, "due versioni:\nquale è meglio?")

    def test_a_link_hidden_behind_words_keeps_the_words(self):
        value = "guarda questa ricetta"
        reading = read(update(text=value, entities=[
            {"type": "bold", "offset": 0, "length": 6},
            {"type": "text_link", "offset": 7, "length": 14, "url": "https://example.com/r"}]))
        self.assertEqual((reading.drafts, reading.words), ((Draft("link", url="https://example.com/r"),), value))

    def test_a_bare_domain_is_a_link_too(self):
        reading = read(update(text="example.org", entities=[{"type": "url", "offset": 0, "length": 11}]))
        self.assertEqual(reading.drafts, (Draft("link", url="example.org"),))

    def test_an_unknown_command_is_not_saved(self):
        for value in ("/aiuto", "/aiuto@QualcheBot", "/aiuto con le ricette"):
            reading = read(text(value))
            self.assertEqual((reading.drafts, reading.words, reading.command), ((), None, "/aiuto"), value)
        self.assertEqual(read(text("/ non è un comando")).words, "/ non è un comando")

    def test_an_edit_is_marked(self):
        reading = read(text("corretto", edited=True))
        self.assertEqual((reading.words, reading.edited), ("corretto", True))


class Files(unittest.TestCase):
    def test_a_photo_points_to_its_largest_size_and_its_caption_links(self):
        caption = "tramonto, come qui https://example.com/scilla"
        reading = read(update(photo=[{"file_id": "s", "file_unique_id": "S", "width": 90},
                                     {"file_id": "b", "file_unique_id": "B", "width": 1280, "file_size": 2048}],
                              caption=caption, caption_entities=url_entities(caption)))
        self.assertEqual(reading.drafts, (
            Draft("photo", media={"file_id": "b", "file_unique_id": "B", "file_size": 2048}),
            Draft("link", url="https://example.com/scilla")))
        self.assertEqual(reading.words, "tramonto, come qui")

    def test_documents_and_audio_get_a_provisional_title(self):
        doc = read(update(document={"file_id": "d", "file_unique_id": "D", "file_name": "contratto.pdf",
                                    "mime_type": "application/pdf", "thumbnail": {"file_id": "t"}}))
        self.assertEqual(doc.drafts, (Draft("document", title="contratto.pdf", media={
            "file_id": "d", "file_unique_id": "D", "file_name": "contratto.pdf", "mime_type": "application/pdf"}),))
        audio = read(update(audio={"file_id": "a", "file_unique_id": "A", "title": "Lezione 3", "duration": 600}))
        self.assertEqual(audio.drafts[0].title, "Lezione 3")
        voice = read(update(voice={"file_id": "v", "file_unique_id": "V", "duration": 12}))
        self.assertEqual((voice.drafts[0].source, voice.drafts[0].title), ("voice", None))

    def test_a_message_with_nothing_to_keep(self):
        reading = read(update(sticker={"file_id": "x", "file_unique_id": "X", "emoji": "👍"}))
        self.assertEqual((reading.drafts, reading.words, reading.command), ((), None, None))


if __name__ == "__main__":
    unittest.main()
