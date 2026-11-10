"""Le corsie dell'agente: ogni messaggio preso in carico subito, i contenuti letti con calma, e
le domande che non aspettano dietro una lettura lunga.

- comandi: /stato e /bilancio, risposti subito, e i tocchi sui pulsanti: Riprova, sotto
  l'avviso di un messaggio messo da parte o di una fonte non letta per un problema passeggero
  (il messaggio o la lettura ripartono da capo, con i tentativi azzerati), e sotto il bilancio
  Mostrameli (sazed risponde a ciascuno da riprovare, così toccando la citazione lo si ritrova)
  e Riprova tutti;
- ingresso (stati received e read): 👍 appena il messaggio arriva, poi link e file in
  coppermind (read); Claude dice a che cosa servono le parole, nel filo della conversazione:
  una nota, un commento a elementi di questo messaggio o di uno di poco prima, una domanda; il
  codice salva note e commenti (saved). Se non c'è nulla da rispondere, il messaggio è finito.
  Le parole dette in un vocale (o in un videomessaggio) valgono come quelle scritte: il
  messaggio aspetta la trascrizione, al massimo listen_wait secondi, poi passa da Claude; un
  vocale che porta solo una domanda o un commento non resta in coppermind;
- risposte (stati saved e answered): una domanda aspetta che siano letti gli elementi di cui
  parla, al massimo read_wait secondi dal suo arrivo, poi Claude risponde cercando in
  coppermind. Se l'utente chiede un file, dopo la risposta il codice gliene rimanda il messaggio
  originale (copiato; se non c'è più, il file dal suo file_id). Da qui passano anche le risposte
  fisse (comando sconosciuto, messaggio che non si sa custodire);
- significati: per ogni elemento letto o cambiato, il vettore della sua scheda, con il modello
  di embedding (se c'è), per cercare anche per significato; uno alla volta, a priorità bassa;
- lettura: un elemento alla volta, prima quelli che qualcuno aspetta (un vocale da
  interpretare, ciò di cui parla una domanda). I link si leggono alla fonte, e dei video di
  YouTube anche descrizione e sottotitoli (o l'audio); di un reel di Instagram prima didascalia
  e copertina, e solo se non bastano a capire di che cosa parla il video, chiesto senza account
  (se Instagram lo dà); i file si scaricano da Telegram in una
  cartella temporanea: le foto Claude le guarda, vocali, audio e video si ascoltano (e dei video
  si prendono anche dei fotogrammi). Dei documenti di norma si leggono solo i dati del file e le
  prime pagine; se non dicono di che cosa parla un'opera pubblicata, si cerca la sua scheda in
  un catalogo libero. Tutto il testo solo se l'utente lo chiede: la richiesta è una domanda,
  che ha risposta a lettura finita.
  Dal contenuto Claude ricava tipo, titolo, argomenti e riassunto, e legge le scritte nelle
  immagini (una foto, i fotogrammi di un video), che si custodiscono con il testo. Una fonte che
  non si legge mette 🤷 sul messaggio. Se non si legge, o se da ciò che si è letto non si capisce
  di che cosa parla, sazed lo dice e chiede due parole, a meno che l'utente non le abbia già
  scritte (la nota dell'elemento).

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
from .coppermind import FILES, Coppermind, Draft, parse_link, unique
from .meaning import MeaningError, card
from .messages import BALANCE_COMMANDS, STATUS_COMMANDS, language_for, messages_for
from .reader import IMAGE_TYPES, MAX_IMAGE, Extract, ReadError
from .telegram import FILE_MAX, TelegramError

log = logging.getLogger("sazed_agent.lanes")

ACTIVE = ("received", "read", "saved", "answered")
INTAKE, ANSWERS = ("received", "read"), ("saved", "answered")
THREAD_WINDOW, THREAD_LIMIT = 3 * 3600, 10   # il filo: i messaggi delle ultime ore, fino a dieci
READ_WAIT = 600     # quanto una domanda aspetta la lettura degli elementi di cui parla
MEANING_BATCH = 8   # schede calcolate insieme
SHOW_MAX = 10       # quanti da riprovare Mostrameli indica per volta; poi «Più indietro» per gli altri
MEANING_PAUSE = 300 # dopo un errore di llama-server, prima di riprovare
# Le piattaforme di cui il video si scarica solo se l'anteprima non basta: meno richieste, e
# Instagram limita chi ne fa troppe senza account.
VIDEO_IF_NEEDED = ("instagram",)
LISTEN_WAIT = 3600  # quanto un vocale aspetta la trascrizione, anche dietro un video lungo
# Ciò che si sa leggere, e con che cosa: i link con il lettore, le foto con Claude (che le
# guarda), vocali, audio e video con chi ascolta (Whisper), i documenti con chi li sfoglia.
LINKS, PICTURES, RECORDINGS = ("link",), ("photo",), ("voice", "audio", "video", "video_note")
PAPERS = ("document",)
NOTES = ("note",)  # solo le note che nominano opere restano da leggere: la loro scheda nasce con i cataloghi
SPOKEN = ("voice", "video_note")  # l'utente che parla, se non sono inoltrati


def lane_for(update):
    """"commands" per i comandi di stato e di bilancio in qualunque lingua e per i tocchi sui
    pulsanti, "acquisition" per tutto il resto."""
    if "callback_query" in update:
        return "commands"
    return "commands" if _command_of(update) in STATUS_COMMANDS | BALANCE_COMMANDS else "acquisition"


def _cursor(text):
    """Il segno di «Più indietro» (":messaggio:tipo:numero"), o None se non è valido."""
    _, message_id, kind, ref = (text.split(":") + [""] * 4)[:4]
    if text.count(":") == 3 and message_id.isdigit() and kind in ("message", "item") and ref.isdigit():
        return int(message_id), kind, int(ref)
    return None


async def _read_note(item):
    """Una nota non si scarica: è già tutta nelle sue parole."""
    return Extract(None, None, item.note, True)


def _command_of(update):
    text = (_message(update).get("text") or "").strip()
    return text.split()[0].split("@")[0].lower() if text.startswith("/") else None


def _message(update):
    return update.get("message") or update.get("edited_message") or {}


class Processor:
    def __init__(self, journal, telegram, bot_name, language, reactions, retry_delays,
                 typing_interval=4.0, idle_wait=60.0, config_pause=60.0, clock=time.time, memory=None,
                 claude=None, reader=None, read_wait=READ_WAIT, listener=None, videos=None,
                 listen_wait=LISTEN_WAIT, documents=None, lookup=None, big_files=None, meanings=None):
        self.journal, self.telegram = journal, telegram
        self.memory = memory or Coppermind(journal.db, clock)  # nello stesso database del registro
        self.claude = claude  # senza, le parole accompagnano i link o sono una nota, e nulla si descrive
        self.reader = reader  # senza, i link non si leggono
        self.listener = listener  # senza, vocali, audio e video non si ascoltano
        self.videos = videos  # senza, dei video di YouTube resta l'anteprima
        self.documents = documents  # senza, i documenti non si leggono
        self.lookup = lookup  # senza, di un libro senza abstract resta ciò che dicono le prime pagine
        self.big_files = big_files  # MTProto: senza, un file oltre i 20 MB non si scarica
        self.meanings = meanings  # senza, si cerca solo per parole
        self.readable = ((LINKS if reader is not None else ()) + (PICTURES + NOTES if claude is not None else ())
                         + (RECORDINGS if listener is not None else ()) + (PAPERS if documents is not None else ()))
        self.bot_name, self.language, self.reactions = bot_name, language, reactions
        self.retry_delays, self.typing_interval, self.idle_wait = retry_delays, typing_interval, idle_wait
        self.config_pause = config_pause  # attesa dopo un token rifiutato, senza contare tentativi
        self.clock, self.read_wait, self.listen_wait = clock, read_wait, listen_wait
        self.wake = {lane: asyncio.Event() for lane in ("commands", "intake", "answers", "reading", "meaning")}

    def notify(self):
        """C'è lavoro nuovo nel registro: sveglia le corsie."""
        for event in self.wake.values():
            event.set()

    async def run(self, stop):
        await asyncio.gather(self._lane("commands", stop), self._lane("intake", stop),
                             self._lane("answers", stop), self._reading(stop), self._meaning(stop))

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
                drafts.append(Draft("note", note=meaning.note, look_up=meaning.look_up and self.lookup is not None,
                                    **described))
            self.memory.save(entry.chat_id, entry.message_id, self._sent_at(entry, reading), drafts)
            for item_id in meaning.comment_on:
                if item_id not in mine:
                    self.memory.annotate(item_id, meaning.comment)
            for item_id in meaning.read_in_full:  # la domanda che lo chiede aspetta la lettura
                self.memory.read_in_full(item_id)
            self.journal.advance(entry.update_id, "saved")
        if meaning.read_in_full or meaning.note and meaning.look_up:  # anche una nota che nomina opere
            self.wake["reading"].set()
        self.wake["meaning"].set()  # una nota o un commento nuovi
        log.info("elemento %s in coppermind: %d link e file, %s%s", entry.update_id, len(pairs), _summary(meaning),
                 "; il vocale portava solo parole e non si custodisce" if forget else "")
        if self._needs_reply(reading, meaning):
            self.wake["answers"].set()
        else:
            self.journal.advance(entry.update_id, "done")
            log.info("elemento %s completato, senza bisogno di risposta", entry.update_id)
        # ultimo, dopo ogni cambio di stato: chi era letto prima che le parole fossero salvate
        for item_id, reason in (self.journal.get(entry.update_id).unknown or {}).items():
            item = self.memory.get(int(item_id))
            if item is not None:
                await self._tell_unknown(entry, item, reason)

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
            replied = (self.journal.of_message(entry.chat_id, reading.reply_to)
                       or self.journal.of_reply(entry.chat_id, reading.reply_to))  # una risposta a sazed
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
                text, send_back = await self._reply_text(entry, reading, meaning, t)
                reply_id = await self.telegram.send_message(entry.chat_id, f"{hello}, {text}",
                                                            reply_to=entry.message_id)
                self.journal.set_reply(entry.update_id, reply_id, text, send_back)
                entry = self.journal.get(entry.update_id)
            await self._send_back(entry)  # dopo un arresto si rimandano tutti: meglio un doppione
            self.journal.advance(entry.update_id, "answered")
            state = "answered"
        if state == "answered":
            self.journal.advance(entry.update_id, "done")
            log.info("elemento %s completato: risposta inviata", entry.update_id)

    async def _reply_text(self, entry, reading, meaning, t):
        """Il testo della risposta e gli elementi di cui rimandare il messaggio originale."""
        if reading.command:
            return t["unknown_command"](reading.command), ()
        if not meaning.question or self.claude is None:
            return t["unsupported"], ()
        about = [i for i in (self.memory.get(item_id) for item_id in meaning.about) if i is not None]
        today = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        async with self._typing(entry.chat_id):
            answer = await self.claude.answer(self._thread(entry, reading), meaning.question, about,
                                              self._language(entry.update), self.memory, today, bool(meaning.spoken),
                                              self.meanings)
        if answer is None:
            return t["no_answer"], ()
        return answer.text or t["here_it_is"](len(answer.send_back)), answer.send_back

    async def _send_back(self, entry):
        """I file che la risposta rimanda, dopo il suo testo: il messaggio originale, copiato
        nella chat (R6); se non c'è più, il file dal suo file_id; se Telegram non ha più neanche
        quello, lo si dice. Un errore da riprovare fa ripetere il passo."""
        for item in (self.memory.get(item_id) for item_id in entry.send_back):
            if item is None or item.source not in FILES:
                continue
            try:
                await self.telegram.copy_message(entry.chat_id, item.chat_id, item.message_id,
                                                 reply_to=entry.message_id)
                continue
            except TelegramError as err:
                if not err.permanent:
                    raise
                log.info("elemento %s: il messaggio originale non si copia (%s), si rimanda il file", item.id, err)
            try:
                await self.telegram.send_file(entry.chat_id, item.source, (item.media or {}).get("file_id"),
                                              reply_to=entry.message_id)
            except TelegramError as err:
                if not err.permanent:
                    raise
                log.warning("elemento %s: il file non si rimanda (%s)", item.id, err)
                t, hello = self._voice(entry)
                await self.telegram.send_message(entry.chat_id, f"{hello}, {t['file_gone'](item.title)}",
                                                 reply_to=entry.message_id)

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
            extract = await (self._read_link(item) if item.source == "link" else _read_note(item)
                             if item.source in NOTES else self._read_file(item))
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
                # final è vuoto quando sono finiti i tentativi per un problema passeggero: lì riprovare ha senso
                await self._say_unknown(item, "unreadable+retry" if final is None else "unreadable")
                self._read_over()
            return
        if not item.read_full and getattr(self.memory.get(item.id), "read_full", False):
            log.info("elemento %s: ora va letto tutto, la lettura ricomincia", item.id)
            self.wake["reading"].set()  # l'utente l'ha chiesto mentre lo si sfogliava
            return
        description = None
        if self.claude is not None and (extract.text or extract.title or extract.images or extract.screen_text):
            try:
                description = await self._describe(item, extract)
                if self._video_needed(item, extract, description):
                    richer = await self._video_after_all(item, extract)
                    if richer is not None:
                        extract, description = richer, await self._describe(item, richer)
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
        written = (d and d.image_text) or extract.screen_text  # la lettura di Claude, o quella di tesseract
        content = "\n\n".join(p for p in (extract.text, written and f"Text in the images:\n{written}") if p) or None
        if item.source in NOTES:
            content = None  # il testo di una nota è già la nota
        self.memory.read_done(item.id, "complete" if extract.complete else "partial", content=content,
                              title=(d and d.title) or extract.title, kind=d and d.kind,
                              topics=d.topics if d else (), summary=d and d.summary, authors=d.authors if d else ())
        log.info("elemento %s letto (%s)%s", item.id, "completo" if extract.complete else "in parte",
                 "" if d else ", senza descrizione")
        read_something = extract.text or extract.title or extract.images or extract.screen_text
        if item.source not in NOTES and (not read_something or d is not None and not d.useful):
            await self._say_unknown(item, "unknown")  # non si è letto nulla, o nulla che dica qualcosa
        self._read_over()

    async def _say_unknown(self, item, reason):
        """Di un elemento non si sa di che cosa parla (reason: "unknown" se si è letto ma non dice
        nulla, "unreadable" se non si legge, "unreadable+retry" se riprovare può servire). Se le
        parole del suo messaggio non sono ancora salvate, l'avviso lo decide l'ingresso dopo averle
        salvate (lo si annota nel registro, senza attese in mezzo: nessuna corsa); altrimenti si
        decide adesso."""
        entry = self.journal.of_message(item.chat_id, item.message_id)
        if entry is None:
            return
        if entry.state in INTAKE:
            self.journal.add_unknown(entry.update_id, item.id, reason)
            return
        await self._tell_unknown(entry, item, reason)

    async def _tell_unknown(self, entry, item, reason):
        """Dice all'utente che di un elemento non si sa di che cosa parla e gli chiede due parole.
        Non se le aveva già scritte lui: la nota basta a ritrovarlo. L'avviso entra nel filo, come
        risposta al messaggio dell'elemento, così le parole che seguono si legano a lui."""
        current = self.memory.get(item.id)
        if current is None or current.note:
            return
        t, hello = self._voice(entry)
        base, _, retry = reason.partition("+")
        text = t[base] + (f" {t['reel_hint']}" if current.platform in VIDEO_IF_NEEDED else "")
        try:
            reply_id = await self.telegram.send_message(
                item.chat_id, f"{hello}, {text}", reply_to=item.message_id,
                buttons=((t["retry"], f"retry:item:{item.id}"),) if retry else ())
        except TelegramError as err:
            log.warning("elemento %s: avviso di contenuto incomprensibile non inviato: %s", item.id, err)
            return
        if entry.reply_message_id is None:
            self.journal.set_reply(entry.update_id, reply_id, text)
        log.info("elemento %s: non si capisce di che cosa parla, chieste due parole all'utente", item.id)

    async def _describe(self, item, extract):
        """La descrizione di Claude. Di un'opera pubblicata letta solo in parte, se le prime pagine
        non dicono di che cosa parla, e delle opere che una nota (scritta o vocale) nomina, Claude
        riguarda l'elemento con le schede trovate in rete."""
        language = self._item_language(item)
        whole = item.source in PAPERS and item.read_full
        d = await self.claude.describe(item, extract, language, whole=whole)
        if d is None or whole or self.lookup is None:
            return d
        if d.published and not d.summary and d.title:
            found = await self.lookup.find(d.title, d.authors, d.kind, language)
        elif d.works and item.source in NOTES + SPOKEN:
            found = [card for w in d.works for card in await self.lookup.find(w.title, w.authors, w.kind, language)]
        else:
            return d
        if not found:
            return d
        log.info("elemento %s: %d schede trovate in rete", item.id, len(found))
        return await self.claude.describe(item, extract, language, found=found) or d

    def _read_over(self):
        """Una lettura è finita: chi la aspettava (un vocale da interpretare, una domanda) riparte,
        e c'è un significato da calcolare."""
        self.wake["intake"].set()
        self.wake["answers"].set()
        self.wake["meaning"].set()

    # --- significati ---

    async def _meaning(self, stop):
        """I vettori degli elementi letti o cambiati, a gruppi. Se llama-server non risponde, si
        riprova dopo una pausa: la ricerca per parole intanto funziona."""
        while self.meanings is not None and not stop.is_set():
            self.wake["meaning"].clear()
            items = self.memory.to_mean(self.meanings.name, MEANING_BATCH)
            if not items:
                await self._sleep("meaning", stop, None)
                continue
            try:
                vectors = await self.meanings.vectors([card(i) for i in items])
            except MeaningError as err:  # una pausa vera: un avviso di lavoro nuovo non la accorcia
                log.warning("significati non calcolati: %s (si riprova tra %d s)", err, MEANING_PAUSE)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), MEANING_PAUSE)
                continue
            for item, vector in zip(items, vectors):
                self.memory.set_meaning(item.id, self.meanings.name, vector, item.updated_at)
            log.info("significato di %d elementi calcolato", len(items))

    async def _read_link(self, item):
        resolved = await self.reader.resolve(item.url)  # un link breve rimasto tale all'ingresso
        if resolved != item.url and self.memory.relink(item.id, resolved):
            log.info("elemento %s: link breve portato al suo indirizzo", item.id)
        platform = parse_link(resolved).platform
        videos = self.videos is not None and platform in self.videos.platforms
        try:
            extract = await self.reader.read(resolved)
        except ReadError:
            if not (videos and platform in VIDEO_IF_NEEDED):
                raise
            extract = await self._video_after_all(item, Extract(None, None, None, False))  # niente anteprima
            if extract is None:
                raise
            return extract
        if videos and platform not in VIDEO_IF_NEEDED:
            try:
                extract = await self.videos.read(resolved, extract)
            except ReadError as err:  # YouTube che blocca: riprovare subito non aiuta
                log.warning("elemento %s: del video resta l'anteprima (%s)", item.id, err)
        return extract

    def _video_needed(self, item, extract, description):
        """True se di un reel didascalia e copertina non bastano: Claude non ne capisce nulla o non
        ne sa fare un riassunto (senza Claude: se non c'è la didascalia)."""
        current = self.memory.get(item.id)
        if (self.videos is None or current is None or current.source != "link"
                or current.platform not in VIDEO_IF_NEEDED or current.platform not in self.videos.platforms):
            return False
        if description is None:
            return not extract.text
        return not description.useful or not description.summary

    async def _video_after_all(self, item, preview):
        """Il video di un reel di cui l'anteprima non basta, chiesto senza account; None se
        Instagram non lo dà: resta l'anteprima."""
        try:
            return await self.videos.read(self.memory.get(item.id).url, preview)
        except ReadError as err:
            log.warning("elemento %s: il video del reel non si scarica (%s), resta l'anteprima", item.id, err)
            return None

    async def _read_file(self, item):
        """Scarica il file da Telegram in una cartella temporanea e lo legge: una foto la
        guarda Claude, un documento si sfoglia (o si legge tutto), il resto si ascolta. Un
        documento che è un'immagine, un audio o un video si tratta come tale."""
        media = item.media or {}
        big = (media.get("file_size") or 0) > FILE_MAX
        if big and self.big_files is None:
            raise ReadError("file oltre i 20 MB dell'API dei bot, e MTProto non è configurato", permanent=True)
        mime = media.get("mime_type") or ""
        with tempfile.TemporaryDirectory(prefix="sazed-") as work:
            path = Path(work) / "file"
            await self._download(item, media, path, big)
            if item.source in PICTURES or (item.source in PAPERS and mime in IMAGE_TYPES and self.claude
                                           and path.stat().st_size <= MAX_IMAGE):
                return Extract(None, None, None, True, ((mime or "image/jpeg", path.read_bytes()),))
            if item.source in PAPERS and not (mime.startswith(("audio/", "video/")) and self.listener):
                return await self.documents.read(path, media.get("file_name"), mime, full=item.read_full)
            return await self.listener.listen(path, video=item.source in ("video", "video_note")
                                              or mime.startswith("video/"))

    async def _download(self, item, media, path, big):
        """Il file da Telegram: con l'API dei bot fino a 20 MB, oltre via MTProto (R7). Anche un
        file senza dimensione che l'API dei bot rifiuta perché troppo grande passa da MTProto."""
        if not big:
            try:
                await self.telegram.download(await self.telegram.get_file(media["file_id"]), path)
                return
            except TelegramError as err:
                if self.big_files is None or "too big" not in str(err):
                    raise
        log.info("elemento %s: file grande, scaricato via MTProto", item.id)
        await self.big_files.download(item.chat_id, item.message_id, path)

    def _item_language(self, item):
        entry = self.journal.of_message(item.chat_id, item.message_id)
        return self._language(entry.update if entry else {})

    # --- comandi, errori, voce ---

    async def _command(self, entry):
        if "callback_query" in entry.update:
            await self._button(entry)
            self.journal.advance(entry.update_id, "done")
            log.info("elemento %s completato: pulsante", entry.update_id)
            return
        balance = _command_of(entry.update) in BALANCE_COMMANDS
        if entry.reply_message_id is None:
            t, hello = self._voice(entry)
            buttons = ()
            if balance:
                counts, readings = self.journal.balance(entry.chat_id), self.memory.readings(entry.chat_id)
                text = t["balance"](sum(counts.values()), counts["done"], sum(counts[s] for s in ACTIVE),
                                    counts["failed"], readings["retry"], readings["lost"])
                if counts["failed"] + readings["retry"]:
                    buttons = ((t["show_them"], "show:retry"), (t["retry_all"], "retry:all"))
            else:
                counts = self.journal.counts("acquisition")
                text = t["status"](self.bot_name, sum(counts[s] for s in ACTIVE), counts["failed"], self.memory.count())
            reply_id = await self.telegram.send_message(entry.chat_id, f"{hello}, {text}", reply_to=entry.message_id,
                                                        buttons=buttons)
            self.journal.set_reply(entry.update_id, reply_id)
        self.journal.advance(entry.update_id, "done")
        log.info("elemento %s completato: %s inviato", entry.update_id, "bilancio" if balance else "stato")

    async def _button(self, entry):
        """Il tocco di un pulsante, solo dalla stessa chat. Riprova: il messaggio messo da parte (o
        la lettura fallita) riparte da capo, e 👍 torna sul messaggio; Riprova tutti: lo stesso per
        tutti quelli da riprovare. Si risponde subito al tocco e il pulsante sparisce, così un
        secondo tocco non riprova due volte; un pulsante vecchio dice che non c'è più nulla.
        Mostrameli: sazed risponde a ciascuno da riprovare, SHOW_MAX per volta, con il suo Riprova;
        Più indietro: i successivi, andando indietro nella chat."""
        query = entry.update["callback_query"]
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        t = messages_for(self.language, (query.get("from") or {}).get("language_code"))
        data = query.get("data") or ""
        if data.startswith("show:retry"):
            cursor = data.removeprefix("show:retry")
            before = _cursor(cursor)
            shown = 0
            if chat_id is not None and (before is not None or not cursor):
                shown = await self._show_retry(chat_id, t, before)
            with contextlib.suppress(TelegramError):
                await self.telegram.answer_button(query.get("id"),
                                                  t["showing"](shown) if shown else t["nothing_to_retry"])
            if cursor and chat_id is not None and message.get("message_id") is not None:
                with contextlib.suppress(TelegramError):  # Più indietro serve una volta sola
                    await self.telegram.remove_button(chat_id, message["message_id"])
            return
        again = []  # i messaggi (o gli elementi) di nuovo da elaborare
        kind, _, ref = data.removeprefix("retry:").partition(":")
        if data == "retry:all" and chat_id is not None:
            again = [e for e in self.journal.set_aside(chat_id) if self.journal.reopen(e.update_id, chat_id)]
            again += [i for i in self.memory.to_retry(chat_id) if self.memory.read_again(i.id, chat_id)]
        elif data.startswith("retry:") and ref.isdigit():
            if kind == "message" and self.journal.reopen(int(ref), chat_id):
                again = [self.journal.get(int(ref))]
            elif kind == "item" and self.memory.read_again(int(ref), chat_id):
                again = [self.memory.get(int(ref))]
        with contextlib.suppress(TelegramError):
            await self.telegram.answer_button(query.get("id"), t["retrying_many"](len(again)) if again
                                              else t["nothing_to_retry"])
        if chat_id is not None and message.get("message_id") is not None:
            with contextlib.suppress(TelegramError):
                await self.telegram.remove_button(chat_id, message["message_id"])
        for done in again:
            await self._react(chat_id, done.message_id, self.reactions.received)
        if again:
            log.info("%d di nuovo da elaborare, come chiesto dall'utente", len(again))
            self.notify()

    async def _show_retry(self, chat_id, t, before=None):
        """Risponde a ciascun messaggio da riprovare (messo da parte, o con una fonte non letta per
        un problema passeggero), SHOW_MAX per volta, dal più recente nella chat (o da prima di
        before), con il suo Riprova: toccando la citazione, la chat salta al messaggio. Se ne
        restano di più vecchi, lo dice con «Più indietro», che riparte dall'ultimo indicato: un
        segno nella chat, non una posizione, così nessuno salta né si ripete se intanto altri
        vengono riprovati. Quanti ne ha indicati."""
        pending = [(e.message_id, "message", e.update_id) for e in self.journal.set_aside(chat_id)]
        pending += [(i.message_id, "item", i.id) for i in self.memory.to_retry(chat_id) if i.message_id is not None]
        pending = [p for p in sorted(pending, reverse=True) if before is None or p < before]
        page, rest = pending[:SHOW_MAX], len(pending) - SHOW_MAX
        shown = 0
        for message_id, kind, ref in page:
            with contextlib.suppress(TelegramError):
                await self.telegram.send_message(chat_id, t["to_retry"], reply_to=message_id,
                                                 buttons=((t["retry"], f"retry:{kind}:{ref}"),))
                shown += 1
        if shown and rest > 0:
            last = ":".join(map(str, page[-1]))
            with contextlib.suppress(TelegramError):
                await self.telegram.send_message(chat_id, t["older"](rest),
                                                 buttons=((t["further_back"], f"show:retry:{last}"),))
        return shown

    async def _report_failure(self, entry):
        """Avvisa una volta sola che un messaggio è stato messo da parte, con Riprova. Se non
        riesce, lo annota."""
        if entry.chat_id is None:
            return
        failed = self.journal.get(entry.update_id)
        t, hello = self._voice(entry)
        try:
            await self.telegram.send_message(entry.chat_id, f"{hello}, {t['failed'](failed.attempts)}",
                                             reply_to=entry.message_id,
                                             buttons=((t["retry"], f"retry:message:{entry.update_id}"),))
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
