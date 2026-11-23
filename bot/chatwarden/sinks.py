"""Where outgoing messages go.

``CW_SINK`` picks the destination:

``console``       print each outgoing message (the default)
``jsonl``         append one JSON object per message to ``CW_SINK_JSONL``
``agent_wechat``  drive a desktop client through its accessibility tree; this
                  is the built-in path in ``core`` and needs that environment
``pkg.mod:Class`` any class with ``send_text``, ``send_image`` and ``send_file``

A sink method raises on failure. ``send_text`` and ``send_image`` return
nothing; ``send_file`` returns a short state word such as ``"done"``.
"""
from __future__ import annotations

import importlib
import json
import os
import threading
import time

BUILT_IN_UI = "agent_wechat"


class ConsoleSink:
    name = "console"

    def _emit(self, kind, chat, private, payload):
        scope = "private" if private else "group"
        print(f"[sink] {kind} -> {scope} {chat!r}: {payload}", flush=True)

    def send_text(self, chat, text, private=False):
        self._emit("text", chat, private, text)

    def send_image(self, chat, path, private=False):
        self._emit("image", chat, private, path)

    def send_file(self, chat, path, private=False):
        self._emit("file", chat, private, path)
        return "done"


class JsonlSink:
    name = "jsonl"

    def __init__(self, path=None):
        self.path = str(
            path
            or os.environ.get("CW_SINK_JSONL", "")
            or "outbox.jsonl"
        )
        self._lock = threading.Lock()

    def _append(self, kind, chat, private, field, value):
        record = {
            "ts": time.time(),
            "kind": kind,
            "chat": str(chat),
            "private": bool(private),
            field: str(value),
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def send_text(self, chat, text, private=False):
        self._append("text", chat, private, "text", text)

    def send_image(self, chat, path, private=False):
        self._append("image", chat, private, "path", path)

    def send_file(self, chat, path, private=False):
        self._append("file", chat, private, "path", path)
        return "done"


def _default_name():
    local_name = f"{__package__}.deploy_local"
    try:
        local = importlib.import_module(local_name)
    except ModuleNotFoundError as exc:
        if exc.name != local_name:
            raise
        return "console"
    return str(getattr(local, "SINK", "console"))


def load_sink():
    """Return the configured sink, or ``None`` for the built-in UI path."""
    name = str(os.environ.get("CW_SINK", "")).strip() or _default_name()
    if name == BUILT_IN_UI:
        return None
    if name == "console":
        return ConsoleSink()
    if name == "jsonl":
        return JsonlSink()
    module_name, _, class_name = name.partition(":")
    if not module_name or not class_name:
        raise RuntimeError(
            "CW_SINK must be console, jsonl, agent_wechat "
            "or 'package.module:ClassName'"
        )
    return getattr(importlib.import_module(module_name), class_name)()
