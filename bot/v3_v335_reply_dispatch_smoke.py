#!/usr/bin/env python3
from __future__ import annotations

import threading
import time

from chatwarden import core
from chatwarden.reply_dispatch import ReplyDispatcher, ReplyJob
from chatwarden.worker import GroupWorker, ImageRef


def _wait(event, label):
    assert event.wait(timeout=2.0), label


def lane_isolation_and_sender_order_smoke():
    dispatcher = ReplyDispatcher(
        name="v335-lanes",
        max_outstanding=4,
        lane_limits={"text": 1, "vision": 1},
    )
    vision_a_started = threading.Event()
    release_vision_a = threading.Event()
    text_a_started = threading.Event()
    text_b_started = threading.Event()
    text_a_done = threading.Event()
    text_b_done = threading.Event()

    def vision_a(is_current):
        assert is_current()
        vision_a_started.set()
        release_vision_a.wait(timeout=2.0)

    def text_a(is_current):
        assert is_current()
        text_a_started.set()
        text_a_done.set()

    def text_b(is_current):
        assert is_current()
        text_b_started.set()
        text_b_done.set()

    try:
        assert dispatcher.submit(
            ReplyJob("vision", "sender-a", 1, vision_a)
        )
        _wait(vision_a_started, "vision A did not start")

        # Same sender must wait for its older Vision request, but a different
        # sender must be able to overtake that blocked item on the text lane.
        assert dispatcher.submit(
            ReplyJob("text", "sender-a", 2, text_a)
        )
        assert dispatcher.submit(
            ReplyJob("text", "sender-b", 3, text_b)
        )
        _wait(text_b_started, "different-sender text did not overlap Vision")
        assert not text_a_started.is_set(), "same sender ran out of order"

        release_vision_a.set()
        _wait(text_a_done, "same-sender text did not resume after Vision")
        _wait(text_b_done, "different-sender text did not complete")
    finally:
        release_vision_a.set()
        dispatcher.stop()

    print("V335_LANE_ISOLATION_AND_SENDER_ORDER=PASS")


def bounded_and_stop_guard_smoke():
    dispatcher = ReplyDispatcher(
        name="v335-stop",
        max_outstanding=2,
        lane_limits={"text": 1, "vision": 1},
    )
    entered = threading.Event()
    release = threading.Event()
    active_cleanup = threading.Event()
    pending_cleanup = threading.Event()
    late_send = []
    pending_started = []

    def active(is_current):
        entered.set()
        release.wait(timeout=2.0)
        if is_current():
            late_send.append("sent")

    def pending(is_current):
        pending_started.append(True)

    assert dispatcher.submit(
        ReplyJob(
            "text",
            "sender-a",
            10,
            active,
            cleanup=active_cleanup.set,
        )
    )
    _wait(entered, "active job did not start")
    assert dispatcher.submit(
        ReplyJob(
            "text",
            "sender-a",
            11,
            pending,
            cleanup=pending_cleanup.set,
        )
    )

    # Active + pending already consume the full bounded outstanding budget.
    assert not dispatcher.submit(
        ReplyJob("vision", "sender-b", 12, lambda current: None)
    )

    dispatcher.stop()
    _wait(pending_cleanup, "stopped pending job was not cleaned up")
    assert pending_started == [], "stopped pending job unexpectedly started"

    release.set()
    _wait(active_cleanup, "active job cleanup did not run")
    assert late_send == [], "active job passed current guard after stop"

    print("V335_BOUNDED_AND_STOP_GUARD=PASS")


def vision_two_sender_parallel_smoke():
    dispatcher = ReplyDispatcher(
        name="v335-vision-two",
        max_outstanding=4,
        lane_limits={"text": 1, "vision": 2},
    )
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def first(is_current):
        assert is_current()
        first_started.set()
        release.wait(timeout=2.0)

    def second(is_current):
        assert is_current()
        second_started.set()
        release.wait(timeout=2.0)

    try:
        assert dispatcher.submit(ReplyJob("vision", "sender-a", 101, first))
        assert dispatcher.submit(ReplyJob("vision", "sender-b", 102, second))
        _wait(first_started, "first Vision did not start")
        _wait(second_started, "different-sender Vision did not run in parallel")
    finally:
        release.set()
        dispatcher.stop()

    print("V3143_VISION_TWO_SENDER_PARALLEL=PASS")


