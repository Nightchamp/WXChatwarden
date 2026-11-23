#!/usr/bin/env python3
from __future__ import annotations

import json
import queue
import tempfile
import threading
import time
from pathlib import Path

from chatwarden import core
from chatwarden.private_worker import PrivateWorker
from chatwarden.worker import GroupWorker


def session_restart_state_smoke():
    # The disposable bounded-turn cache now lives in Gateway, not Bot SQLite.
    # Gateway's protocol tests cover 1/2/3/recycle and fresh-process restoration.
    assert core.session_diet_contract()['session_turns'] == 3
    assert not hasattr(core, '_v35_session_cfg_for_call')
    assert not hasattr(core, 'STATE_DB_FOR_V35')
    assert core.llm_client._EPOCH
    print("V329_DISPOSABLE_SESSION_HAS_NO_BOT_DB_WRITER=PASS")


def recent_cache_commit_order_smoke():
    chat = "v329-cache-failure"
    original_open_db = core.open_db
    core._RECENT_CONTEXT_CACHE.pop(chat, None)

    def fail_open_db():
        raise RuntimeError("synthetic-db-failure")

    core.open_db = fail_open_db
    try:
        try:
            core.save_message(chat, "member:x", "hello")
        except RuntimeError as exc:
            assert str(exc) == "synthetic-db-failure"
        else:
            raise AssertionError("save_message unexpectedly succeeded")
        assert chat not in core._RECENT_CONTEXT_CACHE, (
            "failed DB write must not create a ghost recent-context row"
        )
    finally:
        core.open_db = original_open_db
        core._RECENT_CONTEXT_CACHE.pop(chat, None)
    print("V329_RECENT_CACHE_COMMIT_ORDER=PASS")


def recent_cache_slow_write_isolation_smoke():
    """A slow write in one chat must not freeze another chat's warm cache."""
    writer_chat = "v392-cache-lock-writer"
    reader_chat = "v392-cache-lock-reader"
    original_open_db = core.open_db
    entered_db = threading.Event()
    release_db = threading.Event()
    reader_done = threading.Event()
    writer_errors = []
    reader_result = []

    class SlowConnection:
        def __enter__(self):
            entered_db.set()
            release_db.wait(timeout=2.0)
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs):
            return self

    core.open_db = lambda: SlowConnection()
    with core._RECENT_CONTEXT_CACHE_LOCK:
        core._RECENT_CONTEXT_CACHE[reader_chat] = [
            ("other", "warm-context-row"),
        ]

    def writer():
        try:
            # `other` deliberately avoids the user/group-memory DB branches;
            # this fixture isolates only the save_message cache-lock scope.
            core.save_message(writer_chat, "other", "slow-write-row")
        except Exception as exc:  # pragma: no cover - surfaced below
            writer_errors.append(exc)

    def reader():
        reader_result.append(
            core.recent_context(reader_chat, {"name": reader_chat})
        )
        reader_done.set()

    writer_thread = threading.Thread(target=writer, daemon=True)
    reader_thread = threading.Thread(target=reader, daemon=True)
    try:
        writer_thread.start()
        assert entered_db.wait(timeout=1.0), "slow DB fixture did not start"
        reader_thread.start()
        assert reader_done.wait(timeout=0.25), (
            "a slow save_message DB transaction held the global recent-context "
            "cache lock and blocked an unrelated warm-cache read"
        )
    finally:
        release_db.set()
        writer_thread.join(timeout=2.0)
        reader_thread.join(timeout=2.0)
        core.open_db = original_open_db
        with core._RECENT_CONTEXT_CACHE_LOCK:
            core._RECENT_CONTEXT_CACHE.pop(writer_chat, None)
            core._RECENT_CONTEXT_CACHE.pop(reader_chat, None)

    assert not writer_errors, writer_errors
    assert reader_result and "warm-context-row" in reader_result[0], reader_result
    print("V392_RECENT_CACHE_SLOW_WRITE_ISOLATION=PASS")


def context_freeze_timeout_failsoft_smoke():
    """Pre-dispatch optional context must never block a reply indefinitely."""
    original_freeze = core.freeze_llm_context
    original_normal_timeout = getattr(
        core,
        "_CONTEXT_FREEZE_NORMAL_TIMEOUT_SEC",
        None,
    )
    original_history_timeout = getattr(
        core,
        "_CONTEXT_FREEZE_HISTORY_TIMEOUT_SEC",
        None,
    )
    release = threading.Event()

    def blocked_freeze(*_args, **_kwargs):
        release.wait(timeout=1.0)
        return {
            "context": "late-context",
            "style_bot_texts": ["late-style"],
            "current_sender": "late-sender",
            "memory_context": "late-memory",
            "history_context": "late-history",
        }

    core.freeze_llm_context = blocked_freeze
    try:
        assert hasattr(core, "freeze_llm_context_bounded"), (
            "pre-dispatch context freeze has no bounded fail-soft wrapper"
        )
        core._CONTEXT_FREEZE_NORMAL_TIMEOUT_SEC = 0.05
        core._CONTEXT_FREEZE_HISTORY_TIMEOUT_SEC = 0.20

        started = time.monotonic()
        snapshot = core.freeze_llm_context_bounded(
            {"name": "v392-freeze-timeout"},
            "wxid_sender",
            {},
            current_text="@bot 看看这张图",
        )
        elapsed = time.monotonic() - started
        assert elapsed < 0.20, elapsed
        assert snapshot["context"] == "", snapshot
        assert snapshot["memory_context"] == "", snapshot
        assert snapshot["history_context"] == "", snapshot
        assert snapshot["style_bot_texts"] == [], snapshot
    finally:
        release.set()
        core.freeze_llm_context = original_freeze
        if original_normal_timeout is not None:
            core._CONTEXT_FREEZE_NORMAL_TIMEOUT_SEC = original_normal_timeout
        if original_history_timeout is not None:
            core._CONTEXT_FREEZE_HISTORY_TIMEOUT_SEC = original_history_timeout

    print("V392_CONTEXT_FREEZE_TIMEOUT_FAILSOFT=PASS")


