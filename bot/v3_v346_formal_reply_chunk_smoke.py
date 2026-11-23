#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden import core  # noqa: E402
from chatwarden.worker import GroupWorker  # noqa: E402


def main() -> None:
    original_execute = core.llm_client.execute_formal

    provider_text = (
        ("第一段：" + "甲" * 900)
        + "\n\n"
        + ("第二段：" + "乙" * 900)
        + "\n\n"
        + ("第三段：" + "丙" * 500)
    )

    def fake_execute(*args, **kwargs):
        return {'text': provider_text, 'request_id': 'chunk-fixture', 'session_turn': 1,
                'duration_ms': 1, 'tool_count': 0, 'web_used': False}

    core.llm_client.execute_formal = fake_execute
    try:
        reply = core.call_formal_reply(
            {
                "name": "v346-formal-reply",
                "session_key": "v346-session",
            },
            "请生成完整长回复",
            "wxid_fixture",
            profiles=None,
            context_snapshot={
                "context": "fixture",
                "current_sender": "uid:fixture",
                "memory_context": "",
            },
        )
    finally:
        core.llm_client.execute_formal = original_execute

    assert reply == provider_text, (
        "formal provider reply must not be clipped before send: "
        f"provider={len(provider_text)} output={len(reply)}"
    )

    assert hasattr(core, "FORMAL_REPLY_SEND_CHUNK_CHARS")
    assert core.FORMAL_REPLY_SEND_CHUNK_CHARS == 1600, (
        core.FORMAL_REPLY_SEND_CHUNK_CHARS
    )
    chunks = core.split_formal_reply(reply)
    assert len(chunks) >= 2, len(chunks)
    assert all(
        0 < len(chunk) <= core.FORMAL_REPLY_SEND_CHUNK_CHARS
        for chunk in chunks
    ), [len(chunk) for chunk in chunks]
    assert "".join(chunks).replace("\n\n", "") == reply.replace("\n\n", ""), (
        "formal chunking must preserve all non-separator text"
    )
    assert not any(chunk.endswith("…") for chunk in chunks), chunks[-1]

    original_group_send = core.focus_and_send
    original_private_send = core._private_focus_and_send_unlocked
    group_sent = []
    private_sent = []
    try:
        core.focus_and_send = lambda chat, text: (
            group_sent.append((chat, text)) or 0.1
        )
        core._private_focus_and_send_unlocked = lambda chat, text: (
            private_sent.append((chat, text)) or 0.2
        )
        worker = GroupWorker.__new__(GroupWorker)
        worker.chat_name = "fixture-group"
        worker._v392_send_lock = threading.RLock()
        worker._reply_state_lock = threading.RLock()
        worker.stop_event = threading.Event()
        worker._reply_generation = 7
        worker._v39_last_bot_sent_mono = 0.0
        group_seconds = worker._reply_send_many_guarded(
            7,
            ["g1", "g2", "g3"],
            local_id=346,
        )
        stale_result = worker._reply_send_many_guarded(
            6,
            ["stale"],
            local_id=347,
        )
        private_seconds = core.private_focus_and_send_many(
            "fixture-private",
            ["p1", "p2"],
        )
    finally:
        core.focus_and_send = original_group_send
        core._private_focus_and_send_unlocked = original_private_send

    assert group_sent == [
        ("fixture-group", "g1"),
        ("fixture-group", "g2"),
        ("fixture-group", "g3"),
    ], group_sent
    assert stale_result is None, stale_result
    assert private_sent == [
        ("fixture-private", "p1"),
        ("fixture-private", "p2"),
    ], private_sent
    assert abs(group_seconds - 0.3) < 1e-9, group_seconds
    assert abs(private_seconds - 0.4) < 1e-9, private_seconds

    print("V346_FORMAL_REPLY_CHUNKING=PASS")


if __name__ == "__main__":
    main()
