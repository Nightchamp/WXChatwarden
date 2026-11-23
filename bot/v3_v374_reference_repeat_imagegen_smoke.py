#!/usr/bin/env python3
"""Focused regressions for exact-reference repeat image generation routing."""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from chatwarden import core
from chatwarden.private_worker import PrivateWorker
from chatwarden.worker import GroupWorker, ImageRef


PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
)


def _run_exact_reference_case(text: str) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        reference = Path(temp_dir) / "quoted.png"
        reference.write_bytes(PNG_1X1)

        worker = GroupWorker.__new__(GroupWorker)
        worker.chat_name = "__v374_exact_reference__"
        worker.group_cfg = {"name": worker.chat_name}
        worker.profiles = {}
        worker.owner_wxid = "wxid_owner"
        worker.bot_wxid = "wxid_bot"
        worker._legacy_sender = lambda sender, display: "me"
        worker._v37_prompt_for_event = lambda event: event["text"]
        worker._v315_followup_prompt = lambda event, prompt: prompt
        worker._v312_quote_emoticon_ref = lambda event: None
        worker._v38_quote_image_ref = lambda event: ImageRef(
            sender="wxid_fixture",
            path=str(reference),
            error=None,
            local_id=13732,
            created=time.monotonic(),
        )
        started = []
        worker._v323_start_image_edit = (
            lambda event, image_ref, reference_source="adjacent", instruction_override=None: started.append(
                (event["local_id"], image_ref.path, reference_source)
            )
        )
        worker._cleanup_image_ref = lambda ref: None

        event = {
            "local_id": 14182,
            "sender_username": "wxid_fixture",
            "sender_display": "Owner",
            "text": text,
            "timestamp": 0,
            "_v36_trigger_kind": "explicit",
        }

        original_call = core.call_formal_reply
        core.call_formal_reply = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("exact-reference repeat generation must bypass Vision/Hermes")
        )
        try:
            GroupWorker._answer(worker, event)
        finally:
            core.call_formal_reply = original_call

        assert started == [
            (14182, str(reference), "quoted")
        ], started


def _run_non_generation_case(text: str, *, exact_reference: bool) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        reference = Path(temp_dir) / "quoted.png"
        reference.write_bytes(PNG_1X1)

        worker = GroupWorker.__new__(GroupWorker)
        worker.chat_name = "__v374_non_generation__"
        worker.group_cfg = {"name": worker.chat_name}
        worker.profiles = {}
        worker.owner_wxid = "wxid_owner"
        worker.bot_wxid = "wxid_bot"
        worker._legacy_sender = lambda sender, display: "me"
        worker._v37_prompt_for_event = lambda event: event["text"]
        worker._v315_followup_prompt = lambda event, prompt: prompt
        worker._v312_quote_emoticon_ref = lambda event: None
        worker._v38_quote_image_ref = (
            (lambda event: ImageRef(
                sender="wxid_fixture",
                path=str(reference),
                error=None,
                local_id=13732,
                created=time.monotonic(),
            ))
            if exact_reference
            else (lambda event: None)
        )
        edit_started = []
        worker._v323_start_image_edit = lambda *args, **kwargs: edit_started.append(True)
        worker._cleanup_image_ref = lambda ref: None
        worker._v315_is_no_reply = lambda reply: False
        worker._followup_after_success = lambda event: None
        worker._v392_send_lock = threading.Lock()
        worker._v39_last_bot_sent_mono = 0.0

        event = {
            "local_id": 15000,
            "sender_username": "wxid_fixture",
            "sender_display": "Owner",
            "text": text,
            "timestamp": 0,
            "_v36_trigger_kind": "explicit",
        }

        provider_images = []
        original_call = core.call_formal_reply
        original_send = core.focus_and_send
        original_save = core.save_message
        core.call_formal_reply = lambda *args, **kwargs: (
            provider_images.append(kwargs.get("image_path")) or "普通回复"
        )
        core.focus_and_send = lambda *args, **kwargs: 0.01
        core.save_message = lambda *args, **kwargs: None
        try:
            GroupWorker._answer(worker, event)
        finally:
            core.call_formal_reply = original_call
            core.focus_and_send = original_send
            core.save_message = original_save

        assert edit_started == [], (text, edit_started)
        expected_image = str(reference) if exact_reference else None
        assert provider_images == [expected_image], (text, provider_images)


