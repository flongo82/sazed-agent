"""Chi ascolta, con i veri ffmpeg e ffprobe dell'immagine e un finto whisper-cli: vocali, video,
fotogrammi, registrazioni lunghe ed errori."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sazed_agent import listener
from sazed_agent.listener import Listener
from sazed_agent.reader import ReadError

FAKE_WHISPER = str(Path(__file__).with_name("fake_whisper.py"))


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *args], check=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "servono ffmpeg e ffprobe, che sono nell'immagine")
class ListenerTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = Path(tempfile.mkdtemp())
        sine, picture = ("-f", "lavfi", "-i", "sine=frequency=440:duration=4"), (
            "-f", "lavfi", "-i", "testsrc=duration=4:size=1280x720:rate=10")
        ffmpeg(*sine, "-c:a", "libopus", str(cls.media / "voice.ogg"))
        ffmpeg(*picture, *sine, "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(cls.media / "video.mp4"))
        ffmpeg(*picture, "-c:v", "mpeg4", str(cls.media / "silent.mp4"))
        (cls.media / "notes.txt").write_text("non sono un audio")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.media)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.model = self.dir / "model.bin"
        self.will({"text": "  Comprare il riso\n e il parmigiano "})
        self.listener = Listener(self.model, threads=3, whisper=FAKE_WHISPER)

    def will(self, plan):
        self.model.write_text(json.dumps(plan))

    def heard(self):
        path = self.dir / "heard.json"
        return json.loads(path.read_text()) if path.exists() else None

    async def test_a_voice_message_becomes_text(self):
        extract = await self.listener.listen(self.media / "voice.ogg")
        self.assertEqual((extract.text, extract.complete, extract.images),
                         ("Comprare il riso e il parmigiano", True, ()))
        heard = self.heard()
        self.assertEqual((heard["rate"], heard["channels"]), (16000, 1))
        args = heard["args"]
        self.assertEqual((args[args.index("-l") + 1], args[args.index("-t") + 1]), ("auto", "3"))
        self.assertEqual(heard["nice"], 10)  # priorità bassa

    async def test_a_video_gives_its_words_and_some_frames(self):
        extract = await self.listener.listen(self.media / "video.mp4", video=True)
        self.assertEqual(extract.text, "Comprare il riso e il parmigiano")
        self.assertEqual(len(extract.images), listener.FRAMES)
        self.assertTrue(all(kind == "image/jpeg" and data[:2] == b"\xff\xd8" for kind, data in extract.images))
        self.assertEqual(len({data for _, data in extract.images}), listener.FRAMES)  # momenti diversi

    async def test_a_video_without_sound_gives_only_frames(self):
        extract = await self.listener.listen(self.media / "silent.mp4", video=True)
        self.assertEqual((extract.text, len(extract.images)), (None, listener.FRAMES))
        self.assertIsNone(self.heard())  # Whisper non serve

    async def test_silence_is_no_text(self):
        self.will({"text": " \n "})
        self.assertIsNone((await self.listener.listen(self.media / "voice.ogg")).text)

    async def test_only_the_beginning_of_a_long_recording_is_heard(self):
        self.patch_max(1)
        extract = await self.listener.listen(self.media / "voice.ogg")
        self.assertFalse(extract.complete)
        self.assertLessEqual(self.heard()["bytes"], 44 + 16000 * 2 * 1 + 1024)  # un secondo, a 16 bit

    async def test_what_is_not_audio_or_video_cannot_be_read(self):
        with self.assertRaises(ReadError) as ctx:
            await self.listener.listen(self.media / "notes.txt")
        self.assertTrue(ctx.exception.permanent)

    async def test_a_failed_transcription_is_tried_again(self):
        self.will({"fail": True})
        with self.assertRaisesRegex(ReadError, "trascrizione non riuscita: error: failed") as ctx:
            await self.listener.listen(self.media / "voice.ogg")
        self.assertFalse(ctx.exception.permanent)

    async def test_a_missing_program_is_an_error_not_a_crash(self):
        lonely = Listener(self.model, whisper=str(self.dir / "whisper-cli"))
        with self.assertRaisesRegex(ReadError, "whisper-cli non si avvia"):
            await lonely.listen(self.media / "voice.ogg")

    def patch_max(self, seconds):
        old = listener.MAX_SECONDS
        listener.MAX_SECONDS = seconds
        self.addCleanup(setattr, listener, "MAX_SECONDS", old)


if __name__ == "__main__":
    unittest.main()
