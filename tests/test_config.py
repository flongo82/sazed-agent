"""Configurazione: lettura di config.toml, percorsi relativi, segreti protetti."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from sazed_agent.config import ConfigError, load

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


class Config(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = self.dir / "config.toml"
        shutil.copy(EXAMPLE, self.path)
        (self.dir / "secrets").mkdir(mode=0o700)
        for name, value in [("agent_token", "a" * 64), ("worker_url", "https://w.example/\n"),
                            ("bot_token", " 123:ABC \n")]:
            self.secret(name, value)

    def secret(self, name, value, mode=0o600):
        path = self.dir / "secrets" / name
        path.write_text(value)
        os.chmod(path, mode)

    def edit(self, old, new):
        text = self.path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.path.write_text(text.replace(old, new), encoding="utf-8")

    def test_example_is_valid_and_paths_are_relative_to_the_file(self):
        config = load(self.path)
        self.assertEqual(config.bot_name, "sazed")
        self.assertEqual(config.language, "auto")
        self.assertEqual(config.journal_path, self.dir / "data" / "coppermind.db")
        self.assertEqual(config.reactions.received, "👀")
        self.assertEqual(config.retry_delays, (5.0, 30.0, 120.0, 600.0, 3600.0))

    def test_secrets_are_trimmed(self):
        config = load(self.path)
        self.assertEqual(config.bot_token, "123:ABC")
        self.assertEqual(config.worker_url, "https://w.example")
        self.assertEqual(config.agent_token, "a" * 64)

    def test_missing_config_points_to_setup(self):
        with self.assertRaisesRegex(ConfigError, "setup.sh"):
            load(self.dir / "nessuno.toml")

    def test_secret_readable_by_others_is_refused(self):
        self.secret("bot_token", "123:ABC", mode=0o644)
        with self.assertRaisesRegex(ConfigError, "chmod 600"):
            load(self.path).bot_token

    def test_missing_or_empty_secret(self):
        (self.dir / "secrets" / "agent_token").unlink()
        with self.assertRaisesRegex(ConfigError, "non esiste"):
            load(self.path).agent_token
        self.secret("bot_token", "  \n")
        with self.assertRaisesRegex(ConfigError, "vuoto"):
            load(self.path).bot_token

    def test_error_never_shows_the_secret(self):
        self.secret("bot_token", "123:SEGRETO", mode=0o640)
        with self.assertRaises(ConfigError) as ctx:
            load(self.path).bot_token
        self.assertNotIn("SEGRETO", str(ctx.exception))

    def test_invalid_values(self):
        cases = [
            ('language = "auto"', 'language = "fr"', "language"),
            ("delays = [5, 30, 120, 600, 3600]", "delays = []", "retries.delays"),
            ("ping_interval = 30", "ping_interval = 0", "ping_interval"),
            ('received = "👀"', 'received = ""', "reactions.received"),
            ("pong_timeout = 10", "pong_timeout = 30", "minore di connection.ping_interval"),
        ]
        for old, new, message in cases:
            with self.subTest(new):
                shutil.copy(EXAMPLE, self.path)
                self.edit(old, new)
                with self.assertRaisesRegex(ConfigError, message):
                    load(self.path)


if __name__ == "__main__":
    unittest.main()
