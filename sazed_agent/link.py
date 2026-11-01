"""Connessione con sazed-bot: riceve i messaggi, li scrive nel registro locale e li conferma.

Protocollo (README di sazed-bot): "hello" appena collegati per ricevere l'arretrato, "ping"
ogni ping_interval secondi con risposta "pong", "ack" per ogni elemento. L'ack parte solo
dopo che il registro ha scritto l'elemento su disco: se la scrittura fallisce non si conferma
nulla, e il Worker lo riconsegnerà alla connessione successiva.

Se la connessione cade si riprova con attese crescenti. Se il computer si è sospeso (il tempo
di parete è corso più di quello monotono, che durante la sospensione si ferma) si riprova
subito, senza attese.
"""

import asyncio
import json
import logging
import random
import time

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

log = logging.getLogger("sazed_agent.link")

BACKOFF = (1, 2, 4, 8, 16, 32, 60)


class ConnectionLost(Exception):
    """Il Worker ha chiuso la connessione."""


class PongTimeout(Exception):
    """Nessun pong in tempo: la connessione è appesa."""


class Suspended(Exception):
    """Il computer si è sospeso: la connessione va rifatta subito."""


def agent_url(worker_url):
    """L'indirizzo WebSocket dell'agente a partire da quello del Worker."""
    for http, ws in (("https://", "wss://"), ("http://", "ws://")):
        if worker_url.startswith(http):
            return ws + worker_url[len(http):].rstrip("/") + "/agent"
    raise ValueError("l'indirizzo del Worker deve iniziare con https:// o http://")


class WorkerLink:
    def __init__(self, url, token, journal, lane_for, ping_interval, pong_timeout,
                 on_recorded=None, backoff=BACKOFF, connect=ws_connect,
                 monotonic=time.monotonic, wall=time.time, jitter=random.uniform):
        self.url, self.token = url, token
        self.journal, self.lane_for, self.on_recorded = journal, lane_for, on_recorded
        self.ping_interval, self.pong_timeout = ping_interval, pong_timeout
        self.backoff, self.connect = backoff, connect
        self.monotonic, self.wall, self.jitter = monotonic, wall, jitter
        self.connected = asyncio.Event()
        self.worker_pending = None  # ultimo numero di elementi in coda annunciato dal Worker
        self.delays = []            # attese fatte prima di riprovare, per i log e i test
        self._healthy = False
        self._last_pong = 0.0

    async def run(self, stop):
        """Resta collegato finché stop non viene impostato."""
        attempt = 0
        while not stop.is_set():
            try:
                await self._session(stop)
            except Suspended:
                log.info("ripresa dopo una sospensione: mi ricollego subito")
                attempt = 0
                continue
            except (OSError, WebSocketException, ConnectionLost, PongTimeout, TimeoutError) as err:
                log.warning("connessione con il Worker persa: %s", err or type(err).__name__)
            if stop.is_set():
                break
            if self._healthy:  # la sessione aveva funzionato: si riparte dall'attesa più breve
                attempt = 0
            delay = self.backoff[min(attempt, len(self.backoff) - 1)] * self.jitter(0.8, 1.2)
            attempt += 1
            self.delays.append(delay)
            log.info("nuovo tentativo tra %.1f s", delay)
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except TimeoutError:
                pass

    async def _session(self, stop):
        self._healthy = False
        headers = {"Authorization": f"Bearer {self.token}"}
        async with self.connect(self.url, additional_headers=headers, ping_interval=None,
                                open_timeout=15, close_timeout=5) as ws:
            log.info("collegato al Worker")
            self.connected.set()
            tasks = []
            try:
                await ws.send(json.dumps({"type": "hello"}))
                tasks = [asyncio.create_task(self._receive(ws)), asyncio.create_task(self._ping(ws)),
                         asyncio.create_task(stop.wait())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception():
                        raise task.exception()
            finally:
                self.connected.clear()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _receive(self, ws):
        async for raw in ws:
            if raw == "pong":
                self._last_pong = self.monotonic()
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                log.warning("messaggio non valido dal Worker, ignorato")
                continue
            kind = msg.get("type")
            if kind == "update":
                await self._record_and_ack(ws, msg)
            elif kind == "backlog_end":
                self._healthy = True
                self.worker_pending = msg.get("pending")
                log.info("arretrato ricevuto; in coda sul Worker: %s", self.worker_pending)
            elif kind == "acked":
                self.worker_pending = msg.get("pending")
        raise ConnectionLost("il Worker ha chiuso la connessione")

    async def _record_and_ack(self, ws, msg):
        queue_id, update = msg.get("id"), msg.get("update")
        if not isinstance(queue_id, int) or not isinstance(update, dict) \
                or not isinstance(update.get("update_id"), int):
            log.warning("aggiornamento malformato dal Worker, ignorato")
            return
        # Prima il registro, poi l'ack: se record() solleva un'eccezione, l'ack non parte.
        new = self.journal.record(queue_id, update, self.lane_for(update))
        # Il lavoro nuovo si annuncia subito, prima dell'ack: se l'ack si perde, alla
        # riconsegna l'elemento risulta già presente e nessuno lo annuncerebbe più.
        if new and self.on_recorded:
            self.on_recorded()
        await ws.send(json.dumps({"type": "ack", "id": queue_id}))
        log.info("elemento %s registrato (%s) e confermato", update["update_id"],
                 "nuovo" if new else "già presente")

    async def _ping(self, ws):
        self._last_pong = self.monotonic()
        while True:
            m0, w0 = self.monotonic(), self.wall()
            await ws.send("ping")
            await asyncio.sleep(self.pong_timeout)
            self._check_suspend(m0, w0)
            if self._last_pong < m0:
                raise PongTimeout(f"nessun pong in {self.pong_timeout:g} s")
            await asyncio.sleep(self.ping_interval - self.pong_timeout)
            self._check_suspend(m0, w0)

    def _check_suspend(self, m0, w0):
        if (self.wall() - w0) - (self.monotonic() - m0) > self.ping_interval:
            raise Suspended()
