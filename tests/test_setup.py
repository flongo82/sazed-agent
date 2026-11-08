"""scripts/setup.sh, setup-mtproto.sh e setup-claude.sh, contro finti Telegram e API di Claude."""

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


KEY = "sk-ant-api03-" + "A1b2_C3d4-" * 4
CLAUDE_CONFIG = BEFORE_MTPROTO + """
[claude]
api_base = "{base}"
model = "{model}"
"""


class SetupInstagram(unittest.TestCase):
    def setUp(self):
        self.agent = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.agent)
        (self.agent / "scripts").mkdir()
        (self.agent / "secrets").mkdir(mode=0o700)
        shutil.copy(ROOT / "scripts" / "setup-instagram.sh", self.agent / "scripts")
        self.config = self.agent / "config.toml"
        self.config.write_text(BEFORE_MTPROTO)
        self.exported = self.agent / "esportati.txt"

    def run_setup(self, cookies):
        self.exported.write_text(cookies)
        return subprocess.run(["bash", str(self.agent / "scripts" / "setup-instagram.sh"), str(self.exported)],
                              capture_output=True, text=True)

    def test_only_instagram_cookies_are_kept_with_the_session(self):
        result = self.run_setup("# Netscape HTTP Cookie File\n"
                                ".example.com\tTRUE\t/\tTRUE\t0\tid\tALTRO-SITO\n"
                                ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tCSRF\n"
                                "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tSEGRETO\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = self.agent / "secrets" / "instagram_cookies.txt"
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        text = saved.read_text()
        self.assertIn("#HttpOnly_.instagram.com", text)
        self.assertNotIn("ALTRO-SITO", text)  # gli altri siti restano fuori
        self.assertNotIn("SEGRETO", result.stdout + result.stderr)
        config = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config["secrets"]["instagram_cookies"], "secrets/instagram_cookies.txt")
        self.assertEqual(self.run_setup(text).returncode, 0)  # di nuovo: la riga in config.toml resta una
        self.assertEqual(self.config.read_text().count("instagram_cookies ="), 1)

    def test_without_a_session_nothing_is_saved(self):
        for cookies in (".example.com\tTRUE\t/\tTRUE\t0\tid\tx\n",
                        ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tx\n"):
            with self.subTest(cookies=cookies):
                self.assertNotEqual(self.run_setup(cookies).returncode, 0)
                self.assertFalse((self.agent / "secrets" / "instagram_cookies.txt").exists())


class FakeClaude(BaseHTTPRequestHandler):
    seen = []  # (percorso, chiave, versione) di ogni richiesta

    def do_GET(self):
        key, version = self.headers.get("x-api-key"), self.headers.get("anthropic-version")
        FakeClaude.seen.append((self.path, key, version))
        if key != KEY or not version:
            status, body = 401, {"type": "error", "error": {"type": "authentication_error"}}
        elif self.path == "/v1/models/claude-haiku-5-5":
            status, body = 200, {"type": "model", "id": "claude-haiku-5-5", "display_name": "Claude Haiku 5.5"}
        else:
            status, body = 404, {"type": "error", "error": {"type": "not_found_error"}}
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass


class SetupClaude(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeClaude)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeClaude.seen.clear()
        self.agent = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.agent)
        (self.agent / "scripts").mkdir()
        (self.agent / "secrets").mkdir(mode=0o700)
        shutil.copy(ROOT / "scripts" / "setup-claude.sh", self.agent / "scripts")
        self.config = self.agent / "config.toml"
        self.key = self.agent / "secrets" / "anthropic_api_key"
        self.write_config()

    def write_config(self, model="claude-haiku-5-5", base=None):
        self.config.write_text(CLAUDE_CONFIG.format(base=base or self.base, model=model))

    def run_setup(self, typed, api_base=None):
        env = {k: v for k, v in os.environ.items() if k != "SAZED_CLAUDE_API_BASE"}
        if api_base:
            env["SAZED_CLAUDE_API_BASE"] = api_base
        result = subprocess.run(["bash", str(self.agent / "scripts" / "setup-claude.sh")],
                                input=typed, capture_output=True, text=True, env=env)
        self.assertNotIn(KEY, result.stdout + result.stderr)  # la chiave non si mostra mai
        return result

    def test_the_key_is_checked_with_the_api_then_saved(self):
        result = self.run_setup(f" \x1b[200~{KEY}\x1b[201~\r\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the agent will use Claude Haiku 5.5 (claude-haiku-5-5)", result.stdout)
        self.assertEqual((self.key.read_text(), stat.S_IMODE(self.key.stat().st_mode)), (KEY, 0o600))
        self.assertEqual(FakeClaude.seen, [("/v1/models/claude-haiku-5-5", KEY, "2023-06-01")])
        self.assertEqual(tomllib.loads(self.config.read_text())["secrets"]["anthropic_api_key"],
                         "secrets/anthropic_api_key")

    def test_a_refused_key_is_not_saved(self):
        result = self.run_setup("sk-ant-api03-" + "x" * 40 + "\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("refused the key (401)", result.stderr)
        self.assertFalse(self.key.exists())

    def test_admin_and_malformed_keys_are_refused_before_any_call(self):
        for typed, message in [("sk-ant-admin01-" + "x" * 40 + "\n", "Admin API key"),
                               ("abc\n", "does not look like a Claude API key"),
                               ("\n", "does not look like a Claude API key")]:
            with self.subTest(typed):
                result = self.run_setup(typed)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
        self.assertEqual(FakeClaude.seen, [])
        self.assertFalse(self.key.exists())

    def test_an_unknown_model_keeps_the_key_and_says_what_to_fix(self):
        self.write_config(model="claude-inesistente")
        result = self.run_setup(f"{KEY}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("does not know the model claude-inesistente", result.stderr)
        self.assertEqual(self.key.read_text(), KEY)

    def test_an_older_config_is_completed_and_without_the_api_the_key_is_kept(self):
        self.config.write_text(BEFORE_MTPROTO)
        result = self.run_setup(f"{KEY}\n", api_base="http://127.0.0.1:9")  # porta chiusa
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not be reached", result.stderr)
        self.assertIn("Run this script again later", result.stderr)
        config = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config["claude"], {"api_base": "https://api.anthropic.com", "model": "claude-haiku-5-5"})
        self.assertEqual(config["secrets"]["anthropic_api_key"], "secrets/anthropic_api_key")
        self.assertEqual(config["storage"], {"data_dir": "data"})
        self.assertEqual(self.key.read_text(), KEY)

    def test_the_example_config_and_a_second_run_stay_as_they_are(self):
        shutil.copy(ROOT / "config.example.toml", self.config)
        for _ in range(2):
            result = self.run_setup(f"{KEY}\n", api_base=self.base)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already points to them", result.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"),
                         (ROOT / "config.example.toml").read_text(encoding="utf-8"))

    def test_setup_must_come_first(self):
        self.config.unlink()
        result = self.run_setup(f"{KEY}\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("run scripts/setup.sh first", result.stderr)


if __name__ == "__main__":
    unittest.main()
