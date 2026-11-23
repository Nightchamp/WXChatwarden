#!/usr/bin/env python3
"""v3.205: slow paths no longer hold up the rest of the chat.

- SlowAck sends one "still looking" notice when a model call runs long, never
  after the call finished, and always before the real reply.
- Group voice is transcribed off the GroupWorker thread: other senders flow
  on while it runs, and the sender's own later messages keep their order.
"""
import os
import threading
import time

from chatwarden import core, slow_ack
from chatwarden import worker as worker_mod


def check_slow_ack():
    sent = []
    ack = slow_ack.SlowAck(0.05, lambda: sent.append("ack")).start()
    time.sleep(0.2)
    assert ack.finish() is True and sent == ["ack"], sent
    print("V3205_SLOW_ACK_FIRES_WHEN_SLOW=PASS")

    sent.clear()
    ack = slow_ack.SlowAck(0.3, lambda: sent.append("ack")).start()
    assert ack.finish() is False
    time.sleep(0.4)
    assert sent == [], sent
    print("V3205_SLOW_ACK_SILENT_WHEN_FAST=PASS")

    # finish() waits for a notice already being sent, so the real reply can
    # never overtake it.
    order = []
    entered = threading.Event()

    def slow_send():
        entered.set()
        time.sleep(0.2)
        order.append("ack")

    ack = slow_ack.SlowAck(0.01, slow_send).start()
    assert entered.wait(1.0)
    ack.finish()
    order.append("reply")
    assert order == ["ack", "reply"], order
    print("V3205_SLOW_ACK_BEFORE_REPLY=PASS")

    assert slow_ack.SlowAck(0, lambda: sent.append("x")).start().finish() is False
    for raw, expected in (("", 20.0), ("0", 0.0), ("-3", 0.0), ("7.5", 7.5), ("bad", 20.0)):
        os.environ["CW_VISION_SLOW_ACK_SEC"] = raw
        assert slow_ack.vision_delay() == expected, (raw, slow_ack.vision_delay())
    os.environ.pop("CW_VISION_SLOW_ACK_SEC", None)
    assert slow_ack.vision_text() in slow_ack.VISION_ACK_TEXTS
    print("V3205_SLOW_ACK_SETTINGS=PASS")


class FakeVoice:
    def decode_event(self, event):
        return f"/synthetic/voice-{event['local_id']}.wav"

    def cleanup(self, path):
        pass


def make_worker():
    group_cfg = {"name": "synthetic-voice-async", "session_key": "s", "personality": ""}
    worker = worker_mod.GroupWorker(
        {"group_cfg": group_cfg, "chat_name": group_cfg["name"]},
        None, {}, "wxid_demo_bot", "wxid_demo_owner",
    )
    worker.voice_pipeline = FakeVoice()
    return worker


def voice_event(local_id, sender):
    return {
        "local_type": 34, "local_id": local_id, "server_id": local_id,
        "timestamp": 0, "sender_username": sender, "sender_display": sender,
        "mentioned_wxids": [], "raw_content": "<msg><voicemsg /></msg>",
        "text": "[语音]",
    }


def text_event(local_id, sender, text):
    return {
        "local_type": 1, "local_id": local_id, "server_id": local_id,
        "timestamp": 0, "sender_username": sender, "sender_display": sender,
        "mentioned_wxids": [], "text": text, "msgsource": "",
    }


def check_voice_async():
    worker = make_worker()
    release = threading.Event()
    started = threading.Event()
    seen = []
    saved = []
    originals = (core.call_voice_transcript, core.save_message, core.freeze_llm_context_bounded)

    def blocking_transcript(path):
        started.set()
        release.wait(timeout=3.0)
        return {"speech_detected": True, "transcript": "合成测试语音内容"}

    try:
        core.call_voice_transcript = blocking_transcript
        core.save_message = lambda chat, sender, text: saved.append((sender, text))
        core.freeze_llm_context_bounded = lambda *a, **k: {}
        worker._handle_text = lambda ev: seen.append(("text", ev.get("local_id"), ev.get("text")))
        original_handle = worker._handle
        worker._handle = lambda ev: seen.append(("handle", ev.get("local_id"), ev.get("text")))

        t0 = time.monotonic()
        worker._handle_voice(voice_event(400, "wxid_demo_alice"))
        assert time.monotonic() - t0 < 0.5, "voice handling blocked the caller"
        assert started.wait(1.0), "transcription did not start"

        # Another sender is not held up; the same sender is deferred.
        assert worker._v335_defer_if_blocked(text_event(401, "wxid_demo_bob", "别人的消息")) is False
        assert worker._v335_defer_if_blocked(text_event(402, "wxid_demo_alice", "我刚才说的")) is True
        assert seen == [], seen
        print("V3205_VOICE_ASYNC_DOES_NOT_BLOCK_GROUP=PASS")

        release.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(seen) < 2:
            time.sleep(0.01)
        assert seen == [
            ("text", 400, "合成测试语音内容"),
            ("handle", 402, "我刚才说的"),
        ], seen
        assert ("dbwxid:wxid_demo_alice", "[语音] 合成测试语音内容") in saved, saved
        with worker._reply_sender_lock:
            assert "wxid_demo_alice" not in worker._reply_blocked_senders
            assert not worker._reply_sender_jobs.get("wxid_demo_alice")
        print("V3205_VOICE_ASYNC_KEEPS_SENDER_ORDER=PASS")

        # Inline fallback when switched off.
        seen.clear()
        os.environ["CW_VOICE_ASYNC"] = "0"
        worker._handle_voice(voice_event(403, "wxid_demo_carol"))
        assert seen == [("text", 403, "合成测试语音内容")], seen
        print("V3205_VOICE_ASYNC_OFF_IS_INLINE=PASS")
        worker._handle = original_handle
    finally:
        os.environ.pop("CW_VOICE_ASYNC", None)
        release.set()
        core.call_voice_transcript, core.save_message, core.freeze_llm_context_bounded = originals
        worker.stop()
        worker.thread.join(timeout=2.0)


