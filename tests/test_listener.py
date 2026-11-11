"""Chi ascolta, con i veri ffmpeg e ffprobe dell'immagine e un finto whisper-cli: vocali, video,
fotogrammi, registrazioni lunghe ed errori."""

import json
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from sazed_agent import listener
from sazed_agent.listener import Listener
from sazed_agent.reader import ReadError
from tests.files import pdf

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
        # tre secondi rossi, poi tre blu: un video senza scritte
        ffmpeg("-f", "lavfi", "-i", "color=c=red:s=320x568:d=3:r=10", "-f", "lavfi", "-i",
               "color=c=blue:s=320x568:d=3:r=10", "-filter_complex", "[0][1]concat=n=2:v=1", "-c:v", "mpeg4",
               str(cls.media / "colors.mp4"))
        # tre schermate di testo, due secondi ciascuna: un reel muto fatto di scritte
        (cls.media / "slides.pdf").write_bytes(pdf(["PASTA E PATATE", "Trecento grammi di pasta mista",
                                                    "Manteca con la provola"]))
        subprocess.run(["pdftoppm", "-png", "-r", "60", str(cls.media / "slides.pdf"), str(cls.media / "s")],
                       check=True)
        pages = sorted(cls.media.glob("s-*.png"))
        inputs = [a for page in pages for a in ("-loop", "1", "-t", "2", "-i", str(page))]
        chain = "".join(f"[{i}]scale=720:1280,setsar=1[v{i}];" for i in range(len(pages)))
        ffmpeg(*inputs, "-filter_complex", chain + "".join(f"[v{i}]" for i in range(len(pages)))
               + f"concat=n={len(pages)}:v=1[out]", "-map", "[out]", "-r", "10", "-c:v", "mpeg4",
               str(cls.media / "slides.mp4"))
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

    async def test_a_video_gives_its_words_and_frames_for_claude(self):
        extract = await self.listener.listen(self.media / "video.mp4", video=True)
        self.assertEqual(extract.text, "Comprare il riso e il parmigiano")
        self.assertTrue(all(kind == "image/jpeg" and data[:2] == b"\xff\xd8" for kind, data in extract.images))
        self.assertTrue(1 <= len(extract.images) <= listener.CLAUDE_FRAMES)

    async def test_the_writing_of_a_silent_video_is_read_on_this_computer_in_order(self):
        extract = await self.listener.listen(self.media / "slides.mp4", video=True)
        self.assertEqual(extract.screen_text, "PASTA E PATATE\nTrecento grammi di pasta mista\nManteca con la provola")
        self.assertIsNone(extract.text)
        self.assertEqual(len(extract.images), 4)  # una per scritta, più una per arrivare a quattro
        self.assertIsNone(self.heard())  # Whisper non serve

    async def test_a_video_without_writing_still_shows_a_few_frames(self):
        extract = await self.listener.listen(self.media / "colors.mp4", video=True)
        self.assertEqual((extract.screen_text, len(extract.images)), (None, listener.CONTEXT_FRAMES))

    async def test_a_long_video_is_read_at_fewer_frames_per_second(self):
        with mock.patch.object(listener, "SCREEN_FRAMES", 2):
            extract = await self.listener.listen(self.media / "colors.mp4", video=True)
        self.assertEqual(len(extract.images), 2)

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


class CaptionsTest(unittest.TestCase):
    def test_close_frames_with_the_same_writing_are_one_caption(self):
        pages = [["3", "UOVA", "E", "200", "G"], ["UOVA", "E", "200", "G", "DI", "ZUCCHERO"], [],
                 ["UOVA", "200", "ZUCCHERO"], ["Monta", "tutto", "per", "10", "minuti"], [], [], [],
                 ["Monta", "tutto", "per", "10", "minuti"]]
        self.assertEqual(listener._captions(pages), [(0, 3, "UOVA E 200 G DI ZUCCHERO"),
                                                     (4, 4, "Monta tutto per 10 minuti"),
                                                     (8, 8, "Monta tutto per 10 minuti")])  # tornata dopo un po'

    def test_noise_that_changes_from_frame_to_frame_goes_away(self):
        pages = [["TIRAMISU", "Sa", "IN"], ["TIRAMISU", "IN", "Pei", "5"], ["5", "TIRAMISU", "ty"], ["MINUTI"]]
        self.assertEqual(listener._steady(pages), [["TIRAMISU", "IN"], ["TIRAMISU", "IN", "5"], ["5", "TIRAMISU"], []])
        self.assertEqual(listener._steady([["solo"], ["due"]]), [["solo"], ["due"]])

    def test_claude_sees_a_frame_per_caption_and_at_least_a_few(self):
        self.assertEqual(listener._chosen([(0, 3, "a"), (4, 6, "b")], 20), [0, 1, 5, 6])  # 1 e 5 a metà delle scritte
        many = [(i, i, str(i)) for i in range(0, 60, 2)]
        self.assertEqual(len(listener._chosen(many, 60)), listener.CLAUDE_FRAMES)
        self.assertEqual(listener._chosen([], 1), [0])

    def test_only_words_tesseract_is_sure_of(self):
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
        rows = [header, "5\t1\t1\t1\t1\t1\t0\t0\t9\t9\t91.5\tMonta", "5\t1\t1\t1\t1\t2\t0\t0\t9\t9\t30\tx#q",
                "5\t1\t1\t1\t1\t3\t0\t0\t9\t9\t88\t3", "5\t1\t1\t1\t1\t4\t0\t0\t9\t9\t95\t)", "1\t1\t0\t0\t0\t0",
                "5\t1\t1\t1\t1\t5\t0\t0\t9\t9\t92\tE", "5\t1\t1\t1\t1\t6\t0\t0\t9\t9\t70\ti"]
        self.assertEqual(listener._confident("\n".join(rows)), ["Monta", "3", "E"])  # "i" non abbastanza sicura



class RunProgramTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_output_is_collected_only_up_to_its_limit(self):
        self.assertEqual(await listener.run_program(["echo", "ciao"], 5, "prova"), b"ciao\n")
        out = await listener.run_program(["yes", "sazed"], 5, "prova", limit=1000)  # non finirebbe mai
        self.assertEqual((len(out), out[:6]), (1000, b"sazed\n"))

    async def test_a_flood_of_errors_does_not_block_and_only_its_end_is_kept(self):
        script = "head -c 300000 /dev/zero | tr '\\0' x >&2; echo >&2; echo fatto; echo ultimo errore >&2; exit 3"
        with self.assertRaisesRegex(ReadError, "^prova: ultimo errore$"):
            await listener.run_program(["sh", "-c", script], 10, "prova")

if __name__ == "__main__":
    unittest.main()
