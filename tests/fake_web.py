"""Un finto web per i test del lettore, senza rete: httpx.MockTransport con pagine a indirizzo.

- page(url, html) / route(url, status, body, headers): che cosa risponde un indirizzo
  (l'indirizzo completo, con la query se c'è);
- redirect(da, a, status=302): un rimando;
- oembed(piattaforma, url, dati): la risposta dell'anteprima pubblica per quel link;
- slow(url, secondi): quell'indirizzo risponde dopo un'attesa (senza fermare gli altri lavori).
Un indirizzo sconosciuto risponde 404. Ogni richiesta resta in requests.
"""

import asyncio
import json
from urllib.parse import urlencode

import httpx

from sazed_agent.reader import OEMBED


async def allow_all(host):
    """Ogni host è pubblico, e la richiesta va al nome stesso: così le rotte restano per nome."""
    return host


class FakeWeb:
    def __init__(self):
        self.routes, self.requests, self.delays = {}, [], {}
        self.transport = httpx.MockTransport(self._handle)

    def route(self, url, status=200, body=b"", headers=None):
        body = body.encode() if isinstance(body, str) else body
        self.routes[url] = (status, body, headers or {})

    def page(self, url, html, charset="utf-8"):
        self.route(url, 200, html.encode(charset), {"content-type": f"text/html; charset={charset}"})

    def redirect(self, source, target, status=302):
        self.route(source, status, b"", {"location": target})

    def oembed(self, platform, url, data, status=200):
        endpoint = OEMBED[platform][0]
        self.route(f"{endpoint}?{urlencode({'url': url, 'format': 'json'})}", status, json.dumps(data),
                   {"content-type": "application/json"})

    def slow(self, url, seconds):
        self.delays[url] = seconds

    async def _handle(self, request):
        url = str(request.url)
        self.requests.append(url)
        if url in self.delays:
            await asyncio.sleep(self.delays[url])
        status, body, headers = self.routes.get(url, (404, b"not found", {}))
        if callable(body):
            status, body, headers = body(request)
        return httpx.Response(status, content=body, headers=headers)
