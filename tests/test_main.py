"""python3 -m sazed_agent check: verifica senza collegarsi e senza mostrare segreti."""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from sazed_agent.__main__ import main

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


class Check(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(EXAMPLE, self.dir / "config.toml")
        (self.dir / "secrets").mkdir(mode=0o700)
        for name, value in [("agent_token", "SEGRETO-AGENTE"), ("worker_url", "https://w.example"),
                            ("bot_token", "123:SEGRETO")]:
            (self.dir / "secrets" / name).write_text(value)
            os.chmod(self.dir / "secrets" / name, 0o600)

    def run_check(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(self.dir / "config.toml"), "check"])
        return code, out.getvalue(), err.getvalue()

    def test_valid_setup(self):
        code, out, _ = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("Worker: w.example", out)
        self.assertIn("received 0", out)
        self.assertTrue((self.dir / "data" / "coppermind.db").exists())
        self.assertNotIn("SEGRETO", out)

    def test_missing_secret(self):
        (self.dir / "secrets" / "bot_token").unlink()
        code, out, err = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("bot_token non esiste", err)


if __name__ == "__main__":
    unittest.main()
