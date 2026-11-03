"""Lettura di un messaggio Telegram: che cosa contiene e che cosa ne va in coppermind.

Per ora senza modello: un testo senza link è una nota; ogni link è un elemento, con le parole
che lo accompagnano come nota; un file (foto, video, vocale, audio, documento) è un elemento
che rimanda al messaggio, con la didascalia come nota e i link della didascalia accanto.
Il contenuto di link e file si leggerà nei passi successivi, e sarà Claude a dire se un testo
è una domanda o qualcosa da salvare. Un comando che l'agente non conosce non si salva.
"""

import re
from dataclasses import dataclass

from .coppermind import FILES, Draft

COMMAND = re.compile(r"/[A-Za-z0-9_]{1,32}(@\w+)?")

# Dei file si tengono solo i dati utili a ritrovarli e riconoscerli.
MEDIA_FIELDS = ("file_id", "file_unique_id", "file_name", "mime_type", "file_size", "duration")


@dataclass(frozen=True)
class Reading:
    drafts: tuple          # gli elementi da salvare, vuoto se non c'è nulla da custodire
    command: str | None    # un comando che l'agente non conosce
    sent_at: int | None    # quando l'utente l'ha mandato (date di Telegram)
    edited: bool           # una modifica di un messaggio già mandato


def read(update):
    edited = "message" not in update and "edited_message" in update
    msg = update.get("edited_message" if edited else "message") or {}
    sent_at = msg.get("date")
    text = msg.get("text")
    if text is not None:
        first = text.split(maxsplit=1)[0] if text.strip() else ""
        if COMMAND.fullmatch(first):
            return Reading((), first.split("@")[0], sent_at, edited)
        words, links = _split(text, msg.get("entities"))
        drafts = [Draft("link", url=url, note=words) for url in links] or [Draft("note", note=words)]
        return Reading(tuple(d for d in drafts if d.url or d.note), None, sent_at, edited)
    for source in FILES:
        if msg.get(source):
            words, links = _split(msg.get("caption") or "", msg.get("caption_entities"))
            file = msg[source][-1] if source == "photo" else msg[source]  # la foto più grande
            media = {k: file[k] for k in MEDIA_FIELDS if file.get(k) is not None}
            title = (file.get("title") or file.get("file_name")) if source in ("audio", "document") else None
            drafts = [Draft(source, note=words, title=title, media=media)]
            drafts += [Draft("link", url=url, note=words) for url in links]
            return Reading(tuple(drafts), None, sent_at, edited)
    return Reading((), None, sent_at, edited)


def _split(text, entities):
    """Le parole dell'utente e i link di un testo, dalle entità di Telegram.

    Le posizioni delle entità contano in unità UTF-16, come in JavaScript: un'emoji ne
    occupa due. Un link scritto per esteso esce dal testo, e gli spazi che lascia si
    ricompongono riga per riga; per un link nascosto dietro parole (text_link) restano le
    parole. Un testo senza link resta com'è, a capo compresi.
    """
    data = text.encode("utf-16-le")
    words, links, cut = [], [], 0
    for e in sorted(entities or [], key=lambda e: e["offset"]):
        start, end = 2 * e["offset"], 2 * (e["offset"] + e["length"])
        if e["type"] == "url":
            links.append(data[start:end].decode("utf-16-le"))
            words.append(data[cut:start].decode("utf-16-le"))
            cut = end
        elif e["type"] == "text_link" and e.get("url"):
            links.append(e["url"])
    words.append(data[cut:].decode("utf-16-le"))
    note = "".join(words)
    if cut:
        note = "\n".join(" ".join(line.split()) for line in note.splitlines())
    return note.strip() or None, links
