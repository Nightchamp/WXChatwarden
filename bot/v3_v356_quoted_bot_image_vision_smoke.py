#!/usr/bin/env python3
from __future__ import annotations

"""Regression smoke for quoted bot-sent Type3 images entering Vision.

No real provider call and no WeChat send occur.
"""

import sys
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden import core
from chatwarden.image_pipeline import ImageResourceIncompleteError
from chatwarden.worker import FormalReplyWork, GroupWorker, ImageRef


def _quoted_bot_image_event(bot_wxid: str) -> dict:
    return {
        "local_id": 356,
        "sender_username": "wxid_fixture_user",
        "sender_display": "fixture-user",
        "text": "你解释一下为什么他是这个照片形象\n  ↳ 回复 阿卫: 0 0 0 0 0",
        "mentioned_wxids": [],
        "raw_content": f'''wxid_fixture_user:
<?xml version="1.0"?>
<msg>
  <appmsg>
    <title>你解释一下为什么他是这个照片形象</title>
    <type>57</type>
    <refermsg>
      <type>3</type>
      <svrid>5821486847483349393</svrid>
      <fromusr>fixture@chatroom</fromusr>
      <chatusr>{bot_wxid}</chatusr>
      <displayname>阿卫</displayname>
      <content>0 0 0 0 0</content>
    </refermsg>
  </appmsg>
</msg>''',
    }


