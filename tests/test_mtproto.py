"""I file grandi via MTProto, con un client Telethon finto: il file ricomposto dai pezzi scaricati
insieme, la sessione del bot salvata e riusata, messaggi spariti o di altre chat, errori."""

import asyncio
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from telethon import errors

from sazed_agent import mtproto
from sazed_agent.mtproto import BigFiles
from sazed_agent.reader import ReadError

CHAT = 42


class FakeClient:
    """Un finto TelegramClient: messaggi per id, file scaricati a pezzi come fa iter_download."""

    def __init__(self, session, data, messages, fail=None):
        self.session_string, self.data, self.messages, self.fail = session, data, messages, fail
        self.connected = self.authorized = bool(session)
        self.connects, self.signed_in, self.active, self.most_active, self.requests = 0, None, 0, 0, []
        self.session = SimpleNamespace(save=lambda: "SESSIONE-DEL-BOT")

    async def connect(self):
        self.connects += 1
        self.connected = True

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self.authorized

    async def sign_in(self, bot_token):
        self.signed_in, self.authorized = bot_token, True

    async def get_messages(self, entity, ids):
        if self.fail:
            raise self.fail
        return self.messages.get(ids)

    async def iter_download(self, media, offset, stride, limit, chunk_size, request_size, file_size):
        self.active += 1
        self.most_active = max(self.most_active, self.active)
        try:
            for _ in range(limit):
                self.requests.append(offset)
                await asyncio.sleep(0)  # le parti si alternano, come richieste in volo insieme
                yield self.data[offset:offset + request_size]
                offset += stride
        finally:
            self.active -= 1

    async def disconnect(self):
        self.connected = False


def message(size, chat=CHAT):
    return SimpleNamespace(peer_id=SimpleNamespace(user_id=chat), file=SimpleNamespace(size=size), media="documento")


class BigFilesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.session = self.dir / "session"
        self.data = os.urandom(4096 * 10 + 123)
        self.messages = {10: message(len(self.data)), 11: message(5, chat=7), 12: SimpleNamespace(file=None)}
        self.clients = []

    def big_files(self, fail=None, parts=3):
        def make(session, api_id, api_hash):
            self.clients.append(FakeClient(session, self.data, self.messages, fail))
            return self.clients[-1]
        files = BigFiles(self.session, 1234, "hash", "123:TOKEN", parts=parts, piece=4096, client=make)
        self.addAsyncCleanup(files.close)
        return files

    async def test_a_big_file_comes_in_pieces_downloaded_together(self):
        files = self.big_files()
        size = await files.download(CHAT, 10, self.dir / "file")
        self.assertEqual(((self.dir / "file").read_bytes(), size), (self.data, len(self.data)))
        [client] = self.clients
        self.assertEqual(client.most_active, 3)  # tre parti insieme
        self.assertEqual(sorted(client.requests), [4096 * n for n in range(11)])  # ogni pezzo una volta
        self.assertEqual(client.signed_in, "123:TOKEN")  # entra come bot
        self.assertEqual(self.session.read_text(), "SESSIONE-DEL-BOT")
        self.assertEqual(stat.S_IMODE(self.session.stat().st_mode), 0o600)
        await files.download(CHAT, 10, self.dir / "again")
        self.assertEqual((len(self.clients), client.connects), (1, 1))  # la sessione resta aperta

    async def test_a_saved_session_is_used_again(self):
        self.session.write_text("SESSIONE-DEL-BOT")
        self.session.chmod(0o600)
        await self.big_files(parts=1).download(CHAT, 10, self.dir / "file")
        [client] = self.clients
        self.assertEqual((client.session_string, client.signed_in), ("SESSIONE-DEL-BOT", None))
        self.assertEqual((self.dir / "file").read_bytes(), self.data)

    async def test_what_cannot_be_downloaded_says_why(self):
        files = self.big_files()
        cases = [(99, "non è più su Telegram", True), (12, "non è più su Telegram", True),
                 (11, "chat attesa", True)]
        for message_id, reason, permanent in cases:
            with self.subTest(message_id), self.assertRaises(ReadError) as ctx:
                await files.download(CHAT, message_id, self.dir / "file")
            self.assertIn(reason, str(ctx.exception))
            self.assertEqual(ctx.exception.permanent, permanent)
        with mock.patch.object(mtproto, "BIG_MAX", 100), self.assertRaises(ReadError) as ctx:
            await files.download(CHAT, 10, self.dir / "file")
        self.assertTrue(ctx.exception.permanent)

    async def test_telegram_troubles_are_tried_again_later(self):
        for fail in (ConnectionError("giù"), errors.FloodWaitError(request=None, capture=30)):
            with self.subTest(type(fail).__name__), self.assertRaises(ReadError) as ctx:
                await self.big_files(fail=fail).download(CHAT, 10, self.dir / "file")
            self.assertFalse(ctx.exception.permanent)
            self.assertIn(type(fail).__name__, str(ctx.exception))

    async def test_a_session_others_can_read_is_not_used(self):
        self.session.write_text("SESSIONE-DEL-BOT")
        self.session.chmod(0o644)
        with self.assertRaisesRegex(ReadError, "chmod 600"):
            await self.big_files().download(CHAT, 10, self.dir / "file")
        self.assertEqual(self.clients, [])


if __name__ == "__main__":
    unittest.main()
