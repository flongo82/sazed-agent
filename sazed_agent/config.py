"""Configurazione dell'agente: config.toml, più i segreti letti dai file che indica.

I percorsi sono relativi alla cartella del file di configurazione. I segreti si leggono solo
quando servono, e un errore dice quale file manca senza mai mostrarne il contenuto.
"""

import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .messages import MESSAGES


MTPROTO_SETUP = "scripts/setup-mtproto.sh"
CLAUDE_SETUP = "scripts/setup-claude.sh"


class ConfigError(Exception):
    """Configurazione o segreto mancante o non valido."""


@dataclass(frozen=True)
class Reactions:
    received: str
    done: str
    failed: str


@dataclass(frozen=True)
class Claude:
    api_base: str
    model: str     # il modello si sceglie qui, a ogni richiesta: la chiave vale per tutti


@dataclass(frozen=True)
class Config:
    root: Path
    bot_name: str
    language: str
    data_dir: Path
    agent_token_file: Path
    worker_url_file: Path
    bot_token_file: Path
    api_id_file: Path | None      # MTProto, per i file oltre i 20 MB: scripts/setup-mtproto.sh
    api_hash_file: Path | None
    mtproto_dir: Path | None      # la sessione MTProto del bot, l'unico segreto che si scrive
    anthropic_api_key_file: Path | None  # la chiave dell'API di Claude: scripts/setup-claude.sh
    claude_settings: Claude | None       # [claude]: scripts/setup-claude.sh
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

    @property
    def api_id(self):
        value = _secret(_configured(self.api_id_file, "api_id", MTPROTO_SETUP), MTPROTO_SETUP)
        if not value.isdigit():
            raise ConfigError(f"{self.api_id_file} non contiene un api_id: lancia {MTPROTO_SETUP}")
        return int(value)

    @property
    def api_hash(self):
        return _secret(_configured(self.api_hash_file, "api_hash", MTPROTO_SETUP), MTPROTO_SETUP)

    @property
    def mtproto_session_file(self):
        return _configured(self.mtproto_dir, "mtproto_dir", MTPROTO_SETUP) / "session"

    @property
    def anthropic_api_key(self):
        return _secret(_configured(self.anthropic_api_key_file, "anthropic_api_key", CLAUDE_SETUP), CLAUDE_SETUP)

    @property
    def claude(self):
        if self.claude_settings is None:
            raise ConfigError(f"[claude] manca in config.toml: lancia {CLAUDE_SETUP}")
        return self.claude_settings


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
    claude = section("claude") if "claude" in raw else None

    language = _text(raw, "language").lower()
    if language != "auto" and language not in MESSAGES:
        raise ConfigError(f"language deve essere auto o una di: {', '.join(MESSAGES)}")

    delays = retries.get("delays")
    if not isinstance(delays, list) or not delays or not all(_positive(d) for d in delays):
        raise ConfigError("retries.delays deve essere un elenco non vuoto di secondi positivi")
    ping_interval, pong_timeout = _seconds(connection, "ping_interval"), _seconds(connection, "pong_timeout")
    if pong_timeout >= ping_interval:
        raise ConfigError("connection.pong_timeout deve essere minore di connection.ping_interval")

    return Config(
        root=root,
        bot_name=_text(raw, "bot_name"),
        language=language,
        data_dir=root / _text(storage, "data_dir", "storage."),
        agent_token_file=root / _text(secrets, "agent_token", "secrets."),
        worker_url_file=root / _text(secrets, "worker_url", "secrets."),
        bot_token_file=root / _text(secrets, "bot_token", "secrets."),
        api_id_file=_optional_path(root, secrets, "api_id"),
        api_hash_file=_optional_path(root, secrets, "api_hash"),
        mtproto_dir=_optional_path(root, secrets, "mtproto_dir"),
        anthropic_api_key_file=_optional_path(root, secrets, "anthropic_api_key"),
        claude_settings=None if claude is None else Claude(
            api_base=_text(claude, "api_base", "claude.").rstrip("/"), model=_text(claude, "model", "claude.")),
        telegram_api_base=_text(telegram, "api_base", "telegram.").rstrip("/"),
        reactions=Reactions(
            received=_text(reactions, "received", "reactions."),
            done=_text(reactions, "done", "reactions."),
            failed=_text(reactions, "failed", "reactions."),
        ),
        ping_interval=ping_interval,
        pong_timeout=pong_timeout,
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


def _optional_path(root, secrets, key):
    """Un percorso di [secrets] che può mancare: un config.toml di prima di MTProto o di Claude
    non lo ha."""
    return root / _text(secrets, key, "secrets.") if key in secrets else None


def _configured(path, key, setup):
    if path is None:
        raise ConfigError(f"secrets.{key} manca in config.toml: lancia {setup}")
    return path


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _seconds(table, key):
    value = table.get(key)
    if not _positive(value):
        raise ConfigError(f"connection.{key} deve essere un numero di secondi positivo")
    return float(value)


def _secret(path, setup="scripts/setup.sh"):
    """Il contenuto di un file segreto, senza spazi né a capo. Rifiuta file leggibili da altri."""
    try:
        mode = path.stat().st_mode
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise ConfigError(f"{path} non esiste: lancia {setup}") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{path} è leggibile da altri utenti: chmod 600 {path}")
    if not value:
        raise ConfigError(f"{path} è vuoto")
    return value
