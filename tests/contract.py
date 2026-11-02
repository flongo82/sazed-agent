"""Prova di contratto: il vero agente contro il vero Worker di sazed-bot, in locale.

Gira nel container, senza rete (scripts/contract-test.sh): wrangler dev esegue il codice del
Worker, due finti Telegram rispondono uno al Worker e uno all'agente, e l'agente parte come in
produzione (python3 -m sazed_agent run) con una configurazione ricavata da config.example.toml.
Prova il protocollo da un capo all'altro: arretrato, ack, messaggi con l'agente collegato,
/stato all'agente, doppioni, arresto ordinato e arresto brusco.

Non è nella scoperta di unittest (scripts/test.sh): serve sazed-bot, con Node e node_modules.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from sazed_agent.messages import messages_for
from tests.fake_telegram import FakeTelegram

BOT = Path(os.environ.get("SAZED_BOT_DIR", "/bot"))  # parti di sazed-bot montate in sola lettura
PORT = 8797
WORKER = f"http://127.0.0.1:{PORT}"
WORKER_TELEGRAM_PORT = 8796
SECRET = "contract-webhook-secret"
AGENT_TOKEN = "contract-agent-token"
USER = 42
FIRST_NAME = "Ada"
BOT_NAME = "Custode"

failures = 0
_next_id = iter(range(1000, 10**9))


def check(name, cond, extra=""):
    global failures
    print(f"{'ok  ' if cond else 'FAIL'}  {name}" + (f"  → {extra}" if not cond and extra else ""), flush=True)
    failures += not cond


def wait_until(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if value := pred():
            return value
        time.sleep(0.1)
    return pred()


def message(text, lang="it"):
    """Un aggiornamento come quelli di Telegram, da Ada in chat privata."""
    uid = next(_next_id)
    msg = {"message_id": uid, "date": int(time.time()), "text": text,
           "from": {"id": USER, "is_bot": False, "first_name": FIRST_NAME, "language_code": lang},
           "chat": {"id": USER, "type": "private", "first_name": FIRST_NAME}}
    if text.startswith("/"):
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {"update_id": uid, "message": msg}


def post(update):
    request = urllib.request.Request(
        f"{WORKER}/telegram", data=json.dumps(update).encode(),
        headers={"content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": SECRET})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def http_status(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status
    except urllib.error.HTTPError as err:
        return err.code
    except OSError:
        return None


def replies(fake, update):
    mid = update["message"]["message_id"]
    return [p["text"] for _, p in fake.ok_calls("sendMessage")
            if p.get("reply_parameters", {}).get("message_id") == mid]


def reactions(fake, update):
    mid = update["message"]["message_id"]
    return [p["reaction"][0]["emoji"] for _, p in fake.ok_calls("setMessageReaction")
            if p["message_id"] == mid]


def quiet(fake, seconds):
    """Quante risposte manda fake in seconds secondi."""
    before = len(fake.ok_calls("sendMessage"))
    time.sleep(seconds)
    return len(fake.ok_calls("sendMessage")) - before


# ---------------------------------------------------------------- Worker e agente

def start_worker(work):
    """Il Worker di sazed-bot in wrangler dev, con il modello di configurazione e variabili di prova."""
    root = work / "worker"
    shutil.copytree(BOT / "worker/src", root / "src")
    shutil.copy(BOT / "worker/wrangler.example.jsonc", root / "wrangler.jsonc")
    (root / "node_modules").symlink_to(BOT / "worker/node_modules")
    variables = {
        "WEBHOOK_SECRET": SECRET, "AGENT_TOKEN": AGENT_TOKEN, "TELEGRAM_BOT_TOKEN": FakeTelegram().token,
        "ALLOWED_USER_ID": str(USER), "TELEGRAM_API_BASE": f"http://127.0.0.1:{WORKER_TELEGRAM_PORT}",
        "BOT_NAME": BOT_NAME, "LANGUAGE": "auto",
        "AGENT_TIMEOUT_SECONDS": "3", "AGENT_STATUS_GRACE_SECONDS": "1",
    }
    args = [str(BOT / "node/bin/node"), str(BOT / "worker/node_modules/wrangler/bin/wrangler.js"),
            "dev", "--config", str(root / "wrangler.jsonc"), "--port", str(PORT), "--ip", "127.0.0.1",
            "--persist-to", str(work / "state"), "--show-interactive-dev-session=false"]
    for key, value in variables.items():
        args += ["--var", f"{key}:{value}"]
    env = {**os.environ, "HOME": str(work), "XDG_CONFIG_HOME": str(work / "config"),
           "WRANGLER_SEND_METRICS": "false", "CLOUDFLARE_CF_FETCH_ENABLED": "false"}
    log = open(work / "worker.log", "wb")
    worker = subprocess.Popen(args, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                              start_new_session=True)  # gruppo a parte, per chiuderlo tutto insieme
    if not wait_until(lambda: http_status(f"{WORKER}/") == 404, 120):
        stop_group(worker)
        sys.exit("wrangler dev non è partito:\n" + (work / "worker.log").read_text(errors="replace"))
    return worker


def stop_group(process):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
            process.wait(timeout=5)
            return
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


def agent_config(work, telegram):
    """config.toml e segreti di prova, ricavati dal modello come fa setup.sh."""
    root = work / "agent"
    secrets = root / "secrets"
    secrets.mkdir(parents=True)
    secrets.chmod(0o700)
    for name, value in (("agent_token", AGENT_TOKEN), ("worker_url", WORKER), ("bot_token", telegram.token)):
        (secrets / name).write_text(value)
        (secrets / name).chmod(0o600)
    text = Path("config.example.toml").read_text(encoding="utf-8")
    for key, value in (("bot_name", json.dumps(BOT_NAME)), ("api_base", json.dumps(telegram.api_base)),
                       ("ping_interval", "1"), ("pong_timeout", "0.5")):
        text, n = re.subn(rf"^{key} = .*$", f"{key} = {value}", text, count=1, flags=re.M)
        assert n == 1, f"{key} non trovato in config.example.toml"
    (root / "config.toml").write_text(text, encoding="utf-8")
    return root / "config.toml"


class Agent:
    def __init__(self, config, log):
        self.config, self.log, self.process = config, log, None

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "sazed_agent", "--config", str(self.config), "run"],
            stdout=self.log, stderr=subprocess.STDOUT)

    def stop(self, sig=signal.SIGTERM):
        self.process.send_signal(sig)
        try:
            return self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return None


# ---------------------------------------------------------------- prova

def main():
    work = Path(tempfile.mkdtemp(prefix="sazed-contract-"))
    tg_worker = FakeTelegram().start(WORKER_TELEGRAM_PORT)  # risponde al Worker
    tg_agent = FakeTelegram().start()                         # risponde all'agente
    print("Avvio di wrangler dev…", flush=True)
    worker = start_worker(work)
    agent_log = open(work / "agent.log", "wb")
    agent = Agent(agent_config(work, tg_agent), agent_log)
    it, en = messages_for("it"), messages_for("en")
    try:
        run(agent, tg_worker, tg_agent, it, en)
    finally:
        if agent.process and agent.process.poll() is None:
            agent.stop()
        stop_group(worker)
        tg_worker.stop()
        tg_agent.stop()
        agent_log.close()
    log = (work / "agent.log").read_text(errors="replace")
    check("nel log dell'agente nessun errore imprevisto", "Traceback" not in log, log[-2000:])
    check("nel log dell'agente nessun segreto",
          tg_agent.token not in log and AGENT_TOKEN not in log)
    if failures:
        print("\n--- log dell'agente ---\n" + log[-4000:])
        print("--- log di wrangler dev ---\n" + (work / "worker.log").read_text(errors="replace")[-4000:])
    shutil.rmtree(work, ignore_errors=True)
    print(f"\n{failures} controlli falliti." if failures else "\nTutti i controlli superati.")
    return 1 if failures else 0


def run(agent, tg_worker, tg_agent, it, en):
    hello_it, hello_en = it["hello"](FIRST_NAME), en["hello"](FIRST_NAME)

    print("\n# Fase 1: l'agente non si è mai collegato")
    first = message("ciao")
    check("il Worker accetta il messaggio", post(first) == 200)
    text = (wait_until(lambda: replies(tg_worker, first)) or [""])[0]
    check("il Worker risponde che il messaggio è in coda",
          text.startswith(f"{hello_it}, l'agente {BOT_NAME} non si è mai collegato")
          and text.endswith("Messaggi da processare: 1."), text)
    status = message("/stato")
    post(status)
    text = (wait_until(lambda: replies(tg_worker, status)) or [""])[0]
    check("con l'agente spento /stato risponde il Worker", "Messaggi da processare: 1." in text, text)

    print("\n# Fase 2: l'agente si collega e riceve l'arretrato")
    worker_replies = len(tg_worker.ok_calls("sendMessage"))
    agent.start()
    wait_until(lambda: "👌" in reactions(tg_agent, first), 30)
    check("reazioni 👀 e poi 👌", reactions(tg_agent, first) == ["👀", "👌"], reactions(tg_agent, first))
    check("una sola risposta, in risposta al messaggio",
          replies(tg_agent, first) == [f"{hello_it}, {it['received']}"], replies(tg_agent, first))
    check("\"sta scrivendo...\" durante l'elaborazione",
          any(p["action"] == "typing" for _, p in tg_agent.ok_calls("sendChatAction")))
    check("il /stato di prima non arriva all'agente", replies(tg_agent, status) == [])
    check("il Worker tace", len(tg_worker.ok_calls("sendMessage")) == worker_replies)

    print("\n# Fase 3: l'agente è collegato")
    link = message("https://example.org/risotto")
    post(link)
    wait_until(lambda: "👌" in reactions(tg_agent, link))
    check("un link arriva subito all'agente, che risponde", len(replies(tg_agent, link)) == 1)
    check("il Worker non dice nulla", replies(tg_worker, link) == [])
    status = message("/stato")
    post(status)
    text = (wait_until(lambda: replies(tg_agent, status)) or [""])[0]
    check("/stato passa all'agente, che risponde", text == f"{hello_it}, {it['status'](BOT_NAME, 0, 0)}", text)
    check("nessuna risposta di riserva dal Worker dopo l'attesa", quiet(tg_worker, 2.5) == 0)
    post(link)  # Telegram può riconsegnare un aggiornamento già consegnato
    check("un doppione non ha una seconda risposta", quiet(tg_agent, 2) == 0 and len(replies(tg_agent, link)) == 1)
    english = message("hello", lang="en")
    post(english)
    wait_until(lambda: replies(tg_agent, english))
    check("con l'app in inglese l'agente risponde in inglese",
          replies(tg_agent, english) == [f"{hello_en}, {en['received']}"], replies(tg_agent, english))

    print("\n# Fase 4: arresto ordinato")
    check("l'agente si ferma con SIGTERM", agent.stop() == 0)
    status = message("/stato")
    post(status)
    text = (wait_until(lambda: replies(tg_worker, status)) or [""])[0]
    check("il Worker sa che l'agente non è collegato",
          text.startswith(f"{hello_it}, l'agente {BOT_NAME} non è collegato"), text)
    check("tutto confermato: la coda del Worker è vuota", text.endswith("Messaggi da processare: 0."), text)
    later = message("una nota per dopo")
    post(later)
    text = (wait_until(lambda: replies(tg_worker, later)) or [""])[0]
    check("un messaggio ad agente spento resta in coda", text.endswith("Messaggi da processare: 1."), text)

    print("\n# Fase 5: ripartenza")
    sent = len(tg_agent.ok_calls("sendMessage"))
    agent.start()
    wait_until(lambda: "👌" in reactions(tg_agent, later), 30)
    check("l'agente elabora il messaggio rimasto in coda", len(replies(tg_agent, later)) == 1)
    check("e nient'altro: il registro ricorda quelli già fatti", quiet(tg_agent, 1.5) == 0
          and len(tg_agent.ok_calls("sendMessage")) == sent + 1)

    print("\n# Fase 6: arresto brusco")
    agent.stop(signal.SIGKILL)
    lost = message("scritto mentre l'agente cade")
    post(lost)
    time.sleep(1)
    sent = len(tg_agent.ok_calls("sendMessage"))
    agent.start()
    wait_until(lambda: "👌" in reactions(tg_agent, lost), 30)
    check("dopo un arresto brusco il messaggio arriva comunque", len(replies(tg_agent, lost)) == 1)
    check("senza doppioni", quiet(tg_agent, 1.5) == 0 and len(tg_agent.ok_calls("sendMessage")) == sent + 1)
    check("l'agente si ferma con SIGTERM", agent.stop() == 0)
    status = message("/stato")
    post(status)
    text = (wait_until(lambda: replies(tg_worker, status)) or [""])[0]
    check("alla fine la coda del Worker è vuota", text.endswith("Messaggi da processare: 0."), text)


if __name__ == "__main__":
    sys.exit(main())
