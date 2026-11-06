"""python3 -m sazed_agent check: verifica senza collegarsi e senza mostrare segreti."""

import asyncio
import contextlib
import io
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sazed_agent.__main__ import AlreadyRunning, main, serve, single_instance
from sazed_agent.link import Replaced

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
        self.assertIn("Registro: ricevuti 0, letti 0, salvati 0, risposti 0, completati 0, in errore 0.", out)
        self.assertIn("coppermind: 0 elementi.", out)
        self.assertIn("Claude: non ancora pronto (", out)  # senza chiave l'agente lavora lo stesso
        self.assertIn("setup-claude.sh", out)
        self.assertTrue((self.dir / "data" / "coppermind.db").exists())
        self.assertNotIn("SEGRETO", out)

    def test_addresses_requested_by_the_libraries_stay_out_of_the_logs(self):
        self.run_check()  # httpx scrive ogni indirizzo che apre, cioè i link dell'utente
        self.assertGreaterEqual(logging.getLogger("httpx").getEffectiveLevel(), logging.WARNING)

    def test_missing_secret(self):
        (self.dir / "secrets" / "bot_token").unlink()
        code, out, err = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("bot_token non esiste", err)


class SingleInstance(unittest.TestCase):
    def test_a_second_agent_on_the_same_data_is_refused(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        lock = tmp / "data" / "agent.lock"
        with single_instance(lock):
            with self.assertRaisesRegex(AlreadyRunning, "un altro agente"):
                with single_instance(lock):
                    pass
        with single_instance(lock):  # libero di nuovo dopo l'uscita
            pass

    def test_replaced_agent_exits_with_its_own_code(self):
        err = io.StringIO()
        with mock.patch("sazed_agent.__main__.run", side_effect=Replaced()), contextlib.redirect_stderr(err):
            code = main(["--config", "config.toml", "run"])  # run è finto: il file non serve
        self.assertEqual(code, 2)
        self.assertIn("un altro agente", err.getvalue())

    def test_run_without_configuration(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["--config", "/nessuna/cartella/config.toml", "run"])
        self.assertEqual(code, 1)
        self.assertIn("setup.sh", err.getvalue())


class Serve(unittest.IsolatedAsyncioTestCase):
    async def test_replaced_link_lets_the_lanes_finish_then_propagates(self):
        class Link:
            async def run(self, stop):
                raise Replaced()

        class Lanes:
            finished = False

            async def run(self, stop):
                await stop.wait()
                self.finished = True

        lanes = Lanes()
        with self.assertRaises(Replaced):
            await asyncio.wait_for(serve(Link(), lanes), 2)
        self.assertTrue(lanes.finished)


if __name__ == "__main__":
    unittest.main()