def check_voice_failure_releases_sender():
    worker = make_worker()
    saved = []
    originals = (core.call_voice_transcript, core.save_message)

    def failing(path):
        raise RuntimeError("synthetic failure")

    try:
        core.call_voice_transcript = failing
        core.save_message = lambda chat, sender, text: saved.append((sender, text))
        worker._handle_voice(voice_event(500, "wxid_demo_dave"))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with worker._reply_sender_lock:
                if "wxid_demo_dave" not in worker._reply_blocked_senders:
                    break
            time.sleep(0.01)
        with worker._reply_sender_lock:
            assert "wxid_demo_dave" not in worker._reply_blocked_senders
        assert ("dbwxid:wxid_demo_dave", "[语音]") in saved, saved
        print("V3205_VOICE_ASYNC_FAILURE_RELEASES_SENDER=PASS")
    finally:
        core.call_voice_transcript, core.save_message = originals
        worker.stop()
        worker.thread.join(timeout=2.0)


def check_group_vision_ack():
    from chatwarden.worker import GroupWorker, FormalReplyWork

    cfg = {"name": "synthetic-vision-ack", "session_key": "s", "personality": ""}
    worker = GroupWorker.__new__(GroupWorker)
    worker.chat_name = cfg["name"]
    worker.stop_event = threading.Event()
    worker._reply_state_lock = threading.RLock()
    worker._reply_generation = 0
    worker._v392_send_lock = threading.Lock()
    worker._v39_note_bot_sent = lambda: None
    worker._v380_provider_question = lambda event: event["text"]
    worker._v396_upgrade_vision_path = lambda work, path: path

    def run(delay, provider_seconds):
        sends = []
        event = {"local_id": 9, "timestamp": 0, "text": "看图", "_v36_trigger_kind": "explicit"}
        work = FormalReplyWork(
            generation=0, event=event, image_path="/synthetic.png", provider_prompt="看图",
            sender="wxid_demo_alice", display="Alice", legacy_sender="other", group_cfg=cfg,
            profiles={}, context_snapshot={}, queued_at=time.monotonic(),
        )

        def provider(*args, **kwargs):
            time.sleep(provider_seconds)
            raise core.VisionGenerationError("synthetic")

        originals = (core.call_formal_reply, core.focus_and_send, core.save_message)
        os.environ["CW_VISION_SLOW_ACK_SEC"] = str(delay)
        try:
            core.call_formal_reply = provider
            core.focus_and_send = lambda chat, text: sends.append(text) or 0.01
            core.save_message = lambda *a, **k: None
            worker._run_reply_job(work)
        finally:
            os.environ.pop("CW_VISION_SLOW_ACK_SEC", None)
            core.call_formal_reply, core.focus_and_send, core.save_message = originals
        time.sleep(0.1)
        return sends

    sends = run(0.05, 0.3)
    assert len(sends) == 2, sends
    assert any(text in sends[0] for text in slow_ack.VISION_ACK_TEXTS), sends
    assert "识图" in sends[1], sends
    sends = run(0.5, 0.0)
    assert len(sends) == 1 and "识图" in sends[0], sends
    print("V3205_GROUP_VISION_SLOW_ACK=PASS")


def main():
    check_slow_ack()
    check_group_vision_ack()
    check_voice_async()
    check_voice_failure_releases_sender()
    print("V3205_ASYNC_SLOW_PATHS_SMOKE=PASS")


if __name__ == "__main__":
    main()
