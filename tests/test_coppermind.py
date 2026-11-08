"""coppermind: link riconosciuti, salvataggio idempotente, modifiche, ricerca con filtri e testo."""

import calendar
import shutil
import tempfile
import unittest
from pathlib import Path

from sazed_agent.coppermind import Coppermind, CoppermindError, Draft, Link, parse_link
from sazed_agent.journal import Journal, JournalError
from tests.fake_meanings import vector


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

    def test_a_link_keeps_what_its_reading_found_when_the_words_change(self):
        [out] = self.save(10, Draft("link", url="https://example.com/r", note="buona"))
        self.memory.read_done(out.item.id, "complete", content="testo della pagina", title="Risotto",
                              kind="recipe", topics=("riso",), summary="Un risotto.")
        self.now += 60
        self.save(10, Draft("link", url="https://example.com/r", note="buonissima", kind="article", title="Altro"))
        item = self.memory.get(out.item.id)
        self.assertEqual((item.note, item.kind, item.title, item.topics, item.summary, item.content, item.extraction),
                         ("buonissima", "recipe", "Risotto", ["riso"], "Un risotto.", "testo della pagina", "complete"))
        self.assertEqual(item.saved_at, 5000.0)

    def test_a_document_keeps_its_file_name_as_title_until_read(self):
        [out] = self.save(10, Draft("document", title="contratto.pdf", media={"file_unique_id": "D"}))
        self.memory.read_done(out.item.id, "complete", title="Contratto d'affitto")
        self.save(10, Draft("document", title="contratto.pdf", media={"file_unique_id": "D"}))
        self.assertEqual(self.memory.get(out.item.id).title, "Contratto d'affitto")

    def test_a_note_description_follows_its_words(self):
        draft = Draft("note", note="riso e parmigiano", kind="note", title="Spesa", topics=("riso",), summary="Spesa.")
        [out] = self.save(10, draft)
        self.assertEqual((out.item.kind, out.item.title, out.item.topics), ("note", "Spesa", ["riso"]))
        self.save(10, Draft("note", note="riso e parmigiano"))  # stesse parole, senza descrizione: resta
        self.assertEqual(self.memory.get(out.item.id).title, "Spesa")
        self.save(10, Draft("note", note="riso, parmigiano e burro", kind="note", title="Spesa grande"))
        item = self.memory.get(out.item.id)
        self.assertEqual((item.title, item.topics, item.summary), ("Spesa grande", [], None))
        self.save(10, Draft("note", note="solo burro"))  # parole nuove senza descrizione: decade
        item = self.memory.get(out.item.id)
        self.assertEqual((item.kind, item.title, item.topics, item.summary), (None, None, [], None))

    def test_a_comment_sent_later_joins_the_note(self):
        [out] = self.save(10, Draft("link", url="https://example.com/r"))
        for _ in range(2):  # una volta sola, anche se ripetuto
            self.assertTrue(self.memory.annotate(out.item.id, "da fare sabato"))
        self.memory.annotate(out.item.id, "con le pere")
        self.assertEqual(self.memory.get(out.item.id).note, "da fare sabato\ncon le pere")
        self.assertEqual(self.memory.search("pere").total, 1)
        self.assertFalse(self.memory.annotate(999, "nessuno"))

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


