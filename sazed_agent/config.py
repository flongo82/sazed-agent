"""Configurazione dell'agente: config.toml, più i segreti letti dai file che indica.

I percorsi sono relativi alla cartella del file di configurazione. I segreti si leggono solo
quando servono, e un errore dice quale file manca senza mai mostrarne il contenuto.
"""

import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .messages import MESSAGES


class ConfigError(Exception):
    """Configurazione o segreto mancante o non valido."""


@dataclass(frozen=True)
class Reactions:
    received: str
    done: str
    failed: str


@dataclass(frozen=True)
class Config:
    root: Path
    bot_name: str
    language: str
    data_dir: Path
    agent_token_file: Path
    worker_url_file: Path
    bot_token_file: Path
    telegram_api_base: str
    reactions: Reactions
    ping_interval: float
    pong_timeout: float
    retry_delays: tuple

    @property
    def journal_path(self):
        return self.data_dir / "coppermind.db"

    @property
    def agent_token(self):
        return _secret(self.agent_token_file)

    @property
    def worker_url(self):
        return _secret(self.worker_url_file).rstrip("/")

    @property
    def bot_token(self):
        return _secret(self.bot_token_file)


def load(path):
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path} non esiste: lancia scripts/setup.sh") from None
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"{path} non è TOML valido: {err}") from None

    root = path.parent
    section = lambda name: _table(raw, name)
    secrets, storage, telegram = section("secrets"), section("storage"), section("telegram")
    reactions, connection, retries = section("reactions"), section("connection"), section("retries")

    language = _text(raw, "language").lower()
    if language != "auto" and language not in MESSAGES:
        raise ConfigError(f"language deve essere auto o una di: {', '.join(MESSAGES)}")

    delays = retries.get("delays")
    if not isinstance(delays, list) or not delays or not all(_positive(d) for d in delays):
        raise ConfigError("retries.delays deve essere un elenco non vuoto di secondi positivi")

    return Config(
        root=root,
        bot_name=_text(raw, "bot_name"),
        language=language,
        data_dir=root / _text(storage, "data_dir", "storage."),
        agent_token_file=root / _text(secrets, "agent_token", "secrets."),
        worker_url_file=root / _text(secrets, "worker_url", "secrets."),
        bot_token_file=root / _text(secrets, "bot_token", "secrets."),
        telegram_api_base=_text(telegram, "api_base", "telegram.").rstrip("/"),
        reactions=Reactions(
            received=_text(reactions, "received", "reactions."),
            done=_text(reactions, "done", "reactions."),
            failed=_text(reactions, "failed", "reactions."),
        ),
        ping_interval=_seconds(connection, "ping_interval"),
        pong_timeout=_seconds(connection, "pong_timeout"),
        retry_delays=tuple(float(d) for d in delays),
    )


def _table(raw, name):
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] deve essere una sezione")
    return value


def _text(table, key, prefix=""):
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{prefix}{key} manca o è vuoto")
    return value.strip()


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _seconds(table, key):
    value = table.get(key)
    if not _positive(value):
        raise ConfigError(f"connection.{key} deve essere un numero di secondi positivo")
    return float(value)


def _secret(path):
    """Il contenuto di un file segreto, senza spazi né a capo. Rifiuta file leggibili da altri."""
    try:
        mode = path.stat().st_mode
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise ConfigError(f"{path} non esiste: lancia scripts/setup.sh") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{path} è leggibile da altri utenti: chmod 600 {path}")
    if not value:
        raise ConfigError(f"{path} è vuoto")
    return value
