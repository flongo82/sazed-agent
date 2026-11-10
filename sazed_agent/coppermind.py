"""coppermind, la memoria di sazed: il catalogo di ciò che l'utente ha voluto custodire.

Un unico file SQLite, lo stesso del registro degli ingressi, così che un salvataggio e il
cambio di stato del messaggio da cui nasce stiano in una sola transazione (transaction()).
Il modulo non dipende dal resto dell'agente: di Telegram conosce solo i riferimenti al
messaggio d'origine (chat_id, message_id), con cui il bot può riproporlo.

Ogni elemento nasce da un messaggio: una nota, un link (uno per ogni link del messaggio) o un
file (foto, video, vocale, audio, documento). Il salvataggio è idempotente: rifatto con gli
stessi dati non crea doppioni, e rifatto dopo una modifica del messaggio lo aggiorna. Lo stesso
link, o lo stesso file, mandato una seconda volta resta un elemento solo: le parole nuove che
lo accompagnano si aggiungono alla sua nota.

La lettura di link e file è un lavoro a parte, con i suoi tentativi: ogni elemento da leggere
ha lo stato "pending" e un istante per il prossimo tentativo (to_read, read_done, read_failed).
Di un link o di un file la descrizione (tipo, titolo, autori, argomenti, riassunto) nasce dal
contenuto letto, e un nuovo salvataggio del messaggio non la tocca; di una nota nasce dalle sue
parole. Un documento di norma si legge solo in parte (i dati del file e le prime pagine): tutto
il testo solo se l'utente lo chiede (read_in_full), e dentro un testo lungo passages trova i
punti che parlano di qualcosa.

Ricerca: filtri esatti (tipo, piattaforma, sito, ingresso, date), che danno elenchi completi e
il loro totale, più il testo con FTS5 (senza badare agli accenti, anche per inizio di parola).
E per significato (by_meaning): ogni elemento letto ha un vettore, calcolato fuori da qui
(to_mean, set_meaning) e legato al modello che l'ha prodotto; quando l'elemento cambia, il suo
vettore va ricalcolato. Il confronto è diretto, con numpy: per decine di migliaia di elementi
basta, senza indici vettoriali.
"""

import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass

import numpy
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

VERSION = 5
SOURCES = ("note", "link", "photo", "video", "video_note", "voice", "audio", "document")
FILES = SOURCES[2:]
EXTRACTIONS = ("pending", "complete", "partial", "failed", "unavailable")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS items (
  id          INTEGER PRIMARY KEY,
  chat_id     INTEGER NOT NULL,
  message_id  INTEGER NOT NULL,
  source      TEXT    NOT NULL CHECK (source IN {SOURCES}),
  origin      TEXT    UNIQUE,     -- link normalizzato o file: lo stesso è un elemento solo
  url         TEXT,
  platform    TEXT,               -- youtube, instagram, ..., web
  site        TEXT,               -- l'host senza www.
  kind        TEXT,               -- ricetta, articolo, video, ...: lo assegna il modello
  title       TEXT,
  authors     TEXT,               -- lista JSON: chi ha scritto un documento o un libro
  topics      TEXT,               -- lista JSON
  summary     TEXT,
  note        TEXT,               -- le parole dell'utente
  content     TEXT,               -- il testo estratto dalla fonte
  extraction  TEXT    NOT NULL CHECK (extraction IN {EXTRACTIONS}),
  place       TEXT,
  latitude    REAL,
  longitude   REAL,
  taken_at    INTEGER,
  media       TEXT,               -- JSON: file_id, nome, tipo, dimensione, durata
  sent_at     INTEGER NOT NULL,   -- quando l'utente l'ha mandato
  saved_at    REAL    NOT NULL,
  updated_at  REAL    NOT NULL,
  read_attempts INTEGER NOT NULL DEFAULT 0,
  next_read_at  REAL    NOT NULL DEFAULT 0,
  read_error    TEXT,
  read_full     INTEGER NOT NULL DEFAULT 0,  -- 1: l'utente vuole che si legga tutto
  retryable     INTEGER NOT NULL DEFAULT 0   -- 1: lettura fallita per un problema passeggero: si può riprovare
);
CREATE UNIQUE INDEX IF NOT EXISTS items_note ON items (chat_id, message_id) WHERE origin IS NULL;
CREATE INDEX IF NOT EXISTS items_message ON items (chat_id, message_id);
CREATE INDEX IF NOT EXISTS items_sent ON items (sent_at);
CREATE INDEX IF NOT EXISTS items_reading ON items (extraction, next_read_at);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, authors, topics, summary, note, content, url,
  content='items', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS items_fts_insert AFTER INSERT ON items BEGIN
  INSERT INTO items_fts (rowid, title, authors, topics, summary, note, content, url)
  VALUES (new.id, new.title, new.authors, new.topics, new.summary, new.note, new.content, new.url);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_delete AFTER DELETE ON items BEGIN
  INSERT INTO items_fts (items_fts, rowid, title, authors, topics, summary, note, content, url)
  VALUES ('delete', old.id, old.title, old.authors, old.topics, old.summary, old.note, old.content, old.url);
