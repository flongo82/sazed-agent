#!/usr/bin/env python3
"""Un finto yt-dlp per i test della lettura dei video: stessi argomenti, nessuna rete.

Si lancia come fake_ytdlp.py <piano.json> <argomenti di yt-dlp>. Il piano dice che cosa
rispondere: "info" (il JSON di --dump-single-json), "subtitles" ({lingua: json3}, scritti con
--write-subs o --write-auto-subs), "audio" (il contenuto dell'audio, scritto con -f), "fail"
(il messaggio d'errore di ogni chiamata), "video" (il contenuto del video, scritto in mp4 con
--merge-output-format). Con --cookies, come quello vero, riscrive il file dei cookie a fine lavoro;
ciò che vi ha letto finisce in cookies.json. Ogni chiamata si aggiunge a calls.json, accanto al
piano.
"""

import json
import sys
from pathlib import Path

plan_path, args = Path(sys.argv[1]), sys.argv[2:]
plan = json.loads(plan_path.read_text())
calls = plan_path.parent / "calls.json"
calls.write_text(json.dumps((json.loads(calls.read_text()) if calls.exists() else []) + [args]))
if plan.get("fail"):
    print(plan["fail"], file=sys.stderr)
    sys.exit(1)
value = lambda flag: args[args.index(flag) + 1]
if "--cookies" in args:
    jar = Path(value("--cookies"))
    seen = plan_path.parent / "cookies.json"
    seen.write_text(json.dumps((json.loads(seen.read_text()) if seen.exists() else []) + [jar.read_text()]))
    jar.write_text(jar.read_text() + "# riscritto da yt-dlp\n")
if "--dump-single-json" in args:
    print(json.dumps(plan["info"]))
elif "--write-subs" in args or "--write-auto-subs" in args:
    language = value("--sub-langs")
    if language in plan.get("subtitles", {}):
        Path(value("-o").replace("%(ext)s", f"{language}.json3")).write_text(json.dumps(plan["subtitles"][language]))
elif "--merge-output-format" in args:
    Path(value("-o").replace("%(ext)s", "mp4")).write_text(plan.get("video", "video"))
elif "-f" in args:
    Path(value("-o").replace("%(ext)s", "webm")).write_text(plan.get("audio", "audio"))
