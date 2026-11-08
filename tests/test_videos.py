"""La lettura dei video con un finto yt-dlp: quali sottotitoli, l'audio quando mancano, la
descrizione, e gli errori che non dicono quale video."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sazed_agent import listener
from sazed_agent.reader import Extract, ReadError
from sazed_agent.videos import Videos, _json3

FAKE_YTDLP = str(Path(__file__).with_name("fake_ytdlp.py"))
URL = "https://www.youtube.com/watch?v=abc123"
PREVIEW = Extract("Carbonara perfetta", "Chef", None, False, (("image/jpeg", b"\xff\xd8"),))


def captions(*lines):
    """Sottotitoli json3 come quelli di YouTube: un evento per riga, con le sue parti."""
    return {"events": [{"tStartMs": i * 1000, "segs": [{"utf8": w} for w in line.split("|")]}
                       for i, line in enumerate(lines)] + [{"tStartMs": 99, "segs": [{"utf8": "\n"}]}]}


class FakeListener:
    def __init__(self, text="guanciale croccante", complete=True):
        self.text, self.complete, self.heard = text, complete, []

    async def listen(self, path, video=False):
        self.heard.append(Path(path).read_text())
        return Extract(None, None, self.text, self.complete, ())


class VideosTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.plan = self.dir / "plan.json"
        self.listener = FakeListener()
        self.videos = Videos(self.listener, command=(FAKE_YTDLP, str(self.plan)))

    def will(self, **plan):
        self.plan.write_text(json.dumps(plan))

    def calls(self):
        return json.loads((self.dir / "calls.json").read_text())

    async def test_written_subtitles_in_the_video_language_come_first(self):
        self.will(info={"language": "it", "description": " Ingredienti: guanciale, uova. ", "duration": 200,
                        "subtitles": {"en": [], "it": []}, "automatic_captions": {"it-orig": []}},
                  subtitles={"it": captions("Ciao a| tutti", "oggi la carbonara")})
        extract = await self.videos.read(URL, PREVIEW)
        self.assertEqual(extract.text, "Description:\nIngredienti: guanciale, uova.\n\n"
                                       "Transcript:\nCiao a tutti oggi la carbonara")
        self.assertEqual((extract.title, extract.author, extract.complete, extract.images),
                         ("Carbonara perfetta", "Chef", True, PREVIEW.images))
        info, subtitles = self.calls()
        for flag in ("--ignore-config", "--no-cookies", "--no-cache-dir", "--no-playlist"):
            self.assertIn(flag, info)
        self.assertEqual((subtitles[subtitles.index("--sub-langs") + 1], "--write-subs" in subtitles), ("it", True))
        self.assertEqual(self.listener.heard, [])

    async def test_automatic_subtitles_only_in_the_spoken_language(self):
        cases = [({"language": "it", "automatic_captions": {"en": [], "it-orig": [], "it": []}}, "it-orig"),
                 ({"language": "en", "automatic_captions": {"de": [], "en": []}}, "en")]
        for info, expected in cases:
            with self.subTest(expected):
                (self.dir / "calls.json").unlink(missing_ok=True)
                self.will(info=info, subtitles={expected: captions("parole")})
                self.assertTrue((await self.videos.read(URL, PREVIEW)).text.endswith("parole"))
                subtitles = self.calls()[1]
                self.assertEqual((subtitles[subtitles.index("--sub-langs") + 1], "--write-auto-subs" in subtitles),
                                 (expected, True))

    async def test_without_subtitles_the_audio_is_heard(self):
        self.will(info={"duration": 100, "automatic_captions": {"de": [], "fr": []}}, audio="suono")  # solo traduzioni
        extract = await self.videos.read(URL, PREVIEW)
        self.assertEqual((extract.text, extract.complete), ("Transcript:\nguanciale croccante", True))
        self.assertEqual(self.listener.heard, ["suono"])
        self.assertNotIn("--download-sections", self.calls()[1])

    async def test_a_long_video_is_heard_only_at_its_beginning(self):
        self.will(info={"duration": 4000})
        extract = await self.videos.read(URL, PREVIEW)
        audio = self.calls()[1]
        self.assertEqual(audio[audio.index("--download-sections") + 1], f"*0-{listener.MAX_SECONDS}")
        self.assertFalse(extract.complete)

    async def test_without_whisper_only_subtitles_and_description(self):
        videos = Videos(command=(FAKE_YTDLP, str(self.plan)))
        self.will(info={"description": "Il link alla ricetta", "title": "Titolo", "channel": "Canale"})
        extract = await videos.read(URL, Extract(None, None, None, False))
        self.assertEqual((extract.title, extract.author, extract.text, extract.complete),
                         ("Titolo", "Canale", "Description:\nIl link alla ricetta", False))
        self.assertEqual(len(self.calls()), 1)  # niente audio

    async def test_errors_never_name_the_video(self):
        self.will(fail=f"ERROR: [youtube] abc123: Video unavailable. Watch on {URL}")
        with self.assertRaises(ReadError) as ctx:
            await self.videos.read(URL, PREVIEW)
        message = str(ctx.exception)
        self.assertIn("video non letto: Video unavailable", message)
        self.assertNotIn("abc123", message)
        self.will(info=[1, 2])
        with self.assertRaisesRegex(ReadError, "risposta non valida"):
            await self.videos.read(URL, PREVIEW)

    def test_json3_text(self):
        self.assertEqual(_json3(captions("uno| due", "tre")), "uno due tre")
        self.assertEqual(_json3({"events": [{"tStartMs": 0}]}), "")


if __name__ == "__main__":
    unittest.main()
