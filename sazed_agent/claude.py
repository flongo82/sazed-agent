"""Claude, attraverso la sua API: capisce che cosa vuole un messaggio e descrive ciò che salva.

Il codice salva, il modello interpreta. Claude riceve le parole dell'utente e ciò che il codice
sa di ogni elemento (link con piattaforma e sito, file con tipo e nome), mai il contenuto dietro
un link o dentro un file, e restituisce come dati strutturati:

- l'intenzione: "save" (qualcosa da custodire), "question" (solo una domanda) o "both";
- la domanda, quando c'è, con le parole dell'utente;
- per ogni elemento: tipo, titolo, argomenti, riassunto.

Il codice controlla tutto prima di usarlo: un messaggio con link o file non è mai solo una
domanda, i tipi fuori elenco diventano "non so", titoli e argomenti hanno una misura. Se Claude
rifiuta o la risposta non è utilizzabile, il messaggio si salva senza descrizione: il contenuto
dell'utente non si perde mai per colpa del modello. Gli errori dell'API diventano ClaudeError,
con la stessa forma degli errori di Telegram, così le corsie li trattano allo stesso modo.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Literal

import anthropic
from pydantic import BaseModel

from .coppermind import FILES, parse_link, platform_label

log = logging.getLogger("sazed_agent.claude")

INTENTS = ("save", "question", "both")
KINDS = ("recipe", "article", "video", "note", "photo", "document", "other")
TITLE_MAX, TOPICS_MAX, TOPIC_MAX, SUMMARY_MAX = 120, 8, 40, 600

SYSTEM = """You read the messages one person sends to sazed, their personal knowledge base on \
Telegram. They send it things to keep (notes, links, photos, files) and questions about what \
they kept, with no prefix to tell the two apart. The code has already split each message into \
items: you see the person's own words and what the code knows about each item, never the content \
behind a link or inside a file.

Return:
- intent: "save" when the message gives something to keep, "question" when it only asks \
something, "both" when it does both. A message with links or files always keeps them, so its \
intent is never "question". A reminder, a list, an idea or a fact to remember is something to \
keep, even when it is short.
- question: for "question" and "both", what the person asks, in their words; otherwise null.
- items: one entry per item, by its index, describing it for the catalogue:
  - kind: recipe, article, video, note, photo, document or other; null when what you see does \
not tell, as for a bare link with an uninformative address;
  - title: a short title, at most 80 characters, from the words or the address; null when there \
