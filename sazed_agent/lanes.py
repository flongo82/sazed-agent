"""Le due corsie: i comandi passano davanti all'acquisizione dei contenuti.

Ogni corsia lavora un elemento alla volta, in ordine di arrivo, e non aspetta l'altra.
Ogni passo è scritto nel registro prima del successivo, così un arresto in qualunque punto
riprende dallo stato registrato:

- acquisizione: received → 👀 → read → salvataggio in coppermind → saved → risposta →
  answered → 👌 → done. Salvataggio e passaggio a saved stanno in una sola transazione, e la
  risposta dice ciò che coppermind contiene dopo il commit: rifatta dopo un arresto, è la
  stessa. Un messaggio senza nulla da custodire (un comando sconosciuto, uno sticker) passa
  da read alla risposta senza salvare;
- comandi: received → risposta → done.

Una risposta già annotata nel registro non si rimanda. Se invece l'agente si ferma dopo
l'invio ma prima di annotarla, alla ripresa la rimanda: meglio un doppione che un silenzio.
Le reazioni sono un segnale, non il contenuto: se Telegram ne rifiuta una, si va avanti.
"""

import asyncio
import contextlib
import logging
import time

from . import intake
from .coppermind import FILES, Coppermind, platform_label
from .journal import LANES
from .messages import STATUS_COMMANDS, messages_for
from .telegram import TelegramError

log = logging.getLogger("sazed_agent.lanes")

ACTIVE = ("received", "read", "saved", "answered")


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
                 typing_interval=4.0, idle_wait=60.0, config_pause=60.0, clock=time.time, memory=None):
        self.journal, self.telegram = journal, telegram
        self.memory = memory or Coppermind(journal.db, clock)  # nello stesso database del registro
        self.bot_name, self.language, self.reactions = bot_name, language, reactions
        self.retry_delays, self.typing_interval, self.idle_wait = retry_delays, typing_interval, idle_wait
        self.config_pause = config_pause  # attesa dopo un token rifiutato, senza contare tentativi
        self.clock = clock
        self.wake = {lane: asyncio.Event() for lane in LANES}

    def notify(self):
        """C'è lavoro nuovo nel registro: sveglia le corsie."""
        for event in self.wake.values():
            event.set()

    async def run(self, stop):
        await asyncio.gather(*(self._lane(lane, stop) for lane in LANES))

    async def _lane(self, lane, stop):
        handle = self._command if lane == "commands" else self._acquire
        while not stop.is_set():
            self.wake[lane].clear()  # prima di guardare il registro: un avviso dopo non si perde
            due = self.journal.due(lane, limit=1)
            if due:
                await self._attempt(due[0], handle)
            else:
                await self._sleep(lane, stop)

    async def _sleep(self, lane, stop):
        """Fino al prossimo tentativo in programma, a un avviso di lavoro nuovo o allo stop."""
        next_at = self.journal.next_attempt(lane)
        timeout = self.idle_wait if next_at is None else max(0.0, min(self.idle_wait, next_at - self.clock()))
        waits = [asyncio.create_task(self.wake[lane].wait()), asyncio.create_task(stop.wait())]
        await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for task in waits:
            task.cancel()
        await asyncio.gather(*waits, return_exceptions=True)

    async def _attempt(self, entry, handle):
        try:
            await handle(entry)
            return
        except TelegramError as err:
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

    async def _acquire(self, entry):
        if entry.chat_id is None:  # niente a cui rispondere (per esempio un callback)
            self.journal.advance(entry.update_id, "done")
            log.info("elemento %s completato, senza una chat a cui rispondere", entry.update_id)
            return
        state = entry.state
        if state == "received":
            await self._react(entry, self.reactions.received)
            self.journal.advance(entry.update_id, "read")
            state = "read"
        reading = intake.read(entry.update)
        if state == "read" and reading.drafts:
            with self.journal.transaction():
                sent_at = reading.sent_at if reading.sent_at is not None else int(entry.received_at)
                saved = self.memory.save(entry.chat_id, entry.message_id, sent_at, reading.drafts)
                self.journal.advance(entry.update_id, "saved")
            log.info("elemento %s: %d in coppermind", entry.update_id, len(saved))
            state = "saved"
        if state in ("read", "saved"):
            if entry.reply_message_id is None:
                t, hello = self._voice(entry)
                outcomes = self.memory.outcomes(entry.chat_id, entry.message_id, reading.drafts)
                text = acquired_text(t, reading, outcomes, time.localtime(self.clock()).tm_year)
                async with self._typing(entry.chat_id):
                    reply_id = await self.telegram.send_message(
                        entry.chat_id, f"{hello}, {text}", reply_to=entry.message_id)
                self.journal.set_reply(entry.update_id, reply_id)
            self.journal.advance(entry.update_id, "answered")
            state = "answered"
        if state == "answered":
            await self._react(entry, self.reactions.done)
            self.journal.advance(entry.update_id, "done")
            log.info("elemento %s completato: risposta inviata", entry.update_id)

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
        await self._react(entry, self.reactions.failed)

    def _voice(self, entry):
        """I testi nella lingua di chi scrive, e il saluto con il suo nome preso da Telegram."""
        sender = _message(entry.update).get("from") or {}
        t = messages_for(self.language, sender.get("language_code"))
        return t, t["hello"](sender.get("first_name"))

    async def _react(self, entry, emoji):
        if entry.message_id is None:
            return
        try:
            await self.telegram.set_reaction(entry.chat_id, entry.message_id, emoji)
        except TelegramError as err:
            log.warning("reazione %s non impostata su %s: %s", emoji, entry.update_id, err)

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


def acquired_text(t, reading, outcomes, this_year):
    """La risposta a un messaggio dell'acquisizione, da ciò che coppermind contiene davvero.

    Una frase per ciò che è stato salvato (o aggiornato, se il messaggio è una modifica), una
    se il contenuto resta da leggere, una per ciò che c'era già. La prima segue il saluto e
    comincia in minuscolo, le altre in maiuscolo.
    """
    if reading.command:
        return t["unknown_command"](reading.command)
    if not outcomes:
        return t["unsupported"]
    saved = [o.item for o in outcomes if o.status == "saved"]
    already = [o.item for o in outcomes if o.status == "already"]
    sentences = []
    if saved:
        things = [_thing(t, i) for i in saved if i.source != "link"]
        links = [i for i in saved if i.source == "link"]
        if len(links) == 1:
            things.append(_thing(t, links[0]))
        elif links:
            things.append(t["links"](len(links)))
        sentences.append(t["updated" if reading.edited else "saved"](t["and"].join(things)))
        if any(i.extraction == "pending" for i in saved):
            sentences.append(t["later"])
    if len(already) == 1:
        when = time.localtime(already[0].sent_at)
        year = None if when.tm_year == this_year else when.tm_year
        sentences.append(t["already"](_thing(t, already[0]), t["date"](when.tm_mday, when.tm_mon, year)))
    elif already:
        sentences.append(t["already_many"](len(already)))
    return " ".join([sentences[0]] + [s[:1].upper() + s[1:] for s in sentences[1:]])


def _thing(t, item):
    if item.source == "link":
        return t["link"](platform_label(item.platform))
    return t["file"](item.source) if item.source in FILES else t["note"]
