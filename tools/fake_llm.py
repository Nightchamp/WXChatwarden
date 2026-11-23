#!/usr/bin/env python3
"""A stand-in OpenAI-compatible chat server for demos and tests.

It never calls a model. Plain requests get a short canned reply that quotes
the end of the prompt; JSON requests get the most conservative valid object
for the schema described in the system message.

    python3 tools/fake_llm.py [port]     # default 8650
"""
from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_FIELD = re.compile(r'^\s+"(?P<key>[a-z_]+)": (?P<detail>.+)$', re.M)


def _text_of(content):
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def conservative_object(system_text):
    result = {}
    for match in _FIELD.finditer(system_text):
        key, detail = match.group("key"), match.group("detail")
        if detail.startswith("true or false"):
            result[key] = False
        elif detail.startswith("one of "):
            result[key] = json.loads(detail[len("one of "):].split(", ")[0])
        elif "not empty" in detail:
            result[key] = "placeholder"
        else:
            result[key] = ""
    if result.get("decision") == "SILENT":
        result["reason"] = "fake model stays silent"
    return result


def answer(body):
    messages = body.get("messages") or []
    system = next((_text_of(m.get("content")) for m in messages if m.get("role") == "system"), "")
    last_user = next((_text_of(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
    if (body.get("response_format") or {}).get("type") == "json_object":
        return json.dumps(conservative_object(system), ensure_ascii=False)
    tail = " ".join(last_user.split())[-60:]
    return f"[fake reply] {tail}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        return

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        payload = json.dumps({
            "id": "fake", "object": "chat.completion", "model": body.get("model", "fake"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": answer(body)}}],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(port=8650, host="127.0.0.1"):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8650
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    print(f"fake LLM listening on {host}:{port}", flush=True)
    serve(port, host).serve_forever()
