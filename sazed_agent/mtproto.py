"""I file oltre i 20 MB dell'API dei bot (R7), scaricati via MTProto, il protocollo dei client.

Il bot entra con il suo token, mai con l'account dell'utente, e senza ricevere aggiornamenti:
il webhook del Worker resta l'unica porta dei messaggi. La sessione si apre alla prima richiesta
e resta aperta; sta in secrets/mtproto/, l'unico segreto che l'agente scrive (permessi 600,
scritta in modo atomico). Il file si ritrova dal message_id del messaggio d'origine, che deve
essere della chat attesa, e si scarica a pezzi da 512 KB, più pezzi alla volta: in fila Telethon
fa circa 1 MB/s. Dove finisce il file lo decide chi chiama (una cartella temporanea).
"""

import asyncio
import contextlib
import math
import os
import stat

from telethon import errors

from .reader import ReadError

BIG_MAX = 2_000_000_000    # oltre, nemmeno MTProto: i bot scaricano al più 2 GB
PIECE = 512 * 1024         # la richiesta più grande di MTProto; un pezzo non attraversa mai un mega
PARTS = 4                  # pezzi scaricati insieme
MIN_SPEED = 200_000        # byte al secondo: più lento di così, il download si ferma e si riprova


class BigFiles:
    def __init__(self, session_file, api_id, api_hash, bot_token, parts=PARTS, piece=PIECE, client=None):
        self.session_file, self.api_id, self.api_hash = session_file, api_id, api_hash
        self.bot_token, self.parts, self.piece = bot_token, parts, piece
        self._make = client or _telethon  # i test passano un client finto
        self._client, self._lock = None, asyncio.Lock()

    async def download(self, chat_id, message_id, destination):
        """Scarica in destination il file del messaggio message_id della chat chat_id; la sua
        dimensione. ReadError se il messaggio non c'è più, non ha un file o non si scarica."""
        try:
            client = await self._connected()
            message = await client.get_messages(None, ids=message_id)
            if message is None or getattr(message, "file", None) is None:
                raise ReadError("il messaggio non è più su Telegram", unavailable=True)
            if getattr(message.peer_id, "user_id", None) != chat_id:
                raise ReadError("il messaggio non è della chat attesa", permanent=True)
            size = message.file.size or 0
            if size > BIG_MAX:
                raise ReadError(f"file oltre {BIG_MAX // 1_000_000_000} GB, anche per MTProto", permanent=True)
            await asyncio.wait_for(self._pieces(client, message.media, size, destination), 120 + size / MIN_SPEED)
            return size
        except ReadError:
            raise
        except asyncio.TimeoutError:
            raise ReadError("download via MTProto troppo lento") from None
        except (errors.RPCError, ConnectionError, OSError, ValueError) as err:  # attese imposte, rete, sessione
            raise ReadError(f"MTProto: {type(err).__name__}") from None

    async def close(self):
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._client = None

    async def _connected(self):
        async with self._lock:
            if self._client is None or not self._client.is_connected():
                client = self._make(_read_session(self.session_file), self.api_id, self.api_hash)
                await client.connect()
                if not await client.is_user_authorized():
                    await client.sign_in(bot_token=self.bot_token)
                    _save_session(self.session_file, client.session.save())
                self._client = client
            return self._client

    async def _pieces(self, client, media, size, destination):
        """Il file a pezzi: la parte i scarica i pezzi i, i + parts, i + 2 parts, ..., e ognuno
        si scrive al suo posto."""
        total = math.ceil(size / self.piece)
        with open(destination, "wb") as out:
            out.truncate(size)
            fd = out.fileno()

            async def part(i):
                position = i * self.piece
                async for data in client.iter_download(media, offset=position, stride=self.parts * self.piece,
                                                       limit=len(range(i, total, self.parts)), chunk_size=self.piece,
                                                       request_size=self.piece, file_size=size):
                    os.pwrite(fd, data, position)
                    position += self.parts * self.piece
            await asyncio.gather(*(part(i) for i in range(min(self.parts, total))))


def _telethon(session, api_id, api_hash):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session), api_id, api_hash, receive_updates=False)


def _read_session(path):
    if not path.exists():
        return ""
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ReadError(f"la sessione MTProto ({path.name}) è leggibile da altri utenti: chmod 600")
    return path.read_text(encoding="utf-8").strip()


def _save_session(path, value):
    """La sessione con permessi 600, scritta in modo atomico: un'interruzione non la rovina."""
    old = os.umask(0o077)
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        os.umask(old)
