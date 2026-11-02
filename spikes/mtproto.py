"""Prova MTProto: il bot scarica via MTProto un file oltre i 20 MB, con il webhook attivo.

È un esperimento contro il vero Telegram e il Worker pubblicato, da lanciare a mano
(scripts/mtproto-probe.sh), non un test: risponde alle domande del passo 4.1 di
architettura.md prima di costruire i download veri.

1. Convivenza: con la sessione MTProto del bot aperta (senza aggiornamenti), un messaggio
   nuovo arriva al Worker e all'agente come sempre, e il webhook non segnala errori.
2. Recupero: dal message_id del registro il bot ritrova il messaggio e ne scarica il file,
   che l'API dei bot rifiuta perché supera i 20 MB.
3. Velocità: MB/s del download, e con che cosa Telethon decifra.

Il registro si legge senza scriverci, quindi l'agente può restare acceso. Il file scaricato
si cancella subito, anche se la prova fallisce. Nessun segreto viene stampato.
Uso: python3 -m spikes.mtproto [update_id]   (senza: attende un file nuovo nel registro)
"""

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request

from telethon import TelegramClient, events
from telethon.crypto import aes
from telethon.sessions import StringSession

from sazed_agent.config import ConfigError, load

BOT_API_LIMIT = 20 * 1024 * 1024
FILE_KINDS = ("document", "video", "audio", "voice", "video_note", "animation")
WAIT = 300  # secondi di attesa per il file nuovo

failures = 0


def check(name, cond, extra=""):
    global failures
    failures += 0 if cond else 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}" + (f"  → {extra}" if extra else ""), flush=True)


def megabytes(size):
    return f"{size / 1024 / 1024:.1f} MB"


def file_of(message):
    """Tipo, dimensione e file_id del file di un messaggio dell'API dei bot, o None."""
    for kind in FILE_KINDS:
        if kind in message:
            return kind, message[kind].get("file_size") or 0, message[kind]["file_id"]
    if message.get("photo"):
        biggest = message["photo"][-1]
        return "photo", biggest.get("file_size") or 0, biggest["file_id"]
    return None


def items(journal_path, after=0):
    """Gli elementi del registro con update_id maggiore di after, dal più recente."""
    db = sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT update_id, received_at, chat_id, message_id, payload FROM inbox"
                          " WHERE update_id > ? ORDER BY update_id DESC", (after,)).fetchall()
    finally:
        db.close()
    return [(u, r, c, m, json.loads(p)) for u, r, c, m, p in rows]


def last_update_id(journal_path):
    found = items(journal_path)
    return found[0][0] if found else 0


