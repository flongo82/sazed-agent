"""Finto server dell'API di Claude per i test: risponde a POST /v1/messages come l'API vera.

- answer(funzione): dal messaggio dell'utente (già decodificato dal JSON che manda l'agente)
  alla risposta strutturata da restituire; per difetto, ogni messaggio è da salvare;
- fail(stato, ...): le prossime richieste falliscono con quegli stati;
- stop_with(motivo): la prossima risposta finisce con quello stop_reason (per esempio "refusal").
Ogni richiesta resta in requests, con corpo e intestazioni.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "sk-ant-api03-test-key"


def save_all(message):
    return {"intent": "save", "question": None, "items": []}


class FakeClaude:
    def __init__(self, key=KEY):
        self.key = key
        self.requests = []
        self._answer, self._failures, self._stop = save_all, [], None
        self._lock = threading.Lock()

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                fake._handle(self)

            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.api_base = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def answer(self, function):
        self._answer = function

    def fail(self, *statuses):
        with self._lock:
            self._failures.extend(statuses)

    def stop_with(self, reason):
        self._stop = reason

    def messages(self):
        """I messaggi dell'utente ricevuti, decodificati."""
        return [json.loads(r["body"]["messages"][-1]["content"]) for r in self.requests]

    def _handle(self, handler):
        body = json.loads(handler.rfile.read(int(handler.headers["content-length"])))
        with self._lock:
            self.requests.append({"path": handler.path, "body": body, "headers": dict(handler.headers)})
            failure = self._failures.pop(0) if self._failures else None
            stop, self._stop = self._stop, None
        if handler.headers.get("x-api-key") != self.key:
            return self._send(handler, 401, {"type": "error", "error": {"type": "authentication_error",
                                                                        "message": "invalid x-api-key"}})
        if failure:
            headers = {"retry-after": "1"} if failure == 429 else {}
            return self._send(handler, failure, {"type": "error", "error": {"type": "api_error",
                                                                            "message": "errore finto"}}, headers)
        message = json.loads(body["messages"][-1]["content"])
        content = [] if stop == "refusal" else [{"type": "text",
                                                 "text": json.dumps(self._answer(message), ensure_ascii=False)}]
        self._send(handler, 200, {
            "id": f"msg_{len(self.requests)}", "type": "message", "role": "assistant", "model": body["model"],
            "content": content, "stop_reason": stop or "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20}})

    def _send(self, handler, status, payload, headers=None):
        data = json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(data)))
        for k, v in (headers or {}).items():
            handler.send_header(k, v)
        handler.end_headers()
        handler.wfile.write(data)