class Reading(Base):
    def test_items_are_read_in_order_and_awaited_ones_first(self):
        a, b, c = (self.save(n, Draft("link", url=f"https://example.com/{n}"))[0].item for n in (10, 11, 12))
        self.save(13, Draft("voice", media={"file_unique_id": "V"}))
        self.assertEqual(self.memory.to_read(["link"]).id, a.id)
        self.assertEqual(self.memory.to_read(["link"], first=[c.id]).id, c.id)
        self.assertIsNone(self.memory.to_read([]))  # nulla si legge senza un lettore
        self.assertEqual(self.memory.to_read(["voice"]).source, "voice")

    def test_a_short_link_moves_to_its_true_address_unless_taken(self):
        short = self.save(10, Draft("link", url="https://share.google/abc"))[0].item
        other = self.save(11, Draft("link", url="https://example.com/altro"))[0].item
        self.assertTrue(self.memory.relink(short.id, "https://www.instagram.com/reel/R/?igsh=x"))
        item = self.memory.get(short.id)
        self.assertEqual((item.url, item.platform, item.site), ("https://www.instagram.com/reel/R/", "instagram",
                                                                "instagram.com"))
        self.assertEqual(self.save(12, Draft("link", url="https://instagram.com/reel/R"))[0].status, "already")
        self.assertFalse(self.memory.relink(short.id, "https://example.com/altro"))
        self.assertEqual(self.memory.get(short.id).platform, "instagram")
        self.assertEqual(self.memory.get(other.id).url, "https://example.com/altro")

    def test_a_failed_reading_waits_then_gives_up(self):
        [out] = self.save(10, Draft("link", url="https://example.com/r"))
        item_id = out.item.id
        self.assertEqual(self.memory.read_failed(item_id, "HTTP 503", (10, 60)), "pending")
        self.assertIsNone(self.memory.to_read(["link"]))
        self.assertEqual(self.memory.next_reading(["link"]), 5010.0)
        self.now = 5010.0
        self.assertEqual(self.memory.to_read(["link"]).id, item_id)
        self.assertEqual(self.memory.read_failed(item_id, "HTTP 503", (10, 60), min_delay=120), "pending")
        self.assertEqual(self.memory.next_reading(["link"]), 5130.0)  # l'attesa chiesta conta
        self.assertEqual(self.memory.read_failed(item_id, "HTTP 503", (10, 60)), "failed")
        item = self.memory.get(item_id)
        self.assertEqual((item.extraction, item.read_error), ("failed", "HTTP 503"))
        self.assertIsNone(self.memory.next_reading(["link"]))

    def test_a_vanished_source_is_final_at_once(self):
        [out] = self.save(10, Draft("link", url="https://example.com/r"))
        self.assertEqual(self.memory.read_failed(out.item.id, "HTTP 410", (10,), final="unavailable"), "unavailable")
        self.assertEqual(self.memory.get(out.item.id).extraction, "unavailable")

    def test_postponing_a_reading_uses_no_attempt(self):
        [out] = self.save(10, Draft("link", url="https://example.com/r"))
        self.memory.read_later(out.item.id, 60, "chiave rifiutata")
        self.assertEqual(self.memory.next_reading(["link"]), 5060.0)
        self.assertEqual(self.memory.read_failed(out.item.id, "x", (10,)), "pending")  # il primo tentativo

    def test_what_was_read_is_searchable(self):
        [out] = self.save(10, Draft("link", url="https://example.com/r"))
        self.memory.read_done(out.item.id, "partial", content="guanciale e pecorino", title="Carbonara",
                              kind="recipe", topics=("uova",))
        item = self.memory.get(out.item.id)
        self.assertEqual((item.extraction, item.title, item.kind, item.topics), ("partial", "Carbonara", "recipe", ["uova"]))
        self.assertEqual(self.memory.search("pecorino", kind="recipe").total, 1)
        with self.assertRaises(ValueError):
            self.memory.read_done(out.item.id, "pending")


def as_version_3(db):
    """Riporta il catalogo com'era alla versione 3: senza i significati."""
    db.execute("DROP TRIGGER meanings_delete")
    db.execute("DROP TABLE meanings")
    db.execute("PRAGMA user_version = 3")