def bot_api(config, method, **params):
    """Una chiamata all'API dei bot; il token sta nell'indirizzo, che non viene mai stampato."""
    request = urllib.request.Request(f"{config.telegram_api_base}/bot{config.bot_token}/{method}",
                                     data=json.dumps(params).encode(),
                                     headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        return json.load(err)
    except (urllib.error.URLError, TimeoutError) as err:
        return {"ok": False, "description": f"Telegram non raggiungibile ({type(err).__name__})"}


def webhook_info(config):
    return bot_api(config, "getWebhookInfo").get("result") or {}


def describe_webhook(config):
    """Lo stato del webhook prima della prova, senza giudicarlo: un errore vecchio non conta."""
    info = webhook_info(config)
    error = (f"ultimo errore {time.strftime('%d/%m %H:%M', time.localtime(info['last_error_date']))}:"
             f" {info.get('last_error_message', '')}" if info.get("last_error_date") else "nessun errore")
    print(f"      webhook {'sul Worker' if info.get('url') == config.worker_url + '/telegram' else 'altrove'},"
          f" {info.get('pending_update_count')} in attesa, {error}", flush=True)


def webhook(config, since):
    """Controlla che il webhook punti al Worker, senza errori da since e senza arretrato."""
    info = webhook_info(config)
    check("il webhook punta ancora al Worker", info.get("url") == config.worker_url + "/telegram")
    error_at = info.get("last_error_date") or 0
    check("Telegram non segnala errori di consegna al Worker", error_at < since,
          info.get("last_error_message", "") if error_at >= since else "")
    check("nessun aggiornamento in attesa sul webhook", info.get("pending_update_count") == 0,
          f"{info.get('pending_update_count')} in attesa")


def read_session(path):
    if not path.exists():
        return ""
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{path} è leggibile da altri utenti: chmod 600 {path}")
    return path.read_text(encoding="utf-8").strip()


def save_session(path, value):
    """Scrive la sessione con permessi 600, in modo atomico: un'interruzione non la rovina."""
    old = os.umask(0o077)
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        os.umask(old)


def crypto_backend():
    if aes.cryptg:
        return "cryptg"
    return "OpenSSL (libssl)" if aes.libssl.decrypt_ige else "puro Python (pyaes)"


async def wait_for_file(config, after):
    print(f"\nManda ora al bot un file oltre i 20 MB, come file (graffetta → File). Attendo fino a "
          f"{WAIT // 60} minuti; l'agente deve essere acceso.", flush=True)
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        for item in items(config.journal_path, after):
            if file_of(item[4].get("message") or {}):
                return item
        await asyncio.sleep(2)
    return None


async def probe(config, update_id):
    started = time.time()
    before = last_update_id(config.journal_path)
    print("# Prima della prova", flush=True)
    describe_webhook(config)

    print("\n# Sessione MTProto del bot", flush=True)
    session_file = config.mtproto_session_file
    client = TelegramClient(StringSession(read_session(session_file)), config.api_id, config.api_hash,
                            receive_updates=False)
    updates = []
    client.add_event_handler(lambda update: updates.append(type(update).__name__), events.Raw)
    t = time.monotonic()
    await client.connect()
    try:
        new_login = not await client.is_user_authorized()
        if new_login:
            await client.sign_in(bot_token=config.bot_token)
            save_session(session_file, client.session.save())
        me = await client.get_me()
        check("accesso come bot, non come utente", me.bot and str(me.id) == config.bot_token.split(":")[0],
              f"{'nuovo accesso, sessione salvata' if new_login else 'sessione salvata riusata'},"
              f" {time.monotonic() - t:.1f} s")
        print(f"      Telethon decifra con: {crypto_backend()}", flush=True)

        print("\n# Convivenza con il webhook", flush=True)
        if update_id:
            item = next((i for i in items(config.journal_path, update_id - 1) if i[0] == update_id), None)
            check(f"elemento {update_id} nel registro", item is not None)
        else:
            item = await wait_for_file(config, before)
            check("il file è arrivato al Worker e all'agente con la sessione MTProto aperta",
                  item is not None, "" if item else "nessun file nuovo nel registro")
        if not item:
            return
        update, chat_id, message_id = item[4], item[2], item[3]
        found = file_of(update.get("message") or {})
        if not found:
            check("l'elemento contiene un file", False)
            return
        kind, size, file_id = found
        print(f"      elemento {item[0]}: {kind}, {megabytes(size)}", flush=True)
        check("il file supera i 20 MB dell'API dei bot", size > BOT_API_LIMIT, megabytes(size))
        refused = bot_api(config, "getFile", file_id=file_id)
        check("l'API dei bot non lo scarica", size <= BOT_API_LIMIT or not refused.get("ok"),
              refused.get("description", "") if not refused.get("ok") else "getFile lo ha accettato")

        print("\n# Download via MTProto", flush=True)
        message = await client.get_messages(None, ids=message_id)
        check("il bot ritrova il messaggio dal suo message_id", message is not None)
        if message is None:
            return
        check("il messaggio è quello della chat privata", getattr(message.peer_id, "user_id", None) == chat_id)
        check("il messaggio ha un file della stessa dimensione", message.file is not None and message.file.size == size,
              megabytes(message.file.size) if message.file else "")
        folder = tempfile.mkdtemp(prefix="sazed-mtproto-")
        try:
            t = time.monotonic()
            path = await client.download_media(message, file=folder)
            elapsed = time.monotonic() - t
            got = os.path.getsize(path) if path else 0
            check("file scaricato per intero", got == size, f"{megabytes(got)} in {elapsed:.1f} s,"
                  f" {got / 1024 / 1024 / max(elapsed, 0.001):.1f} MB/s")
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        check("file cancellato subito dopo", not os.path.exists(folder))
    finally:
        await client.disconnect()
        print("\n# Dopo la prova", flush=True)
        check("la sessione MTProto non ha ricevuto aggiornamenti", not updates,
              ", ".join(sorted(set(updates))))
        webhook(config, started)


def main(argv):
    logging.basicConfig(level=logging.WARNING)
    update_id = int(argv[0]) if argv else None
    try:
        config = load("config.toml")
        config.api_id, config.api_hash  # verifica subito i segreti MTProto
        asyncio.run(probe(config, update_id))
    except ConfigError as err:
        print(f"Configurazione: {err}", file=sys.stderr)
        return 1
    except Exception as err:  # errori di Telegram (api_id rifiutato, attese imposte, rete)
        print(f"FAIL  {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    print(f"\n{failures} controlli falliti." if failures else "\nTutti i controlli superati.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