def main() -> None:
    worker = GroupWorker.__new__(GroupWorker)
    worker.bot_wxid = "wxid_fixture_bot"
    worker.chat_name = "v356-quoted-bot-image"
    worker.group_cfg = {"name": worker.chat_name}
    worker.profiles = {}
    worker.pending_image_edit = {}
    worker.pending = {}
    worker.pending_wait = 8.0
    worker.recent_text = {}
    worker.recent_app = {}
    worker.recent = {}

    worker._remember_adjacent_text = lambda event: None
    worker._v39_note_ordinary_human = lambda event: None
    worker._cleanup_image_ref = lambda ref: None

    answered = []
    worker._answer = lambda event, image_ref=None: answered.append(
        (
            event["local_id"],
            event.get("_v36_trigger_kind"),
            int((event.get("_v350_quoted_bot_meta") or {}).get("refer_type") or 0),
        )
    )

    original_gate = core.call_followup_gate
    core.call_followup_gate = lambda *args, **kwargs: True
    try:
        event = _quoted_bot_image_event(worker.bot_wxid)
        worker._handle_text(event)
    finally:
        core.call_followup_gate = original_gate

    assert answered == [(356, "quote", 3)], (
        "quoted-bot Type3 image questions must bypass NEXT-image waiting "
        "and enter _answer immediately"
    )
    assert worker.pending == {}, (
        "quoted-bot Type3 image questions must not create a NEXT-image pending trigger"
    )
    assert event.get("_v350_quote_gate_passed") is True

    # v3.90 regression: an exact quoted Type3 image must beat unrelated
    # same-sender recent media. Real incident 19104/19105/19107 had a quoted
    # image from another member, then the requester sent a sticker, then
    # explicitly asked the bot to analyse the quoted image. The stale sticker
    # must never be passed as the current image and suppress exact quote
    # resolution inside _answer().
    precedence_worker = GroupWorker.__new__(GroupWorker)
    precedence_worker.bot_wxid = "wxid_fixture_bot"
    precedence_worker.chat_name = "v390-quoted-image-precedence"
    precedence_worker.group_cfg = {"name": precedence_worker.chat_name}
    precedence_worker.profiles = {}
    precedence_worker.pending_image_edit = {}
    precedence_worker.pending = {}
    precedence_worker.pending_wait = 8.0
    precedence_worker.recent_text = {}
    precedence_worker.recent_app = {}
    precedence_worker.image_max_age = 30.0
    precedence_worker.recent = {
        "wxid_fixture_requester": ImageRef(
            sender="wxid_fixture_requester",
            path="/tmp/unrelated-recent-sticker.webp",
            error=None,
            local_id=19105,
            created=__import__("time").monotonic(),
        )
    }
    precedence_worker._v313_true_mention = lambda event: True
    precedence_worker._v37_quote_metadata = lambda event: (
        {
            "svrid": 4162242322884808434,
            "refer_type": 3,
            "chatusr": "wxid_fixture_image_sender",
        },
        None,
    )
    precedence_worker._cleanup_image_ref = lambda ref: None
    precedence_worker._remember_adjacent_text = lambda event: None
    precedence_worker._v39_note_ordinary_human = lambda event: None
    precedence_answers = []
    precedence_worker._answer = lambda event, image_ref=None: precedence_answers.append(
        (event["local_id"], None if image_ref is None else image_ref.local_id)
    )

    precedence_event = {
        "local_id": 19107,
        "sender_username": "wxid_fixture_requester",
        "sender_display": "戒",
        "text": (
            "@喵の神-4i沙狩 解析一下笑点\n"
            "  ↳ 回复 葛工: opaque-image-preview"
        ),
        "raw_content": '''wxid_fixture_requester:
<msg><appmsg><title>@喵の神-4i沙狩 解析一下笑点</title><type>57</type>
<refermsg><type>3</type><svrid>4162242322884808434</svrid>
<fromusr>fixture@chatroom</fromusr><chatusr>wxid_fixture_image_sender</chatusr>
<displayname>葛工</displayname><content>opaque-image-preview</content></refermsg>
</appmsg></msg>''',
        "mentioned_wxids": ["wxid_fixture_bot"],
        "bot_display_name": "喵の神-4i沙狩",
    }
    precedence_worker._handle_text(precedence_event)
    assert precedence_answers == [(19107, None)], precedence_answers
    print("V390_QUOTED_IMAGE_EXACT_REFERENCE_PRECEDENCE=PASS")

    prompt_worker = GroupWorker.__new__(GroupWorker)
    prompt_worker.chat_name = "v380-quoted-media-prompt"
    prompt_worker.group_cfg = {"name": prompt_worker.chat_name}
    prompt_worker.profiles = {}
    prompt_worker._legacy_sender = lambda sender, display: sender or display
    prompt_worker._v315_followup_prompt = lambda event, prompt: prompt
    prompt_worker._cleanup_image_ref = lambda ref: None
    prompt_worker._reply_send_many_guarded = lambda *args, **kwargs: 0.01
    prompt_worker._followup_after_success = lambda event: None

    opaque_preview = (
        "eyJwaGFzaCI6IjEwMTAxMDEwMTAxMDEwMTAiLCJwZHFIYXNoIjoi"
        "ZGVlYTEwMGI5MGViZGVmYWZlZmE1ZmZjMWYzNDRhMTQ0MjE2NDIxNzQzOTc0"
    )
    prompt_event = {
        "local_id": 440,
        "sender_username": "wxid_fixture_user",
        "sender_display": "Owner",
        "text": (
            "@Owner-flash-JARVIS\n"
            f"  ↳ 回复 Owner: {opaque_preview}&#x0A;wxf14070..."
        ),
        "raw_content": f'''wxid_fixture_user:
<msg>
  <appmsg>
    <title>@Owner-flash-JARVIS</title>
    <type>57</type>
    <refermsg>
      <type>3</type>
      <svrid>1937417219989854190</svrid>
      <fromusr>fixture@chatroom</fromusr>
      <chatusr>wxid_fixture_image_sender</chatusr>
      <displayname>Owner</displayname>
      <content>{opaque_preview}</content>
    </refermsg>
  </appmsg>
</msg>''',
        "timestamp": 0,
        "_v36_trigger_kind": "explicit",
        "_v37_adjacent_quote_context": (
            "【DB-native 引用历史上下文】\n"
            "[被引用消息 | Owner] [图片]"
        ),
    }
    provider_calls = []
    original_call_hermes = core.call_formal_reply
    original_save_message = core.save_message

    def fake_call_hermes(group_cfg, prompt, sender, profiles, **kwargs):
        provider_calls.append((prompt, kwargs.get("raw_question")))
        return "已识别图片"

    core.call_formal_reply = fake_call_hermes
    core.save_message = lambda *args, **kwargs: None
    try:
        prompt_worker._answer(
            prompt_event,
            image_ref=ImageRef(
                sender="wxid_fixture_user",
                path="/tmp/v380-quoted-media.jpg",
                error=None,
                local_id=435,
                created=0.0,
            ),
        )
    finally:
        core.call_formal_reply = original_call_hermes
        core.save_message = original_save_message

    assert len(provider_calls) == 1, provider_calls
    provider_prompt, raw_question = provider_calls[0]
    assert "@Owner-flash-JARVIS" in provider_prompt, provider_prompt
    assert "[被引用消息 | Owner] [图片]" in provider_prompt, provider_prompt
    assert opaque_preview not in provider_prompt, provider_prompt
    assert "&#x0A;" not in provider_prompt, provider_prompt
    assert raw_question == "@Owner-flash-JARVIS", raw_question
    assert (
        prompt_worker._v396_last_vision["wxid_fixture_user"]["local_id"]
        == 435
    ), prompt_worker._v396_last_vision

    print("V356_QUOTED_BOT_IMAGE_VISION=PASS")
    print("V380_QUOTED_MEDIA_PROMPT_SANITIZED=PASS")

    # v3.96 regression: after a successful Vision reply, a same-sender
    # correction such as "你看图了吗" inside the semantic follow-up lease must
    # reattach the exact previous image instead of degrading to text-only.
    correction_worker = GroupWorker.__new__(GroupWorker)
    correction_worker.bot_wxid = "wxid_fixture_bot"
    correction_worker.chat_name = "v396-vision-correction"
    correction_worker.group_cfg = {"name": correction_worker.chat_name}
    correction_worker.profiles = {}
    correction_worker.pending_image_edit = {}
    correction_worker.pending = {}
    correction_worker.pending_wait = 8.0
    correction_worker.recent_text = {}
    correction_worker.recent_app = {}
    correction_worker.recent = {}
    correction_worker.image_max_age = 120.0
    correction_worker.followup_ttl = 180.0
    correction_worker.followups = {
        "wxid_fixture_user": {"last_success": time.monotonic()},
    }
    correction_worker.stop_event = threading.Event()
    correction_worker.q = queue.Queue()
    correction_worker._followup_gate_queue = queue.Queue(maxsize=1)
    correction_worker._followup_gate_slot = threading.BoundedSemaphore(1)
    correction_worker._followup_gate_state_lock = threading.RLock()
    correction_worker._followup_gate_generation = {}
    correction_worker._followup_gate_thread = threading.Thread(
        target=correction_worker._v3144_followup_gate_run,
        daemon=True,
    )
    correction_worker._followup_gate_thread.start()
    correction_worker._v396_last_vision = {
        "wxid_fixture_user": {
            "local_id": 746,
            "last_success": time.monotonic(),
        }
    }
    image_event = {
        "local_id": 746,
        "server_id": 2953893945338103023,
        "local_type": 3,
        "msg_type": "图片",
        "timestamp": 1,
        "sender_username": "wxid_fixture_user",
        "sender_display": "fixture-user",
        "chat_name": correction_worker.chat_name,
        "chat_username": "fixture@chatroom",
        "text": "[图片]",
    }
    fake_db = SimpleNamespace(
        event_by_local_id=lambda *args, **kwargs: dict(image_event),
    )
    correction_worker.image_pipeline = SimpleNamespace(
        db=fake_db,
        decode_event=lambda event, **kwargs: "/tmp/v396-inherited.jpg",
    )
    correction_worker._v313_true_mention = lambda event: False
    correction_worker._v343_current_instruction_text = lambda event: event["text"]
    correction_worker._v37_quote_metadata = lambda event: (None, None)
    correction_worker._event_quotes_bot = lambda event: False
    correction_worker._remember_adjacent_text = lambda event: None
    correction_worker._v39_note_ordinary_human = lambda event: None
    correction_worker._cleanup_image_ref = lambda ref: None
    correction_answers = []
    correction_worker._answer = lambda event, image_ref=None: correction_answers.append(
        (
            event["local_id"],
            None if image_ref is None else image_ref.local_id,
        )
    )

    def run_followup(event):
        correction_worker._handle_text(event)
        control = correction_worker.q.get(timeout=1.0)
        assert control.get("_v3144_control") == "followup_gate_result", control
        correction_worker._v3144_resume_followup_gate(control)

    correction_event = {
        "local_id": 750,
        "sender_username": "wxid_fixture_user",
        "sender_display": "fixture-user",
        "text": "你认真点，看图了吗",
        "raw_content": "",
        "mentioned_wxids": [],
        "bot_display_name": "fixture-bot",
    }
    original_gate = core.call_followup_gate
    core.call_followup_gate = lambda *args, **kwargs: True
    try:
        run_followup(correction_event)
    finally:
        core.call_followup_gate = original_gate

    assert correction_answers == [(750, 746)], correction_answers
    print("V396_VISION_CORRECTION_REUSES_LAST_IMAGE=PASS")

    continuation_event = {
        "local_id": 751,
        "sender_username": "wxid_fixture_user",
        "sender_display": "fixture-user",
        "text": "他这个图片镜像反转了，他在哪个地铁站？",
        "raw_content": "",
        "mentioned_wxids": [],
        "bot_display_name": "fixture-bot",
    }
    short_deictic_event = {
        "local_id": 752,
        "sender_username": "wxid_fixture_user",
        "sender_display": "fixture-user",
        "text": "他在哪？",
        "raw_content": "",
        "mentioned_wxids": [],
        "bot_display_name": "fixture-bot",
    }
    core.call_followup_gate = lambda *args, **kwargs: True
    try:
        run_followup(continuation_event)
        run_followup(short_deictic_event)
    finally:
        core.call_followup_gate = original_gate

    assert correction_answers[-2:] == [(751, 746), (752, 746)], correction_answers
    print("V396_VISION_CONTINUATION_REUSES_LAST_IMAGE=PASS")

    # v3.158 regression from 小明同学: after Vision answered an exact image,
    # the user quoted that bot Vision reply and asked "这图里的角色是谁".  The
    # old route matched generic image wording first, opened NEXT-image wait,
    # and lost the already-proven visual context.  An exact quote of the bot's
    # own prior Vision reply must inherit that image before NEXT-image routing.
    correction_worker._v396_last_vision = {
        "wxid_fixture_user": {
            "local_id": 746,
            "last_seen": time.monotonic(),
            "reply_text": "就这群小肥鱼，下来怕不是只能把我萌死。",
        }
    }
    quoted_vision_reply_event = {
        "local_id": 7521,
        "sender_username": "wxid_fixture_user",
        "sender_display": "fixture-user",
        "text": (
            "这图里的角色是谁\n"
            "  ↳ 回复 fixture-bot: 就这群小肥鱼，下来怕不是只能把我萌死。"
        ),
        "raw_content": (
            "<msg><appmsg><title>这图里的角色是谁</title>"
            "<refermsg><type>1</type><svrid>9001</svrid>"
            "<chatusr>wxid_fixture_bot</chatusr>"
            "<content>就这群小肥鱼，下来怕不是只能把我萌死。</content>"
            "</refermsg></appmsg></msg>"
        ),
        "mentioned_wxids": [],
        "bot_display_name": "fixture-bot",
    }
    correction_worker._event_quotes_bot = lambda event: True
    original_gate = core.call_followup_gate
    core.call_followup_gate = lambda *args, **kwargs: True
    try:
        correction_worker._handle_text(quoted_vision_reply_event)
    finally:
        core.call_followup_gate = original_gate
    assert correction_answers[-1] == (7521, 746), correction_answers
    assert "wxid_fixture_user" not in correction_worker.pending, (
        correction_worker.pending
    )
    print("V3158_QUOTED_VISION_REPLY_REUSES_EXACT_IMAGE=PASS")

    # A different bot text quote must not borrow the stale Vision image just
    # because its current wording mentions "这图".
    correction_worker._v396_last_vision = {
        "wxid_fixture_user": {
            "local_id": 746,
            "last_seen": time.monotonic(),
            "reply_text": "就这群小肥鱼，下来怕不是只能把我萌死。",
        }
    }
    unrelated_bot_quote_event = dict(
        quoted_vision_reply_event,
        local_id=7522,
        raw_content=(
            "<msg><appmsg><title>这图里的角色是谁</title>"
            "<refermsg><type>1</type><svrid>9002</svrid>"
            "<chatusr>wxid_fixture_bot</chatusr>"
            "<content>这是另一条与图片无关的机器人回复。</content>"
            "</refermsg></appmsg></msg>"
        ),
    )
    core.call_followup_gate = lambda *args, **kwargs: True
    try:
        correction_worker._handle_text(unrelated_bot_quote_event)
    finally:
        core.call_followup_gate = original_gate
    assert correction_answers[-1] == (7521, 746), correction_answers
    assert "wxid_fixture_user" in correction_worker.pending
    correction_worker.pending.clear()
    correction_worker._event_quotes_bot = lambda event: False
    print("V3158_UNRELATED_BOT_QUOTE_NO_VISION_REUSE=PASS")

    correction_worker._v396_last_vision = {}
    no_anchor_event = {
        "local_id": 753,
        "sender_username": "wxid_fixture_user",
        "sender_display": "fixture-user",
        "text": "他在哪？",
        "raw_content": "",
        "mentioned_wxids": [],
        "bot_display_name": "fixture-bot",
    }
    core.call_followup_gate = lambda *args, **kwargs: True
    try:
        run_followup(no_anchor_event)
    finally:
        core.call_followup_gate = original_gate
    assert correction_answers[-1] == (753, None), correction_answers
    print("V396_VISION_CONTINUATION_NO_ANCHOR_NO_REUSE=PASS")

    correction_worker._v396_last_vision = {
        "wxid_fixture_user": {
            "local_id": 746,
            "last_success": time.monotonic(),
        }
    }
    unrelated_event = {
        "local_id": 754,
        "sender_username": "wxid_fixture_user",
        "sender_display": "fixture-user",
        "text": "这个方案你觉得怎么样？",
        "raw_content": "",
        "mentioned_wxids": [],
        "bot_display_name": "fixture-bot",
    }
    core.call_followup_gate = lambda *args, **kwargs: True
    try:
        run_followup(unrelated_event)
    finally:
        core.call_followup_gate = original_gate
    assert correction_answers[-1] == (754, None), correction_answers
    print("V396_VISION_CONTINUATION_UNRELATED_TEXT_NO_REUSE=PASS")

    correction_worker.stop_event.set()
    try:
        correction_worker._followup_gate_queue.put_nowait(None)
    except queue.Full:
        pass
    correction_worker._followup_gate_thread.join(timeout=1.0)

    # If a Vision job reaches the quality gate with only a tiny thumbnail and
    # the original never arrives, provider invocation must be suppressed. The
    # bot should tell the user the resource is incomplete instead of letting
    # the model hallucinate from stale text context.
    quality_worker = GroupWorker.__new__(GroupWorker)
    quality_worker.chat_name = "v396-quality-fail-closed"
    quality_worker.image_pipeline = SimpleNamespace(
        vision_file_is_tiny=lambda path: True,
        db=SimpleNamespace(
            event_by_local_id=lambda *args, **kwargs: dict(image_event),
        ),
        decode_event=lambda *args, **kwargs: (_ for _ in ()).throw(
            ImageResourceIncompleteError("tiny thumbnail only")
        ),
    )
    quality_worker._reply_job_current = lambda generation: True
    quality_notices = []
    quality_followups = []
    quality_worker._reply_send_fixed_guarded = (
        lambda generation, event, text: quality_notices.append(text) or True
    )
    quality_worker._followup_after_success = (
        lambda event: quality_followups.append(event["local_id"])
    )
    provider_called = []
    original_call_hermes = core.call_formal_reply
    core.call_formal_reply = lambda *args, **kwargs: provider_called.append(True) or "bad"
    try:
        quality_worker._run_reply_job(
            FormalReplyWork(
                generation=0,
                event={
                    "local_id": 748,
                    "timestamp": 0,
                    "sender_username": "wxid_fixture_user",
                    "text": "@fixture-bot",
                },
                image_path="/tmp/v396-tiny.jpg",
                provider_prompt="请看图",
                sender="wxid_fixture_user",
                display="fixture-user",
                legacy_sender="fixture-user",
                group_cfg={"name": quality_worker.chat_name},
                profiles={},
                context_snapshot=None,
                queued_at=time.monotonic(),
                image_local_id=746,
            )
        )
    finally:
        core.call_formal_reply = original_call_hermes

    assert provider_called == [], provider_called
    assert quality_notices and "低清缩略图" in quality_notices[0], quality_notices
    assert quality_followups == [748], quality_followups
    assert (
        quality_worker._v396_last_vision["wxid_fixture_user"]["local_id"]
        == 746
    ), quality_worker._v396_last_vision
    print("V396_TINY_THUMBNAIL_PROVIDER_SUPPRESSED=PASS")

    stale_worker = GroupWorker.__new__(GroupWorker)
    stale_worker.chat_name = "v396-stale-quality-guard"
    stale_checks = []
    stale_worker.image_pipeline = SimpleNamespace(
        vision_file_is_tiny=lambda path: stale_checks.append(path) or True,
    )
    stale_worker._reply_job_current = lambda generation: False
    stale_worker._run_reply_job(
        FormalReplyWork(
            generation=9,
            event={
                "local_id": 752,
                "timestamp": 0,
                "sender_username": "wxid_fixture_user",
                "text": "你看图了吗",
            },
            image_path="/tmp/v396-stale-thumb.jpg",
            provider_prompt="请看图",
            sender="wxid_fixture_user",
            display="fixture-user",
            legacy_sender="fixture-user",
            group_cfg={"name": stale_worker.chat_name},
            profiles={},
            context_snapshot=None,
            queued_at=time.monotonic(),
            image_local_id=746,
        )
    )
    assert stale_checks == [], stale_checks
    print("V396_STALE_REPLY_SKIPS_IMAGE_QUALITY_WAIT=PASS")


if __name__ == "__main__":
    main()