is nothing to go on;
  - topics: up to 8 short lowercase topics to find it again; for a recipe, its main ingredients;
  - summary: one or two sentences on what the person's words say about it; null if they say \
nothing about it.
Use only what you see: never guess the content of a page or a file from its address. Write the \
title, the topics, the summary and the question in the language whose ISO 639-1 code is given \
with the message."""


class ClaudeError(Exception):
    """Un errore dell'API di Claude, classificato come quelli di Telegram.

    permanent: inutile riprovare (richiesta rifiutata); config_error: chiave, crediti o modello
    da sistemare, si rimanda senza contare tentativi; retry_after: attesa chiesta dall'API.
    """

    def __init__(self, message, permanent=False, config_error=False, retry_after=None):
        super().__init__(message)
        self.permanent, self.config_error, self.retry_after = permanent, config_error, retry_after


@dataclass(frozen=True)
class Classification:
    index: int
    kind: str | None = None
    title: str | None = None
    topics: tuple = ()
    summary: str | None = None


@dataclass(frozen=True)
class Interpretation:
    intent: str = "save"
    question: str | None = None
    items: tuple = field(default_factory=tuple)   # Classification, già controllate

    @property
    def saves(self):
        return self.intent in ("save", "both")

    @property
    def asks(self):
        return self.intent in ("question", "both")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(data["intent"], data.get("question"),
                   tuple(Classification(**dict(c, topics=tuple(c.get("topics") or ()))) for c in data.get("items") or ()))


class _Item(BaseModel):
    index: int
    kind: Literal[KINDS] | None
    title: str | None
    topics: list[str]
    summary: str | None


class _Answer(BaseModel):
    intent: Literal[INTENTS]
    question: str | None
    items: list[_Item]


class Claude:
    def __init__(self, api_key, api_base, model, timeout=60.0, max_retries=2, effort="low"):
        self.model, self.effort = model, effort
        self.client = anthropic.AsyncAnthropic(api_key=api_key, base_url=api_base, timeout=timeout,
                                               max_retries=max_retries)

    async def interpret(self, words, drafts, language):
        """Che cosa vuole il messaggio e come descriverne gli elementi (drafts, già senza doppioni)."""
        message = {"language": language, "words": words,
                   "items": [_describe(i, d) for i, d in enumerate(drafts)]}
        try:
            response = await self.client.messages.parse(
                model=self.model, max_tokens=2048, system=SYSTEM, output_format=_Answer,
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": json.dumps(message, ensure_ascii=False)}])
        except anthropic.APIError as err:
            raise _error(err) from None
        except ValueError as err:  # testo che non rispetta lo schema
            log.warning("risposta di Claude non utilizzabile (%s): salvo senza descrizione", type(err).__name__)
            return Interpretation()
        usage = response.usage
        log.info("Claude: %s token in ingresso, %s in uscita", usage.input_tokens, usage.output_tokens)
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            log.warning("Claude ha finito con %s: salvo senza descrizione", response.stop_reason)
            return Interpretation()
        return _checked(response.parsed_output, drafts)


def _describe(index, draft):
    """Ciò che Claude vede di un elemento: niente contenuti, solo i dati che il codice ha già."""
    item = {"index": index, "source": draft.source}
    if draft.source == "link":
        link = parse_link(draft.url)  # lo stesso indirizzo pulito che si salva
        item.update(url=link.url, platform=platform_label(link.platform) or "web", site=link.site)
    elif draft.source in FILES:
        media = draft.media or {}
        item.update({k: media[k] for k in ("file_name", "mime_type", "duration") if k in media})
    return item


def _checked(answer, drafts):
    intent = answer.intent
    if intent == "question" and any(d.source != "note" for d in drafts):
        intent = "both"  # link e file si custodiscono sempre
    question = _text(answer.question, SUMMARY_MAX) if intent != "save" else None
    items, seen = [], set()
    for item in answer.items:
        if not 0 <= item.index < len(drafts) or item.index in seen:
            continue
        seen.add(item.index)
        topics = []
        for topic in item.topics:
            topic = _text(topic, TOPIC_MAX)
            if topic and topic.lower() not in topics:
                topics.append(topic.lower())
        items.append(Classification(item.index, item.kind if item.kind in KINDS else None,
                                    _text(item.title, TITLE_MAX), tuple(topics[:TOPICS_MAX]),
                                    _text(item.summary, SUMMARY_MAX)))
    return Interpretation(intent, question, tuple(items))


def _text(value, limit):
    value = " ".join((value or "").split())
    return value[:limit].rstrip() or None


def _error(err):
    """Da un errore dell'SDK a un ClaudeError, senza mai la chiave nel messaggio."""
    if isinstance(err, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return ClaudeError(f"API di Claude non raggiungibile: {type(err).__name__}")
    status = getattr(err, "status_code", None)
    if status in (401, 403):
        return ClaudeError(f"chiave dell'API di Claude rifiutata ({status}): lancia scripts/setup-claude.sh",
                           config_error=True)
    if status == 402:
        return ClaudeError("crediti dell'API di Claude finiti (402)", config_error=True)
    if status == 404:
        return ClaudeError("modello sconosciuto all'API di Claude (404): controlla [claude] model",
                           config_error=True)
    if status == 429:
        retry = (err.response.headers.get("retry-after") or "") if getattr(err, "response", None) else ""
        return ClaudeError("API di Claude: troppe richieste (429)",
                           retry_after=float(retry) if retry.replace(".", "", 1).isdigit() else None)
    if status is not None and status < 500:
        return ClaudeError(f"API di Claude: richiesta rifiutata ({status})", permanent=True)
    return ClaudeError(f"API di Claude: errore {status or type(err).__name__}")
