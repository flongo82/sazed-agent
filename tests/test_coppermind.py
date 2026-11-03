"""coppermind: link riconosciuti, salvataggio idempotente, modifiche, ricerca con filtri e testo."""

import calendar
import shutil
import tempfile
import unittest
from pathlib import Path

from sazed_agent.coppermind import Coppermind, CoppermindError, Draft, Link, parse_link
from sazed_agent.journal import Journal, JournalError


def day(y, m, d):
    """Mezzogiorno UTC di quel giorno: la stessa data in qualunque fuso."""
    return calendar.timegm((y, m, d, 12, 0, 0))


def update(update_id, chat=42):
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": chat}, "text": "x"}}


class Links(unittest.TestCase):
    def test_tracking_goes_away_and_the_rest_stays(self):
        link = parse_link("https://www.giallozafferano.it/ricette/risotto.html?utm_source=x&page=2&fbclid=y#passi")
        self.assertEqual(link, Link("https://www.giallozafferano.it/ricette/risotto.html?page=2#passi",
                                    "giallozafferano.it/ricette/risotto.html?page=2", "web", "giallozafferano.it"))

    def test_every_form_of_a_youtube_video_is_the_same_link(self):
        keys = {parse_link(u).key for u in ("https://youtu.be/abc123?si=XYZ",
                                            "https://www.youtube.com/watch?v=abc123&t=30s",
                                            "http://m.youtube.com/watch?feature=share&v=abc123")}
        self.assertEqual(keys, {"youtube.com/watch?v=abc123"})
        self.assertEqual(parse_link("https://youtu.be/abc123?si=XYZ").url, "https://youtu.be/abc123")
        self.assertEqual(parse_link("https://music.youtube.com/watch?v=q").platform, "youtube")

    def test_instagram_and_tiktok_keep_no_parameters(self):
        link = parse_link("https://www.instagram.com/reel/C0ffee/?igsh=abc&utm_source=ig_web")
        self.assertEqual((link.url, link.key, link.platform),
                         ("https://www.instagram.com/reel/C0ffee/", "instagram.com/reel/C0ffee", "instagram"))
        self.assertEqual(parse_link("https://www.tiktok.com/@chef/video/1?_r=1&_t=2").key, "tiktok.com/@chef/video/1")

    def test_scheme_www_and_trailing_slash_do_not_matter(self):
        self.assertEqual(parse_link("http://example.com/a/").key, parse_link("https://www.example.com/a").key)
        self.assertEqual(parse_link("https://twitter.com/u/status/1?s=20").key, parse_link("https://x.com/u/status/1").key)

    def test_address_without_scheme_and_malformed_address(self):
        self.assertEqual(parse_link("example.org/pagina").url, "https://example.org/pagina")
        self.assertEqual(parse_link("https://example.com:porta/x"),
                         Link("https://example.com:porta/x", "https://example.com:porta/x", "web", None))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.now = 5000.0
        self.journal = Journal(self.dir / "coppermind.db")
        self.addCleanup(self.journal.close)
        self.memory = Coppermind(self.journal.db, clock=lambda: self.now)

    def save(self, message_id, *drafts, sent_at=None):
        return self.memory.save(42, message_id, sent_at or day(2026, 11, 1), drafts)

    def classify(self, item_id, **values):
        """Ciò che farà il modello: tipo, titolo, argomenti, riassunto."""
        sets = ", ".join(f"{k} = ?" for k in values)
        self.journal.db.execute(f"UPDATE items SET {sets} WHERE id = ?", (*values.values(), item_id))