def _run_invalid_group_marker_no_leak_case(
    reply_text: str = "[开始生图 - 描述]",
) -> None:
    worker = GroupWorker.__new__(GroupWorker)
    worker.chat_name = "__v374_marker_guard__"
    worker.group_cfg = {"name": worker.chat_name}
    worker.profiles = {}
    worker.owner_wxid = "wxid_owner"
    worker.bot_wxid = "wxid_bot"
    worker._legacy_sender = lambda sender, display: "me"
    worker._v37_prompt_for_event = lambda event: event["text"]
    worker._v315_followup_prompt = lambda event, prompt: prompt
    worker._v312_quote_emoticon_ref = lambda event: None
    worker._v38_quote_image_ref = lambda event: None
    worker._v323_start_image_edit = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("invalid marker must not enter image edit")
    )
    worker._v322_start_imagegen = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("invalid marker must not be normalized as imagegen")
    )
    worker._cleanup_image_ref = lambda ref: None
    worker._v315_is_no_reply = lambda reply: False
    worker._followup_after_success = lambda event: None
    worker._v392_send_lock = threading.Lock()
    worker._v39_last_bot_sent_mono = 0.0

    event = {
        "local_id": 16000,
        "sender_username": "wxid_fixture",
        "sender_display": "Owner",
        "text": "@阿卫 画个东西",
        "timestamp": 0,
        "_v36_trigger_kind": "explicit",
    }
    sent = []
    original_call = core.call_formal_reply
    original_send = core.focus_and_send
    original_save = core.save_message
    core.call_formal_reply = lambda *args, **kwargs: reply_text
    core.focus_and_send = lambda chat, text: sent.append(text) or 0.01
    core.save_message = lambda *args, **kwargs: None
    try:
        GroupWorker._answer(worker, event)
    finally:
        core.call_formal_reply = original_call
        core.focus_and_send = original_send
        core.save_message = original_save

    assert len(sent) == 1, sent
    assert "开始生图" not in sent[0], sent


def _run_invalid_private_marker_no_leak_case(
    reply_text: str = "[开始生图 - 描述]",
) -> None:
    worker = PrivateWorker.__new__(PrivateWorker)
    worker.chat_name = "Owner"
    worker.peer_wxid = "wxid_fixture"
    worker.context_name = "private:wxid_fixture"
    worker.private_cfg = {"name": worker.context_name}
    worker.profiles = {}
    worker.bot_wxid = "wxid_bot"
    worker.stop_event = threading.Event()
    worker.image_pipeline = object()
    worker.emoticon_pipeline = object()
    worker._v321_app_detail = lambda event: (0, "")
    worker._parse_quote_payload = lambda raw: None
    worker._is_emoticon = lambda event: False
    worker._v322_start_imagegen = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("invalid private marker must not enter imagegen")
    )

    event = {
        "local_id": 16001,
        "local_type": 1,
        "msg_type": "文本",
        "sender_username": "wxid_fixture",
        "text": "画个东西",
        "raw_content": "",
        "timestamp": 0,
    }
    sent = []
    original_call = core.call_formal_reply
    original_send = core.private_focus_and_send
    original_send_many = core.private_focus_and_send_many
    original_save = core.save_message
    original_freeze = core.freeze_llm_context_bounded
    worker.stop_event = threading.Event()
    core.freeze_llm_context_bounded = lambda *a, **kw: {'context': '', 'current_sender': 'fixture'}
    core.call_formal_reply = lambda *args, **kwargs: reply_text
    core.private_focus_and_send = lambda chat, text: sent.append(text) or 0.01
    core.private_focus_and_send_many = (
        lambda chat, texts, **kwargs: sent.extend(texts) or 0.01
    )
    core.save_message = lambda *args, **kwargs: None
    try:
        PrivateWorker._handle(worker, event)
    finally:
        core.call_formal_reply = original_call
        core.private_focus_and_send = original_send
        core.private_focus_and_send_many = original_send_many
        core.save_message = original_save
        core.freeze_llm_context_bounded = original_freeze

    assert len(sent) == 1, sent
    assert "开始生图" not in sent[0], sent


