"""Gateway tests against the fake model server. Run: python3 -m unittest tests.test_gateway"""
from __future__ import annotations

import base64
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "tools"))

import fake_llm  # noqa: E402
import server as gateway  # noqa: E402

KEY = "k" * 40
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
WAV = base64.b64encode(b"RIFF0000WAVE" + b"0" * 32).decode()


class ScriptedBackend:
    """Returns queued raw replies, then falls back to a fixed one."""

    def __init__(self, replies, models=None):
        self.replies = list(replies)
        self.models = models or {"default": "m"}
        self.calls = []

    def model_for(self, tier):
        return self.models.get(tier) or self.models["default"]

    def complete(self, messages, tier, timeout, json_mode=False):
        self.calls.append({"messages": messages, "tier": tier, "json": json_mode})
        return self.replies.pop(0) if self.replies else "ok"


def request(kind, n, **extra):
    data = {"task_kind": kind, "request_id": f"t-{n}", "prompt": "hello", "explicit": True}
    data.update(extra)
    return data


class ManagerTests(unittest.TestCase):
    def test_text_reply_and_session_turns(self):
        backend = ScriptedBackend([])
        manager = gateway.Manager(backend, session_turns=3)
        turns = [
            manager.execute(request("formal", i, session_key="s1"))["session_turn"]
            for i in range(5)
        ]
        self.assertEqual(turns, [1, 2, 3, 1, 2])
        # the third call carried two earlier exchanges plus system and user
        self.assertEqual(len(backend.calls[2]["messages"]), 6)
        # the fourth call started a fresh conversation
        self.assertEqual(len(backend.calls[3]["messages"]), 2)

    def test_reusable_kind_needs_session(self):
        manager = gateway.Manager(ScriptedBackend([]))
        with self.assertRaises(ValueError):
            manager.execute(request("formal", 1))

    def test_duplicate_request_id(self):
        manager = gateway.Manager(ScriptedBackend([]))
        manager.execute(request("search", 1))
        with self.assertRaises(gateway.GatewayError) as caught:
            manager.execute(request("search", 1))
        self.assertEqual(caught.exception.kind, "duplicate_request")

    def test_structured_valid_first_time(self):
        backend = ScriptedBackend(['{"addressed": true}'])
        result = gateway.Manager(backend).execute(request("classifier", 1))
        self.assertEqual(result["structured"], {"addressed": True})
        self.assertEqual(result["text"], "")
        self.assertTrue(backend.calls[0]["json"])
        self.assertEqual(backend.calls[0]["tier"], "fast")

    def test_structured_retries_once_then_fails(self):
        backend = ScriptedBackend(["not json", '```json\n{"addressed": false}\n```'])
        result = gateway.Manager(backend).execute(request("classifier", 1))
        self.assertEqual(result["structured"], {"addressed": False})
        self.assertEqual(len(backend.calls), 2)

        backend = ScriptedBackend(["nope", '{"addressed": "yes"}'])
        with self.assertRaises(gateway.GatewayError) as caught:
            gateway.Manager(backend).execute(request("classifier", 2))
        self.assertEqual(caught.exception.kind, "invalid_structured_output")

    def test_structured_cross_field_rules(self):
        schema = gateway.SCHEMAS["proactive"]
        ok = {"decision": "SILENT", "draft": "", "reason": "quiet"}
        self.assertTrue(gateway.validate_structured(ok, schema))
        self.assertFalse(gateway.validate_structured({**ok, "draft": "hi"}, schema))
        self.assertFalse(gateway.validate_structured({**ok, "extra": 1}, schema))
        window = gateway.SCHEMAS["history_metadata"]
        self.assertFalse(gateway.validate_structured({"mode": "window", "start": "", "end": ""}, window))
        self.assertTrue(gateway.validate_structured({"mode": "none", "start": "", "end": ""}, window))
        voice = gateway.SCHEMAS["voice_transcript"]
        self.assertFalse(gateway.validate_structured({"speech_detected": True, "transcript": " "}, voice))

    def test_vision_needs_image_and_marks_it_viewed(self):
        manager = gateway.Manager(ScriptedBackend([], {"default": "m", "vision": "v"}))
        with self.assertRaises(ValueError):
            manager.execute(request("vision", 1))
        media = {"mime_type": "image/png", "data_base64": PNG}
        result = manager.execute(request("vision", 2, media=media))
        self.assertTrue(result["image_viewed"])
        self.assertEqual(result["model"], "v")
        parts = manager.backend.calls[0]["messages"][1]["content"]
        self.assertEqual(parts[1]["type"], "image_url")

    def test_voice_uses_audio_part(self):
        backend = ScriptedBackend(['{"speech_detected": true, "transcript": "hi"}'])
        media = {"mime_type": "audio/wav", "data_base64": WAV}
        result = gateway.Manager(backend).execute(request("voice_transcript", 1, media=media))
        self.assertTrue(result["audio_viewed"])
        self.assertEqual(backend.calls[0]["messages"][1]["content"][1]["type"], "input_audio")

    def test_media_rejected_where_not_expected(self):
        media = {"mime_type": "image/png", "data_base64": PNG}
        with self.assertRaises(ValueError):
            gateway.Manager(ScriptedBackend([])).execute(request("search", 1, media=media))

    def test_think_block_removed(self):
        backend = ScriptedBackend(["<think>plan</think>final"])
        result = gateway.Manager(backend).execute(request("search", 1))
        self.assertEqual(result["text"], "final")


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.llm = fake_llm.serve(0)
        threading.Thread(target=cls.llm.serve_forever, daemon=True).start()
        backend = gateway.ChatBackend(
            f"http://127.0.0.1:{cls.llm.server_address[1]}/v1", "unused", {"default": "fake"}
        )
        cls.manager = gateway.Manager(backend)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), gateway.handler_class(cls.manager, KEY))
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.llm.shutdown()

    def post(self, path, data, key=KEY):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(data).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_end_to_end_text(self):
        status, body = self.post("/v1/execute", request("search", "h1"))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"] and body["text"].startswith("[fake reply]"))
        self.assertIsNone(body["structured"])

    def test_end_to_end_every_structured_kind(self):
        for n, (kind, schema) in enumerate(gateway.SCHEMAS.items()):
            extra = {}
            if kind == "voice_transcript":
                extra["media"] = {"mime_type": "audio/wav", "data_base64": WAV}
            status, body = self.post("/v1/execute", request(kind, f"s{n}", **extra))
            self.assertEqual(status, 200, (kind, body))
            self.assertTrue(gateway.validate_structured(body["structured"], schema), kind)

    def test_auth_and_unknown_paths(self):
        status, body = self.post("/v1/execute", request("search", "h2"), key="wrong" * 8)
        self.assertEqual((status, body["error"]["kind"]), (401, "unauthorized"))
        status, body = self.post("/v1/owner-agent", {})
        self.assertEqual((status, body["error"]["kind"]), (404, "not_found"))
        status, body = self.post("/v1/execute", {"task_kind": "nope"})
        self.assertEqual((status, body["error"]["kind"]), (400, "invalid_request"))


if __name__ == "__main__":
    unittest.main()
