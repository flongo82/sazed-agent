"""Registro locale degli ingressi: ogni messaggio ricevuto dal Worker, con il suo stato.

Vive nello stesso database di coppermind, sulla stessa connessione (db): un salvataggio in
coppermind e il cambio di stato del messaggio stanno in una sola transaction(). Un messaggio
si conferma al Worker solo dopo che record() è tornato: a quel punto è scritto su disco (WAL
con synchronous=FULL) e sopravvive a un arresto improvviso. Il registro è idempotente
sull'update_id di Telegram.

Stati, nell'ordine: received → read → saved → answered → done; failed da qualunque stato non
finale. Uno stato può solo avanzare: dopo un arresto si riprende da quello registrato.
"""

import json
import time
from dataclasses import dataclass

from .coppermind import connect, transaction

STATES = ("received", "read", "saved", "answered", "done", "failed")
FINAL = ("done", "failed")
LANES = ("commands", "acquisition")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS inbox (
  update_id        INTEGER PRIMARY KEY,
  queue_id         INTEGER NOT NULL,
  received_at      REAL    NOT NULL,
  chat_id          INTEGER,
  message_id       INTEGER,
  lane             TEXT    NOT NULL CHECK (lane IN {LANES}),
  state            TEXT    NOT NULL CHECK (state IN {STATES}),
  attempts         INTEGER NOT NULL DEFAULT 0,
  next_attempt_at  REAL    NOT NULL,
  last_error       TEXT,
  reply_message_id INTEGER,
  updated_at       REAL    NOT NULL,
  payload          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS inbox_due ON inbox (lane, state, next_attempt_at);
"""


class JournalError(Exception):
    """Operazione non ammessa sul registro (per esempio uno stato che torna indietro)."""


@dataclass(frozen=True)
class Entry:
    update_id: int
    queue_id: int
    received_at: float
    chat_id: int
    message_id: int
    lane: str
    state: str
    attempts: int
    next_attempt_at: float
    last_error: str
    reply_message_id: int
    update: dict


class Journal:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.db = connect(path)
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def transaction(self):
        """Una transazione sul database del registro, che è anche quello di coppermind."""
        return transaction(self.db)

    def record(self, queue_id, update, lane):
        """Registra un messaggio arrivato dal Worker. True se è nuovo, False se c'era già."""
        if lane not in LANES:
            raise JournalError(f"corsia sconosciuta: {lane}")
        msg = update.get("message") or update.get("edited_message") or {}
        now = self.clock()
        with self.transaction():
            cur = self.db.execute(
                "INSERT OR IGNORE INTO inbox (update_id, queue_id, received_at, chat_id, message_id,"
                " lane, state, next_attempt_at, updated_at, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, 'received', ?, ?, ?)",
                (update["update_id"], queue_id, now, (msg.get("chat") or {}).get("id"),
                 msg.get("message_id"), lane, now, now, json.dumps(update, ensure_ascii=False)),
            )
        return cur.rowcount == 1

    def get(self, update_id):
        row = self.db.execute("SELECT * FROM inbox WHERE update_id = ?", (update_id,)).fetchone()
        return _entry(row) if row else None

    def due(self, lane, limit=1):
        """Gli elementi non finali di una corsia da lavorare adesso, in ordine di arrivo."""
        rows = self.db.execute(
            "SELECT * FROM inbox WHERE lane = ? AND state NOT IN ('done', 'failed')"
            " AND next_attempt_at <= ? ORDER BY received_at, update_id LIMIT ?",
            (lane, self.clock(), limit),
        ).fetchall()
        return [_entry(r) for r in rows]

    def advance(self, update_id, state, reply_message_id=None):
        """Porta un elemento a uno stato successivo. Uno stato non torna mai indietro."""
        if state not in STATES or state == "failed":
            raise JournalError(f"stato non ammesso per advance: {state}")
        with self.transaction():
            current = self._state(update_id)
            if STATES.index(state) <= STATES.index(current) or current in FINAL:
                raise JournalError(f"{update_id}: da {current} non si va a {state}")
            self.db.execute(
                "UPDATE inbox SET state = ?, reply_message_id = COALESCE(?, reply_message_id),"
                " last_error = NULL, updated_at = ? WHERE update_id = ?",
                (state, reply_message_id, self.clock(), update_id),
            )

    def set_reply(self, update_id, reply_message_id):
        """Annota il message_id della risposta appena Telegram lo restituisce."""
        with self.transaction():
            self.db.execute(
                "UPDATE inbox SET reply_message_id = ?, updated_at = ? WHERE update_id = ?",
                (reply_message_id, self.clock(), update_id),
            )

    def next_attempt(self, lane):
        """L'istante del prossimo tentativo in programma nella corsia, o None se non c'è nulla."""
        row = self.db.execute(
            "SELECT MIN(next_attempt_at) FROM inbox WHERE lane = ? AND state NOT IN ('done', 'failed')",
            (lane,)).fetchone()
        return row[0]

    def retry_later(self, update_id, error, delays, min_delay=0):
        """Un tentativo è fallito: si riprova dopo l'attesa successiva, o l'elemento fallisce.

        min_delay allunga l'attesa quando chi ha rifiutato ha chiesto di aspettare (429).
        Restituisce lo stato risultante: quello di prima se si riproverà, "failed" se le
        attese sono finite.
        """
        with self.transaction():
            current = self._state(update_id)
            if current in FINAL:
                raise JournalError(f"{update_id}: è già {current}")
            attempts = self.db.execute(
                "SELECT attempts FROM inbox WHERE update_id = ?", (update_id,)).fetchone()[0] + 1
            now = self.clock()
            if attempts > len(delays):
                state, next_at = "failed", now
            else:
                state, next_at = current, now + max(delays[attempts - 1], min_delay or 0)
            self.db.execute(
                "UPDATE inbox SET attempts = ?, state = ?, next_attempt_at = ?, last_error = ?,"
                " updated_at = ? WHERE update_id = ?",
                (attempts, state, next_at, str(error)[:500], now, update_id),
            )
        return state

    def postpone(self, update_id, seconds, error):
        """Rimanda un elemento senza contare un tentativo: il problema non è suo (per esempio
        un token rifiutato), e non deve fargli consumare le attese."""
        with self.transaction():
            if self._state(update_id) in FINAL:
                raise JournalError(f"{update_id}: è già finale")
            now = self.clock()
            self.db.execute(
                "UPDATE inbox SET next_attempt_at = ?, last_error = ?, updated_at = ? WHERE update_id = ?",
                (now + seconds, str(error)[:500], now, update_id),
            )

    def fail(self, update_id, error):
        """Errore definitivo (per esempio Telegram rifiuta in modo permanente): niente tentativi."""
        with self.transaction():
            current = self._state(update_id)
            if current in FINAL:
                raise JournalError(f"{update_id}: è già {current}")
            self.db.execute(
                "UPDATE inbox SET state = 'failed', attempts = attempts + 1, last_error = ?,"
                " updated_at = ? WHERE update_id = ?",
                (str(error)[:500], self.clock(), update_id),
            )

    def counts(self, lane=None):
        """Quanti elementi per stato, in tutto o in una corsia, per /stato."""
        if lane is None:
            rows = self.db.execute("SELECT state, COUNT(*) AS n FROM inbox GROUP BY state").fetchall()
        else:
            rows = self.db.execute(
                "SELECT state, COUNT(*) AS n FROM inbox WHERE lane = ? GROUP BY state", (lane,)).fetchall()
        found = {r["state"]: r["n"] for r in rows}
        return {state: found.get(state, 0) for state in STATES}

    def _state(self, update_id):
        row = self.db.execute("SELECT state FROM inbox WHERE update_id = ?", (update_id,)).fetchone()
        if row is None:
            raise JournalError(f"{update_id} non è nel registro")
        return row[0]


def _entry(row):
    return Entry(
        update_id=row["update_id"], queue_id=row["queue_id"], received_at=row["received_at"],
        chat_id=row["chat_id"],
        message_id=row["message_id"], lane=row["lane"], state=row["state"],
        attempts=row["attempts"], next_attempt_at=row["next_attempt_at"],
        last_error=row["last_error"], reply_message_id=row["reply_message_id"],
        update=json.loads(row["payload"]),
    )
