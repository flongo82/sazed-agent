"""Finto server dell'API dei bot di Telegram per i test.

Annota ogni chiamata con il suo esito e risponde come Telegram. Si può farlo fallire:
- fail(method, status, ...): i prossimi tentativi di quel metodo falliscono con quegli stati;
- rule(funzione): per ogni chiamata, la funzione può restituire uno stato di errore;
- slow(funzione, secondi): le chiamate per cui la funzione è vera rispondono in ritardo.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "123:TEST-TOKEN"


class FakeTelegram:
    def __init__(self, token=TOKEN):
        self.token = token
        self.calls = []          # [(metodo, parametri, stato)]
        self._lock = threading.Lock()
        self._plans, self._rules, self._slow = {}, [], []
        self._next_message_id = 9000

    def start(self, port=0):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                fake._handle(self)

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
        with self._lock:
            result = True
            if method == "sendMessage":
                result = {"message_id": self._next_message_id}
                self._next_message_id += 1
        self._reply(request, method, params, 200, {"ok": True, "result": result})

    def _reply(self, request, method, params, status, body):
        with self._lock:
            self.calls.append((method, params, status))
        data = json.dumps(body).encode()
        request.send_response(status)
        request.send_header("content-type", "application/json")
        request.send_header("content-length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)
