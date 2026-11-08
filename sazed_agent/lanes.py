"""Le corsie dell'agente: ogni messaggio preso in carico subito, i contenuti letti con calma, e
le domande che non aspettano dietro una lettura lunga.

- comandi: /stato, risposto subito;
- ingresso (stati received e read): 👍 appena il messaggio arriva, poi link e file in
  coppermind (read); Claude dice a che cosa servono le parole, nel filo della conversazione:
  una nota, un commento a elementi di questo messaggio o di uno di poco prima, una domanda; il
  codice salva note e commenti (saved). Se non c'è nulla da rispondere, il messaggio è finito.
  Le parole dette in un vocale (o in un videomessaggio) valgono come quelle scritte: il
  messaggio aspetta la trascrizione, al massimo listen_wait secondi, poi passa da Claude; un
  vocale che porta solo una domanda o un commento non resta in coppermind;
- risposte (stati saved e answered): una domanda aspetta che siano letti gli elementi di cui
  parla, al massimo read_wait secondi dal suo arrivo, poi Claude risponde cercando in
  coppermind. Da qui passano anche le risposte fisse (comando sconosciuto, messaggio che non si
  sa custodire);
- lettura: un elemento alla volta, prima quelli che qualcuno aspetta (un vocale da
  interpretare, ciò di cui parla una domanda). I link si leggono alla fonte, e dei video di
  YouTube anche descrizione e sottotitoli (o l'audio); i file si scaricano da Telegram in una
  cartella temporanea: le foto Claude le guarda, vocali, audio e video si ascoltano (e dei video
  si prendono anche dei fotogrammi). Dei documenti di norma si leggono solo i dati del file e le
  prime pagine; se non dicono di che cosa parla un'opera pubblicata, si cerca la sua scheda in
  un catalogo libero. Tutto il testo solo se l'utente lo chiede: la richiesta è una domanda,
  che ha risposta a lettura finita.
  Dal contenuto Claude ricava tipo, titolo, argomenti e riassunto. Una fonte che non si legge
  mette 🤷 sul messaggio.

Ogni passo è scritto nel registro (o in coppermind, per la lettura) prima del successivo, così
un arresto in qualunque punto riprende dallo stato registrato; salvataggio e cambio di stato
stanno in una transazione. Una risposta già annotata nel registro non si rimanda; se l'agente si
ferma dopo l'invio ma prima di annotarla, alla ripresa la rimanda: meglio un doppione che un
silenzio. Le reazioni sono un segnale, non il contenuto: se Telegram ne rifiuta una, si va avanti.
"""

import asyncio
import contextlib
import dataclasses
import logging
import tempfile
import time
from pathlib import Path

from . import intake
from .claude import ClaudeError, Interpretation, view
from .coppermind import Coppermind, Draft, parse_link, unique
from .messages import STATUS_COMMANDS, language_for, messages_for
from .reader import IMAGE_TYPES, MAX_IMAGE, Extract, ReadError
from .telegram import FILE_MAX, TelegramError
from .videos import PLATFORMS as VIDEO_PLATFORMS

log = logging.getLogger("sazed_agent.lanes")

ACTIVE = ("received", "read", "saved", "answered")
INTAKE, ANSWERS = ("received", "read"), ("saved", "answered")
THREAD_WINDOW, THREAD_LIMIT = 3 * 3600, 10   # il filo: i messaggi delle ultime ore, fino a dieci
READ_WAIT = 600     # quanto una domanda aspetta la lettura degli elementi di cui parla
LISTEN_WAIT = 3600  # quanto un vocale aspetta la trascrizione, anche dietro un video lungo
# Ciò che si sa leggere, e con che cosa: i link con il lettore, le foto con Claude (che le
# guarda), vocali, audio e video con chi ascolta (Whisper), i documenti con chi li sfoglia.
LINKS, PICTURES, RECORDINGS = ("link",), ("photo",), ("voice", "audio", "video", "video_note")
PAPERS = ("document",)
SPOKEN = ("voice", "video_note")  # l'utente che parla, se non sono inoltrati