def as_version_2(db):
    """Riporta il catalogo com'era alla versione 2: senza autori né lettura completa, con l'indice di allora."""
    as_version_3(db)
    for name in ("items_fts_insert", "items_fts_delete", "items_fts_update"):
        db.execute(f"DROP TRIGGER {name}")
    db.execute("DROP TABLE items_fts")
    for column in ("authors", "read_full"):
        db.execute(f"ALTER TABLE items DROP COLUMN {column}")
    db.execute("CREATE VIRTUAL TABLE items_fts USING fts5(title, topics, summary, note, content, url,"
               " content='items', content_rowid='id', tokenize='unicode61 remove_diacritics 2')")
    db.execute("CREATE TRIGGER items_fts_insert AFTER INSERT ON items BEGIN"
               " INSERT INTO items_fts (rowid, title, topics, summary, note, content, url)"
               " VALUES (new.id, new.title, new.topics, new.summary, new.note, new.content, new.url); END")
    db.execute("CREATE TRIGGER items_fts_delete AFTER DELETE ON items BEGIN"
               " INSERT INTO items_fts (items_fts, rowid, title, topics, summary, note, content, url)"
               " VALUES ('delete', old.id, old.title, old.topics, old.summary, old.note, old.content, old.url); END")
    db.execute("CREATE TRIGGER items_fts_update AFTER UPDATE OF title, topics, summary, note, content, url"
               " ON items BEGIN"
               " INSERT INTO items_fts (items_fts, rowid, title, topics, summary, note, content, url)"
               " VALUES ('delete', old.id, old.title, old.topics, old.summary, old.note, old.content, old.url);"
               " INSERT INTO items_fts (rowid, title, topics, summary, note, content, url)"
               " VALUES (new.id, new.title, new.topics, new.summary, new.note, new.content, new.url); END")
    db.execute("INSERT INTO items_fts (items_fts) VALUES ('rebuild')")
    db.execute("PRAGMA user_version = 2")


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
        self.assertEqual(journal.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(journal.get(1).state, "received")
        Coppermind(journal.db)  # una seconda apertura non cambia nulla

    def test_a_catalogue_of_version_1_learns_to_read(self):
        journal = Journal(self.dir / "coppermind.db")
        self.addCleanup(journal.close)
        memory = Coppermind(journal.db, clock=lambda: 100.0)
        memory.save(42, 10, 1, [Draft("link", url="https://example.com/r", note="vecchio")])
        db = journal.db  # il catalogo com'era alla versione 1
        as_version_2(db)
        db.execute("DROP INDEX items_reading")
        for column in ("read_attempts", "next_read_at", "read_error"):
            db.execute(f"ALTER TABLE items DROP COLUMN {column}")
        db.execute("PRAGMA user_version = 1")
        memory = Coppermind(db, clock=lambda: 100.0)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
        item = memory.to_read(["link"])
        self.assertEqual((item.note, item.extraction, item.read_error), ("vecchio", "pending", None))

    def test_a_catalogue_of_version_2_learns_about_authors(self):
        journal = Journal(self.dir / "coppermind.db")
        self.addCleanup(journal.close)
        memory = Coppermind(journal.db, clock=lambda: 100.0)
        [old] = [o.item for o in memory.save(42, 10, 1, [Draft("document", media={"file_unique_id": "D"})])]
        memory.read_done(old.id, "partial", title="Il pozzo dell'ascensione")
        as_version_2(journal.db)
        memory = Coppermind(journal.db, clock=lambda: 100.0)
        self.assertEqual(journal.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(memory.search("pozzo").total, 1)  # l'indice ricostruito trova ciò che c'era
        memory.read_done(old.id, "partial", authors=["Brandon Sanderson"])
        self.assertEqual([i.id for i in memory.search("sanderson").items], [old.id])
        self.assertEqual((memory.get(old.id).authors, memory.get(old.id).read_full), (["Brandon Sanderson"], False))

    def test_a_catalogue_of_version_3_learns_meanings(self):
        journal = Journal(self.dir / "coppermind.db")
        self.addCleanup(journal.close)
        memory = Coppermind(journal.db, clock=lambda: 100.0)
        [note] = [o.item for o in memory.save(42, 10, 1, [Draft("note", note="riso")])]
        as_version_3(journal.db)
        memory = Coppermind(journal.db, clock=lambda: 100.0)
        self.assertEqual(journal.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual([i.id for i in memory.to_mean("modello")], [note.id])

    def test_a_database_from_a_newer_version_is_refused(self):
        journal = Journal(self.dir / "coppermind.db")
        self.addCleanup(journal.close)
        journal.db.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(CoppermindError, "versione 99"):
            Coppermind(journal.db)


class Documents(Base):
    def test_a_document_is_read_in_full_only_once_asked(self):
        [doc] = [o.item for o in self.save(10, Draft("document", media={"file_unique_id": "D"}))]
        [link] = [o.item for o in self.save(11, Draft("link", url="https://example.com/x"))]
        self.memory.read_failed(doc.id, "rete", (1,))
        self.memory.read_done(doc.id, "partial", content="prime pagine", title="Il pozzo", authors=["Sanderson"])
        self.assertTrue(self.memory.read_in_full(doc.id))
        again = self.memory.get(doc.id)
        self.assertEqual((again.extraction, again.read_full, again.title, again.authors, again.read_error),
                         ("pending", True, "Il pozzo", ["Sanderson"], None))  # ciò che si sapeva resta
        self.assertEqual(self.memory.to_read(["document"]).id, doc.id)
        self.memory.read_done(doc.id, "complete", content="tutto il libro")
        self.assertTrue(self.memory.read_in_full(doc.id))  # già letto tutto: nulla cambia
        self.assertEqual(self.memory.get(doc.id).extraction, "complete")
        self.assertFalse(self.memory.read_in_full(link.id))
        self.assertFalse(self.memory.read_in_full(999))

    def test_passages_find_where_a_long_text_speaks_of_something(self):
        [doc] = [o.item for o in self.save(10, Draft("document", media={"file_unique_id": "D"}))]
        paragraphs = [f"Paragrafo {i}: " + "parole qualunque " * 40 for i in range(20)]
        paragraphs[3] += "Il Lord Reggente governa l'Impero Finale."
        paragraphs[15] += "Vin scopre l'atium e il Lord Reggente cade; l'impero trema."
        self.memory.read_done(doc.id, "complete", content="\n".join(paragraphs))
        found = self.memory.passages(doc.id, "lord reggente, impèro!")
        self.assertEqual(len(found), 2)
        self.assertLess(found[0][0], found[1][0])  # nell'ordine del testo
        self.assertIn("Impero Finale", found[0][1])
        self.assertIn("atium", self.memory.passages(doc.id, "atium", limit=1)[0][1])
        self.assertTrue(all(len(text) <= 1500 for _, text in self.memory.passages(doc.id, "parole")))
        self.assertEqual(self.memory.passages(doc.id, "di"), [])  # parole troppo corte
        self.assertEqual(self.memory.passages(999, "lord"), [])


class Meanings(Base):
    def test_items_are_meant_once_read_and_again_when_they_change(self):
        [link] = [o.item for o in self.save(10, Draft("link", url="https://example.com/torta"))]
        [note] = [o.item for o in self.save(11, Draft("note", note="comprare il riso"))]
        self.assertEqual([i.id for i in self.memory.to_mean("m")], [note.id])  # il link aspetta la lettura
        self.memory.read_done(link.id, "complete", title="Torta di mele")
        for item in self.memory.to_mean("m"):
            self.memory.set_meaning(item.id, "m", vector(item.title or item.note), item.updated_at)
        self.assertEqual(self.memory.to_mean("m"), [])
        self.assertEqual(len(self.memory.to_mean("altro-modello")), 2)  # un altro modello ricomincia
        self.now += 10
        self.memory.annotate(link.id, "per domenica")
        self.assertEqual([i.id for i in self.memory.to_mean("m")], [link.id])
        self.memory.set_meaning(999, "m", vector("niente"), 0)  # un elemento che non c'è: nulla
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM meanings").fetchone()[0], 2)
        self.save(11)  # la nota sparisce, e il suo significato con lei
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM meanings").fetchone()[0], 1)

    def test_the_closest_items_come_first_within_the_filters(self):
        texts = {"torta di mele della nonna": "recipe", "torta salata di verdure": "recipe",
                 "contratto di affitto": "document"}
        for n, (text, kind) in enumerate(texts.items()):
            [item] = [o.item for o in self.save(20 + n, Draft("note", note=text))]
            self.classify(item.id, kind=kind)
            self.memory.set_meaning(item.id, "m", vector(text), self.memory.get(item.id).updated_at)
        found = self.memory.by_meaning(vector("una torta di mele"), "m")
        self.assertEqual([i.note for i, _ in found][:2], ["torta di mele della nonna", "torta salata di verdure"])
        self.assertGreater(found[0][1], found[1][1])
        self.assertEqual([i.note for i, _ in self.memory.by_meaning(vector("torta"), "m", kind="document")],
                         ["contratto di affitto"])
        self.assertEqual(len(self.memory.by_meaning(vector("torta"), "m", limit=1)), 1)
        self.assertEqual(self.memory.by_meaning(vector("torta"), "altro-modello"), [])


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