END;
CREATE TABLE IF NOT EXISTS meanings (
  item_id     INTEGER PRIMARY KEY,
  model       TEXT    NOT NULL,   -- il modello che ha calcolato il vettore
  vector      BLOB    NOT NULL,   -- float32, normalizzato
  updated_at  REAL    NOT NULL    -- items.updated_at quando si è calcolato: se l'elemento cambia, si ricalcola
);
CREATE TRIGGER IF NOT EXISTS meanings_delete AFTER DELETE ON items BEGIN
  DELETE FROM meanings WHERE item_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS items_fts_update AFTER UPDATE OF title, authors, topics, summary, note, content, url
ON items BEGIN
  INSERT INTO items_fts (items_fts, rowid, title, authors, topics, summary, note, content, url)
  VALUES ('delete', old.id, old.title, old.authors, old.topics, old.summary, old.note, old.content, old.url);
  INSERT INTO items_fts (rowid, title, authors, topics, summary, note, content, url)
  VALUES (new.id, new.title, new.authors, new.topics, new.summary, new.note, new.content, new.url);
END;
"""

# Da una versione dello schema alla successiva: ciò che manca a un database di prima. Dopo una
# migrazione l'indice del testo si ricostruisce da items (la versione 3 gli aggiunge gli autori).
MIGRATIONS = {
    2: ("ALTER TABLE items ADD COLUMN read_attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE items ADD COLUMN next_read_at REAL NOT NULL DEFAULT 0",
        "ALTER TABLE items ADD COLUMN read_error TEXT"),
    3: ("ALTER TABLE items ADD COLUMN authors TEXT",
        "ALTER TABLE items ADD COLUMN read_full INTEGER NOT NULL DEFAULT 0",
        "DROP TRIGGER items_fts_insert", "DROP TRIGGER items_fts_delete", "DROP TRIGGER items_fts_update",
        "DROP TABLE items_fts"),
    4: (),  # la tabella dei significati, nuova: la crea lo schema
    5: ("ALTER TABLE items ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0",),
}

# Peso di ogni colonna di items_fts nell'ordinamento per pertinenza (bm25).
WEIGHTS = (4.0, 3.0, 3.0, 2.0, 2.0, 1.0, 1.0)
PASSAGE = 1500   # caratteri di un passaggio di un testo lungo

# Le piattaforme che si riconoscono dall'indirizzo: nome, come si scrive, domini.
PLATFORMS = {
    "youtube": ("YouTube", ("youtube.com", "youtu.be", "youtube-nocookie.com")),
    "instagram": ("Instagram", ("instagram.com", "instagr.am")),
    "tiktok": ("TikTok", ("tiktok.com",)),
    "facebook": ("Facebook", ("facebook.com", "fb.com", "fb.watch")),
    "x": ("X", ("x.com", "twitter.com")),
    "reddit": ("Reddit", ("reddit.com", "redd.it")),
    "vimeo": ("Vimeo", ("vimeo.com",)),
    "spotify": ("Spotify", ("spotify.com",)),
    "github": ("GitHub", ("github.com",)),
    "linkedin": ("LinkedIn", ("linkedin.com",)),
}

# Parametri che servono solo a tracciare chi condivide: via dagli indirizzi salvati.
TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "yclid", "mc_cid", "mc_eid", "igsh", "igshid",
            "_hsenc", "_hsmi", "mkt_tok", "shem"}
PLATFORM_TRACKING = {"youtube": {"si", "feature", "pp"}, "spotify": {"si"}, "x": {"s", "t"},
                     "instagram": None, "tiktok": None}  # None: nessun parametro serve


class CoppermindError(Exception):
    """Database non utilizzabile da questa versione del modulo."""


@dataclass(frozen=True)
class Draft:
    """Un elemento da salvare, come lo ricava chi legge il messaggio; tipo, argomenti e
    riassunto (e il titolo, se c'è) li aggiunge il modello."""
    source: str
    url: str | None = None
    note: str | None = None
    title: str | None = None
    media: dict | None = None
    kind: str | None = None
    topics: tuple = ()
    summary: str | None = None
    look_up: bool = False   # una nota che nomina opere: resta da leggere, e la sua scheda nasce con i cataloghi

    @property
    def described(self):
        return self.kind is not None or self.summary is not None or bool(self.topics)


@dataclass(frozen=True)
class Item:
    id: int
    chat_id: int
    message_id: int
    source: str
    url: str
    platform: str
    site: str
    kind: str
    title: str
    authors: list
    topics: list
    summary: str
    note: str
    content: str
    extraction: str
    place: str
    latitude: float
    longitude: float
    taken_at: int
    media: dict
    sent_at: int
    saved_at: float
    read_error: str | None = None
    read_full: bool = False    # l'utente ha chiesto di leggerlo tutto
    retryable: bool = False    # la lettura è fallita per un problema passeggero: si può riprovare
    updated_at: float = 0.0    # l'ultimo cambiamento: il significato calcolato prima va rifatto


@dataclass(frozen=True)
class Outcome:
    """Che ne è stato di un elemento: "saved" se è di questo messaggio, "already" se c'era già."""
    item: Item
    status: str


@dataclass(frozen=True)
class Results:
    items: list
    total: int   # quanti elementi rispondono ai criteri, anche oltre il limite


@dataclass(frozen=True)
class Link:
    url: str        # l'indirizzo senza i parametri di tracciamento
    key: str        # la forma con cui si riconosce lo stesso link
    platform: str
    site: str


def connect(path):
    """Il database, pronto per un arresto improvviso: WAL e synchronous=FULL."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


class transaction:
    """BEGIN IMMEDIATE ... COMMIT, con ROLLBACK in caso di errore.

    Dentro una transazione già aperta non apre nulla: le operazioni annidate diventano parte
    di quella esterna, che decide per tutte.
    """

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.outer = self.db.in_transaction
        if not self.outer:
            self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, exc, tb):
        if not self.outer:
            self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


class Coppermind:
    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > VERSION:
            raise CoppermindError(f"database alla versione {version}, questo modulo arriva alla {VERSION}")
        with transaction(db):
            if version and db.execute("SELECT 1 FROM sqlite_master WHERE name = 'items'").fetchone():
                for step in range(version + 1, VERSION + 1):
                    for statement in MIGRATIONS.get(step, ()):
                        db.execute(statement)
            for statement in _statements(SCHEMA):
                db.execute(statement)
            if version < VERSION:
                db.execute("INSERT INTO items_fts (items_fts) VALUES ('rebuild')")
                db.execute(f"PRAGMA user_version = {VERSION}")

    def save(self, chat_id, message_id, sent_at, drafts):
        """Salva gli elementi di un messaggio e ne restituisce l'esito, nello stesso ordine.

        Rifatto con gli stessi dati non cambia nulla; dopo una modifica del messaggio aggiorna
        i suoi elementi e toglie quelli spariti. Di una nota, una descrizione nuova del modello
        sostituisce la vecchia; senza, se cambiano le parole, la vecchia decade. Di link e file
        la descrizione e il testo letto restano finché resta la fonte.
        """
        now = self.clock()
        drafts = unique(drafts)
        with transaction(self.db):
            kept = []
            for draft in drafts:
                origin = _origin(draft)
                row = self._holder(chat_id, message_id, origin)
                if row is not None and (row["chat_id"], row["message_id"]) != (chat_id, message_id):
                    self._add_note(row, draft.note, now)
                    continue
                values = _values(draft)
                if row is None:
                    kept.append(self.db.execute(
                        "INSERT INTO items (chat_id, message_id, source, origin, url, platform, site,"
                        " kind, title, topics, summary, note, media, extraction, sent_at, saved_at, updated_at,"
                        " next_read_at)"
                        " VALUES (:chat_id, :message_id, :source, :origin, :url, :platform, :site,"
                        " :kind, :title, :topics, :summary, :note, :media, :extraction, :sent_at, :now, :now, :now)",
                        dict(values, chat_id=chat_id, message_id=message_id, origin=origin,
                             sent_at=sent_at, now=now)).lastrowid)
                    continue
                self.db.execute(
                    "UPDATE items SET source = :source, url = :url, platform = :platform, site = :site,"
                    " media = :media, sent_at = :sent_at, updated_at = :now,"
                    " kind = CASE WHEN source != 'note' THEN kind WHEN :described THEN :kind"
                    "  WHEN note IS :note THEN kind END,"
                    " title = CASE WHEN source != 'note' THEN COALESCE(title, :title)"
                    "  WHEN :described OR note IS NOT :note THEN :title ELSE title END,"
                    " topics = CASE WHEN source != 'note' THEN topics WHEN :described THEN :topics"
                    "  WHEN note IS :note THEN topics END,"
                    " summary = CASE WHEN source != 'note' THEN summary WHEN :described THEN :summary"
                    "  WHEN note IS :note THEN summary END,"
                    " note = :note WHERE id = :id",
                    dict(values, sent_at=sent_at, now=now, id=row["id"], described=draft.described))
                kept.append(row["id"])
            marks = ", ".join("?" * len(kept))
            self.db.execute(f"DELETE FROM items WHERE chat_id = ? AND message_id = ? AND id NOT IN ({marks})",
                            (chat_id, message_id, *kept))
        return self.outcomes(chat_id, message_id, drafts)

    def outcomes(self, chat_id, message_id, drafts):
        """L'esito di un salvataggio già fatto, senza scrivere nulla: serve anche dopo un arresto."""
        found = []
        for draft in unique(drafts):
            row = self._holder(chat_id, message_id, _origin(draft))
            if row is not None:
                mine = (row["chat_id"], row["message_id"]) == (chat_id, message_id)
                found.append(Outcome(_item(row), "saved" if mine else "already"))
        return found

    def annotate(self, item_id, note):
        """Aggiunge parole dell'utente alla nota di un elemento (un commento mandato dopo).
        False se l'elemento non c'è."""
        row = self.db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return False
        with transaction(self.db):
            self._add_note(row, note, self.clock())
        return True

    def relink(self, item_id, url):
        """Porta un link al suo indirizzo vero (dietro un link breve). False, e nulla cambia, se
        quell'indirizzo è già di un altro elemento."""
        link = parse_link(url)
        with transaction(self.db):
            holder = self.db.execute("SELECT id FROM items WHERE origin = ?", ("link:" + link.key,)).fetchone()
            if holder is not None and holder[0] != item_id:
                return False
            self.db.execute("UPDATE items SET url = ?, platform = ?, site = ?, origin = ?, updated_at = ?"
                            " WHERE id = ? AND source = 'link'",
                            (link.url, link.platform, link.site, "link:" + link.key, self.clock(), item_id))
        return True

    def to_read(self, sources, first=()):
        """Il prossimo elemento da leggere adesso fra quelli di questi ingressi: prima quelli
        indicati in first (una domanda li aspetta), poi in ordine di arrivo. None se nessuno."""
        sources, first = tuple(sources), tuple(first)
        if not sources:
            return None
        row = self.db.execute(
            f"SELECT * FROM items WHERE extraction = 'pending' AND next_read_at <= ?"
            f" AND source IN ({', '.join('?' * len(sources))})"
            f" ORDER BY id IN ({', '.join('?' * len(first))}) DESC, saved_at, id LIMIT 1",
            (self.clock(), *sources, *first)).fetchone()
        return _item(row) if row else None

    def next_reading(self, sources):
        """L'istante del prossimo tentativo di lettura in programma, o None."""
        sources = tuple(sources)
        if not sources:
            return None
        return self.db.execute(
            f"SELECT MIN(next_read_at) FROM items WHERE extraction = 'pending'"
            f" AND source IN ({', '.join('?' * len(sources))})", sources).fetchone()[0]

    def read_done(self, item_id, extraction, content=None, title=None, kind=None, topics=(), summary=None,
                  authors=()):
        """Annota ciò che la lettura ha trovato: il testo e la descrizione che ne nasce. Con
        "unavailable" o "failed" (una fonte che non si legge) nulla di ciò che c'era si perde."""
        if extraction not in EXTRACTIONS or extraction == "pending":
            raise ValueError(f"esito di lettura non ammesso: {extraction}")
        with transaction(self.db):
            self.db.execute(
                "UPDATE items SET extraction = :extraction, content = COALESCE(:content, content),"
                " title = COALESCE(:title, title), kind = COALESCE(:kind, kind),"
                " authors = COALESCE(:authors, authors),"
                " topics = COALESCE(:topics, topics), summary = COALESCE(:summary, summary),"
                " read_error = NULL, retryable = 0, updated_at = :now WHERE id = :id",
                {"extraction": extraction, "content": content, "title": title, "kind": kind,
                 "authors": json.dumps(list(authors), ensure_ascii=False) if authors else None,
                 "topics": json.dumps(list(topics), ensure_ascii=False) if topics else None,
                 "summary": summary, "now": self.clock(), "id": item_id})

    def read_in_full(self, item_id):
        """L'utente vuole che un documento si legga tutto: torna da leggere, con i suoi
        tentativi da capo. Se è già letto tutto, o in lettura, nulla cambia. False se
        l'elemento non c'è o non è un documento."""
        with transaction(self.db):
            row = self.db.execute("SELECT source, read_full FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None or row["source"] != "document":
                return False
            if not row["read_full"]:
                self.db.execute(
                    "UPDATE items SET read_full = 1, extraction = 'pending', read_attempts = 0,"
                    " next_read_at = ?, read_error = NULL, updated_at = ? WHERE id = ?",
                    (self.clock(), self.clock(), item_id))
        return True

    def read_again(self, item_id, chat_id):
        """Una lettura fallita di un elemento della chat chat_id, che l'utente chiede di riprovare:
        torna da leggere, con i tentativi azzerati. False se non è fallita (o non c'è)."""
        with transaction(self.db):
            row = self.db.execute("SELECT extraction, chat_id FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None or row["extraction"] != "failed" or row["chat_id"] != chat_id:
                return False
            self.db.execute("UPDATE items SET extraction = 'pending', read_attempts = 0, next_read_at = ?,"
                            " read_error = NULL, retryable = 0, updated_at = ? WHERE id = ?",
                            (self.clock(), self.clock(), item_id))
        return True

    def readings(self, chat_id):
        """Come sono andate le letture della chat: quante sono da riprovare (fallite per un problema
        passeggero) e quante sono perse (fonte sparita, o illeggibile per sempre)."""
        retry, lost = self.db.execute(
            "SELECT COALESCE(SUM(extraction = 'failed' AND retryable), 0),"
            " COALESCE(SUM(extraction = 'unavailable' OR extraction = 'failed' AND NOT retryable), 0)"
            " FROM items WHERE chat_id = ?", (chat_id,)).fetchone()
        return {"retry": retry, "lost": lost}

    def to_retry(self, chat_id, limit=None):
        """Gli elementi della chat la cui lettura è da riprovare, i più recenti per primi."""
        rows = self.db.execute("SELECT * FROM items WHERE chat_id = ? AND extraction = 'failed' AND retryable"
                               " ORDER BY sent_at DESC, id DESC LIMIT ?", (chat_id, -1 if limit is None else limit))
        return [_item(r) for r in rows]

    def passages(self, item_id, text, limit=5):
        """I punti del testo letto di un elemento che parlano di più delle parole date (anche
        come inizio di parola, senza badare agli accenti), nell'ordine in cui vengono, come
        (posizione in percentuale, passaggio). Per un testo lungo, un libro letto tutto."""
        item = self.get(item_id)
        terms = [t for t in re.findall(r"\w+", _plain(text or "")) if len(t) >= 3] if item and item.content else []
        if not terms:
            return []
        pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, terms)) + ")")
        chunks, start = [], 0
        while start < len(item.content):  # passaggi di circa PASSAGE caratteri, tagliati a fine paragrafo
            end = min(len(item.content), start + PASSAGE)
            cut = item.content.rfind("\n", start + PASSAGE // 2, end) if end < len(item.content) else -1
            end = cut if cut > 0 else end
            chunks.append((start, item.content[start:end].strip()))
            start = end
        scored = []
        for start, chunk in chunks:
            found = pattern.findall(_plain(chunk))
            if found:  # prima i passaggi con più parole diverse, poi con più occorrenze
                scored.append((len(set(found)), len(found), start, chunk))
        best = sorted(sorted(scored, key=lambda s: (-s[0], -s[1], s[2]))[:limit], key=lambda s: s[2])
        return [(round(100 * start / len(item.content)), chunk) for *_, start, chunk in best]

    def read_failed(self, item_id, error, delays, min_delay=0, final=None):
        """Una lettura non è riuscita. Con final ("unavailable" o "failed") è l'esito definitivo;
        altrimenti si riprova dopo l'attesa successiva, e finite le attese l'esito è "failed", ma
        si può riprovare (retryable): il problema era passeggero. Restituisce lo stato di lettura
        risultante."""
        with transaction(self.db):
            row = self.db.execute("SELECT read_attempts FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                return None
            attempts, now = row[0] + 1, self.clock()
            retryable = final is None and attempts > len(delays)
            if retryable:
                final = "failed"
            next_at = now if final else now + max(delays[attempts - 1], min_delay or 0)
            self.db.execute(
                "UPDATE items SET read_attempts = ?, next_read_at = ?, read_error = ?,"
                " extraction = COALESCE(?, extraction), retryable = ?, updated_at = ? WHERE id = ?",
                (attempts, next_at, str(error)[:500], final, int(retryable), now, item_id))
        return final or "pending"

    def read_later(self, item_id, seconds, error):
        """Rimanda una lettura senza contare un tentativo: il problema non è della fonte (per
        esempio la chiave di Claude rifiutata)."""
        with transaction(self.db):
            self.db.execute("UPDATE items SET next_read_at = ?, read_error = ?, updated_at = ? WHERE id = ?",
                            (self.clock() + seconds, str(error)[:500], self.clock(), item_id))

    def get(self, item_id):
        row = self.db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _item(row) if row else None

    def of_message(self, chat_id, message_id):
        rows = self.db.execute("SELECT * FROM items WHERE chat_id = ? AND message_id = ? ORDER BY id",
                               (chat_id, message_id)).fetchall()
        return [_item(r) for r in rows]

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def search(self, text=None, *, kind=None, platform=None, site=None, source=None, extraction=None,
               since=None, until=None, words="all", limit=20, offset=0):
        """Gli elementi che rispondono a tutti i criteri dati, con il loro totale.

        kind, platform, source ed extraction accettano un valore o una lista; site vale anche
        per i sottodomini; since e until sono istanti Unix (since compreso, until escluso).
        Con il testo, words="all" vuole tutte le parole e "any" almeno una; ogni parola di tre
        lettere o più vale anche come inizio di parola, e l'ordine è per pertinenza. Senza
        testo, dal più recente. limit=None li restituisce tutti.
        """
        if words not in ("all", "any"):
            raise ValueError(f"words deve essere \"all\" o \"any\", non {words!r}")
        where, params = _filters(kind, platform, site, source, extraction, since, until)
        tables, order = "items", "items.sent_at DESC, items.id DESC"
        if text is not None and text.strip():
            match = _match(text, words)
            if match is None:  # niente parole da cercare, solo segni
                return Results([], 0)
            tables = "items JOIN items_fts ON items_fts.rowid = items.id"
            where.append("items_fts MATCH ?")
            params.append(match)
            order = f"bm25(items_fts, {', '.join(map(str, WEIGHTS))}), " + order
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.db.execute(f"SELECT COUNT(*) FROM {tables}{clause}", params).fetchone()[0]
        rows = self.db.execute(f"SELECT items.* FROM {tables}{clause} ORDER BY {order} LIMIT ? OFFSET ?",
                               (*params, -1 if limit is None else limit, offset)).fetchall()
        return Results([_item(r) for r in rows], total)

    def to_mean(self, model, limit=8):
        """Gli elementi di cui calcolare il significato con questo modello: già letti (o note) e
        senza un vettore, o con uno di un altro modello o di prima dell'ultimo cambiamento."""
        rows = self.db.execute(
            "SELECT items.* FROM items LEFT JOIN meanings ON meanings.item_id = items.id"
            " WHERE items.extraction != 'pending' AND (meanings.item_id IS NULL OR meanings.model != ?"
            " OR meanings.updated_at < items.updated_at) ORDER BY items.id LIMIT ?", (model, limit)).fetchall()
        return [_item(r) for r in rows]

    def set_meaning(self, item_id, model, vector, updated_at):
        """Il vettore di un elemento com'era a updated_at (il suo items.updated_at). Se nel frattempo
        l'elemento è sparito, nulla."""
        with transaction(self.db):
            if self.db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
                self.db.execute(
                    "INSERT INTO meanings (item_id, model, vector, updated_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(item_id) DO UPDATE SET model = excluded.model, vector = excluded.vector,"
                    " updated_at = excluded.updated_at",
                    (item_id, model, numpy.asarray(vector, dtype=numpy.float32).tobytes(), updated_at))

    def by_meaning(self, vector, model, *, kind=None, platform=None, site=None, source=None, since=None,
                   until=None, limit=10):
        """Gli elementi più vicini per significato a un vettore (normalizzato, dello stesso modello),
        fra quelli che rispondono ai filtri, come (elemento, somiglianza da -1 a 1), i più simili
        per primi."""
        where, params = _filters(kind, platform, site, source, None, since, until)
        rows = self.db.execute(
            "SELECT items.*, meanings.vector AS meaning FROM items JOIN meanings ON meanings.item_id = items.id"
            f" WHERE meanings.model = ?{''.join(' AND ' + w for w in where)}", (model, *params)).fetchall()
        if not rows:
            return []
        matrix = numpy.frombuffer(b"".join(r["meaning"] for r in rows), dtype=numpy.float32).reshape(len(rows), -1)
        scores = matrix @ numpy.asarray(vector, dtype=numpy.float32)
        best = numpy.argsort(-scores)[:limit]
        return [(_item(rows[i]), float(scores[i])) for i in best]

    def _holder(self, chat_id, message_id, origin):
        """L'elemento che ha già questa origine, o la nota di questo messaggio."""
        if origin is None:
            return self.db.execute("SELECT * FROM items WHERE chat_id = ? AND message_id = ? AND origin IS NULL",
                                   (chat_id, message_id)).fetchone()
        return self.db.execute("SELECT * FROM items WHERE origin = ?", (origin,)).fetchone()

    def _add_note(self, row, note, now):
        if note and note not in (row["note"] or ""):
            self.db.execute("UPDATE items SET note = ?, updated_at = ? WHERE id = ?",
                            (f"{row['note']}\n{note}" if row["note"] else note, now, row["id"]))


def parse_link(url):
    """Indirizzo pulito, chiave di riconoscimento, piattaforma e sito di un link.

    La chiave ignora protocollo, www., barra finale, frammento e ordine dei parametri, e
    riconduce le forme brevi (youtu.be) a quella completa: lo stesso video è lo stesso link.
    """
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = f":{parts.port}" if parts.port else ""
    except ValueError:  # indirizzo malformato (porta non valida, IPv6 rotto): si tiene com'è
        return Link(url, url, "web", None)
    site = _bare_host(host)
    platform = next((name for name, (_, domains) in PLATFORMS.items()
                     if any(site == d or site.endswith("." + d) for d in domains)), "web")
    dropped = PLATFORM_TRACKING.get(platform, set())
    params = [] if dropped is None else [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.startswith("utm_") and k not in TRACKING and k not in dropped]
    clean = urlunsplit((parts.scheme.lower(), host + port, parts.path, urlencode(params), parts.fragment))
    path = parts.path.rstrip("/")
    key_site, key_params = site, sorted(params)
    if platform == "youtube":
        video = dict(params).get("v")
        if site == "youtu.be" and path:
            path, video = "/watch", path.lstrip("/")
        key_site = "youtube.com"
        if path == "/watch" and video:
            key_params = [("v", video)]
    elif platform == "x":
        key_site = "x.com"
    key = key_site + port + path
    if key_params:
        key += "?" + urlencode(key_params)
    return Link(clean, key, platform, site)


def platform_label(platform):
    """Il nome della piattaforma come si scrive, o None per un sito qualunque."""
    return PLATFORMS.get(platform, (None,))[0]


def _bare_host(host):
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            return host[len(prefix):]
    return host


def _origin(draft):
    if draft.source == "link":
        return "link:" + parse_link(draft.url).key
    file_id = (draft.media or {}).get("file_unique_id")
    return f"file:{file_id}" if draft.source in FILES and file_id else None


def unique(drafts):
    """Una volta sola ciascuno, nell'ordine dato: un link ripetuto nel messaggio conta una volta."""
    seen, out = set(), []
    for draft in drafts:
        origin = _origin(draft) or ("note", draft.source)
        if origin not in seen:
            seen.add(origin)
            out.append(draft)
    return out


def _values(draft):
    if draft.source not in SOURCES:
        raise ValueError(f"ingresso sconosciuto: {draft.source}")
    link = parse_link(draft.url) if draft.source == "link" else None
    return {"source": draft.source, "url": link and link.url, "platform": link and link.platform,
            "site": link and link.site, "title": draft.title, "note": draft.note or None,
            "kind": draft.kind, "topics": json.dumps(list(draft.topics), ensure_ascii=False) if draft.topics else None,
            "summary": draft.summary,
            "media": json.dumps(draft.media, ensure_ascii=False) if draft.media else None,
            "extraction": "complete" if draft.source == "note" and not draft.look_up else "pending"}


def _match(text, words):
    """La query FTS5 per le parole di un testo: ogni parola tra virgolette, così nessun
    carattere diventa sintassi; da tre lettere in su vale anche come inizio di parola."""
    terms = re.findall(r"\w+", text)
    if not terms:
        return None
    return (" AND " if words == "all" else " OR ").join(
        f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in terms)


def _item(row):
    return Item(
        id=row["id"], chat_id=row["chat_id"], message_id=row["message_id"], source=row["source"],
        url=row["url"], platform=row["platform"], site=row["site"], kind=row["kind"], title=row["title"],
        authors=json.loads(row["authors"]) if row["authors"] else [],
        topics=json.loads(row["topics"]) if row["topics"] else [], summary=row["summary"],
        note=row["note"], content=row["content"], extraction=row["extraction"], place=row["place"],
        latitude=row["latitude"], longitude=row["longitude"], taken_at=row["taken_at"],
        media=json.loads(row["media"]) if row["media"] else {}, sent_at=row["sent_at"],
        saved_at=row["saved_at"], read_error=row["read_error"], read_full=bool(row["read_full"]),
        retryable=bool(row["retryable"]),
        updated_at=row["updated_at"],
    )


def _filters(kind, platform, site, source, extraction, since, until):
    """Le condizioni SQL dei filtri esatti, con i loro parametri."""
    where, params = [], []
    for column, value in (("kind", kind), ("platform", platform), ("source", source), ("extraction", extraction)):
        if value is not None:
            values = (value,) if isinstance(value, str) else tuple(value)
            where.append(f"items.{column} IN ({', '.join('?' * len(values))})")
            params += values
    if site:
        site = _bare_host(site.strip().lower())
        where.append("(items.site = ? OR substr(items.site, -?) = ?)")
        params += [site, len(site) + 1, "." + site]
    if since is not None:
        where.append("items.sent_at >= ?")
        params.append(since)
    if until is not None:
        where.append("items.sent_at < ?")
        params.append(until)
    return where, params


def _plain(text):
    """Il testo in minuscolo e senza accenti, per confrontare parole come fa la ricerca."""
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c))


def _statements(script):
    """Le istruzioni di uno script SQL, una per una: executescript chiuderebbe la transazione.
    I trigger contengono punti e virgola: un'istruzione finisce solo dove sqlite3 la dà completa."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement.strip()
            statement = ""