def lane_queue_budget_isolation_smoke():
    dispatcher = ReplyDispatcher(
        name="v335-lane-budgets",
        max_outstanding=3,
        lane_limits={"text": 1, "vision": 1},
        lane_max_outstanding={"text": 2, "vision": 1},
    )
    vision_started = threading.Event()
    release_vision = threading.Event()
    text_started = threading.Event()

    def vision(is_current):
        vision_started.set()
        release_vision.wait(timeout=2.0)

    try:
        assert dispatcher.submit(
            ReplyJob("vision", "vision-a", 13, vision)
        )
        _wait(vision_started, "budget Vision did not start")

        # Vision has exhausted its own outstanding budget, but must not consume
        # the text lane's reserved queue capacity.
        assert not dispatcher.submit(
            ReplyJob("vision", "vision-b", 14, lambda current: None)
        )
        assert dispatcher.submit(
            ReplyJob(
                "text",
                "text-a",
                15,
                lambda current: text_started.set(),
            )
        )
        _wait(text_started, "Vision budget blocked text submission")
    finally:
        release_vision.set()
        dispatcher.stop()

    print("V335_LANE_QUEUE_BUDGET_ISOLATION=PASS")


def group_worker_vision_isolation_smoke():
    worker = GroupWorker.__new__(GroupWorker)
    worker.chat_name = "v335-worker"
    worker.group_cfg = {"name": worker.chat_name}
    worker.profiles = {}
    worker.owner_wxid = "wxid_owner"
    worker.bot_wxid = "wxid_bot"
    worker.stop_event = threading.Event()
    worker.q = __import__("queue").Queue()
    worker._reply_state_lock = threading.RLock()
    worker._reply_generation = 0
    worker._reply_sender_lock = threading.RLock()
    worker._reply_sender_jobs = {}
    worker._reply_blocked_senders = set()
    worker._reply_deferred_events = {}
    worker._v392_send_lock = threading.Lock()
    worker._v39_last_bot_sent_mono = 0.0
    worker._legacy_sender = lambda sender, display: "other"
    worker._v37_prompt_for_event = lambda event: event["text"]
    worker._v315_followup_prompt = lambda event, prompt: prompt
    worker._v38_quote_image_ref = lambda event: None
    worker._v312_quote_emoticon_ref = lambda event: None
    worker._v323_start_image_edit = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("image edit must not be used in this smoke")
    )
    worker._v315_is_no_reply = lambda reply: False
    worker._followup_after_success = lambda event: None
    worker._start_agent_reach_async = lambda *args, **kwargs: False
    cleaned = []
    worker._cleanup_image_ref = lambda ref: cleaned.append(ref.path)
    worker._reply_dispatcher = ReplyDispatcher(
        name="v335-worker-dispatch",
        max_outstanding=4,
        lane_limits={"text": 1, "vision": 1},
    )

    vision_started = threading.Event()
    text_b_sent = threading.Event()
    text_a2_sent = threading.Event()
    release_vision = threading.Event()
    sends = []

    original_call = core.call_formal_reply
    original_send = core.focus_and_send
    original_save = core.save_message
    original_freeze = core.freeze_llm_context

    def fake_call(group_cfg, prompt, sender, profiles, image_path=None, **kwargs):
        assert kwargs.get("context_snapshot", {}).get("context") == "frozen", kwargs
        if image_path:
            vision_started.set()
            release_vision.wait(timeout=2.0)
            return "vision-a"
        if sender == "sender-b":
            return "text-b"
        return "text-a2"

    def fake_send(chat, text):
        sends.append((chat, text))
        if text.endswith("text-b"):
            text_b_sent.set()
        if text.endswith("text-a2"):
            text_a2_sent.set()
        return 0.01

    core.call_formal_reply = fake_call
    core.focus_and_send = fake_send
    core.save_message = lambda *args, **kwargs: None
    core.freeze_llm_context = lambda *args, **kwargs: {
        "context": "frozen",
        "current_sender": "fixture",
        "memory_context": "",
    }

    def event(local_id, sender, text):
        return {
            "local_id": local_id,
            "sender_username": sender,
            "sender_display": sender,
            "text": text,
            "timestamp": 0,
            "_v36_trigger_kind": "explicit",
        }

    try:
        GroupWorker._answer(
            worker,
            event(20, "sender-a", "这张图是什么"),
            image_ref=ImageRef(
                sender="sender-a",
                path="/tmp/v335-fixture.png",
                error=None,
                local_id=19,
                created=time.monotonic(),
            ),
        )
        _wait(vision_started, "GroupWorker Vision did not start")

        GroupWorker._answer(worker, event(21, "sender-b", "文本B"))
        GroupWorker._answer(worker, event(22, "sender-a", "文本A2"))

        _wait(text_b_sent, "different sender text was blocked by Vision")
        assert not text_a2_sent.is_set(), "same sender text overtook Vision"

        release_vision.set()
        _wait(text_a2_sent, "same sender text did not resume after Vision")
        deadline = time.monotonic() + 2.0
        while "/tmp/v335-fixture.png" not in cleaned and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cleaned == ["/tmp/v335-fixture.png"], cleaned
    finally:
        release_vision.set()
        worker.stop_event.set()
        with worker._reply_state_lock:
            worker._reply_generation += 1
        worker._reply_dispatcher.stop()
        core.call_formal_reply = original_call
        core.focus_and_send = original_send
        core.save_message = original_save
        core.freeze_llm_context = original_freeze

    assert any(text.endswith("text-b") for _, text in sends), sends
    assert any(text.endswith("vision-a") for _, text in sends), sends
    assert any(text.endswith("text-a2") for _, text in sends), sends
    print("V335_GROUP_WORKER_VISION_ISOLATION=PASS")


