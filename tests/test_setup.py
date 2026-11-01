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


if __name__ == "__main__":
    unittest.main()
