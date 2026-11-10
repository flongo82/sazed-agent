"""Punto d'ingresso: python3 -m sazed_agent <comando>.

- check: verifica configurazione, segreti, registro e coppermind senza collegarsi a nulla;
- run: si collega al Worker, registra e conferma i messaggi, e li elabora nelle corsie:
  note, link e file finiscono in coppermind, link e file si leggono (vocali e video si
  ascoltano, se c'è il modello di Whisper; di YouTube anche sottotitoli e descrizione), le
  domande hanno risposta.
"""

import argparse
import asyncio
import contextlib
import fcntl
import logging
import signal
import sys
from urllib.parse import urlsplit

from .claude import Claude
from .config import ConfigError, load
from .coppermind import Coppermind, CoppermindError
from .documents import Documents
from .journal import Journal
from .lanes import Processor, lane_for
from .link import Replaced, WorkerLink, agent_url
from .listener import Listener
from .lookup import Lookup
from .meaning import Meanings
from .mtproto import BigFiles
from .reader import Reader
from .telegram import Telegram
from .videos import Videos

log = logging.getLogger("sazed_agent")

# Gli stati del registro come li legge chi usa check.
STATE_NAMES = {"received": "ricevuti", "read": "letti", "saved": "salvati", "answered": "risposti",
               "done": "completati", "failed": "in errore"}


class AlreadyRunning(Exception):
    """Un altro agente sta già usando la stessa cartella dei dati."""


@contextlib.contextmanager
def single_instance(lock_path):
    """Un solo agente per cartella dei dati: due userebbero lo stesso registro."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning(f"un altro agente sta già usando {lock_path.parent}") from None
        yield


def check(config_path):
    config = load(config_path)
    # Leggere i segreti basta a verificarli (esistono, non sono vuoti, non sono leggibili
    # da altri): i valori non vengono mai stampati.
    config.agent_token, config.bot_token
    host = urlsplit(config.worker_url).hostname
    journal = Journal(config.journal_path)
    try:
        counts, kept = journal.counts(), Coppermind(journal.db).count()
    finally:
        journal.close()
    print(f"Configurazione valida: bot \"{config.bot_name}\", lingua {config.language}.")
    print(f"Worker: {host}. Segreti presenti e protetti.")
    print("Registro: " + ", ".join(f"{STATE_NAMES.get(state, state)} {n}" for state, n in counts.items()) + ".")
    print(f"coppermind: {kept} {'elemento' if kept == 1 else 'elementi'}.")
    try:  # Claude non serve ancora per elaborare i messaggi: se manca, check lo dice e basta
        config.anthropic_api_key
        print(f"Claude: modello {config.claude.model}, chiave presente e protetta.")
    except ConfigError as err:
        print(f"Claude: non ancora pronto ({err}).")
    print(f"Whisper: {listening(config)}.")
    print(f"File oltre i 20 MB: {big_files_status(config)}.")
    print(f"Ricerca per significato: {meaning_status(config)}.")
    print(f"Instagram: {instagram_status(config)}.")


def big_files_status(config):
    try:
        config.api_id, config.api_hash
        session = config.mtproto_session_file
    except ConfigError as err:
        return f"MTProto non ancora pronto ({err})"
    if session.exists():
        return "MTProto pronto, sessione del bot salvata"
    return "MTProto pronto, la sessione si apre al primo file"


def run(config_path):
    config = load(config_path)
    url, agent_token, bot_token = agent_url(config.worker_url), config.agent_token, config.bot_token
    with single_instance(config.data_dir / "agent.lock"):
        journal = Journal(config.journal_path)
        try:
            listener, reader = listener_for(config), Reader()
            processor = Processor(journal, Telegram(config.telegram_api_base, bot_token), config.bot_name,
                                  config.language, config.reactions, config.retry_delays,
                                  memory=Coppermind(journal.db), claude=claude_for(config), reader=reader,
                                  listener=listener, videos=Videos(listener, cookies=instagram_cookies(config)),
                                  documents=Documents(),
                                  lookup=Lookup(reader), big_files=big_files_for(config),
                                  meanings=meanings_for(config))
            link = WorkerLink(url, agent_token, journal, lane_for, config.ping_interval,
                              config.pong_timeout, on_recorded=processor.notify)
            asyncio.run(serve(link, processor))
        finally:
            journal.close()


def claude_for(config):
    """Claude, se configurato; senza, l'agente custodisce lo stesso: le parole accompagnano i link
    o diventano una nota, nulla si descrive e alle domande non si risponde."""
    try:
        return Claude(config.anthropic_api_key, config.claude.api_base, config.claude.model,
                      config.claude.careful_model)
    except ConfigError as err:
        log.warning("Claude non configurato, ogni testo diventa una nota: %s", err)
        return None


def listening(config):
    if config.whisper is None:
        return "non configurato, vocali e video non si ascoltano ([whisper] in config.toml)"
    if not config.whisper.model.is_file():
        return f"manca il modello {config.whisper.model}: lancia scripts/setup-whisper.sh"
    return f"modello {config.whisper.model.name}, {config.whisper.threads} processori"


def instagram_cookies(config):
    """I cookie dell'account secondario di Instagram, se ci sono e hanno la sessione; senza, dei
    reel resta l'anteprima pubblica."""
    path = config.instagram_cookies_file
    if path is None or not path.is_file():
        return None
    if path.stat().st_mode & 0o077:
        log.warning("Instagram: %s è leggibile da altri utenti (chmod 600): non si usa", path.name)
        return None
    if not _has_session(path):
        log.warning("Instagram: nei cookie manca la sessione: lancia scripts/setup-instagram.sh")
        return None
    return path