def group_worker_same_sender_deferral_smoke():
    import queue

    worker = GroupWorker.__new__(GroupWorker)
    worker.chat_name = "v335-sender-deferral"
    worker.group_cfg = {"name": worker.chat_name}
    worker.profiles = {}
    worker.bot_wxid = "wxid_bot"
    worker.stop_event = threading.Event()
    worker.q = queue.Queue()
    worker._reply_sender_lock = threading.RLock()
    worker._reply_sender_jobs = {}
    worker._reply_blocked_senders = set()
    worker._reply_deferred_events = {}
    worker._expire = lambda: None
    worker._followup_note_intervening = lambda sender: None
    worker._remember_adjacent_app = lambda event: None
    processed = []
    worker._handle_text = lambda event: processed.append(
        (event["sender_username"], event["local_id"])
    )

    original_save = core.save_message
    original_freeze = core.freeze_llm_context
    saved = []
    core.save_message = lambda chat, sender, text: saved.append(
        (chat, sender, text)
    )
    core.freeze_llm_context = lambda *args, **kwargs: {
        "context": "deferred-at-arrival",
        "current_sender": "fixture",
        "memory_context": "",
    }

    def event(local_id, sender):
        return {
            "local_id": local_id,
            "sender_username": sender,
            "sender_display": sender,
            "text": f"message-{local_id}",
            "timestamp": 0,
            "local_type": 1,
            "msg_type": "文本",
        }

    try:
        worker._v335_reserve_sender("sender-a")
        worker._handle(event(31, "sender-a"))
        worker._handle(event(32, "sender-b"))

        assert processed == [("sender-b", 32)], processed
        assert len(saved) == 1 and saved[0][1].endswith("sender-b"), saved
        deferred = worker._reply_deferred_events["sender-a"]
        assert [item["local_id"] for item in deferred] == [31], deferred
        assert (
            deferred[0]["_v335_context_snapshot"]["context"]
            == "deferred-at-arrival"
        )

        worker._v335_reply_job_finished("sender-a")
        marker = worker.q.get(timeout=1.0)
        assert marker == {
            "_v335_control": "resume_sender",
            "sender_key": "sender-a",
        }, marker
        worker._v335_resume_sender("sender-a")

        assert processed == [
            ("sender-b", 32),
            ("sender-a", 31),
        ], processed
        assert "sender-a" not in worker._reply_blocked_senders
        assert "sender-a" not in worker._reply_deferred_events
    finally:
        core.save_message = original_save
        core.freeze_llm_context = original_freeze

    print("V335_GROUP_WORKER_SAME_SENDER_DEFERRAL=PASS")