class Saving(Base):
    def test_a_note_keeps_the_words_and_needs_no_reading(self):
        [out] = self.save(10, Draft("note", note="riso carnaroli\nda comprare"))
        item = out.item
        self.assertEqual(out.status, "saved")
        self.assertEqual((item.chat_id, item.message_id, item.source, item.note, item.extraction, item.sent_at),
                         (42, 10, "note", "riso carnaroli\nda comprare", "complete", day(2026, 11, 1)))
        self.assertEqual(self.memory.count(), 1)

    def test_saving_again_creates_nothing_new(self):
        first = self.save(10, Draft("link", url="https://example.com/r?utm_medium=x", note="da provare"))
        again = self.save(10, Draft("link", url="https://example.com/r?utm_medium=x", note="da provare"))
        self.assertEqual([o.item.id for o in first], [o.item.id for o in again])
        self.assertEqual([o.status for o in again], ["saved"])
        self.assertEqual(self.memory.count(), 1)

    def test_a_link_gets_platform_site_and_waits_to_be_read(self):
        [out] = self.save(10, Draft("link", url="https://youtu.be/abc?si=t", note="per la cena"))
        item = out.item
        self.assertEqual((item.url, item.platform, item.site, item.note, item.extraction),
                         ("https://youtu.be/abc", "youtube", "youtu.be", "per la cena", "pending"))

    def test_the_same_link_from_another_message_stays_one_item_and_keeps_the_new_words(self):
        [first] = self.save(10, Draft("link", url="https://www.youtube.com/watch?v=abc"))
        for _ in range(2):  # due volte: le parole si aggiungono una volta sola
            [out] = self.save(11, Draft("link", url="https://youtu.be/abc", note="rifallo con meno sale"))
        self.assertEqual((out.status, out.item.id, out.item.message_id), ("already", first.item.id, 10))
        self.assertEqual(self.memory.get(first.item.id).note, "rifallo con meno sale")
        self.save(12, Draft("link", url="https://youtu.be/abc", note="e con più pepe"))
        self.assertEqual(self.memory.get(first.item.id).note, "rifallo con meno sale\ne con più pepe")
        self.assertEqual(self.memory.count(), 1)
        self.assertEqual(self.memory.of_message(42, 11), [])

    def test_a_link_repeated_in_the_message_counts_once(self):
        outs = self.save(10, Draft("link", url="https://example.com/a"), Draft("link", url="http://example.com/a/"),
                         Draft("link", url="https://example.com/b"))
        self.assertEqual([o.item.url for o in outs], ["https://example.com/a", "https://example.com/b"])

    def test_the_same_file_twice_is_one_item(self):
        media = {"file_id": "AAA", "file_unique_id": "U1", "file_name": "contratto.pdf"}
        [first] = self.save(10, Draft("document", title="contratto.pdf", media=media))
        [again] = self.save(11, Draft("document", title="contratto.pdf", media=dict(media, file_id="BBB")))
        self.assertEqual((again.status, again.item.id), ("already", first.item.id))
        self.assertEqual((first.item.title, first.item.media["file_name"], first.item.extraction),
                         ("contratto.pdf", "contratto.pdf", "pending"))

    def test_an_edit_with_new_words_drops_the_classification_but_keeps_what_was_read(self):
        [out] = self.save(10, Draft("link", url="https://example.com/r", note="buona"))
        self.classify(out.item.id, kind="ricetta", title="Risotto", topics='["riso"]', summary="Un risotto.",
                      content="testo della pagina", extraction="complete")
        self.save(10, Draft("link", url="https://example.com/r", note="buona"))  # stesse parole
        self.assertEqual(self.memory.get(out.item.id).kind, "ricetta")
        self.now += 60
        self.save(10, Draft("link", url="https://example.com/r", note="buonissima"))
        item = self.memory.get(out.item.id)
        self.assertEqual((item.note, item.kind, item.title, item.topics, item.summary),
                         ("buonissima", None, None, [], None))
        self.assertEqual((item.content, item.extraction, item.saved_at), ("testo della pagina", "complete", 5000.0))

    def test_an_edit_that_changes_the_link_replaces_the_item(self):
        [old] = self.save(10, Draft("link", url="https://example.com/vecchio"))
        [new] = self.save(10, Draft("link", url="https://example.com/nuovo"))
        self.assertIsNone(self.memory.get(old.item.id))
        self.assertEqual([i.url for i in self.memory.of_message(42, 10)], ["https://example.com/nuovo"])
        self.save(10, Draft("note", note="niente link, alla fine"))
        self.assertEqual([i.source for i in self.memory.of_message(42, 10)], ["note"])
        self.assertIsNone(self.memory.get(new.item.id))

    def test_saving_and_the_state_change_are_one_transaction(self):
        self.journal.record(7, update(1), "acquisition")
        with self.assertRaises(JournalError):
            with self.journal.transaction():
                self.save(10, Draft("note", note="da non perdere"))
                self.journal.advance(1, "received")  # uno stato che torna indietro: errore
        self.assertEqual(self.memory.count(), 0)  # niente salvato senza il cambio di stato
        with self.journal.transaction():
            self.save(10, Draft("note", note="da non perdere"))
            self.journal.advance(1, "saved")
        self.assertEqual((self.memory.count(), self.journal.get(1).state), (1, "saved"))

    def test_outcomes_read_without_writing(self):
        drafts = (Draft("link", url="https://example.com/a"),)
        self.assertEqual(self.memory.outcomes(42, 10, drafts), [])
        self.memory.save(42, 10, day(2026, 11, 1), drafts)
        self.now += 60
        [out] = self.memory.outcomes(42, 10, drafts)
        self.assertEqual((out.status, out.item.saved_at), ("saved", 5000.0))

    def test_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            self.save(10, Draft("sticker"))
        self.assertEqual(self.memory.count(), 0)