def _has_session(path):
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.removeprefix("#HttpOnly_").split("\t")
        if len(fields) == 7 and fields[5] == "sessionid" and fields[6]:
            return True
    return False


def instagram_status(config):
    if instagram_cookies(config) is not None:
        return "con i cookie dell'account secondario"
    return "senza account: didascalia e copertina, e il video quando non bastano e Instagram lo dà"


def meanings_for(config):
    """Chi calcola i significati, se il modello c'è; senza, si cerca solo per parole."""
    if config.embeddings is None or not config.embeddings.model.is_file():
        log.warning("Ricerca per significato: %s", meaning_status(config))
        return None
    return Meanings(config.embeddings.model, config.embeddings.threads)


def meaning_status(config):
    if config.embeddings is None:
        return "non configurata: lancia scripts/setup-embeddings.sh"
    if not config.embeddings.model.is_file():
        return f"manca il modello {config.embeddings.model.name}: lancia scripts/setup-embeddings.sh"
    return f"modello {config.embeddings.model.name}, {config.embeddings.threads} processori"


def big_files_for(config):
    """MTProto per i file oltre i 20 MB, se api_id e api_hash ci sono; senza, quei file
    ricevono 🤷."""
    try:
        return BigFiles(config.mtproto_session_file, config.api_id, config.api_hash, config.bot_token)
    except ConfigError as err:
        log.warning("MTProto non configurato, i file oltre i 20 MB non si scaricano: %s", err)
        return None


def listener_for(config):
    """Chi ascolta vocali e video, se il modello c'è; senza, quei file restano da leggere."""
    if config.whisper is None or not config.whisper.model.is_file():
        log.warning("Whisper: %s", listening(config))
        return None
    return Listener(config.whisper.model, config.whisper.threads)


async def serve(link, processor, stop=None):
    """Connessione e corsie insieme, fino a SIGTERM o SIGINT (o fino a stop, nei test).

    Se la connessione finisce da sé (un altro agente ha preso il posto di questo), le corsie
    chiudono il lavoro in corso e l'eccezione risale.
    """
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("agente avviato")
    lanes = asyncio.create_task(processor.run(stop))
    try:
        await link.run(stop)
    finally:
        stop.set()
        await lanes
        if getattr(processor, "reader", None) is not None:
            await processor.reader.close()
        for helper in ("big_files", "meanings"):
            if getattr(processor, helper, None) is not None:
                await getattr(processor, helper).close()
        log.info("agente fermato")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sazed_agent", description="La mente di sazed.")
    parser.add_argument("--config", default="config.toml", help="file di configurazione")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="verifica configurazione, segreti, registro e coppermind")
    commands.add_parser("run", help="si collega al Worker e registra i messaggi")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # scrive ogni indirizzo: i link dell'utente non vanno nei log
    try:
        if args.command == "check":
            check(args.config)
        else:
            run(args.config)
    except ConfigError as err:
        print(f"Errore di configurazione: {err}", file=sys.stderr)
        return 1
    except CoppermindError as err:
        print(f"coppermind non utilizzabile: {err}", file=sys.stderr)
        return 1
    except AlreadyRunning as err:
        print(f"Già in esecuzione: {err}", file=sys.stderr)
        return 1
    except Replaced:
        print("Fermato: un altro agente si è collegato al Worker al posto di questo, e ne deve "
              "girare uno solo.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
