"""Testi dell'agente, una voce per lingua: nessun testo rivolto all'utente sta nel codice.

Stesse regole del catalogo di sazed-bot (worker/src/messages.js): blocchi con le stesse chiavi,
saluto per nome e poi il fatto, nome del comando di stato preso da qui. Per aggiungere una
lingua basta aggiungere un blocco: i test controllano che abbia tutte le chiavi.
"""

MESSAGES = {
    "it": {
        "hello": lambda first=None: f"Ciao {first}" if first else "Ciao",
        "received": "ho ricevuto il tuo messaggio: per ora lo custodisco, presto saprò leggerlo.",
        "status": lambda name, pending, failed: (
            f"l'agente {name} è collegato. Messaggi da processare: {pending}, in errore: {failed}."
        ),
        "failed": lambda attempts: (
            f"non sono riuscito a elaborare questo messaggio dopo {attempts} "
            f"{'tentativo' if attempts == 1 else 'tentativi'}: lo tengo da parte."
        ),
        "status_command": "stato",
    },
    "en": {
        "hello": lambda first=None: f"Hi {first}" if first else "Hi",
        "received": "I received your message: for now I'm keeping it safe, soon I'll be able to read it.",
        "status": lambda name, pending, failed: (
            f"the {name} agent is connected. Messages to process: {pending}, failed: {failed}."
        ),
        "failed": lambda attempts: (
            f"I couldn't process this message after {attempts} "
            f"{'attempt' if attempts == 1 else 'attempts'}: I'm keeping it aside."
        ),
        "status_command": "status",
    },
}

# Il comando di stato in ogni lingua del catalogo (/stato, /status), qualunque sia la lingua.
STATUS_COMMANDS = frozenset(f"/{t['status_command']}" for t in MESSAGES.values())


def messages_for(setting, language_code=None):
    """I testi nella lingua giusta: quella fissata, o con "auto" quella dell'app Telegram.

    Una lingua senza testi ricade sull'inglese, come in sazed-bot.
    """
    setting = (setting or "auto").strip().lower()
    lang = (language_code or "")[:2].lower() if setting == "auto" else setting
    return MESSAGES.get(lang, MESSAGES["en"])
