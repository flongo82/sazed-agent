"""Punto d'ingresso: python3 -m sazed_agent <comando>.

Per ora c'è solo "check", che verifica configurazione, segreti e registro senza collegarsi a
nulla. La connessione con il Worker arriva con la seconda tappa del primo passo.
"""

import argparse
import sys
from urllib.parse import urlsplit

from .config import ConfigError, load
from .journal import Journal


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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sazed_agent", description="La mente di sazed.")
    parser.add_argument("--config", default="config.toml", help="file di configurazione")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="verifica configurazione, segreti e registro")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            check(args.config)
    except ConfigError as err:
        print(f"Errore di configurazione: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