def group_worker_stop_late_send_guard_smoke():
    import queue

    worker = GroupWorker.__new__(GroupWorker)
    worker.chat_name = "v335-stop-guard"
    worker.group_cfg = {"name": worker.chat_name}
    worker.profiles = {}
    worker.owner_wxid = "wxid_owner"
    worker.bot_wxid = "wxid_bot"
    worker.stop_event = threading.Event()
    worker.q = queue.Queue()
    worker._reply_state_lock = threading.RLock()
    worker._reply_generation = 0
    worker._reply_sender_lock = threading.RLock()
    worker._reply_sender_jobs = {}
    worker._reply_blocked_senders = set()
    worker._reply_deferred_events = {}
    worker._reach_state_lock = threading.RLock()
    worker._reach_generation = 0
    worker._reach_queue = queue.Queue(maxsize=2)
    worker._reach_slots = threading.BoundedSemaphore(2)
    worker._v392_send_lock = threading.Lock()
    worker._v39_last_bot_sent_mono = 0.0
    worker._legacy_sender = lambda sender, display: "other"
    worker._v37_prompt_for_event = lambda event: event["text"]
    worker._v315_followup_prompt = lambda event, prompt: prompt
    worker._v38_quote_image_ref = lambda event: None
    worker._v312_quote_emoticon_ref = lambda event: None
    worker._v315_is_no_reply = lambda reply: False
    worker._followup_after_success = lambda event: None
    worker._start_agent_reach_async = lambda *args, **kwargs: False
    worker._reply_dispatcher = ReplyDispatcher(
        name="v335-worker-stop",
        max_outstanding=2,
        lane_limits={"text": 1, "vision": 1},
    )

    provider_started = threading.Event()
    release_provider = threading.Event()
    provider_returned = threading.Event()
    sends = []

    original_call = core.call_formal_reply
    original_send = core.focus_and_send
    original_save = core.save_message
    original_freeze = core.freeze_llm_context

    def fake_call(*args, **kwargs):
        provider_started.set()
        release_provider.wait(timeout=2.0)
        provider_returned.set()
        return "must-not-send"

    core.call_formal_reply = fake_call
    core.focus_and_send = lambda chat, text: sends.append((chat, text)) or 0.01
    core.save_message = lambda *args, **kwargs: None
    core.freeze_llm_context = lambda *args, **kwargs: {
        "context": "frozen",
        "current_sender": "fixture",
        "memory_context": "",
    }

    event = {
        "local_id": 41,
        "sender_username": "sender-a",
        "sender_display": "sender-a",
        "text": "slow text",
        "timestamp": 0,
        "_v36_trigger_kind": "explicit",
    }
    try:
        GroupWorker._answer(worker, event)
        _wait(provider_started, "provider did not start before stop")
        worker.stop()
        release_provider.set()
        _wait(provider_returned, "provider did not return after stop")

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with worker._reply_sender_lock:
                if not worker._reply_sender_jobs:
                    break
            time.sleep(0.01)
        assert sends == [], "stopped GroupWorker emitted a late reply"
    finally:
        release_provider.set()
        worker._reply_dispatcher.stop()
        core.call_formal_reply = original_call
        core.focus_and_send = original_send
        core.save_message = original_save
        core.freeze_llm_context = original_freeze

    print("V335_GROUP_WORKER_STOP_LATE_SEND_GUARD=PASS")


def main():
    lane_isolation_and_sender_order_smoke()
    bounded_and_stop_guard_smoke()
    vision_two_sender_parallel_smoke()
    lane_queue_budget_isolation_smoke()
    group_worker_vision_isolation_smoke()
    group_worker_same_sender_deferral_smoke()
    group_worker_stop_late_send_guard_smoke()


if __name__ == "__main__":
    main()
