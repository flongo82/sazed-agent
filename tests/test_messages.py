"""Catalogo dei testi: stesse chiavi in ogni lingua, testi esatti, scelta della lingua."""

import inspect
import re
import unittest

from sazed_agent.messages import MESSAGES, STATUS_COMMANDS, messages_for

# Argomenti di prova per i testi che non accettano una stringa qualunque.
SAMPLES = {"failed": (2,)}


def shape(block):
    """Chiavi e tipi di un blocco; per le funzioni anche il numero di argomenti obbligatori."""
    out = {}
    for key, value in block.items():
        if callable(value):
            params = inspect.signature(value).parameters.values()
            out[key] = ("function", sum(p.default is p.empty for p in params))
        else:
            out[key] = type(value).__name__
    return out


class Catalog(unittest.TestCase):
    def test_same_keys_types_and_arguments_in_every_language(self):
        for lang, block in MESSAGES.items():
            self.assertEqual(shape(block), shape(MESSAGES["it"]), lang)

    def test_every_text_is_a_full_string(self):
        for lang, block in MESSAGES.items():
            for key, value in block.items():
                if callable(value):
                    text = value(*SAMPLES.get(key, ["x"] * shape(block)[key][1]))
                else:
                    text = value
                self.assertIsInstance(text, str, f"{lang}.{key}")
                self.assertTrue(text and "None" not in text, f"{lang}.{key}: {text!r}")

    def test_status_command_valid_for_telegram(self):
        for lang, block in MESSAGES.items():
            self.assertRegex(block["status_command"], r"^[a-z0-9_]{1,32}$", lang)
        self.assertEqual(STATUS_COMMANDS, {"/stato", "/status"})

    def test_exact_texts(self):
        it, en = MESSAGES["it"], MESSAGES["en"]
        self.assertEqual(it["hello"]("Ada"), "Ciao Ada")
        self.assertEqual(it["hello"](), "Ciao")
        self.assertEqual(en["hello"]("Ada"), "Hi Ada")
        self.assertEqual(en["hello"](), "Hi")
        self.assertEqual(it["status"]("Custode", 2, 1, 40),
                         "l'agente Custode è collegato. Messaggi da processare: 2, in errore: 1. "
                         "Elementi custoditi: 40.")
        self.assertEqual(en["status"]("Custode", 2, 1, 40),
                         "the Custode agent is connected. Messages to process: 2, failed: 1. Items kept: 40.")
        self.assertEqual((it["unknown_command"]("/aiuto"), en["unknown_command"]("/help")),
                         ("non conosco il comando /aiuto.", "I don't know the command /help."))
        self.assertEqual(it["no_answer"], "non sono riuscito a rispondere a questa domanda: riprova tra poco.")
        self.assertEqual(it["failed"](5),
                         "non sono riuscito a elaborare questo messaggio dopo 5 tentativi: lo tengo da parte.")


class Language(unittest.TestCase):
    def test_auto_follows_the_telegram_app(self):
        self.assertIs(messages_for("auto", "it"), MESSAGES["it"])
        self.assertIs(messages_for("auto", "en-GB"), MESSAGES["en"])

    def test_auto_falls_back_to_english(self):
        self.assertIs(messages_for("auto", "xx"), MESSAGES["en"])
        self.assertIs(messages_for("auto", None), MESSAGES["en"])

    def test_fixed_language_wins_over_the_app(self):
        self.assertIs(messages_for("it", "en"), MESSAGES["it"])
        self.assertIs(messages_for(" EN ", "it"), MESSAGES["en"])


class NoHardcodedTexts(unittest.TestCase):
    def test_greetings_only_in_the_catalog(self):
        import pathlib
        package = pathlib.Path(__file__).resolve().parent.parent / "sazed_agent"
        for path in package.glob("*.py"):
            if path.name == "messages.py":
                continue
            self.assertIsNone(re.search(r"\"(Ciao|Hi) ", path.read_text(encoding="utf-8")), path.name)


if __name__ == "__main__":
    unittest.main()
