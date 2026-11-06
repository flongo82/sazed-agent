"""Testi dell'agente, una voce per lingua: nessun testo rivolto all'utente sta nel codice.

Stesse regole del catalogo di sazed-bot (worker/src/messages.js): blocchi con le stesse chiavi,
saluto per nome e poi il fatto, nome del comando di stato preso da qui. Per aggiungere una
lingua basta aggiungere un blocco: i test controllano che abbia tutte le chiavi.
"""

MESSAGES = {
    "it": {
        "hello": lambda first=None: f"Ciao {first}" if first else "Ciao",
        "unknown_command": lambda command: f"non conosco il comando {command}.",
        "unsupported": "questo tipo di messaggio non so ancora custodirlo.",
        "no_answer": "non sono riuscito a rispondere a questa domanda: riprova tra poco.",
        "status": lambda name, pending, failed, kept: (
            f"l'agente {name} è collegato. Messaggi da processare: {pending}, in errore: {failed}. "
            f"Elementi custoditi: {kept}."
        ),
        "failed": lambda attempts: (
            f"non sono riuscito a elaborare questo messaggio dopo {attempts} "
            f"{'tentativo' if attempts == 1 else 'tentativi'}: lo tengo da parte."
        ),
        "status_command": "stato",
    },
    "en": {
        "hello": lambda first=None: f"Hi {first}" if first else "Hi",
        "unknown_command": lambda command: f"I don't know the command {command}.",
        "unsupported": "I can't keep this kind of message yet.",
        "no_answer": "I couldn't answer this question: try again in a little while.",
        "status": lambda name, pending, failed, kept: (
            f"the {name} agent is connected. Messages to process: {pending}, failed: {failed}. "
            f"Items kept: {kept}."
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


def language_for(setting, language_code=None):
    """La lingua giusta: quella fissata, o con "auto" quella dell'app Telegram.

    Una lingua senza testi ricade sull'inglese, come in sazed-bot.
    """
    setting = (setting or "auto").strip().lower()
    lang = (language_code or "")[:2].lower() if setting == "auto" else setting
    return lang if lang in MESSAGES else "en"


def messages_for(setting, language_code=None):
    """I testi nella lingua giusta (language_for)."""
    return MESSAGES[language_for(setting, language_code)]
