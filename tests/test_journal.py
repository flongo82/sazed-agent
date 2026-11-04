"""Registro locale: idempotenza, durabilità, stati che solo avanzano, tentativi."""

import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from sazed_agent.journal import Journal, JournalError

ROOT = Path(__file__).resolve().parent.parent
DELAYS = (5, 30, 120)


def update(update_id, text="ciao", chat=42):
    return {"update_id": update_id,
            "message": {"message_id": update_id + 1, "chat": {"id": chat, "type": "private"},
                        "from": {"id": chat, "first_name": "Ada"}, "text": text}}


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = self.dir / "data" / "coppermind.db"
        self.clock = Clock()
        self.journal = Journal(self.path, clock=self.clock)
        self.addCleanup(self.journal.close)

    def test_record_is_idempotent_on_update_id(self):
        self.assertTrue(self.journal.record(7, update(100), "acquisition"))
        self.assertFalse(self.journal.record(8, update(100, "altro"), "acquisition"))
        entry = self.journal.get(100)
        self.assertEqual((entry.queue_id, entry.state, entry.chat_id, entry.message_id),
                         (7, "received", 42, 101))
        self.assertEqual(entry.update["message"]["text"], "ciao")

    def test_durability_settings(self):
        self.assertEqual(self.journal.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.journal.db.execute("PRAGMA synchronous").fetchone()[0], 2)  # FULL

    def test_record_survives_a_crash_right_after_returning(self):
        # Un processo registra e muore subito, senza chiudere il database: l'elemento c'è.
        code = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {str(ROOT)!r})
            from sazed_agent.journal import Journal
            j = Journal({str(self.path)!r})
            j.record(1, {update(200)!r}, "acquisition")
            os._exit(9)
        """)
        result = subprocess.run([sys.executable, "-c", code])
        self.assertEqual(result.returncode, 9)
        self.assertEqual(Journal(self.path).get(200).state, "received")

    def test_states_only_move_forward(self):
        self.journal.record(1, update(1), "acquisition")
        self.journal.advance(1, "read")
        self.journal.advance(1, "answered", reply_message_id=55)
        with self.assertRaisesRegex(JournalError, "da answered non si va a read"):
            self.journal.advance(1, "read")
        self.journal.advance(1, "done")
        with self.assertRaises(JournalError):
            self.journal.advance(1, "done")
        self.assertEqual(self.journal.get(1).reply_message_id, 55)

    def test_reply_id_recorded_as_soon_as_known(self):
        self.journal.record(1, update(1), "acquisition")
        self.journal.set_reply(1, 77)
        self.assertEqual((self.journal.get(1).state, self.journal.get(1).reply_message_id), ("received", 77))

    def test_retries_with_growing_delays_then_failure(self):
        self.journal.record(1, update(1), "acquisition")
        self.journal.advance(1, "read")
        for attempt, delay in enumerate(DELAYS, start=1):
            self.assertEqual(self.journal.retry_later(1, f"errore {attempt}", DELAYS), "read")
            entry = self.journal.get(1)
            self.assertEqual((entry.attempts, entry.next_attempt_at), (attempt, self.clock.now + delay))
            self.assertEqual(self.journal.due("acquisition"), [])  # non ancora
            self.clock.now += delay
            self.assertEqual([e.update_id for e in self.journal.due("acquisition")], [1])
        self.assertEqual(self.journal.retry_later(1, "ultimo", DELAYS), "failed")
        self.assertEqual(self.journal.get(1).last_error, "ultimo")
        self.assertEqual(self.journal.due("acquisition"), [])

    def test_permanent_failure_skips_the_retries(self):
        self.journal.record(1, update(1), "acquisition")
        self.journal.fail(1, "messaggio cancellato")
        self.assertEqual(self.journal.get(1).state, "failed")
        with self.assertRaises(JournalError):
            self.journal.retry_later(1, "ancora", DELAYS)

    def test_a_failing_item_does_not_block_the_others(self):
        for i in (1, 2, 3):
            self.journal.record(i, update(i), "acquisition")
        self.journal.retry_later(1, "difettoso", DELAYS)
        self.assertEqual([e.update_id for e in self.journal.due("acquisition", limit=5)], [2, 3])

    def test_lanes_are_separate_and_ordered_by_arrival(self):
        self.journal.record(1, update(1), "acquisition")
        self.clock.now += 1
        self.journal.record(2, update(2, "/stato"), "commands")
        self.clock.now += 1
        self.journal.record(3, update(3), "acquisition")
        self.assertEqual([e.update_id for e in self.journal.due("acquisition", 5)], [1, 3])
        self.assertEqual([e.update_id for e in self.journal.due("commands", 5)], [2])
        with self.assertRaises(JournalError):
            self.journal.record(4, update(4), "altro")

    def test_counts_per_state(self):
        for i in (1, 2, 3):
            self.journal.record(i, update(i), "acquisition")
        self.journal.advance(2, "done")
        self.journal.fail(3, "x")
        counts = self.journal.counts()
        self.assertEqual((counts["received"], counts["done"], counts["failed"], counts["read"]), (1, 1, 1, 0))

    def test_nested_transactions_belong_to_the_outer_one(self):
        self.journal.record(1, update(1), "acquisition")
        with self.assertRaises(JournalError):
            with self.journal.transaction():
                self.journal.advance(1, "read")     # dentro: non ancora confermato
                self.journal.advance(1, "received")  # all'indietro: errore, si annulla tutto
        self.assertEqual(self.journal.get(1).state, "received")
        with self.journal.transaction():
            self.journal.advance(1, "read")
        self.assertEqual(self.journal.get(1).state, "read")
        self.assertFalse(self.journal.db.in_transaction)

    def test_a_journal_from_before_the_interpretation_gets_its_column(self):
        old = self.dir / "vecchio.db"
        db = sqlite3.connect(old)
        db.execute("CREATE TABLE inbox (update_id INTEGER PRIMARY KEY, queue_id INTEGER NOT NULL,"
                   " received_at REAL NOT NULL, chat_id INTEGER, message_id INTEGER, lane TEXT NOT NULL,"
                   " state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,"
                   " last_error TEXT, reply_message_id INTEGER, updated_at REAL NOT NULL, payload TEXT NOT NULL)")
        db.execute("INSERT INTO inbox VALUES (1, 1, 0, 42, 2, 'acquisition', 'done', 0, 0, NULL, NULL, 0, '{}')")
        db.commit()
        db.close()
        journal = Journal(old)
        self.addCleanup(journal.close)
        self.assertIsNone(journal.get(1).interpretation)
        journal.set_interpretation(1, {"intent": "question", "question": "dove?", "items": []})
        self.assertEqual(journal.get(1).interpretation["question"], "dove?")

    def test_unknown_item(self):
        with self.assertRaisesRegex(JournalError, "non è nel registro"):
            self.journal.advance(999, "read")


if __name__ == "__main__":
    unittest.main()
