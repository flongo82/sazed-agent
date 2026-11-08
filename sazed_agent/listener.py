"""L'ascolto dei file: vocali, audio e video diventano testo, e un video anche qualche fotogramma.

- ffprobe dice che cosa c'è nel file (audio, immagini, durata);
- ffmpeg ne prende l'audio, a 16 kHz, e i fotogrammi, a intervalli regolari;
- whisper.cpp trascrive, sul processore e con priorità bassa, per non rallentare il computer.

Tutto avviene in una cartella temporanea che sparisce subito dopo: del file resta solo ciò che
se ne è letto. La trascrizione è automatica: chi la usa (Claude) sa che può sbagliare, e la
tratta come un dato, mai come istruzioni.
"""

import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path

from .reader import Extract, ReadError

MAX_SECONDS = 30 * 60   # si trascrive al più la prima mezz'ora
FRAMES = 4              # fotogrammi di un video, presi a intervalli regolari
FRAME_WIDTH = 768       # abbastanza per leggere le scritte, poco per l'API di Claude
PROBE_TIMEOUT = 30
NICE = 10               # la priorità della trascrizione: il computer resta di chi lo usa


class Listener:
    def __init__(self, model, threads=8, ffmpeg="ffmpeg", ffprobe="ffprobe", whisper="whisper-cli"):
        self.model, self.threads = Path(model), threads
        self.ffmpeg, self.ffprobe, self.whisper = ffmpeg, ffprobe, whisper

    async def listen(self, path, video=False):
        """Ciò che si sente in un file e, se video, ciò che si vede: un Extract con la
        trascrizione e i fotogrammi. Completo se si è ascoltato tutto il file."""
        streams, duration = await self._probe(path)
        with tempfile.TemporaryDirectory(prefix="sazed-") as work:
            work = Path(work)
            text = await self._transcribe(path, work, duration) if "audio" in streams else None
            frames = await self._frames(path, work, duration) if video and "video" in streams else ()
        return Extract(None, None, text, duration is None or duration <= MAX_SECONDS, frames)

    async def _probe(self, path):
        out = await run_program([self.ffprobe, "-v", "error", "-print_format", "json", "-show_format",
                          "-show_streams", str(path)], PROBE_TIMEOUT, "file che non so leggere", permanent=True)
        try:
            data = json.loads(out)
            streams = {s.get("codec_type") for s in data.get("streams", ())}
            duration = data.get("format", {}).get("duration")
            return streams, float(duration) if duration not in (None, "N/A") else None
        except (ValueError, AttributeError, TypeError):
            raise ReadError("file che non so leggere", permanent=True) from None

    async def _transcribe(self, path, work, duration):
        seconds = min(duration or MAX_SECONDS, MAX_SECONDS)
        audio = work / "audio.wav"
        await run_program([self.ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-vn", "-t", str(MAX_SECONDS),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(audio)],
                   60 + seconds / 10, "audio che non so estrarre", permanent=True)
        # language auto: i vocali sono in italiano, i reel in qualunque lingua
        await run_program([self.whisper, "-m", str(self.model), "-f", str(audio), "-l", "auto",
                    "-t", str(self.threads), "-np", "-nt", "-otxt", "-of", str(work / "text")],
                   120 + seconds * 3, "trascrizione non riuscita", nice=True)
        text = " ".join((work / "text.txt").read_text(encoding="utf-8", errors="replace").split())
        return text or None

    async def _frames(self, path, work, duration):
        moments = [duration * (i + 0.5) / FRAMES for i in range(FRAMES)] if duration else [0.0]
        frames = []
        for i, moment in enumerate(moments):
            frame = work / f"frame{i}.jpg"
            try:
                await run_program([self.ffmpeg, "-nostdin", "-v", "error", "-ss", f"{moment:.2f}", "-i", str(path),
                            "-frames:v", "1", "-vf", f"scale='min({FRAME_WIDTH},iw)':-2", "-q:v", "5", str(frame)],
                           60, "fotogramma non estratto")
            except ReadError:
                continue  # senza un fotogramma la lettura va avanti
            if frame.exists() and frame.stat().st_size:
                frames.append(("image/jpeg", frame.read_bytes()))
        return tuple(frames)


async def run_program(command, timeout, failure, permanent=False, nice=False):
    """Esegue un programma e ne restituisce l'uscita; ReadError se fallisce o non finisce in tempo.
    Con nice, a priorità bassa. Se l'attesa viene interrotta (l'agente si ferma), il programma si
    ferma con lei."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    except OSError as err:
        raise ReadError(f"{failure}: {Path(command[0]).name} non si avvia ({type(err).__name__})") from None
    if nice:  # subito, prima che il programma avvii i suoi thread, che ereditano la priorità
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PROCESS, process.pid, NICE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        raise ReadError(f"{failure}: tempo scaduto") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        last = (err.decode(errors="replace").strip().splitlines() or ["nessun messaggio"])[-1][:200]
        raise ReadError(f"{failure}: {last}", permanent=permanent)
    return out
