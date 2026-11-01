"""Finto sazed-bot per i test: stesso protocollo WebSocket del vero Worker (README di sazed-bot).

Tiene una coda, la manda a chi si collega con "hello", toglie gli elementi confermati con
"ack", risponde "pong" ai "ping" e accetta una sola connessione alla volta. Può simulare i
guasti: connessione che cade, pong che non arrivano, connessione chiusa subito dopo aver
mandato un elemento (così l'ack si perde).
"""

import json

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

TOKEN = "test-agent-token"


class FakeWorker:
    def __init__(self, token=TOKEN):
        self.token = token
        self.queue = []              # [(id, update)]
        self.next_id = 1
        self.connections = 0
        self.hellos = 0
        self.rejected = 0
        self.acks = []
        self.events = []             # ordine di ciò che il Worker vede, per i test
        self.answer_pings = True
        self.close_after_update = False
        self.current = None

    async def start(self):
        self.server = await serve(self._handler, "127.0.0.1", 0, process_request=self._check)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/agent"

    def _check(self, connection, request):
        # Come il vero Worker: percorso o token sbagliati ricevono lo stesso 404 vuoto.
        if request.path != "/agent" or request.headers.get("Authorization") != f"Bearer {self.token}":
            self.rejected += 1
            return connection.respond(404, "")
        return None

    async def _handler(self, ws):
        if self.current is not None:
            await self.current.close(4000, "sostituita da una nuova connessione")
        self.current = ws
        self.connections += 1
        try:
            async for raw in ws:
                if raw == "ping":
                    if self.answer_pings:
                        await ws.send("pong")
                    continue
                msg = json.loads(raw)
                if msg["type"] == "hello":
                    self.hellos += 1
                    for queue_id, update in list(self.queue):
                        await self._send(ws, queue_id, update)
                    await ws.send(json.dumps({"type": "backlog_end", "pending": len(self.queue)}))
                elif msg["type"] == "ack":
                    self.acks.append(msg["id"])
                    self.events.append(("ack", msg["id"]))
                    self.queue = [(q, u) for q, u in self.queue if q != msg["id"]]
                    await ws.send(json.dumps({"type": "acked", "id": msg["id"], "pending": len(self.queue)}))
        except ConnectionClosed:
            pass

    async def push(self, update):
        """Un messaggio nuovo arriva da Telegram: entra in coda e va subito a chi è collegato."""
        queue_id = self.next_id
        self.next_id += 1
        self.queue.append((queue_id, update))
        if self.current is not None:
            try:
                await self._send(self.current, queue_id, update)
            except ConnectionClosed:
                pass
        return queue_id

    async def drop(self):
        if self.current is not None:
            await self.current.close()

    async def _send(self, ws, queue_id, update):
        await ws.send(json.dumps({"type": "update", "id": queue_id, "update": update}))
        if self.close_after_update:
            await ws.close()