class Versions(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_a_journal_from_before_coppermind_gets_the_catalogue(self):
        journal = Journal(self.dir / "coppermind.db")
        self.addCleanup(journal.close)
        journal.record(7, update(1), "acquisition")
        self.assertEqual(journal.db.execute("PRAGMA user_version").fetchone()[0], 0)
        Coppermind(journal.db)
        self.assertEqual(journal.db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(journal.get(1).state, "received")
        Coppermind(journal.db)  # una seconda apertura non cambia nulla

    def test_a_database_from_a_newer_version_is_refused(self):
        journal = Journal(self.dir / "coppermind.db")
        self.addCleanup(journal.close)
        journal.db.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(CoppermindError, "versione 99"):
            Coppermind(journal.db)


class Search(Base):
    def setUp(self):
        super().setUp()
        self.ids = {}
        for name, mid, sent, draft, kind in (
            ("risotto", 1, day(2026, 10, 1), Draft("link", url="https://youtu.be/r1", note="risotto alla zucca"), "ricetta"),
            ("carbonara", 2, day(2026, 10, 10),
             Draft("link", url="https://www.giallozafferano.it/ricette/carbonara.html", note="carbonara perfetta"),
             "ricetta"),
            ("schiena", 3, day(2026, 11, 1),
             Draft("link", url="https://www.instagram.com/reel/X1/", note="esercizi per la schiena"), "video"),
            ("perche", 4, day(2026, 11, 2), Draft("note", note="Perché il riso carnaroli tiene la cottura"), "nota"),
            ("scilla", 5, day(2026, 11, 3), Draft("photo", note="tramonto a Scilla", media={"file_unique_id": "P5"}), "foto"),
        ):
            [out] = self.save(mid, draft, sent_at=sent)
            self.classify(out.item.id, kind=kind)
            self.ids[name] = out.item.id

    def names(self, results):
        by_id = {v: k for k, v in self.ids.items()}
        return [by_id[i.id] for i in results.items]

    def test_filters_give_complete_lists_newest_first(self):
        found = self.memory.search(kind="ricetta")
        self.assertEqual((self.names(found), found.total), (["carbonara", "risotto"], 2))
        self.assertEqual(self.names(self.memory.search(kind="ricetta", platform="youtube")), ["risotto"])
        self.assertEqual(self.names(self.memory.search(source=("photo", "note"))), ["scilla", "perche"])
        self.assertEqual(self.names(self.memory.search(since=day(2026, 10, 10), until=day(2026, 11, 2))),
                         ["schiena", "carbonara"])
        self.assertEqual(self.memory.search(kind="articolo").total, 0)

    def test_site_also_matches_its_subdomains_but_only_whole_names(self):
        for site in ("giallozafferano.it", "www.giallozafferano.it", "GialloZafferano.it"):
            self.assertEqual(self.names(self.memory.search(site=site)), ["carbonara"], site)
        self.assertEqual(self.memory.search(site="zafferano.it").total, 0)
        self.assertEqual(self.names(self.memory.search(site="youtu.be")), ["risotto"])

    def test_text_ignores_accents_and_case_and_matches_word_beginnings(self):
        for text in ("perche", "PERCHÉ", "carnar"):
            self.assertEqual(self.names(self.memory.search(text)), ["perche"], text)
        self.assertEqual(self.names(self.memory.search("risott")), ["risotto"])
        self.assertEqual(self.names(self.memory.search("giallozafferano")), ["carbonara"])  # anche l'indirizzo

    def test_all_words_or_any_word(self):
        self.assertEqual(self.memory.search("risotto carbonara").total, 0)
        self.assertEqual(sorted(self.names(self.memory.search("risotto carbonara", words="any"))),
                         ["carbonara", "risotto"])
        with self.assertRaises(ValueError):
            self.memory.search("riso", words="qualcuna")

    def test_text_and_filters_together(self):
        self.assertEqual(sorted(self.names(self.memory.search("riso"))), ["perche", "risotto"])
        self.assertEqual(self.names(self.memory.search("riso", kind="ricetta")), ["risotto"])

    def test_total_counts_beyond_the_limit(self):
        found = self.memory.search(kind="ricetta", limit=1)
        self.assertEqual((self.names(found), found.total), (["carbonara"], 2))
        self.assertEqual(self.names(self.memory.search(kind="ricetta", limit=1, offset=1)), ["risotto"])
        self.assertEqual(self.memory.search(limit=None).total, 5)
        self.assertEqual(len(self.memory.search(limit=None).items), 5)

    def test_query_syntax_in_the_text_is_just_text(self):
        self.assertEqual(self.memory.search("?!").total, 0)
        self.assertEqual(self.names(self.memory.search('zucca" OR * NEAR(')), [])
        self.assertEqual(self.names(self.memory.search('"zucca"')), ["risotto"])

    def test_the_text_index_follows_edits_and_deletions(self):
        self.save(4, Draft("note", note="Il riso venere va sciacquato"), sent_at=day(2026, 11, 2))
        self.assertEqual(self.memory.search("carnaroli").total, 0)
        self.assertEqual(self.names(self.memory.search("venere")), ["perche"])
        self.save(1, Draft("link", url="https://youtu.be/r2"), sent_at=day(2026, 10, 1))  # il link cambia
        self.assertEqual(self.memory.search("zucca").total, 0)


if __name__ == "__main__":
    unittest.main()
