"""Testi dell'agente, una voce per lingua: nessun testo rivolto all'utente sta nel codice.

Stesse regole del catalogo di sazed-bot (worker/src/messages.js): blocchi con le stesse chiavi,
saluto per nome e poi il fatto, nome del comando di stato preso da qui. Per aggiungere una
lingua basta aggiungere un blocco: i test controllano che abbia tutte le chiavi.
"""

def _it_retry(set_aside, sources):
    parts = [f"{set_aside} {'messaggio messo' if set_aside == 1 else 'messaggi messi'} da parte" if set_aside else None,
             f"{sources} {'fonte non letta' if sources == 1 else 'fonti non lette'} per un problema passeggero"
             if sources else None]
    return f"🔁 Da riprovare: {set_aside + sources} ({', '.join(p for p in parts if p)})"


def _en_retry(set_aside, sources):
    parts = [f"{set_aside} {'message' if set_aside == 1 else 'messages'} set aside" if set_aside else None,
             f"{sources} {'source' if sources == 1 else 'sources'} not read because of a passing problem"
             if sources else None]
    return f"🔁 To retry: {set_aside + sources} ({', '.join(p for p in parts if p)})"


MESSAGES = {
    "it": {
        "hello": lambda first=None: f"Ciao {first}" if first else "Ciao",
        "unknown_command": lambda command: f"non conosco il comando {command}.",
        "unsupported": "questo tipo di messaggio non so ancora custodirlo.",
        "no_answer": "non sono riuscito a rispondere a questa domanda: riprova tra poco.",
        "here_it_is": lambda files: "eccolo." if files == 1 else "eccoli.",
        "retry": "Riprova",
        "retrying": "Riprovo.",
        "nothing_to_retry": "Non c'è più nulla da riprovare.",
        "balance": lambda sent, done, working, set_aside, sources, lost: "\n".join(line for line in (
            f"finora mi hai mandato {sent} {'messaggio' if sent == 1 else 'messaggi'}.",
            f"✅ Elaborati: {done}",
            f"⏳ In lavorazione: {working}" if working else None,
            _it_retry(set_aside, sources) if set_aside + sources else "🔁 Niente da riprovare",
            f"⛔ Fonti non recuperabili: {lost} (sparite, o in un formato che non so leggere)" if lost else None)
            if line),
        "show_them": "Mostrameli",
        "retry_all": "Riprova tutti",
        "to_retry": "🔁 Questo è da riprovare.",
        "showing": lambda n: f"Te ne mostro {n}.",
        "older": lambda n: "Ce n'è un altro, più indietro." if n == 1 else f"Ce ne sono altri {n}, più indietro.",
        "further_back": "Più indietro",
        "retrying_many": lambda n: "Riprovo." if n == 1 else f"Ne riprovo {n}.",
        "balance_command": "bilancio",
        "balance_description": "Quanti messaggi ho elaborato, quanti sono da riprovare",
        "unknown": "di questo non sono riuscito a capire di che cosa parla: scrivimi due parole e le terrò insieme.",
        "unreadable": ("questo non sono riuscito a leggerlo, quindi non so di che cosa parla: scrivimi due "
                       "parole e le terrò insieme."),
        "reel_hint": ("Se Instagram ti lascia scaricare il reel (Condividi → Scarica), puoi anche mandarmelo "
                      "come video."),
        "file_gone": lambda title=None: (
            f"non riesco più a rimandarti {f'«{title}»' if title else 'questo file'}: su Telegram non c'è più."),
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
        "here_it_is": lambda files: "here it is." if files == 1 else "here they are.",
        "retry": "Retry",
        "retrying": "Trying again.",
        "nothing_to_retry": "There is nothing left to retry.",
        "balance": lambda sent, done, working, set_aside, sources, lost: "\n".join(line for line in (
            f"so far you have sent me {sent} {'message' if sent == 1 else 'messages'}.",
            f"✅ Processed: {done}",
            f"⏳ In progress: {working}" if working else None,
            _en_retry(set_aside, sources) if set_aside + sources else "🔁 Nothing to retry",
            f"⛔ Sources lost: {lost} (gone, or in a format I cannot read)" if lost else None)
            if line),
        "show_them": "Show them",
        "retry_all": "Retry all",
        "to_retry": "🔁 This one is to retry.",
        "showing": lambda n: f"Showing {n}.",
        "older": lambda n: "There is one more, further back." if n == 1 else f"There are {n} more, further back.",
        "further_back": "Further back",
        "retrying_many": lambda n: "Trying again." if n == 1 else f"Trying {n} again.",
        "balance_command": "balance",
        "balance_description": "How many messages I processed, how many to retry",
        "unknown": "I couldn't tell what this is about: write me a few words and I'll keep them with it.",
        "unreadable": ("I couldn't read this, so I don't know what it is about: write me a few words and I'll keep "
                       "them with it."),
        "reel_hint": ("If Instagram lets you download the reel (Share → Download), you can also send it to me "
                      "as a video."),
        "file_gone": lambda title=None: (
            f"I can't send you {f'“{title}”' if title else 'this file'} back any more: it is gone from Telegram."),
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
# E quello del bilancio (/bilancio, /balance).
BALANCE_COMMANDS = frozenset(f"/{t['balance_command']}" for t in MESSAGES.values())


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
