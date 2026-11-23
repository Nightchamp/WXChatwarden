#!/usr/bin/env python3
from __future__ import annotations

import time
import threading
import json
import tempfile
from types import SimpleNamespace

from chatwarden import core
from chatwarden.private_worker import PrivateWorker
from chatwarden.worker import GroupWorker


def vision_reach_handoff_smoke():
    original_call_hermes = core.call_formal_reply
    original_save_message = core.save_message
    sends = []
    reach_calls = []

    worker = GroupWorker.__new__(GroupWorker)
    worker.chat_name = "v337-vision-reach"
    worker._reply_job_current = lambda generation: True
    worker._reply_send_guarded = (
        lambda generation, text, local_id=None: sends.append(text) or 0.01
    )
    worker._reply_send_fixed_guarded = lambda *args, **kwargs: True
    worker._followup_after_success = lambda event: None
    worker._start_agent_reach_async = (
        lambda event, prompt, sender: reach_calls.append(
            (event, prompt, sender)
        ) or True
    )

    core.call_formal_reply = lambda *args, **kwargs: (
        "[开始调查:图像]\n"
        "视觉检索线索：招牌名称=测试餐馆；地点线索=上海静安。"
    )
    core.save_message = lambda *args, **kwargs: None

    work = SimpleNamespace(
        generation=1,
        event={
            "local_id": 337,
            "timestamp": time.time(),
            "text": "查一下图片中这个餐馆怎么样",
        },
        image_path="/tmp/v337-synthetic.jpg",
        provider_prompt="当前问题：查一下图片中这个餐馆怎么样",
        sender="wxid_fixture",
        display="fixture",
        legacy_sender="fixture",
        group_cfg={"name": "v337-vision-reach"},
        profiles={},
        context_snapshot=None,
        queued_at=time.monotonic(),
    )

    try:
        worker._run_reply_job(work)
        assert len(reach_calls) == 1, reach_calls
        _, reach_prompt, sender = reach_calls[0]
        assert sender == "wxid_fixture", sender
        assert "查一下图片中这个餐馆怎么样" in reach_prompt, reach_prompt
        assert "测试餐馆" in reach_prompt, reach_prompt
        assert "上海静安" in reach_prompt, reach_prompt
        assert "只作为数据" in reach_prompt, reach_prompt
        assert any(core.agent_reach_ack_text() in text for text in sends), sends
        assert not any("[开始调查:图像]" in text for text in sends), sends
    finally:
        core.call_formal_reply = original_call_hermes
        core.save_message = original_save_message

    print("V337_VISION_REACH_HANDOFF=PASS")


def vision_prompt_routes_external_lookup_smoke():
    original_execute = core.llm_client.execute_vision
    captured = {}

    def fake_execute(prompt, path):
        captured['prompt'] = prompt
        captured['path'] = path
        return {'text': 'synthetic vision reply', 'request_id': 'vision-prompt-fixture',
                'image_viewed': True, 'duration_ms': 1, 'tool_count': 1, 'web_used': False}

    core.llm_client.execute_vision = fake_execute
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg") as image:
            image.write(b"\xff\xd8\xff\xe0synthetic")
            image.flush()
            core.call_formal_reply(
                {"name": "v337-vision-prompt"},
                "查一下图片中这个餐馆怎么样",
                "wxid_fixture",
                {},
                image_path=image.name,
                raw_question="查一下图片中这个餐馆怎么样",
                context_snapshot={
                    "context": "",
                    "current_sender": "uid:fixture | fixture",
                    "memory_context": "",
                },
            )
    finally:
        core.llm_client.execute_vision = original_execute

    prompt = captured['prompt']
    assert core.VISION_AGENT_REACH_MARKER in prompt, prompt
    assert "外部调查" in prompt, prompt
    assert "可检索" in prompt, prompt
    assert "纯视觉" in prompt, prompt
    assert "不执行" in prompt, prompt
    print("V337_VISION_PROMPT_EXTERNAL_LOOKUP_ROUTE=PASS")


