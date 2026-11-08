"""L'ascolto dei file: vocali, audio e video diventano testo, e un video anche i suoi fotogrammi.

- ffprobe dice che cosa c'è nel file (audio, immagini, durata);
- ffmpeg ne prende l'audio, a 16 kHz, e un fotogramma al secondo; tesseract ne legge le scritte,
  in locale, e le scritte uguali che si susseguono diventano una: in un video muto o senza
  descrizione sono spesso tutto ciò che dice. A chi descrive (Claude) vanno quel testo e un
  fotogramma per ogni scritta diversa, che legge meglio di tesseract le scritte sopra immagini
  mosse: i fotogrammi si scelgono quando cambia la scritta, non la scena;
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
SCREEN_RATE = 1         # fotogrammi al secondo che tesseract legge
SCREEN_FRAMES = 120     # al più, distribuiti su tutto il video
CLAUDE_FRAMES = 12      # fotogrammi per Claude al più: uno per ogni scritta diversa
CONTEXT_FRAMES = 4      # almeno, per vedere che cosa mostra un video anche senza scritte
FRAME_WIDTH = 640       # abbastanza per leggere le scritte, poco per l'API di Claude
OCR_CONFIDENCE = 60     # sotto, una parola letta da tesseract è più probabilmente rumore
OCR_SURE = 85           # una lettera sola ("e", "a", "è") si tiene solo se tesseract ne è sicuro
OCR_PARALLEL = 4        # letture di tesseract insieme, un processore ciascuna
SAME_CAPTION = 0.6      # quanta parte della lettura più corta, in comune, fa di due fotogrammi la stessa scritta
SCREEN_TEXT_MAX = 8_000
# Due versioni di ogni fotogramma per tesseract: il grigio, per le scritte nette (su una fascia,
# su uno sfondo calmo), e solo il chiaro vicino a un bordo scuro, ingrandito, per le scritte
# bianche bordate di nero sopra il video, le più comuni nei reel.
OUTLINED = ("format=gray,split[a][b];[a]lutyuv=y='if(gt(val,200),255,0)'[l];"
            "[b]lutyuv=y='if(lt(val,70),255,0)',dilation,dilation,dilation[d];"
            "[l][d]blend=all_mode=multiply,negate,scale=iw*2:-2")
PROBE_TIMEOUT = 30
NICE = 10               # la priorità della trascrizione: il computer resta di chi lo usa


class Listener:
    def __init__(self, model, threads=8, ffmpeg="ffmpeg", ffprobe="ffprobe", whisper="whisper-cli",
                 tesseract="tesseract"):
        self.model, self.threads = Path(model), threads
        self.ffmpeg, self.ffprobe, self.whisper, self.tesseract = ffmpeg, ffprobe, whisper, tesseract

    async def listen(self, path, video=False):
        """Ciò che si sente in un file e, se video, ciò che si vede: un Extract con la
        trascrizione, le scritte e i fotogrammi per Claude. Completo se si è ascoltato tutto il file."""
        streams, duration = await self._probe(path)
        with tempfile.TemporaryDirectory(prefix="sazed-") as work:
            work = Path(work)
            text = await self._transcribe(path, work, duration) if "audio" in streams else None
            screen, frames = await self._screen(path, work, duration) if video and "video" in streams else (None, ())
        return Extract(None, None, text, duration is None or duration <= MAX_SECONDS, frames, screen)

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

    async def _screen(self, path, work, duration):
        """Le scritte del video e i fotogrammi da mostrare a Claude. ffmpeg prende un fotogramma al
        secondo (meno in un video lungo: al più SCREEN_FRAMES), nelle due versioni per tesseract e
        in una piccola a colori; tesseract le legge in locale. A Claude va un fotogramma per ogni
        scritta diversa e, se sono pochi, altri distribuiti su tutto il video."""
        seconds = min(duration or MAX_SECONDS, MAX_SECONDS)
        rate = min(SCREEN_RATE, SCREEN_FRAMES / seconds) if seconds else SCREEN_RATE
        folder = work / "frames"
        folder.mkdir()
        graph = (f"[0:v]fps={rate:.4f},split=3[g][o][c];[g]format=gray,scale='min(1080,iw)':-2[gray];"
                 f"[o]{OUTLINED}[outlined];[c]scale='min({FRAME_WIDTH},iw)':-2[small]")
        try:
            await run_program([self.ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-t", str(MAX_SECONDS), "-an",
                               "-filter_complex", graph, "-map", "[gray]", str(folder / "g%04d.png"),
                               "-map", "[outlined]", str(folder / "o%04d.png"), "-map", "[small]", "-q:v", "5",
                               str(folder / "c%04d.jpg")], 60 + seconds, "fotogrammi non estratti", nice=True)
        except ReadError:
            return None, ()  # senza fotogrammi la lettura va avanti
        small = sorted(folder.glob("c*.jpg"))
        if not small:
            return None, ()
        segments = _captions(_steady(await self._read_screens(folder, len(small))))
        text = "\n".join(dict.fromkeys(caption for _, _, caption in segments))[:SCREEN_TEXT_MAX] or None
        return text, tuple(("image/jpeg", small[i].read_bytes()) for i in _chosen(segments, len(small)))

    async def _read_screens(self, folder, count):
        """Le parole di ogni fotogramma, lette da tesseract nelle due versioni: si tiene quella che
        ne legge di più. OCR_PARALLEL letture insieme, a priorità bassa."""
        gate = asyncio.Semaphore(OCR_PARALLEL)

        async def words(path):
            async with gate:
                try:
                    out = await run_program([self.tesseract, str(path), "stdout", "-l", "ita+eng", "--psm", "11",
                                             "tsv"], 60, "scritte non lette", nice=True, env={"OMP_THREAD_LIMIT": "1"})
                except ReadError:
                    return []  # un fotogramma che non si legge non ferma gli altri
            return _confident(out.decode("utf-8", "replace"))
        both = await asyncio.gather(*(words(folder / f"{kind}{i:04d}.png") for i in range(1, count + 1)
                                      for kind in "go"))
        return [max(both[2 * i], both[2 * i + 1], key=_letters) for i in range(count)]


def _confident(tsv):
    """Le parole di una lettura di tesseract (formato TSV) di cui è abbastanza sicuro, in ordine."""
    words = []
    for row in tsv.splitlines()[1:]:
        fields = row.split("\t")
        if len(fields) != 12 or not fields[11].strip():
            continue
        word, confidence, letters = fields[11].strip(), float(fields[10]), _letters([fields[11]])
        if confidence >= OCR_CONFIDENCE and (letters >= 2 or word.isdigit() or letters == 1 and confidence >= OCR_SURE):
            words.append(word)
    return words


def _letters(words):
    return sum(c.isalnum() for word in words for c in word)


def _steady(pages):
    """Le parole che restano almeno due fotogrammi di seguito: una scritta resta a schermo qualche
    secondo, mentre il rumore che tesseract legge su un'immagine mossa cambia da un fotogramma
    all'altro. Un video di uno o due fotogrammi resta com'è."""
    if len(pages) < 3:
        return pages
    bags = [{w.lower() for w in words} for words in pages] + [set()]  # dopo l'ultimo, niente
    return [[w for w in words if w.lower() in bags[i - 1] | bags[i + 1]] if i else
            [w for w in words if w.lower() in bags[1]] for i, words in enumerate(pages)]


def _captions(pages):
    """Le scritte del video, in ordine, da le parole di ogni fotogramma: fotogrammi vicini (al più
    uno senza parole in mezzo) con abbastanza parole in comune, rispetto alla lettura più corta
    (tesseract a volte ne legge solo una parte), portano la stessa scritta, di cui si tiene la
    lettura più ricca. Come (primo fotogramma, ultimo, testo)."""
    segments = []  # [primo, ultimo, parole migliori, tutte le parole viste]
    for i, words in enumerate(pages):
        if not words:
            continue
        bag = {w.lower() for w in words}
        last = segments[-1] if segments else None
        if last and i - last[1] <= 2 and len(bag & last[3]) / min(len(bag), len(last[3])) >= SAME_CAPTION:
            last[1] = i
            last[3] |= bag
            if _letters(words) > _letters(last[2]):
                last[2] = words
        else:
            segments.append([i, i, words, bag])
    return [(first, last, " ".join(words)) for first, last, words, _ in segments]


def _chosen(segments, count):
    """I fotogrammi per Claude: uno a metà di ogni scritta, al più CLAUDE_FRAMES distribuiti fra le
    scritte; se sono meno di CONTEXT_FRAMES, altri distribuiti su tutto il video."""
    marks = [(first + last) // 2 for first, last, _ in segments]
    if len(marks) > CLAUDE_FRAMES:
        marks = [marks[round(k * (len(marks) - 1) / (CLAUDE_FRAMES - 1))] for k in range(CLAUDE_FRAMES)]
    chosen = set(marks)
    for k in range(CONTEXT_FRAMES):
        if len(chosen) >= min(CONTEXT_FRAMES, count):
            break
        chosen.add(round(k * (count - 1) / (CONTEXT_FRAMES - 1)) if count > 1 else 0)
    return sorted(chosen)


async def run_program(command, timeout, failure, permanent=False, nice=False, env=None):
    """Esegue un programma e ne restituisce l'uscita; ReadError se fallisce o non finisce in tempo.
    Con nice, a priorità bassa; env aggiunge variabili d'ambiente a PATH, l'unica che passa. Se
    l'attesa viene interrotta (l'agente si ferma), il programma si ferma con lei."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **(env or {})})
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
