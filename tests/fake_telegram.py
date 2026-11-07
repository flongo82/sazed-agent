"""Finto server dell'API dei bot di Telegram per i test.

Annota ogni chiamata con il suo esito e risponde come Telegram. Si può farlo fallire:
- fail(method, status, ...): i prossimi tentativi di quel metodo falliscono con quegli stati;
- rule(funzione): per ogni chiamata, la funzione può restituire uno stato di errore;
- slow(funzione, secondi): le chiamate per cui la funzione è vera rispondono in ritardo.
file(file_id, dati) mette un file sui finti server: getFile ne dà il percorso e GET
/file/bot<token>/<percorso> lo scarica; downloads elenca i percorsi scaricati.

url_entities(testo) dà le entità che Telegram aggiunge ai link di un messaggio.
"""

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "123:TEST-TOKEN"


def url_entities(text):
    """Le entità "url" di Telegram per i link http(s) di un testo, con le posizioni in UTF-16."""
    units = lambda s: len(s.encode("utf-16-le")) // 2
    return [{"type": "url", "offset": units(text[:m.start()]), "length": units(m.group())}
            for m in re.finditer(r"https?://\S+", text)]


class FakeTelegram:
    def __init__(self, token=TOKEN):
        self.token = token
        self.calls = []          # [(metodo, parametri, stato)]
        self._lock = threading.Lock()
        self._plans, self._rules, self._slow = {}, [], []
        self._next_message_id = 9000
        self.files, self.downloads = {}, []

    def start(self, port=0):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                fake._handle(self)

            def do_GET(self):
                fake._download(self)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.api_base = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def fail(self, method, *statuses):
        with self._lock:
            self._plans.setdefault(method, []).extend(statuses)

    def rule(self, decide):
        self._rules.append(decide)

    def file(self, file_id, data):
        self.files[file_id] = data

    def slow(self, match, seconds):
        self._slow.append((match, seconds))

    def ok_calls(self, method=None):
        with self._lock:
            return [(m, p) for m, p, s in self.calls if s == 200 and (method is None or m == method)]

    def _handle(self, request):
        parts = request.path.strip("/").split("/")
        params = json.loads(request.rfile.read(int(request.headers.get("content-length", 0))) or b"{}")
        if len(parts) != 2 or parts[0] != f"bot{self.token}":
            return self._reply(request, None, params, 404, {"ok": False, "description": "Not Found"})
        method = parts[1]
        for match, seconds in self._slow:
            if match(method, params):
                time.sleep(seconds)
        with self._lock:
            plan = self._plans.get(method)
            status = plan.pop(0) if plan else None
        if status is None:
            status = next((s for s in (rule(method, params) for rule in self._rules) if s), None)
        if status:
            body = {"ok": False, "error_code": status, "description": f"errore finto {status}"}
            if status == 429:
                body["parameters"] = {"retry_after": 1}
            return self._reply(request, method, params, status, body)
        if method == "getFile" and params.get("file_id") not in self.files:
            return self._reply(request, method, params, 400, {"ok": False, "error_code": 400,
                                                              "description": "Bad Request: invalid file_id"})
        with self._lock:
            result = True
            if method == "sendMessage":
                result = {"message_id": self._next_message_id}
                self._next_message_id += 1
            if method == "getFile":
                result = {"file_id": params["file_id"], "file_path": f"files/{params['file_id']}"}
        self._reply(request, method, params, 200, {"ok": True, "result": result})

    def _download(self, request):
        prefix = f"/file/bot{self.token}/files/"
        data = self.files.get(request.path[len(prefix):]) if request.path.startswith(prefix) else None
        status = 200 if data is not None else 401 if not request.path.startswith(f"/file/bot{self.token}/") else 404
        with self._lock:
            if status == 200:
                self.downloads.append(request.path[len(prefix):])
        request.send_response(status)
        request.send_header("content-length", str(len(data or b"")))
        request.end_headers()
        request.wfile.write(data or b"")

    def _reply(self, request, method, params, status, body):
        with self._lock:
            self.calls.append((method, params, status))
        data = json.dumps(body).encode()
        request.send_response(status)
        request.send_header("content-type", "application/json")
        request.send_header("content-length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)
