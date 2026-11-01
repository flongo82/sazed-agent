"""Le poche chiamate all'API dei bot di Telegram che servono all'agente.

Ogni errore diventa un TelegramError che dice se ha senso riprovare:
- definitivo (permanent): 400 e 403, per esempio un messaggio che non esiste più o un bot
  bloccato dall'utente; riprovare non cambierebbe nulla;
- da riprovare: 5xx, rete, tempo scaduto, 429 (con l'attesa chiesta da Telegram);
- di configurazione (config_error): 401 e 404, cioè token rifiutato. Non è colpa del singolo
  messaggio: l'elemento si rimanda senza consumare i suoi tentativi.

Il token sta nell'indirizzo delle richieste: non compare mai nei messaggi di errore né nei log.
"""

import asyncio
import json
import urllib.error
import urllib.request


class TelegramError(Exception):
    def __init__(self, description, permanent=False, retry_after=None, config_error=False):
        super().__init__(description)
        self.permanent = permanent
        self.retry_after = retry_after
        self.config_error = config_error  # token rifiutato: non è colpa del singolo messaggio


class Telegram:
    def __init__(self, api_base, token, timeout=30):
        self.api_base, self.token, self.timeout = api_base.rstrip("/"), token, timeout

    async def send_message(self, chat_id, text, reply_to=None):
        """Manda un messaggio, in risposta a reply_to se c'è. Restituisce il suo message_id."""
        params = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        result = await self.call("sendMessage", params)
        return result["message_id"]

    async def set_reaction(self, chat_id, message_id, emoji):
        await self.call("setMessageReaction", {
            "chat_id": chat_id, "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}]})

    async def send_typing(self, chat_id):
        await self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    async def call(self, method, params):
        return await asyncio.to_thread(self._call, method, params)

    def _call(self, method, params):
        request = urllib.request.Request(
            f"{self.api_base}/bot{self.token}/{method}", data=json.dumps(params).encode(),
            headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as err:
            raise _from_http(method, err.code, _json(err)) from None
        except (urllib.error.URLError, OSError, ValueError) as err:
            # Il motivo, mai la richiesta: l'indirizzo contiene il token.
            reason = getattr(err, "reason", None) or type(err).__name__
            raise TelegramError(f"{method}: Telegram non raggiungibile ({reason})") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', 'risposta non valida')}", permanent=True)
        return body["result"]


def _json(err):
    try:
        return json.load(err)
    except (ValueError, OSError):
        return {}


def _from_http(method, code, body):
    description = f"{method}: {body.get('description') or f'HTTP {code}'}"
    if code == 429:
        return TelegramError(description, retry_after=(body.get("parameters") or {}).get("retry_after"))
    if code in (400, 403):
        return TelegramError(description, permanent=True)
    if code in (401, 404):
        return TelegramError(f"{method}: token del bot rifiutato da Telegram (HTTP {code})", config_error=True)
    return TelegramError(description)
