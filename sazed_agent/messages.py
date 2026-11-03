"""Testi dell'agente, una voce per lingua: nessun testo rivolto all'utente sta nel codice.

Stesse regole del catalogo di sazed-bot (worker/src/messages.js): blocchi con le stesse chiavi,
saluto per nome e poi il fatto, nome del comando di stato preso da qui. Per aggiungere una
lingua basta aggiungere un blocco: i test controllano che abbia tutte le chiavi.
"""

MONTHS_IT = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
             "settembre", "ottobre", "novembre", "dicembre")
MONTHS_EN = ("January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December")
FILES_IT = {"photo": "la foto", "video": "il video", "video_note": "il videomessaggio",
            "voice": "il vocale", "audio": "l'audio", "document": "il documento"}
FILES_EN = {"photo": "the photo", "video": "the video", "video_note": "the video message",
            "voice": "the voice message", "audio": "the audio", "document": "the document"}

MESSAGES = {
    "it": {
        "hello": lambda first=None: f"Ciao {first}" if first else "Ciao",
        "saved": lambda things: f"ho salvato {things}.",
        "updated": lambda things: f"ho aggiornato {things}.",
        "and": " e ",
        "note": "la tua nota",
        "link": lambda platform=None: f"il link di {platform}" if platform else "il link",
        "links": lambda n: f"i {n} link",
        "file": lambda source: FILES_IT.get(source, "il file"),
        "later": "il contenuto lo leggerò più avanti.",
        "already": lambda thing, when: f"{thing} era già tra ciò che custodisco, dal {when}.",
        "already_many": lambda n: f"{n} di questi erano già tra ciò che custodisco.",
        "date": lambda day, month, year=None: f"{day} {MONTHS_IT[month - 1]}" + (f" {year}" if year else ""),
        "unknown_command": lambda command: f"non conosco il comando {command}.",
        "unsupported": "questo tipo di messaggio non so ancora custodirlo.",
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
        "saved": lambda things: f"I saved {things}.",
        "updated": lambda things: f"I updated {things}.",
        "and": " and ",
        "note": "your note",
        "link": lambda platform=None: f"the {platform} link" if platform else "the link",
        "links": lambda n: f"the {n} links",
        "file": lambda source: FILES_EN.get(source, "the file"),
        "later": "I'll read its content later.",
        "already": lambda thing, when: f"{thing} was already among the things I keep, since {when}.",
        "already_many": lambda n: f"{n} of them were already among the things I keep.",
        "date": lambda day, month, year=None: f"{day} {MONTHS_EN[month - 1]}" + (f" {year}" if year else ""),
        "unknown_command": lambda command: f"I don't know the command {command}.",
        "unsupported": "I can't keep this kind of message yet.",
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


def messages_for(setting, language_code=None):
    """I testi nella lingua giusta: quella fissata, o con "auto" quella dell'app Telegram.

    Una lingua senza testi ricade sull'inglese, come in sazed-bot.
    """
    setting = (setting or "auto").strip().lower()
    lang = (language_code or "")[:2].lower() if setting == "auto" else setting
    return MESSAGES.get(lang, MESSAGES["en"])
