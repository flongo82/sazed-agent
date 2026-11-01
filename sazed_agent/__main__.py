"""Punto d'ingresso: python3 -m sazed_agent <comando>.

- check: verifica configurazione, segreti e registro senza collegarsi a nulla;
- run: si collega al Worker, registra e conferma i messaggi. Per ora non li elabora ancora:
  corsie, reazioni e risposte arrivano con la terza tappa del primo passo.
"""

import argparse
import asyncio
import contextlib
import fcntl
import logging
import signal
import sys
from urllib.parse import urlsplit

from .config import ConfigError, load
from .journal import Journal
from .lanes import lane_for
from .link import WorkerLink, agent_url

log = logging.getLogger("sazed_agent")


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
    counts = journal.counts()
    journal.close()
    print(f"Configurazione valida: bot \"{config.bot_name}\", lingua {config.language}.")
    print(f"Worker: {host}. Segreti presenti e protetti.")
    print("Registro: " + ", ".join(f"{state} {n}" for state, n in counts.items()) + ".")


def run(config_path):
    config = load(config_path)
    url, token = agent_url(config.worker_url), config.agent_token
    with single_instance(config.data_dir / "agent.lock"):
        journal = Journal(config.journal_path)
        try:
            link = WorkerLink(url, token, journal, lane_for, config.ping_interval, config.pong_timeout)
            asyncio.run(_serve(link))
        finally:
            journal.close()


async def _serve(link):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("agente avviato")
    await link.run(stop)
    log.info("agente fermato")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sazed_agent", description="La mente di sazed.")
    parser.add_argument("--config", default="config.toml", help="file di configurazione")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="verifica configurazione, segreti e registro")
    commands.add_parser("run", help="si collega al Worker e registra i messaggi")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "check":
            check(args.config)
        else:
            run(args.config)
    except ConfigError as err:
        print(f"Errore di configurazione: {err}", file=sys.stderr)
        return 1
    except AlreadyRunning as err:
        print(f"Già in esecuzione: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
