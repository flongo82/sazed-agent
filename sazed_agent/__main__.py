"""Punto d'ingresso: python3 -m sazed_agent <comando>.

- check: verifica configurazione, segreti, registro e coppermind senza collegarsi a nulla;
- run: si collega al Worker, registra e conferma i messaggi, e li elabora nelle corsie:
  note, link e file finiscono in coppermind, link e file si leggono (vocali e video si
  ascoltano, se c'è il modello di Whisper), le domande hanno risposta.
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
from .journal import Journal
from .lanes import Processor, lane_for
from .link import Replaced, WorkerLink, agent_url
from .listener import Listener
from .reader import Reader
from .telegram import Telegram

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


def run(config_path):
    config = load(config_path)
    url, agent_token, bot_token = agent_url(config.worker_url), config.agent_token, config.bot_token
    with single_instance(config.data_dir / "agent.lock"):
        journal = Journal(config.journal_path)
        try:
            processor = Processor(journal, Telegram(config.telegram_api_base, bot_token), config.bot_name,
                                  config.language, config.reactions, config.retry_delays,
                                  memory=Coppermind(journal.db), claude=claude_for(config), reader=Reader(),
                                  listener=listener_for(config))
            link = WorkerLink(url, agent_token, journal, lane_for, config.ping_interval,
                              config.pong_timeout, on_recorded=processor.notify)
            asyncio.run(serve(link, processor))
        finally:
            journal.close()


def claude_for(config):
    """Claude, se configurato; senza, l'agente custodisce lo stesso: le parole accompagnano i link
    o diventano una nota, nulla si descrive e alle domande non si risponde."""
    try:
        return Claude(config.anthropic_api_key, config.claude.api_base, config.claude.model)
    except ConfigError as err:
        log.warning("Claude non configurato, ogni testo diventa una nota: %s", err)
        return None


def listening(config):
    if config.whisper is None:
        return "non configurato, vocali e video non si ascoltano ([whisper] in config.toml)"
    if not config.whisper.model.is_file():
        return f"manca il modello {config.whisper.model}: lancia scripts/setup-whisper.sh"
    return f"modello {config.whisper.model.name}, {config.whisper.threads} processori"


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
