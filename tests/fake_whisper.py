#!/usr/bin/env python3
"""Un finto whisper-cli per i test di chi ascolta: stessi argomenti, nessun modello vero.

Il "modello" è un file JSON con ciò che deve fare: {"text": "..."} lo scrive come trascrizione
(in <-of>.txt, come -otxt), {"fail": true} esce con un errore. Ciò che ha ricevuto (argomenti,
frequenza, canali e byte dell'audio, priorità) finisce in heard.json, accanto al modello.
"""

import json
import os
import struct
import sys
from pathlib import Path

args = sys.argv[1:]
value = lambda flag: args[args.index(flag) + 1]
model = Path(value("-m"))
plan = json.loads(model.read_text())
audio = Path(value("-f")).read_bytes()
channels, rate = struct.unpack("<HI", audio[22:28])
(model.parent / "heard.json").write_text(json.dumps({"args": args, "rate": rate, "channels": channels,
                                                     "bytes": len(audio), "nice": os.nice(0)}))
if plan.get("fail"):
    print("error: failed to process audio", file=sys.stderr)
    sys.exit(1)
Path(value("-of") + ".txt").write_text(plan.get("text", ""))
