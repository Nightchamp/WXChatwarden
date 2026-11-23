#!/usr/bin/env python3

"""Regression smoke for the v3.15 follow-up decision seam.

The desired behavior is that an unmentioned follow-up message is classified
before the expensive normal reply lane runs.  A negative gate decision must
therefore skip core.call_formal_reply() entirely.
"""

import sys
import threading
import time
from types import SimpleNamespace

from chatwarden import core
from chatwarden import worker as worker_mod


def main():
    group_cfg = {
        "name": "synthetic-followup-test",
        "session_key": "synthetic-session",
        "personality": "",
    }
    worker = worker_mod.GroupWorker(
        {
            "group_cfg": group_cfg,
            "chat_name": group_cfg["name"],
        },
        None,
        {},
        "wxid_bot",
        "wxid_owner",
    )

    sender = "wxid_sender"
    worker.followups[sender] = {"last_success": time.monotonic()}
    def event(local_id, text):
        return {
            "local_id": local_id,
            "server_id": local_id,
            "timestamp": 0,
            "local_type": 1,
            "sender_username": sender,
            "sender_display": "Tester",
            "text": text,
            "msgsource": "",
        }

    calls = {"gate": 0, "answer": 0}
    answer_events = []
    original_gate = getattr(worker_mod.core, "call_followup_gate", None)
    original_answer = worker._answer

    gate_decision = {"value": False}

    def fake_gate(*args, **kwargs):
        calls["gate"] += 1
        return gate_decision["value"]

    def fake_answer(*args, **kwargs):
        calls["answer"] += 1
        if args and isinstance(args[0], dict):
            answer_events.append(args[0])

    try:
        worker_mod.core.call_followup_gate = fake_gate
        worker._answer = fake_answer

        # Negative decision: classification is async and the expensive answer
        # path must be skipped.
        negative = event(100, "这是发给群友的，不是在继续问机器人")
        worker.followups[sender] = {"last_success": time.monotonic()}
        worker._handle_text(negative)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and calls["gate"] < 1:
            time.sleep(0.01)
        time.sleep(0.05)
        assert calls == {"gate": 1, "answer": 0}, calls
        print("FOLLOWUP_LIGHT_GATE_NEGATIVE=PASS")

        # Positive decision: classify once, then enter the normal answer path.
        calls.update(gate=0, answer=0)
        gate_decision["value"] = True
        positive = event(101, "那你继续说")
        worker.followups[sender] = {"last_success": time.monotonic()}
        worker._handle_text(positive)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and calls["answer"] < 1:
            time.sleep(0.01)
        assert calls == {"gate": 1, "answer": 1}, calls
        assert answer_events[-1].get("_v324_followup_gate_passed") is True
        print("FOLLOWUP_LIGHT_GATE_POSITIVE=PASS")

        # A slow semantic gate must not block a newer explicit @ from the same
        # sender, and its stale YES must not emit a late second reply.
        calls.update(gate=0, answer=0)
        gate_started = threading.Event()
        release_gate = threading.Event()

        def slow_gate(*args, **kwargs):
            calls["gate"] += 1
            gate_started.set()
            release_gate.wait(timeout=1.0)
            return True

        worker_mod.core.call_followup_gate = slow_gate
        pending = event(103, "那这个呢")
        worker.followups[sender] = {"last_success": time.monotonic()}
        worker._handle_text(pending)
        assert gate_started.wait(timeout=1.0), "async gate did not start"

        explicit = event(104, "@fixture-bot 直接回答我")
        explicit["mentioned_wxids"] = ["wxid_bot"]
        explicit["bot_display_name"] = "fixture-bot"
        worker._handle_text(explicit)
        assert calls["answer"] == 1, calls
        release_gate.set()
        time.sleep(0.1)
        assert calls["answer"] == 1, "stale gate emitted a late reply"
        print("V3144_FOLLOWUP_ASYNC_EXPLICIT_OVERTAKE=PASS")

        # Existing recent-image follow-up behavior remains on the media path;
        # the new text-only gate must not pre-empt it.
        calls.update(gate=0, answer=0)
        gate_decision["value"] = False
        media = event(102, "这张呢")
        worker.followups[sender] = {"last_success": time.monotonic()}
        worker.recent[sender] = SimpleNamespace(
            created=time.monotonic(),
            local_id=99,
            path="/synthetic/not-read-by-test.jpg",
            error=None,
        )
        worker._handle_text(media)
        assert calls == {"gate": 0, "answer": 1}, calls
        assert media.get("_v324_followup_gate_passed") is not True
        print("FOLLOWUP_MEDIA_PATH_PRESERVED=PASS")
    finally:
        worker.stop()
        worker.thread.join(timeout=2.0)
        if original_gate is None:
            try:
                delattr(worker_mod.core, "call_followup_gate")
            except AttributeError:
                pass
        else:
            worker_mod.core.call_followup_gate = original_gate
        worker._answer = original_answer

    if "--real" not in sys.argv:
        return

    # Real network probe: synthetic context only, no WeChat send and no write
    # to the production chat-history database.
    original_context = core._followup_gate_context
    core._followup_gate_context = lambda *_args, **_kwargs: (
        "[机器人] 我刚才在解释缓存为什么会失效。\n"
        "[Tester] 明白了"
    )
    try:
        started = time.monotonic()
        yes = core.call_followup_gate(
            group_cfg,
            "那你继续说缓存的部分",
            sender,
            {},
        )
        yes_elapsed = time.monotonic() - started
        assert yes is True, yes
        print(f"FOLLOWUP_REAL_YES=PASS elapsed={yes_elapsed:.2f}s")

        started = time.monotonic()
        no = core.call_followup_gate(
            group_cfg,
            "@小王 周末去哪吃饭",
            sender,
            {},
        )
        no_elapsed = time.monotonic() - started
        assert no is False, no
        print(f"FOLLOWUP_REAL_NO=PASS elapsed={no_elapsed:.2f}s")
    finally:
        core._followup_gate_context = original_context


if __name__ == "__main__":
    main()