def lane_for(update):
    """"commands" per il comando di stato in qualunque lingua, "acquisition" per tutto il resto."""
    text = (_message(update).get("text") or "").strip()
    if not text.startswith("/"):
        return "acquisition"
    command = text.split()[0].split("@")[0].lower()
    return "commands" if command in STATUS_COMMANDS else "acquisition"


def _message(update):
    return update.get("message") or update.get("edited_message") or {}


class Processor:
    def __init__(self, journal, telegram, bot_name, language, reactions, retry_delays,
                 typing_interval=4.0, idle_wait=60.0, config_pause=60.0, clock=time.time, memory=None,
                 claude=None, reader=None, read_wait=READ_WAIT, listener=None, videos=None,
                 listen_wait=LISTEN_WAIT, documents=None, lookup=None):
        self.journal, self.telegram = journal, telegram
        self.memory = memory or Coppermind(journal.db, clock)  # nello stesso database del registro
        self.claude = claude  # senza, le parole accompagnano i link o sono una nota, e nulla si descrive
        self.reader = reader  # senza, i link non si leggono
        self.listener = listener  # senza, vocali, audio e video non si ascoltano
        self.videos = videos  # senza, dei video di YouTube resta l'anteprima
        self.documents = documents  # senza, i documenti non si leggono
        self.lookup = lookup  # senza, di un libro senza abstract resta ciò che dicono le prime pagine
        self.readable = ((LINKS if reader is not None else ()) + (PICTURES if claude is not None else ())
                         + (RECORDINGS if listener is not None else ()) + (PAPERS if documents is not None else ()))
        self.bot_name, self.language, self.reactions = bot_name, language, reactions
        self.retry_delays, self.typing_interval, self.idle_wait = retry_delays, typing_interval, idle_wait
        self.config_pause = config_pause  # attesa dopo un token rifiutato, senza contare tentativi
        self.clock, self.read_wait, self.listen_wait = clock, read_wait, listen_wait
        self.wake = {lane: asyncio.Event() for lane in ("commands", "intake", "answers", "reading")}

    def notify(self):
        """C'è lavoro nuovo nel registro: sveglia le corsie."""
        for event in self.wake.values():
            event.set()

    async def run(self, stop):
        await asyncio.gather(self._lane("commands", stop), self._lane("intake", stop),
                             self._lane("answers", stop), self._reading(stop))

    async def _lane(self, lane, stop):
        pick, handle = {"commands": (self._next_command, self._command),
                        "intake": (self._next_intake, self._intake),
                        "answers": (self._next_answer, self._answer)}[lane]
        while not stop.is_set():
            self.wake[lane].clear()  # prima di guardare il registro: un avviso dopo non si perde
            entry, later = pick()
            if entry is not None:
                await self._attempt(entry, handle)
            else:
                await self._sleep(lane, stop, later)

    def _next_command(self):
        due = self.journal.due("commands", limit=1)
        return (due[0], None) if due else (None, self.journal.next_attempt("commands"))

    def _next_intake(self):
        return self._next(INTAKE, self._listening, self.listen_wait)

    def _next_answer(self):
        return self._next(ANSWERS, self._waiting, self.read_wait)

    def _next(self, states, waiting, wait):
        """Il primo messaggio pronto fra quelli in questi stati; se nessuno lo è, quando guardare
        di nuovo: al prossimo tentativo in programma o allo scadere di un'attesa (waiting dice
        se un messaggio aspetta ancora una lettura, al massimo wait secondi dal suo arrivo)."""
        now, later = self.clock(), []
        for entry in self.journal.pending("acquisition", states):
            if entry.next_attempt_at > now:
                later.append(entry.next_attempt_at)
            elif waiting(entry):
                later.append(entry.received_at + wait)
            else:
                return entry, None
        return None, min(later, default=None)

    async def _sleep(self, lane, stop, at):
        """Fino a at, a un avviso di lavoro nuovo o allo stop, e comunque non oltre idle_wait."""
        timeout = self.idle_wait if at is None else max(0.0, min(self.idle_wait, at - self.clock()))
        waits = [asyncio.create_task(self.wake[lane].wait()), asyncio.create_task(stop.wait())]
        await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for task in waits:
            task.cancel()
        await asyncio.gather(*waits, return_exceptions=True)

    async def _attempt(self, entry, handle):
        try:
            await handle(entry)
            return
        except (TelegramError, ClaudeError) as err:
            if err.config_error:
                log.error("elemento %s rimandato di %g s: %s", entry.update_id, self.config_pause, err)
                self.journal.postpone(entry.update_id, self.config_pause, err)
                return
            if err.permanent:
                self.journal.fail(entry.update_id, err)
                state = "failed"
            else:
                state = self.journal.retry_later(entry.update_id, err, self.retry_delays, err.retry_after or 0)
            log.warning("elemento %s: %s (%s)", entry.update_id, err,
                        "definitivo" if err.permanent else "si riproverà" if state != "failed" else "tentativi finiti")
        except Exception as err:  # un difetto nel codice non deve fermare la corsia
            log.exception("elemento %s: errore inatteso", entry.update_id)
            state = self.journal.retry_later(entry.update_id, f"errore inatteso: {err!r}", self.retry_delays)
        if state == "failed":
            await self._report_failure(entry)

    # --- ingresso ---

    async def _intake(self, entry):
        if entry.chat_id is None:  # niente a cui rispondere (per esempio un callback)
            self.journal.advance(entry.update_id, "done")
            log.info("elemento %s completato, senza una chat a cui rispondere", entry.update_id)
            return
        reading = intake.read(entry.update)
        if entry.state == "received":
            await self._react(entry.chat_id, entry.message_id, self.reactions.received)
            links = entry.links if entry.links is not None else await self._resolve(reading)
            with self.journal.transaction():
                self.memory.save(entry.chat_id, entry.message_id, self._sent_at(entry, reading),
                                 self._drafts(reading, links))
                self.journal.set_links(entry.update_id, links)
                self.journal.advance(entry.update_id, "read")
            self.wake["reading"].set()
            entry = self.journal.get(entry.update_id)
            if self._listening(entry):
                log.info("elemento %s: le parole del vocale aspettano la trascrizione", entry.update_id)
                return
        pairs = self._pairs(entry, reading)
        spoken = self._spoken(entry, reading)
        meaning = Interpretation.from_dict(entry.interpretation) if entry.interpretation else None
        if meaning is None:
            heard = spoken.content if spoken is not None and spoken.extraction in ("complete", "partial") else None
            meaning = await self._interpret(entry, reading, [item for _, item in pairs], heard)
            self.journal.set_interpretation(entry.update_id, meaning.to_dict())
        mine = {item.id for _, item in pairs}
        forget = spoken is not None and not meaning.keep_spoken and spoken.id not in meaning.comment_on
        if forget:  # senza l'elemento fra quelli da salvare, save lo toglie
            pairs = [(d, item) for d, item in pairs if item.id != spoken.id]
        with self.journal.transaction():
            drafts = [dataclasses.replace(d, note=meaning.comment if item.id in meaning.comment_on else None)
                      for d, item in pairs]
            if meaning.note:
                d = meaning.description
                described = {k: getattr(d, k) for k in ("kind", "title", "topics", "summary")} if d else {}
                drafts.append(Draft("note", note=meaning.note, **described))
            self.memory.save(entry.chat_id, entry.message_id, self._sent_at(entry, reading), drafts)
            for item_id in meaning.comment_on:
                if item_id not in mine:
                    self.memory.annotate(item_id, meaning.comment)
            for item_id in meaning.read_in_full:  # la domanda che lo chiede aspetta la lettura
                self.memory.read_in_full(item_id)
            self.journal.advance(entry.update_id, "saved")
        if meaning.read_in_full:
            self.wake["reading"].set()
        log.info("elemento %s in coppermind: %d link e file, %s%s", entry.update_id, len(pairs), _summary(meaning),
                 "; il vocale portava solo parole e non si custodisce" if forget else "")
        if self._needs_reply(reading, meaning):
            self.wake["answers"].set()
        else:
            self.journal.advance(entry.update_id, "done")
            log.info("elemento %s completato, senza bisogno di risposta", entry.update_id)

    async def _resolve(self, reading):
        """Gli indirizzi veri dei link brevi. Se un rimando non si segue, il link resta com'è."""
        links = {}
        for draft in reading.drafts if self.reader is not None else ():
            if draft.source != "link" or draft.url in links:
                continue
            try:
                resolved = await self.reader.resolve(draft.url)
            except ReadError as err:
                log.warning("link breve non risolto, resta com'è: %s", err)
                continue
            if resolved != draft.url:
                links[draft.url] = resolved
        return links

    def _drafts(self, reading, links):
        return unique([dataclasses.replace(d, url=links.get(d.url, d.url)) if d.source == "link" else d
                       for d in reading.drafts])

    def _pairs(self, entry, reading):
        """Link e file del messaggio, ciascuno con il suo elemento in coppermind (anche quando
        c'era già, da un messaggio di prima)."""
        return [(d, o.item) for d in self._drafts(reading, entry.links or {})
                for o in self.memory.outcomes(entry.chat_id, entry.message_id, [d])]

    async def _interpret(self, entry, reading, items, spoken=None):
        """A che cosa servono le parole, scritte e dette (spoken, la trascrizione del vocale).
        Senza Claude le parole accompagnano link e file, o sono una nota."""
        if reading.command or not (reading.words or spoken):
            return Interpretation()
        if self.claude is None:
            return Interpretation.plain(reading.words, [i.id for i in items])
        meaning = await self.claude.interpret(self._thread(entry, reading), reading.words, items,
                                              self._language(entry.update), reading.reply_to, spoken)
        log.info("elemento %s: per Claude %s", entry.update_id, _summary(meaning))
        return self._named(meaning)

    def _named(self, meaning):
        """Un documento da leggere tutto, chiamato per nome: si cerca fra i documenti. Se è uno
        solo si legge; se sono di più, la risposta chiederà quale."""
        if not meaning.read_in_full_named:
            return meaning
        found = self.memory.search(meaning.read_in_full_named, source="document", limit=3)
        if not found.items:
            found = self.memory.search(meaning.read_in_full_named, source="document", words="any", limit=3)
        ids = tuple(i.id for i in found.items)
        return dataclasses.replace(meaning, read_in_full=ids if len(ids) == 1 else (),
                                   about=tuple(dict.fromkeys(meaning.about + ids)))

    def _needs_reply(self, reading, meaning):
        if reading.edited:
            return False  # una modifica aggiorna ciò che si custodisce, senza rispondere di nuovo
        nothing = not reading.drafts and not reading.words
        return bool(reading.command or meaning.question or nothing)

    def _sent_at(self, entry, reading):
        return reading.sent_at if reading.sent_at is not None else int(entry.received_at)

    def _spoken(self, entry, reading):
        """Il vocale (o videomessaggio) in cui l'utente parla in questo messaggio, come elemento
        di coppermind. None se non c'è, se è inoltrato o se c'era già da un messaggio di prima."""
        if reading.forwarded:
            return None
        return next((item for d, item in self._pairs(entry, reading) if d.source in SPOKEN
                     and (item.chat_id, item.message_id) == (entry.chat_id, entry.message_id)), None)

    def _listening(self, entry):
        """Il vocale di cui l'interpretazione aspetta ancora la trascrizione, o None. Si aspetta
        solo se c'è Claude a interpretare, e al massimo listen_wait secondi."""
        if (entry.state != "read" or entry.interpretation or self.claude is None
                or self.clock() >= entry.received_at + self.listen_wait):
            return None
        item = self._spoken(entry, intake.read(entry.update))
        return item if item is not None and self._unread(item.id) else None

    # --- il filo della conversazione ---

    def _thread(self, entry, reading):
        """I messaggi di poco prima, dal più vecchio, con le parole, gli elementi e la risposta
        di sazed; più quello a cui l'utente ha risposto, se è più lontano."""
        past = self.journal.recent(entry.chat_id, entry.received_at, entry.received_at - THREAD_WINDOW,
                                   THREAD_LIMIT)
        if reading.reply_to and all(e.message_id != reading.reply_to for e in past):
            replied = self.journal.of_message(entry.chat_id, reading.reply_to)
            if replied is not None and replied.received_at < entry.received_at:
                past.append(replied)
        latest = {}
        for e in sorted(past, key=lambda e: (e.received_at, e.update_id)):
            if e.message_id != entry.message_id:
                latest[e.message_id] = e  # di un messaggio modificato conta l'ultima versione
        return [self._thread_message(e, entry.received_at)
                for e in sorted(latest.values(), key=lambda e: (e.received_at, e.update_id))]

    def _thread_message(self, e, now):
        reading = intake.read(e.update)
        out = {"message": e.message_id, "minutes_ago": round((now - e.received_at) / 60)}
        if reading.words:
            out["words"] = reading.words
        if reading.command:
            out["command"] = reading.command
        spoken = Interpretation.from_dict(e.interpretation).spoken if e.interpretation else None
        if spoken:
            out["spoken"] = spoken
        items = [item for _, item in self._pairs(e, reading)]
        items += [i for i in self.memory.of_message(e.chat_id, e.message_id) if i.source == "note"]
        if items:
            out["items"] = [view(i) for i in items]
        if e.reply_text:
            out["sazed"] = e.reply_text
        return out

    # --- risposte ---

    def _waiting(self, entry):
        """True se è una domanda che aspetta ancora la lettura di ciò di cui parla."""
        if entry.state != "saved" or not entry.interpretation:
            return False
        meaning = Interpretation.from_dict(entry.interpretation)
        if not meaning.question or self.clock() >= entry.received_at + self.read_wait:
            return False
        return any(self._unread(item_id) for item_id in meaning.about)

    def _unread(self, item_id):
        item = self.memory.get(item_id)
        return item is not None and item.extraction == "pending" and item.source in self.readable

    async def _answer(self, entry):
        state = entry.state
        if state == "saved":
            reading = intake.read(entry.update)
            meaning = Interpretation.from_dict(entry.interpretation) if entry.interpretation else Interpretation()
            if not self._needs_reply(reading, meaning):  # arresto fra il salvataggio e la fine
                self.journal.advance(entry.update_id, "done")
                log.info("elemento %s completato, senza bisogno di risposta", entry.update_id)
                return
            if entry.reply_message_id is None:
                t, hello = self._voice(entry)
                text = await self._reply_text(entry, reading, meaning, t)
                reply_id = await self.telegram.send_message(entry.chat_id, f"{hello}, {text}",
                                                            reply_to=entry.message_id)
                self.journal.set_reply(entry.update_id, reply_id, text)
            self.journal.advance(entry.update_id, "answered")
            state = "answered"
        if state == "answered":
            self.journal.advance(entry.update_id, "done")
            log.info("elemento %s completato: risposta inviata", entry.update_id)

    async def _reply_text(self, entry, reading, meaning, t):
        if reading.command:
            return t["unknown_command"](reading.command)
        if not meaning.question or self.claude is None:
            return t["unsupported"]
        about = [i for i in (self.memory.get(item_id) for item_id in meaning.about) if i is not None]
        today = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        async with self._typing(entry.chat_id):
            text = await self.claude.answer(self._thread(entry, reading), meaning.question, about,
                                            self._language(entry.update), self.memory, today, bool(meaning.spoken))
        return text or t["no_answer"]

    # --- lettura ---

    async def _reading(self, stop):
        while not stop.is_set():
            self.wake["reading"].clear()
            item = self.memory.to_read(self.readable, first=self._awaited())
            if item is not None:
                await self._read(item)
            else:
                await self._sleep("reading", stop, self.memory.next_reading(self.readable))

    def _awaited(self):
        """Gli elementi che qualcuno aspetta, da leggere per primi: i vocali di cui si attendono
        le parole e ciò di cui parlano le domande in attesa."""
        ids = []
        for entry in self.journal.pending("acquisition", ("read", "saved")):
            if entry.state == "read":
                listened = self._listening(entry)
                ids += [listened.id] if listened is not None else []
            elif entry.interpretation:
                meaning = Interpretation.from_dict(entry.interpretation)
                ids += meaning.about if meaning.question else ()
        return ids

    async def _read(self, item):
        try:
            extract = await (self._read_link(item) if item.source == "link" else self._read_file(item))
        except Exception as err:  # anche un difetto nel codice: la corsia va avanti
            if not isinstance(err, (ReadError, TelegramError)):  # il download di un file da Telegram
                log.exception("lettura dell'elemento %s: errore inatteso", item.id)
            final = "unavailable" if getattr(err, "unavailable", False) else "failed" if getattr(
                err, "permanent", False) else None
            outcome = self.memory.read_failed(item.id, err, self.retry_delays, final=final)
            log.warning("lettura dell'elemento %s: %s (%s)", item.id, err,
                        "si riproverà" if outcome == "pending" else outcome)
            if outcome != "pending":
                await self._react(item.chat_id, item.message_id, self.reactions.unreadable)
                self._read_over()
            return
        if not item.read_full and getattr(self.memory.get(item.id), "read_full", False):
            log.info("elemento %s: ora va letto tutto, la lettura ricomincia", item.id)
            self.wake["reading"].set()  # l'utente l'ha chiesto mentre lo si sfogliava
            return
        description = None
        if self.claude is not None and (extract.text or extract.title or extract.images):
            try:
                description = await self._describe(item, extract)
            except ClaudeError as err:
                if err.config_error:
                    log.error("lettura dell'elemento %s rimandata di %g s: %s", item.id, self.config_pause, err)
                    self.memory.read_later(item.id, self.config_pause, err)
                    return
                if not err.permanent and self.memory.read_failed(
                        item.id, err, self.retry_delays, err.retry_after or 0) == "pending":
                    log.warning("descrizione dell'elemento %s: %s (si riproverà)", item.id, err)
                    return
                log.warning("elemento %s custodito senza descrizione: %s", item.id, err)
        d = description
        self.memory.read_done(item.id, "complete" if extract.complete else "partial", content=extract.text,
                              title=(d and d.title) or extract.title, kind=d and d.kind,
                              topics=d.topics if d else (), summary=d and d.summary, authors=d.authors if d else ())
        log.info("elemento %s letto (%s)%s", item.id, "completo" if extract.complete else "in parte",
                 "" if d else ", senza descrizione")
        self._read_over()

    async def _describe(self, item, extract):
        """La descrizione di Claude. Di un'opera pubblicata letta solo in parte, se le prime pagine
        non dicono di che cosa parla, Claude riguarda il documento con le schede trovate in rete."""
        language = self._item_language(item)
        whole = item.source in PAPERS and item.read_full
        d = await self.claude.describe(item, extract, language, whole=whole)
        if d is None or whole or not d.published or d.summary or not d.title or self.lookup is None:
            return d
        found = await self.lookup.find(d.title, d.authors, d.kind, language)
        if not found:
            return d
        log.info("elemento %s: %d schede trovate in rete", item.id, len(found))
        return await self.claude.describe(item, extract, language, found=found) or d

    def _read_over(self):
        """Una lettura è finita: chi la aspettava (un vocale da interpretare, una domanda) riparte."""
        self.wake["intake"].set()
        self.wake["answers"].set()

    async def _read_link(self, item):
        resolved = await self.reader.resolve(item.url)  # un link breve rimasto tale all'ingresso
        if resolved != item.url and self.memory.relink(item.id, resolved):
            log.info("elemento %s: link breve portato al suo indirizzo", item.id)
        extract = await self.reader.read(resolved)
        if self.videos is not None and parse_link(resolved).platform in VIDEO_PLATFORMS:
            try:
                extract = await self.videos.read(resolved, extract)
            except ReadError as err:  # YouTube che blocca, yt-dlp vecchio: riprovare subito non aiuta
                log.warning("elemento %s: del video resta l'anteprima (%s)", item.id, err)
        return extract

    async def _read_file(self, item):
        """Scarica il file da Telegram in una cartella temporanea e lo legge: una foto la
        guarda Claude, un documento si sfoglia (o si legge tutto), il resto si ascolta. Un
        documento che è un'immagine, un audio o un video si tratta come tale."""
        media = item.media or {}
        if (media.get("file_size") or 0) > FILE_MAX:
            raise ReadError("file oltre i 20 MB dell'API dei bot", permanent=True)
        mime = media.get("mime_type") or ""
        with tempfile.TemporaryDirectory(prefix="sazed-") as work:
            path = Path(work) / "file"
            await self.telegram.download(await self.telegram.get_file(media["file_id"]), path)
            if item.source in PICTURES or (item.source in PAPERS and mime in IMAGE_TYPES and self.claude
                                           and path.stat().st_size <= MAX_IMAGE):
                return Extract(None, None, None, True, ((mime or "image/jpeg", path.read_bytes()),))
            if item.source in PAPERS and not (mime.startswith(("audio/", "video/")) and self.listener):
                return await self.documents.read(path, media.get("file_name"), mime, full=item.read_full)
            return await self.listener.listen(path, video=item.source in ("video", "video_note")
                                              or mime.startswith("video/"))

    def _item_language(self, item):
        entry = self.journal.of_message(item.chat_id, item.message_id)
        return self._language(entry.update if entry else {})

    # --- comandi, errori, voce ---

    async def _command(self, entry):
        if entry.reply_message_id is None:
            t, hello = self._voice(entry)
            counts = self.journal.counts("acquisition")
            pending = sum(counts[state] for state in ACTIVE)
            text = f"{hello}, {t['status'](self.bot_name, pending, counts['failed'], self.memory.count())}"
            reply_id = await self.telegram.send_message(entry.chat_id, text, reply_to=entry.message_id)
            self.journal.set_reply(entry.update_id, reply_id)
        self.journal.advance(entry.update_id, "done")
        log.info("elemento %s completato: stato inviato", entry.update_id)

    async def _report_failure(self, entry):
        """Avvisa una volta sola che un messaggio è stato messo da parte. Se non riesce, lo annota."""
        if entry.chat_id is None:
            return
        failed = self.journal.get(entry.update_id)
        t, hello = self._voice(entry)
        try:
            await self.telegram.send_message(entry.chat_id, f"{hello}, {t['failed'](failed.attempts)}",
                                             reply_to=entry.message_id)
        except TelegramError as err:
            log.warning("avviso di errore per %s non inviato: %s", entry.update_id, err)
        await self._react(entry.chat_id, entry.message_id, self.reactions.failed)

    def _language(self, update):
        return language_for(self.language, (_message(update).get("from") or {}).get("language_code"))

    def _voice(self, entry):
        """I testi nella lingua di chi scrive, e il saluto con il suo nome preso da Telegram."""
        sender = _message(entry.update).get("from") or {}
        t = messages_for(self.language, sender.get("language_code"))
        return t, t["hello"](sender.get("first_name"))

    async def _react(self, chat_id, message_id, emoji):
        if chat_id is None or message_id is None:
            return
        try:
            await self.telegram.set_reaction(chat_id, message_id, emoji)
        except TelegramError as err:
            log.warning("reazione %s non impostata sul messaggio %s: %s", emoji, message_id, err)

    @contextlib.asynccontextmanager
    async def _typing(self, chat_id):
        """"Sta scrivendo..." finché il lavoro dura: Telegram lo mostra per 5 secondi al massimo."""
        with contextlib.suppress(TelegramError):
            await self.telegram.send_typing(chat_id)

        async def keep_typing():
            while True:
                await asyncio.sleep(self.typing_interval)
                with contextlib.suppress(TelegramError):
                    await self.telegram.send_typing(chat_id)
        task = asyncio.create_task(keep_typing())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _summary(meaning):
    """Che cosa ha capito Claude, senza le parole dell'utente: per i log."""
    parts = [name for name, present in (("nota", meaning.note), ("commento", meaning.comment),
                                        ("domanda", meaning.question),
                                        ("lettura completa", meaning.read_in_full or meaning.read_in_full_named))
             if present]
    return ", ".join(parts) or "niente da custodire nelle parole"