def followup_yes_fail_closed_smoke():
    original_context = core._followup_gate_context
    original_execute = core.call_llm_auxiliary
    calls = {"native": 0}

    def fake_execute(kind, prompt, **kwargs):
        assert kind == 'classifier'
        calls['native'] += 1
        return {'structured': {'addressed': False}}

    core._followup_gate_context = lambda *args, **kwargs: (
        "[机器人] 上一条是机器人回答。\n[小王] 另一段群聊。"
    )
    core.call_llm_auxiliary = fake_execute
    try:
        decision = core.call_followup_gate(
            {"name": "v329-followup"},
            "刚才小王让我继续给他发文件",
            "wxid_sender",
            {},
        )
        assert decision is False, decision
        assert calls['native'] == 1, calls
    finally:
        core._followup_gate_context = original_context
        core.call_llm_auxiliary = original_execute
    print("V329_FOLLOWUP_YES_FAIL_CLOSED=PASS")


def _reach_test_worker():
    worker = GroupWorker.__new__(GroupWorker)
    worker.chat_name = "v329-reach"
    worker.group_cfg = {"name": worker.chat_name}
    worker.profiles = {}
    worker.stop_event = threading.Event()
    worker.q = queue.Queue()
    worker._v392_send_lock = threading.Lock()
    worker._v39_last_bot_sent_mono = 0.0
    worker._followup_after_success = lambda event: None
    worker._reach_max_inflight = 2
    worker._reach_queue = queue.Queue(maxsize=2)
    worker._reach_slots = threading.BoundedSemaphore(2)
    worker._reach_state_lock = threading.Lock()
    worker._reach_generation = 0
    worker._reach_thread = threading.Thread(
        target=worker._reach_run,
        name="v329-reach-dispatcher",
        daemon=True,
    )
    worker._reach_thread.start()
    return worker


def reach_bounded_lifecycle_smoke():
    original_call = core.call_agent_reach
    original_send = core.focus_and_send
    original_save = core.save_message
    entered = threading.Event()
    release = threading.Event()
    sends = []

    def fake_call(*args, **kwargs):
        entered.set()
        release.wait(timeout=2.0)
        return "synthetic reach result"

    core.call_agent_reach = fake_call
    core.focus_and_send = lambda chat, text: sends.append((chat, text)) or 0.01
    core.save_message = lambda *args, **kwargs: None
    worker = _reach_test_worker()
    try:
        event = {"local_id": 1, "sender_username": "wxid_sender"}
        assert worker._start_agent_reach_async(event, "p1", "wxid_sender") is True
        assert entered.wait(timeout=1.0), "first Reach job did not start"
        assert worker._start_agent_reach_async(
            {"local_id": 2}, "p2", "wxid_sender"
        ) is True
        assert worker._start_agent_reach_async(
            {"local_id": 3}, "p3", "wxid_sender"
        ) is False

        worker.stop()
        release.set()
        worker._reach_thread.join(timeout=2.0)
        assert not worker._reach_thread.is_alive(), "Reach dispatcher did not stop"
        assert sends == [], "stopped worker must not send a late Reach result"
    finally:
        release.set()
        core.call_agent_reach = original_call
        core.focus_and_send = original_send
        core.save_message = original_save
    print("V329_REACH_BOUNDED_LIFECYCLE=PASS")


def imagegen_empty_marker_smoke():
    group = GroupWorker.__new__(GroupWorker)
    group.chat_name = "v329-imagegen-group"
    assert group._v322_start_imagegen(
        {"local_id": 1, "text": "@bot"}
    ) is False

    private = PrivateWorker.__new__(PrivateWorker)
    private.chat_name = "v329-imagegen-private"
    assert private._v322_start_imagegen(
        {"local_id": 2},
        "@bot",
    ) is False
    print("V329_IMAGEGEN_EMPTY_MARKER_GUARD=PASS")


def main():
    session_restart_state_smoke()
    recent_cache_commit_order_smoke()
    recent_cache_slow_write_isolation_smoke()
    context_freeze_timeout_failsoft_smoke()
    followup_yes_fail_closed_smoke()
    reach_bounded_lifecycle_smoke()
    imagegen_empty_marker_smoke()


if __name__ == "__main__":
    main()
