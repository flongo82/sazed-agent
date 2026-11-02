"""scripts/setup.sh contro un finto Telegram e una finta cartella di sazed-bot."""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOOD = "123:ABC_def-ghi"


class FakeTelegram(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == f"/bot{GOOD}/getMe"
        body = {"ok": True, "result": {"username": "prova_bot"}} if ok else {"ok": False}
        self.send_response(200 if ok else 401)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass


class Setup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTelegram)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bot = self.tmp / "sazed-bot"
        (self.bot / "secrets").mkdir(parents=True)
        (self.bot / "worker").mkdir()
        (self.bot / "secrets" / "agent_token").write_text("f" * 64)
        (self.bot / "secrets" / "worker_url").write_text("https://w.example\n")
        (self.bot / "worker" / "wrangler.jsonc").write_text(
            '{\n  // personale\n  "name": "x-1", "vars": {"BOT_NAME": "Il Custode", "LANGUAGE": "it"}\n}\n')
        self.agent = self.tmp / "sazed-agent"
        (self.agent / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "setup.sh", self.agent / "scripts")
        shutil.copy(ROOT / "config.example.toml", self.agent)

    def run_setup(self, typed):
        env = {**os.environ, "SAZED_TELEGRAM_API_BASE": f"http://127.0.0.1:{self.server.server_port}"}
        return subprocess.run(["bash", str(self.agent / "scripts" / "setup.sh"), str(self.bot)],
                              input=typed, capture_output=True, text=True, env=env)

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_full_setup(self):
        result = self.run_setup(f"  {GOOD} \r\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("@prova_bot", result.stdout)
        secrets = self.agent / "secrets"
        self.assertEqual(self.mode(secrets), 0o700)
        self.assertEqual(self.mode(self.agent / "data"), 0o700)
        for name, value in [("agent_token", "f" * 64), ("worker_url", "https://w.example"), ("bot_token", GOOD)]:
            self.assertEqual((secrets / name).read_text(), value, name)
            self.assertEqual(self.mode(secrets / name), 0o600, name)
        config = tomllib.loads((self.agent / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual((config["bot_name"], config["language"]), ("Il Custode", "it"))
        self.assertNotIn(GOOD, result.stdout + result.stderr)

    def test_token_refused_by_telegram(self):
        result = self.run_setup("999:BAD\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not accept", result.stderr)
        self.assertFalse((self.agent / "secrets" / "bot_token").exists())

    def test_malformed_and_empty_input(self):
        self.assertIn("does not look like a bot token", self.run_setup("nonvalido\n").stderr)
        self.assertIn("Nothing was pasted", self.run_setup("\n").stderr)

    def test_existing_config_is_kept(self):
        (self.agent / "config.toml").write_text("bot_name = \"mio\"\n")
        result = self.run_setup(f"{GOOD}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stays as it is", result.stdout)
        self.assertEqual((self.agent / "config.toml").read_text(), "bot_name = \"mio\"\n")

    def test_without_sazed_bot_config_defaults_are_used(self):
        (self.bot / "worker" / "wrangler.jsonc").unlink()
        self.assertEqual(self.run_setup(f"{GOOD}\n").returncode, 0)
        config = tomllib.loads((self.agent / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual((config["bot_name"], config["language"]), ("sazed", "auto"))

    def test_quotes_in_the_name_stay_valid_toml(self):
        (self.bot / "worker" / "wrangler.jsonc").write_text('{"vars": {"BOT_NAME": "Il \\"Custode\\""}}')
        self.assertEqual(self.run_setup(f"{GOOD}\n").returncode, 0)
        config = tomllib.loads((self.agent / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["bot_name"], 'Il "Custode"')

    def test_sazed_bot_not_set_up(self):
        (self.bot / "secrets" / "agent_token").unlink()
        result = self.run_setup(f"{GOOD}\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("set up sazed-bot first", result.stderr)


API_ID, API_HASH = "1234567", "0123456789abcdef0123456789ABCDEF"
BEFORE_MTPROTO = """bot_name = "mio"

[secrets]
agent_token = "secrets/agent_token"
worker_url = "secrets/worker_url"
bot_token = "secrets/bot_token"

[storage]
data_dir = "data"
"""


class SetupMtproto(unittest.TestCase):
    def setUp(self):
        self.agent = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.agent)
        (self.agent / "scripts").mkdir()
        (self.agent / "secrets").mkdir(mode=0o700)
        shutil.copy(ROOT / "scripts" / "setup-mtproto.sh", self.agent / "scripts")
        self.config = self.agent / "config.toml"
        self.config.write_text(BEFORE_MTPROTO)

    def run_setup(self, typed):
        return subprocess.run(["bash", str(self.agent / "scripts" / "setup-mtproto.sh")],
                              input=typed, capture_output=True, text=True)

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_saves_the_secrets_and_completes_an_older_config(self):
        result = self.run_setup(f" {API_ID}\r\n\x1b[200~{API_HASH}\x1b[201~\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        secrets = self.agent / "secrets"
        for name, value in [("api_id", API_ID), ("api_hash", API_HASH)]:
            self.assertEqual((secrets / name).read_text(), value, name)
            self.assertEqual(self.mode(secrets / name), 0o600, name)
        self.assertEqual(self.mode(secrets / "mtproto"), 0o700)
        config = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config["secrets"], {
            "agent_token": "secrets/agent_token", "worker_url": "secrets/worker_url",
            "bot_token": "secrets/bot_token", "api_id": "secrets/api_id",
            "api_hash": "secrets/api_hash", "mtproto_dir": "secrets/mtproto"})
        self.assertEqual(config["storage"], {"data_dir": "data"})
        for value in (API_ID, API_HASH):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_running_it_again_changes_only_the_secrets(self):
        self.assertEqual(self.run_setup(f"{API_ID}\n{API_HASH}\n").returncode, 0)
        config = self.config.read_text(encoding="utf-8")
        result = self.run_setup(f"7654321\n{'f' * 32}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already points to them", result.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), config)
        self.assertEqual((self.agent / "secrets" / "api_id").read_text(), "7654321")

    def test_the_example_config_already_has_the_lines(self):
        shutil.copy(ROOT / "config.example.toml", self.config)
        self.assertEqual(self.run_setup(f"{API_ID}\n{API_HASH}\n").returncode, 0)
        self.assertEqual(self.config.read_text(encoding="utf-8"),
                         (ROOT / "config.example.toml").read_text(encoding="utf-8"))

    def test_malformed_values_are_refused_and_nothing_is_saved(self):
        for typed, message in [("abc\n", "does not look like an api_id"), ("\n", "does not look like an api_id"),
                               (f"{API_ID}\nnon-esadecimale\n", "does not look like an api_hash"),
                               (f"{API_ID}\n{'a' * 31}\n", "does not look like an api_hash")]:
            with self.subTest(typed):
                result = self.run_setup(typed)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertFalse((self.agent / "secrets" / "api_id").exists())

    def test_setup_must_come_first(self):
        self.config.unlink()
        result = self.run_setup(f"{API_ID}\n{API_HASH}\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("run scripts/setup.sh first", result.stderr)


if __name__ == "__main__":
    unittest.main()