def _run_post_vision_imagegen_gate_case(
    reply_text: str = "[开始生图]  \n参考图片生成一张新的穿搭照",
) -> None:
    """Every Vision reply must still pass the imagegen result gate."""
    with tempfile.TemporaryDirectory() as temp_dir:
        reference = Path(temp_dir) / "quoted.png"
        reference.write_bytes(PNG_1X1)

        worker = GroupWorker.__new__(GroupWorker)
        worker.chat_name = "__v376_post_vision_gate__"
        worker.group_cfg = {"name": worker.chat_name}
        worker.profiles = {}
        worker.owner_wxid = "wxid_owner"
        worker.bot_wxid = "wxid_bot"
        worker._legacy_sender = lambda sender, display: "me"
        worker._v37_prompt_for_event = lambda event: event["text"]
        worker._v315_followup_prompt = lambda event, prompt: prompt
        worker._v312_quote_emoticon_ref = lambda event: None
        worker._v38_quote_image_ref = lambda event: ImageRef(
            sender="wxid_fixture",
            path=str(reference),
            error=None,
            local_id=923,
            created=time.monotonic(),
        )
        worker._v323_start_image_edit = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ordinary Vision question must not pre-route to image edit")
        )
        routed = []
        worker._v322_start_imagegen = (
            lambda event, **kwargs: routed.append(kwargs.get("routed_reply")) or True
        )
        worker._cleanup_image_ref = lambda ref: None
        worker._v315_is_no_reply = lambda reply: False
        worker._followup_after_success = lambda event: None
        worker._v392_send_lock = threading.Lock()
        worker._v39_last_bot_sent_mono = 0.0

        event = {
            "local_id": 16002,
            "sender_username": "wxid_fixture",
            "sender_display": "Owner",
            "text": "@阿卫 这张图怎么样",
            "timestamp": 0,
            "_v36_trigger_kind": "explicit",
        }
        sent = []
        original_call = core.call_formal_reply
        original_send = core.focus_and_send
        original_save = core.save_message
        core.call_formal_reply = lambda *args, **kwargs: reply_text
        core.focus_and_send = lambda chat, text: sent.append(text) or 0.01
        core.save_message = lambda *args, **kwargs: None
        try:
            GroupWorker._answer(worker, event)
        finally:
            core.call_formal_reply = original_call
            core.focus_and_send = original_send
            core.save_message = original_save

        assert routed == [reply_text], routed
        assert sent == [], sent


def main() -> None:
    positives = (
        "@阿卫 再给我画一个",
        "@阿卫 再画一个",
        "@阿卫 照这个再画一个",
        "@阿卫 照这个来一个",
        "@阿卫 来个类似的",
        "@阿卫 再来一个",
        "@阿卫 再来一版",
        "@阿卫 按这个再做一个",
        "@阿卫 生成一张类似的图",
        "@阿卫 生成类似的图",
        "@阿卫 按这个生成一张",
        "@阿卫 照这个生成一张",
        "@阿卫 以该杯子为基础，生成一张桌面摆拍图片",
        "@阿卫 以该杯子为基础，生成一张露营桌面的照片",
        "@阿卫 画一张图人狗脸调换的图",
    )
    for text in positives:
        _run_exact_reference_case(text)

    for text in (
        "@阿卫 再来一个",
        "@阿卫 再来一版",
        "@阿卫 生成一张类似的图",
        "@阿卫 以该杯子为基础，生成一张桌面摆拍图片",
        "@阿卫 以该杯子为基础，生成一张露营桌面的照片",
        "@阿卫 画一张图人狗脸调换的图",
        "@阿卫 这里是一段很长的文字，用来讨论创作工作流。"
        "其中偶然举例说以后可以再画一个，但当前没有提供任何参考图，也没有要求立即生图。",
    ):
        _run_non_generation_case(text, exact_reference=False)

    for text in (
        "@阿卫 这个好看吗",
        "@阿卫 这个要不要再画一个？",
        "@阿卫 这是什么",
        "@阿卫 这里写了什么",
    ):
        _run_non_generation_case(text, exact_reference=True)

    assert core.is_image_gen_marker("[开始生图]\n描述") is True
    assert core.image_gen_routed_prompt("[开始生图]\n描述", "当前请求") == "描述"
    assert core.is_image_gen_marker("[开始生图]") is True
    assert core.image_gen_routed_prompt("[开始生图]", "当前请求") == "当前请求"
    assert core.is_image_gen_marker("[开始生图: 描述]") is True
    assert core.image_gen_routed_prompt("[开始生图: 描述]", "当前请求") == "描述"
    assert core.is_image_gen_marker("[开始生图]  \n描述") is True
    assert core.image_gen_routed_prompt("[开始生图]  \n描述", "当前请求") == "描述"
    assert core.is_image_gen_marker("普通正文中提到“开始生图”") is False
    assert core.image_gen_routed_prompt("普通正文中提到“开始生图”", "当前请求") == ""
    _run_invalid_group_marker_no_leak_case()
    _run_invalid_private_marker_no_leak_case()
    _run_post_vision_imagegen_gate_case()
    _run_post_vision_imagegen_gate_case(
        "[开始生图] 保留原图人物、姿势、场景和裤子不变，将上衣替换为黑灰色工装夹克"
    )
    _run_post_vision_imagegen_gate_case(
        "[开始生图]将上衣替换为做旧黑灰色短款工装夹克"
    )

    print("V374_REFERENCE_REPEAT_ROUTE=PASS")
    print("V374_REFERENCE_REPEAT_NEGATIVES=PASS")
    print("V374_IMAGEGEN_MARKER_NORMALIZATION=PASS")
    print("V374_IMAGEGEN_MARKER_GROUP_GUARD=PASS")
    print("V374_IMAGEGEN_MARKER_PRIVATE_GUARD=PASS")
    print("V376_POST_VISION_IMAGEGEN_GATE=PASS")


if __name__ == "__main__":
    main()
