"""Il significato dei testi, per ritrovare ciò che si è custodito anche con altre parole.

llama-server (llama.cpp) calcola gli embedding con un modello multilingue, sul processore e a
priorità bassa: un vettore per elemento, dalla sua scheda (titolo, autori, argomenti, riassunto,
nota e l'inizio del testo letto), e uno per ogni domanda, preceduta da un'istruzione. Testi vicini
per significato hanno vettori vicini, anche con altre parole o in un'altra lingua. La scheda
resta corta: sul processore un testo di qualche migliaio di token richiede più di un minuto.

Il server parte alla prima richiesta e resta acceso finché l'agente non si ferma. Ascolta su un
socket UNIX, in una cartella temporanea che solo l'agente può aprire, e vuole una chiave nuova a
ogni avvio. Il testo che riceve è un dato: un modello di embedding non esegue istruzioni.
"""

import asyncio
import contextlib
import os
import secrets
import shutil
import tempfile
from pathlib import Path

import httpx
import numpy

NICE = 10
CONTEXT = 8192            # token di una richiesta: una scheda ne usa poche centinaia
CARD_MAX = 2000           # caratteri della scheda di un elemento
CONTENT_PART = 800        # del testo letto, solo l'inizio
START_TIMEOUT, REQUEST_TIMEOUT = 120.0, 300.0
# Qwen3-Embedding vuole un'istruzione, in inglese, prima di ogni domanda; i documenti no.
INSTRUCT = ("Instruct: Given a question or a request, retrieve the items of a personal knowledge base that match it"
            "\nQuery:")


class MeaningError(Exception):
    """Il significato non si calcola adesso: si riprova più tardi."""


class Meanings:
    def __init__(self, model, threads=4, server="llama-server"):
        self.model, self.threads, self.server = Path(model), threads, server
        self.name = self.model.name  # i vettori di modelli diversi non si confrontano
        self._process = self._client = self._work = None
        self._lock = asyncio.Lock()

    async def vectors(self, texts, query=False):
        """I vettori dei testi, normalizzati (float32), nello stesso ordine; con query, come
        domande. MeaningError se il server non parte o non risponde."""
        if not texts:
            return []
        client = await self._ready()
        inputs = [INSTRUCT + t if query else t for t in texts]
        try:
            response = await client.post("/v1/embeddings", json={"input": inputs})
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda d: d["index"])
            matrix = numpy.array([d["embedding"] for d in data], dtype=numpy.float32)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as err:
            await self.close()  # un server che sbaglia si riavvia alla prossima richiesta
            raise MeaningError(f"llama-server non ha risposto ({type(err).__name__})") from None
        if matrix.shape[0] != len(texts):
            raise MeaningError("llama-server ha dato un numero sbagliato di vettori")
        norms = numpy.linalg.norm(matrix, axis=1, keepdims=True)
        return list(matrix / numpy.where(norms == 0, 1, norms))

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 10)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._work is not None:
            shutil.rmtree(self._work, ignore_errors=True)
        self._process = self._client = self._work = None

    async def _ready(self):
        async with self._lock:
            if self._process is not None and self._process.returncode is None:
                return self._client
            await self.close()
            self._work = tempfile.mkdtemp(prefix="sazed-meaning-")  # 700: solo l'agente
            socket, key = os.path.join(self._work, "llama.sock"), secrets.token_urlsafe(24)
            key_file = Path(self._work) / "key"
            key_file.write_text(key)  # la chiave in un file, non fra gli argomenti visibili a tutti
            try:
                self._process = await asyncio.create_subprocess_exec(
                    self.server, "-m", str(self.model), "--embeddings", "--pooling", "last", "--host", socket,
                    "--api-key-file", str(key_file), "-t", str(self.threads), "-c", str(CONTEXT),
                    "-ub", str(CONTEXT), "--no-webui", "--log-disable", stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
            except OSError as err:
                await self.close()
                raise MeaningError(f"{Path(self.server).name} non si avvia ({type(err).__name__})") from None
            with contextlib.suppress(OSError):  # il computer resta di chi lo usa
                os.setpriority(os.PRIO_PROCESS, self._process.pid, NICE)
            self._client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket), base_url="http://llama",
                                             headers={"authorization": f"Bearer {key}"}, timeout=REQUEST_TIMEOUT)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + START_TIMEOUT
            while loop.time() < deadline:
                if self._process.returncode is not None:
                    break
                with contextlib.suppress(httpx.TransportError):
                    if (await self._client.get("/health")).status_code == 200:
                        return self._client
                await asyncio.sleep(0.2)
            await self.close()
            raise MeaningError("llama-server non è partito")


def card(item):
    """Il testo di cui si calcola il significato di un elemento: ciò che lo descrive e l'inizio di
    ciò che se ne è letto, al più CARD_MAX caratteri."""
    lines = [f"Title: {item.title}" if item.title else None,
             f"Authors: {', '.join(item.authors)}" if item.authors else None,
             f"Kind: {item.kind}" if item.kind else None,
             f"Topics: {', '.join(item.topics)}" if item.topics else None,
             f"Summary: {item.summary}" if item.summary else None,
             f"Note: {item.note}" if item.note else None,
             f"From: {item.platform if item.platform and item.platform != 'web' else item.site}"
             if item.site else None,
             f"Content: {' '.join(item.content[:CONTENT_PART].split())}" if item.content else None]
    return "\n".join(line for line in lines if line)[:CARD_MAX]
