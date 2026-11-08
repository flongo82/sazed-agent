#!/usr/bin/env python3
"""Un finto llama-server per i test del significato: stessi argomenti, nessun modello vero.

Ascolta sul socket UNIX dato con --host e vuole la chiave del file --api-key-file. A ogni testo
dà un vettore che dipende dalle sue parole (dopo "Query:", per una domanda): testi con parole in
comune sono vicini. Il "modello" è un file JSON con ciò che deve fare: {"exit": true} esce
subito, {"fail": true} risponde 500 agli embedding. Argomenti, priorità e testi ricevuti finiscono
in calls.json, accanto al modello.
"""

import hashlib
import json
import os
import re
import socketserver
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

args = sys.argv[1:]
value = lambda flag: args[args.index(flag) + 1]
model = Path(value("-m"))
plan = json.loads(model.read_text())
key = Path(value("--api-key-file")).read_text().strip()
calls = model.parent / "calls.json"


def record(entry):
    seen = json.loads(calls.read_text()) if calls.exists() else []
    calls.write_text(json.dumps(seen + [entry]))


def vector(text):
    out = [0.0] * 64
    for word in re.findall(r"\w+", text.split("Query:")[-1].lower()):
        out[hashlib.md5(word.encode()).digest()[0] % 64] += 1.0
    return out


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send(200 if self.path == "/health" else 404, {"status": "ok"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.headers.get("authorization") != f"Bearer {key}":
            return self.send(401, {"error": "invalid api key"})
        record({"texts": body["input"], "nice": os.getpriority(os.PRIO_PROCESS, 0)})
        if plan.get("fail"):
            return self.send(500, {"error": "finto errore"})
        self.send(200, {"object": "list", "data": [{"index": i, "embedding": vector(t)}
                                                     for i, t in enumerate(body["input"])]})

    def send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def address_string(self):
        return "socket"

    def log_message(self, *args):
        pass


record({"args": args})
if plan.get("exit"):
    sys.exit(1)
with socketserver.UnixStreamServer(value("--host"), Handler) as server:
    server.serve_forever()
