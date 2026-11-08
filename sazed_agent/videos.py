"""I video delle piattaforme, con yt-dlp: descrizione, sottotitoli e, se mancano, l'audio.

Si comincia dall'anteprima pubblica (titolo, canale, copertina), poi yt-dlp aggiunge:
- la descrizione del video, che spesso ha la ricetta, i passi o i link;
- i sottotitoli nella lingua originale: prima quelli scritti da chi ha pubblicato il video,
  poi quelli automatici della lingua parlata (mai le traduzioni automatiche);
- se non ci sono sottotitoli, l'audio, che chi ascolta (Whisper) trascrive: al più la prima
  mezz'ora, scaricando solo quella.

yt-dlp parte senza configurazione, senza cookie né cache, e mai su una playlist intera. Tutto
avviene in una cartella temporanea che sparisce subito dopo. Gli errori non riportano
l'indirizzo del video: i link dell'utente non vanno nei log.
"""

import json
import re
import tempfile
from pathlib import Path

from . import listener
from .listener import run_program
from .reader import Extract, ReadError

PLATFORMS = ("youtube",)    # Instagram vuole un account, TikTok per ora non serve
MAX_TRANSCRIPT = 100_000    # caratteri della trascrizione che si custodiscono
INFO_TIMEOUT, SUBTITLES_TIMEOUT, AUDIO_TIMEOUT = 90, 90, 600
BASE = ["--ignore-config", "--no-cookies", "--no-cache-dir", "--no-update", "--no-playlist", "--no-progress",
        "--no-warnings"]


class Videos:
    def __init__(self, listener=None, command=("yt-dlp",)):
        self.listener = listener  # senza, solo i sottotitoli
        self.command = list(command)

    async def read(self, url, preview):
        """Ciò che si legge di un video oltre all'anteprima (preview): un Extract con la
        descrizione e la trascrizione, completo se la trascrizione copre tutto il video."""
        with tempfile.TemporaryDirectory(prefix="sazed-") as work:
            work = Path(work)
            info = await self._info(url)
            duration = info.get("duration") if isinstance(info.get("duration"), (int, float)) else None
            transcript, complete = None, False
            track = _track(info)
            if track:
                transcript = await self._subtitles(url, *track, work)
                complete = transcript is not None
            if transcript is None and self.listener is not None:
                heard = await self.listener.listen(await self._audio(url, duration, work))
                transcript = heard.text
                complete = heard.complete and (duration is None or duration <= listener.MAX_SECONDS)
        description = _text(info.get("description"))
        parts = [f"Description:\n{description}" if description else None,
                 f"Transcript:\n{transcript[:MAX_TRANSCRIPT]}" if transcript else None]
        return Extract(preview.title or _text(info.get("title")), preview.author or _text(info.get("channel")),
                       "\n\n".join(p for p in parts if p) or preview.text, complete, preview.images)

    async def _info(self, url):
        out = await self._yt_dlp(["--skip-download", "--dump-single-json", url], INFO_TIMEOUT, "video non letto")
        try:
            info = json.loads(out)
        except ValueError:
            raise ReadError("video non letto: risposta non valida") from None
        if not isinstance(info, dict):
            raise ReadError("video non letto: risposta non valida")
        return info

    async def _subtitles(self, url, language, automatic, work):
        await self._yt_dlp(["--skip-download", "--write-auto-subs" if automatic else "--write-subs",
                            "--sub-langs", language, "--sub-format", "json3", "-o", str(work / "video.%(ext)s"),
                            url], SUBTITLES_TIMEOUT, "sottotitoli non letti")
        files = sorted(work.glob("video*.json3"))
        if not files:
            return None
        try:
            return _json3(json.loads(files[0].read_text(encoding="utf-8", errors="replace"))) or None
        except ValueError:
            return None

    async def _audio(self, url, duration, work):
        section = [] if duration is None or duration <= listener.MAX_SECONDS else [
            "--download-sections", f"*0-{listener.MAX_SECONDS}"]
        await self._yt_dlp(["-f", "bestaudio/best", *section, "-o", str(work / "audio.%(ext)s"), url],
                           AUDIO_TIMEOUT, "audio non scaricato")
        files = [f for f in work.glob("audio.*") if not f.name.endswith(".part")]
        if not files:
            raise ReadError("audio non scaricato: nessun file")
        return files[0]

    async def _yt_dlp(self, arguments, timeout, failure):
        try:
            return await run_program([*self.command, *BASE, *arguments], timeout, failure)
        except ReadError as err:  # "[youtube] <id>: ..." o un indirizzo direbbero quale video: resta il motivo
            reason = re.sub(r"(ERROR: )?\[[^\]]*\] [^:\s]+: ", "", str(err))
            raise ReadError(re.sub(r"https?://\S+", "(indirizzo)", reason), err.permanent) from None


def _track(info):
    """Quali sottotitoli scaricare, come (lingua, automatici): quelli scritti nella lingua del
    video (o, se non si sa, i primi), altrimenti quelli automatici della lingua parlata. None se
    non ce ne sono."""
    language = info.get("language") if isinstance(info.get("language"), str) else None
    written = [k for k in (info.get("subtitles") or {}) if k != "live_chat"]
    if written:
        same = [k for k in written if language and (k == language or k.startswith(f"{language}-"))]
        return (same or written)[0], False
    automatic = list(info.get("automatic_captions") or {})
    spoken = [k for k in automatic if k.endswith("-orig")] or [k for k in automatic if language and k == language]
    return (spoken[0], True) if spoken else None


def _json3(data):
    """Il testo dei sottotitoli in formato json3: gli eventi in fila, gli spazi in ordine."""
    lines = ("".join(s.get("utf8", "") for s in e.get("segs") or ()) for e in data.get("events") or ())
    return " ".join(" ".join(lines).split())


def _text(value):
    return (value.strip() or None) if isinstance(value, str) else None
