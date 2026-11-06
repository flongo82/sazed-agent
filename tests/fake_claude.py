"""Finto server dell'API di Claude per i test: risponde a POST /v1/messages come l'API vera.

Riconosce i tre compiti dell'agente dalla richiesta: "interpret" e "describe" (risposte
strutturate, dal prompt di sistema) e "answer" (con gli strumenti). Per ciascuno:
- on(compito, funzione): la funzione riceve il messaggio dell'utente già decodificato dal JSON
  (per "answer", l'elenco dei messaggi della conversazione con gli strumenti) e restituisce la
  risposta: un dizionario per i compiti strutturati; per "answer" un testo, oppure
  tool(nome, input) per chiedere uno strumento;
- per difetto: le parole accompagnano gli elementi del messaggio o sono una nota, la
  descrizione riprende il titolo letto, la risposta è "ecco la risposta.".
fail(stato, ...) fa fallire le prossime richieste; stop_with(motivo) chiude la prossima con quello
stop_reason (per esempio "refusal"). Ogni richiesta resta in requests, con il suo compito.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "sk-ant-api03-test-key"


def plain(message):
    current = message["current"]
    ids = [i["id"] for i in current["items"]]
    words = current["words"]
    return interpretation(comment=words if ids else None, comment_on=ids, note=None if ids else words)


def interpretation(note=None, comment=None, comment_on=(), question=None, about=(), kind=None, title=None,
                   topics=(), summary=None):
    return {"note": note, "note_kind": kind, "note_title": title, "note_topics": list(topics),
            "note_summary": summary, "comment": comment, "comment_on": list(comment_on),
            "question": question, "about": list(about)}


def description(kind="article", title=None, topics=(), summary=None):
    return {"kind": kind, "title": title, "topics": list(topics), "summary": summary}


def tool(name, arguments):
    return {"tool": name, "input": arguments}


DEFAULTS = {
    "interpret": plain,
    "describe": lambda message: description(title=message.get("title")),
    "answer": lambda messages: "ecco la risposta.",
}


class FakeClaude:
    def __init__(self, key=KEY):
        self.key = key
        self.requests = []
        self._answers, self._failures, self._stop = dict(DEFAULTS), [], None
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

    def on(self, task, function):
        self._answers[task] = function

    def fail(self, *statuses):
        with self._lock:
            self._failures.extend(statuses)

    def stop_with(self, reason):
        self._stop = reason

    def of(self, task):
        return [r for r in self.requests if r["task"] == task]

    def messages(self, task):
        """I primi messaggi dell'utente delle richieste di un compito, decodificati."""
        return [json.loads(r["body"]["messages"][0]["content"]) for r in self.of(task)]

    def _handle(self, handler):
        body = json.loads(handler.rfile.read(int(handler.headers["content-length"])))
        task = "answer" if "tools" in body else "describe" if body["system"].startswith("You describe") \
            else "interpret"
        with self._lock:
            self.requests.append({"task": task, "path": handler.path, "body": body,
                                  "headers": dict(handler.headers)})
            failure = self._failures.pop(0) if self._failures else None
            stop, self._stop = self._stop, None
        if handler.headers.get("x-api-key") != self.key:
            return self._send(handler, 401, {"type": "error", "error": {"type": "authentication_error",
                                                                        "message": "invalid x-api-key"}})
        if failure:
            headers = {"retry-after": "1"} if failure == 429 else {}
            return self._send(handler, failure, {"type": "error", "error": {"type": "api_error",
                                                                            "message": "errore finto"}}, headers)
        if stop == "refusal":
            content, reason = [], "refusal"
        elif task == "answer":
            reply = self._answers[task](body["messages"])
            if isinstance(reply, dict):
                content = [{"type": "tool_use", "id": f"toolu_{len(self.requests)}", "name": reply["tool"],
                            "input": reply["input"]}]
                reason = "tool_use"
            else:
                content, reason = [{"type": "text", "text": reply}], "end_turn"
        else:
            message = json.loads(body["messages"][-1]["content"])
            content = [{"type": "text", "text": json.dumps(self._answers[task](message), ensure_ascii=False)}]
            reason = "end_turn"
        self._send(handler, 200, {
            "id": f"msg_{len(self.requests)}", "type": "message", "role": "assistant", "model": body["model"],
            "content": content, "stop_reason": stop or reason, "stop_sequence": None,
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