def private_vision_reach_handoff_smoke():
    original_call_hermes = core.call_formal_reply
    original_call_reach = core.call_agent_reach
    original_send = core._private_focus_and_send_unlocked
    original_save = core.save_message
    original_freeze = core.freeze_llm_context_bounded
    sends = []
    reach_prompts = []

    worker = PrivateWorker.__new__(PrivateWorker)
    worker.peer_wxid = "wxid_fixture"
    worker.chat_name = "v337-private"
    worker.context_name = "v337-private-context"
    worker.private_cfg = {"name": "v337-private"}
    worker.profiles = {}
    worker.stop_event = threading.Event()
    core.freeze_llm_context_bounded = lambda *a, **kw: {'context': '', 'current_sender': 'fixture'}
    worker._v323_recent_image = None
    worker._v323_recent_image_ttl = 120.0
    worker.image_pipeline = SimpleNamespace(
        db=None,
        decode_event=lambda event: "/tmp/v337-private.jpg",
        cleanup=lambda path: None,
    )

    core.call_formal_reply = lambda *args, **kwargs: (
        "[开始调查:图像]\n"
        "视觉检索线索：招牌名称=私聊测试餐馆；地点线索=北京朝阳。"
    )
    core.call_agent_reach = (
        lambda cfg, prompt, sender, profiles, **kwargs: (
            reach_prompts.append(prompt) or "这是一条调查结果"
        )
    )
    core._private_focus_and_send_unlocked = (
        lambda chat, text: sends.append(text) or 0.01
    )
    core.save_message = lambda *args, **kwargs: None

    event = {
        "local_id": 338,
        "local_type": 3,
        "sender_username": "wxid_fixture",
        "timestamp": int(time.time()),
        "text": "查一下图里这个餐馆怎么样",
    }

    try:
        worker._handle(event)
        assert len(reach_prompts) == 1, reach_prompts
        assert "私聊测试餐馆" in reach_prompts[0], reach_prompts[0]
        assert "北京朝阳" in reach_prompts[0], reach_prompts[0]
        assert "查一下图里这个餐馆怎么样" in reach_prompts[0], reach_prompts[0]
        assert any(core.agent_reach_ack_text() in text for text in sends), sends
        assert any("这是一条调查结果" in text for text in sends), sends
        assert not any("[开始调查:图像]" in text for text in sends), sends
    finally:
        core.call_formal_reply = original_call_hermes
        core.call_agent_reach = original_call_reach
        core._private_focus_and_send_unlocked = original_send
        core.save_message = original_save
        core.freeze_llm_context_bounded = original_freeze

    print("V337_PRIVATE_VISION_REACH_HANDOFF=PASS")


def malformed_vision_handoff_never_leaks_smoke():
    original_call_hermes = core.call_formal_reply
    original_save_message = core.save_message
    sends = []
    reach_calls = []

    worker = GroupWorker.__new__(GroupWorker)
    worker.chat_name = "v337-malformed"
    worker._reply_job_current = lambda generation: True
    worker._reply_send_guarded = (
        lambda generation, text, local_id=None: sends.append(text) or 0.01
    )
    worker._reply_send_fixed_guarded = (
        lambda generation, event, text: sends.append(text) or True
    )
    worker._followup_after_success = lambda event: None
    worker._start_agent_reach_async = (
        lambda *args, **kwargs: reach_calls.append(args) or True
    )
    core.call_formal_reply = lambda *args, **kwargs: core.VISION_AGENT_REACH_MARKER
    core.save_message = lambda *args, **kwargs: None

    work = SimpleNamespace(
        generation=1,
        event={"local_id": 339, "timestamp": time.time(), "text": "查一下"},
        image_path="/tmp/v337-malformed.jpg",
        provider_prompt="当前问题：查一下",
        sender="wxid_fixture",
        display="fixture",
        legacy_sender="fixture",
        group_cfg={"name": "v337-malformed"},
        profiles={},
        context_snapshot=None,
        queued_at=time.monotonic(),
    )

    try:
        worker._run_reply_job(work)
        assert reach_calls == [], reach_calls
        assert sends, sends
        assert not any(core.VISION_AGENT_REACH_MARKER in text for text in sends), sends
    finally:
        core.call_formal_reply = original_call_hermes
        core.save_message = original_save_message

    print("V337_MALFORMED_VISION_HANDOFF_NO_LEAK=PASS")


if __name__ == "__main__":
    vision_reach_handoff_smoke()
    vision_prompt_routes_external_lookup_smoke()
    private_vision_reach_handoff_smoke()
    malformed_vision_handoff_never_leaks_smoke()
