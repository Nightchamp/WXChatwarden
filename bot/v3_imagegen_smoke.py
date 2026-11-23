#!/usr/bin/env python3
from __future__ import annotations

import sys
import ast
import base64
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden import core
from chatwarden.worker import GroupWorker, ImageRef
from chatwarden.private_worker import PrivateWorker


def imagegen_independent_key_path_smoke():
    """Image gateway auth has its own name, not a retired Hermes profile name."""
    import os
    expected = os.environ.get(
        "CW_IMAGEGEN_AUTH_KEY_FILE", "/etc/chatwarden/secrets/imagegen_gateway.key"
    )
    assert core.IMAGE_GEN_KEY_FILE == Path(expected)
    source = (BOT_DIR / "chatwarden/core.py").read_text(encoding="utf-8")
    assert "hermes_bot2reach_api_key" not in source
    print("IMAGEGEN_INDEPENDENT_AUTH_KEY_PATH=PASS")


def image_clipboard_foreground_smoke():
    """Lock the live fix for xclip's default background-fork behavior."""
    source = (BOT_DIR / "atspi_send_image.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    commands = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr == "Popen"
        ):
            continue
        if not node.args or not isinstance(node.args[0], ast.List):
            continue
        values = []
        for item in node.args[0].elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                values.append(item.value)
        if values and values[0] == "xclip":
            commands.append(values)
    assert len(commands) == 1, commands
    assert "-quiet" in commands[0], commands[0]
    assert commands[0][-2:] == ["-loops", "1"], commands[0]
    print("IMAGE_CLIPBOARD_XCLIP_FOREGROUND=PASS")


def imagegen_route_regression_smoke():
    def make_worker():
        worker = GroupWorker.__new__(GroupWorker)
        worker.chat_name = "__imagegen_route_probe__"
        worker.pending_wait = 8.0
        worker.pending = {}
        worker.recent = {}
        worker.recent_text = {}
        worker.recent_app = {}
        worker.image_max_age = 120.0
        worker._v313_true_mention = lambda event: True
        worker._v37_quote_metadata = lambda event: (None, None)
        worker._match_adjacent_app_context = lambda event: None
        worker._match_adjacent_quoted_image = lambda event: None
        worker._consume_queued_forward_image = lambda event: None
        worker._cleanup_image_ref = lambda ref: None
        answered = []
        worker._answer = lambda event, image_ref=None: answered.append(
            (event.get("local_id"), image_ref)
        )
        return worker, answered

    worker, answered = make_worker()

    event = {
        "local_id": 951,
        "sender_username": "wxid_fixture",
        "sender_display": "Owner",
        "text": "@演示bot\u2005帮我生成一桌家常菜的图片",
    }
    GroupWorker._handle_text(worker, event)
    assert answered == [(951, None)], (
        "text-to-image request must enter normal Hermes/imagegen routing"
    )
    assert "wxid_fixture" not in worker.pending, (
        "text-to-image request must not wait for a future user image"
    )
    print("IMAGEGEN_ROUTE_NO_NEXT_IMAGE_WAIT=PASS")

    # v3.153: ``生成图片：`` is a deterministic local command after removing
    # only the current bot's leading @ field. It wakes the group even without a
    # structured mention and bypasses the LLM imagegen marker decision.
    assert core.image_gen_explicit_prompt(
        "@演示bot\u2005生成图片：一只戴墨镜的猫",
        "演示bot",
        "wxid_bot",
    ) == "一只戴墨镜的猫"
    assert core.image_gen_explicit_prompt(
        "生成图片: 未来城市夜景",
        "演示bot",
        "wxid_bot",
    ) == "未来城市夜景"
    assert core.image_gen_explicit_prompt(
        "@别人\u2005生成图片：不应触发",
        "演示bot",
        "wxid_bot",
    ) is None

    worker, answered = make_worker()
    worker.bot_wxid = "wxid_bot"
    worker._v313_true_mention = lambda event: False
    explicit_started = []
    worker._v322_start_imagegen = (
        lambda event, reply_generation=None, routed_reply=None, prompt_override=None:
        explicit_started.append(prompt_override) or True
    )
    event = {
        "local_id": 9511,
        "sender_username": "wxid_fixture",
        "sender_display": "Owner",
        "bot_display_name": "演示bot",
        "text": "@演示bot\u2005生成图片：一只戴墨镜的猫",
    }
    GroupWorker._handle_text(worker, event)
    assert explicit_started == ["一只戴墨镜的猫"], explicit_started
    assert answered == [], "explicit image command must bypass the LLM route"
    print("V3153_EXPLICIT_IMAGEGEN_GROUP_DIRECT=PASS")

    # Regression for live local_id=51786 in 示例群聊壹号测试: the explicit
    # image command must establish its explicit trigger state before the exact
    # quote resolver runs. Otherwise a quoted image is invisible at this point
    # and the request incorrectly becomes text-to-image with reference=no.
    worker, answered = make_worker()
    worker.bot_wxid = "wxid_bot"
    quote_trigger_states = []

    def quoted_image_meta(event):
        quote_trigger_states.append(event.get("_v36_trigger_kind"))
        if event.get("_v36_trigger_kind") != "explicit":
            return None, None
        return {"refer_type": 3}, None

    worker._v37_quote_metadata = quoted_image_meta
    worker._v313_true_mention = lambda event: True
    explicit_started = []
    worker._v322_start_imagegen = (
        lambda event, reply_generation=None, routed_reply=None, prompt_override=None:
        explicit_started.append(prompt_override) or True
    )
    event = {
        "local_id": 9512,
        "sender_username": "wxid_fixture",
        "sender_display": "Owner",
        "bot_display_name": "小卫机器人",
        "text": (
            "@小卫机器人\u2005"
            "生成图片:把这张图片放大人物的占比，并高清化"
        ),
    }
    GroupWorker._handle_text(worker, event)
    assert quote_trigger_states, "quoted image resolver must run"
    assert quote_trigger_states[0] == "explicit", quote_trigger_states
    assert event.get("_v3153_explicit_imagegen_reference_expected") is True
    assert explicit_started == [], (
        "quoted explicit image command must not start text-to-image"
    )
    assert answered == [(9512, None)], answered
    print("V3155_EXPLICIT_IMAGEGEN_QUOTE_ORDER=PASS")

    # Regression for the 2026-09-15 PartyJam incident: a plain text-to-image
    # request must not inherit an unrelated adjacent sticker/image just because
    # an image exists in the same-sender recent-media cache.
    worker, answered = make_worker()
    adjacent = ImageRef(
        sender="wxid_fixture",
        path="/tmp/v3137-adjacent.jpg",
        error=None,
        local_id=4224,
        created=time.monotonic(),
    )
    worker.recent["wxid_fixture"] = adjacent
    event = {
        "local_id": 9530,
        "sender_username": "wxid_fixture",
        "sender_display": "Owner",
        "text": "@演示bot\u2005生成一个朋友聚会的图",
    }
    GroupWorker._handle_text(worker, event)
    assert answered == [(9530, None)], answered

    # A real adjacent edit request must continue to bind the media.
    worker, answered = make_worker()
    adjacent = ImageRef(
        sender="wxid_fixture",
        path="/tmp/v3137-adjacent-edit.jpg",
        error=None,
        local_id=4267,
        created=time.monotonic(),
    )
    worker.recent["wxid_fixture"] = adjacent
    event = {
        "local_id": 9531,
        "sender_username": "wxid_fixture",
        "sender_display": "Owner",
        "text": "@演示bot\u2005把图片后面路人去掉",
    }
    GroupWorker._handle_text(worker, event)
    assert answered == [(9531, adjacent)], answered
    print("IMAGEGEN_ADJACENT_CREATE_EDIT_DISAMBIGUATION=PASS")

    # v3.161 regression from live local_id=57695: the bot's visible group
    # nickname contains an ordinary space, so ``@\S+`` cannot normalize it.
    # Structured atuserlist proof lets media-intent routing strip only our exact
    # displayed mention and bind the same-sender image sent one second earlier.
    worker, answered = make_worker()
    worker.bot_wxid = "wxid_bot"
    adjacent = ImageRef(
        sender="wxid_fixture",
        path="/tmp/v3161-pants.jpg",
        error=None,
        local_id=57694,
        created=time.monotonic(),
    )
    worker.recent["wxid_fixture"] = adjacent
    event = {
        "local_id": 57695,
        "sender_username": "wxid_fixture",
        "sender_display": "Owner",
        "bot_display_name": "小卫机器人",
        "mentioned_wxids": ["wxid_bot"],
        "text": "@小卫机器人\u2005这个裤子怎么样",
    }
    GroupWorker._handle_text(worker, event)
    assert answered == [(57695, adjacent)], answered
    print("V3161_SPACED_BOT_NAME_ADJACENT_VISION=PASS")

    for local_id, text in enumerate(
        (
            "@演示bot\u2005我接下来发张图片给你看",
            "@演示bot\u2005等下我发图片，你帮我看看",
            "@演示bot\u2005这张图片里是什么",
        ),
        start=952,
    ):
        worker, answered = make_worker()
        event = {
            "local_id": local_id,
            "sender_username": "wxid_fixture",
            "sender_display": "Owner",
            "text": text,
        }
        GroupWorker._handle_text(worker, event)
        assert not answered, "future/existing image intent must keep NEXT-image wait"
        assert "wxid_fixture" in worker.pending, (
            "future/existing image intent must remain pending for a user image"
        )
    print("NEXT_IMAGE_WAIT_PRESERVED=PASS")

    # Regression for a real production false positive: a long, pure-text
    # message used the discourse marker "下面" ("below / next, I will explain")
    # and was incorrectly treated as a request to wait for a future image.
    # The route seam must send it to the normal text answer path instead.
    worker, answered = make_worker()
    event = {
        "local_id": 955,
        "sender_username": "wxid_fixture",
        "sender_display": "Owner",
        "text": (
            "@阿卫 以下是kimi k3针对我最初提问提示词的答复。"
            "我为了简化逻辑被它质疑了一些无关痛痒的地方，不过总体它是支持我的："
            "你的问题触及了江苏地域文化研究中最富争议也最有深度的议题之一。"
            "下面我分几个层面来梳理。\n\n"
            "一、核心判断：永嘉南渡确实是江苏南北分化的关键历史节点。"
        ),
    }
    GroupWorker._handle_text(worker, event)
    assert answered == [(955, None)], (
        "pure-text discourse words such as '下面' must not open NEXT-image wait"
    )
    assert "wxid_fixture" not in worker.pending, (
        "pure-text long message incorrectly opened NEXT-image pending"
    )
    print("NEXT_IMAGE_TEXT_FALSE_POSITIVE=PASS")

    # Other broad substrings that used to collide with ordinary prose must
    # likewise stay on the text route unless they are tied to an actual media
    # reference/send instruction.
    for local_id, text in enumerate(
        (
            "@阿卫 接下来我分析一下这个问题",
            "@阿卫 随后双方就这个结论达成了一致",
            "@阿卫 我发现这里的因果链还有一个漏洞",
            "@阿卫 图片生成技术这两年发展得怎么样",
            "@阿卫 识图模型现在哪家做得比较好",
            "@阿卫 帮我看一下图片生成技术这两年发展得怎么样",
        ),
        start=956,
    ):
        worker, answered = make_worker()
        event = {
            "local_id": local_id,
            "sender_username": "wxid_fixture",
            "sender_display": "Owner",
            "text": text,
        }
        GroupWorker._handle_text(worker, event)
        assert answered == [(local_id, None)], (
            f"ordinary text incorrectly opened NEXT-image wait: {text!r}"
        )
        assert "wxid_fixture" not in worker.pending
    print("NEXT_IMAGE_BROAD_TEXT_FALSE_POSITIVES=PASS")


def group_image_edit_route_smoke():
    with tempfile.TemporaryDirectory() as temp_dir:
        reference = Path(temp_dir) / "reference.png"
        reference.write_bytes(PNG_1X1)

        def run_case(
            quoted,
            text="@演示bot\u2005生成一张这个人穿西装的图片",
            trigger_kind="explicit",
            quote_gate_passed=False,
            provider_context=None,
            expected_override_terms=(),
            explicit_command_prompt=None,
        ):
            worker = GroupWorker.__new__(GroupWorker)
            worker.chat_name = "__group_image_edit__"
            worker.group_cfg = {"name": worker.chat_name}
            worker.profiles = {}
            worker.owner_wxid = "wxid_owner"
            worker.bot_wxid = "wxid_bot"
            worker._legacy_sender = lambda sender, display: "me"
            worker._v37_prompt_for_event = lambda event: (
                provider_context if provider_context is not None else event["text"]
            )
            worker._v315_followup_prompt = lambda event, prompt: prompt
            worker._v312_quote_emoticon_ref = lambda event: None
            worker._v38_quote_image_ref = (
                (lambda event: ImageRef(
                    sender="wxid_fixture",
                    path=str(reference),
                    error=None,
                    local_id=700,
                    created=time.monotonic(),
                ))
                if quoted
                else (lambda event: None)
            )
            started = []
            overrides = []
            cleaned = []
            worker._v323_start_image_edit = (
                lambda event, image_ref, reference_source="adjacent", instruction_override=None: (
                    started.append((event["local_id"], image_ref.path, reference_source)),
                    overrides.append(instruction_override),
                )
            )
            worker._cleanup_image_ref = lambda ref: cleaned.append(ref.path)

            event = {
                "local_id": 701 if quoted else 702,
                "sender_username": "wxid_fixture",
                "sender_display": "Owner",
                "text": text,
                "timestamp": 0,
                "_v36_trigger_kind": trigger_kind,
            }
            if quote_gate_passed:
                event["_v350_quote_gate_passed"] = True
            if explicit_command_prompt is not None:
                event["_v3153_explicit_imagegen_prompt"] = explicit_command_prompt
                event["_v3153_explicit_imagegen_reference_expected"] = True
            initial_ref = None if quoted else ImageRef(
                sender="wxid_fixture",
                path=str(reference),
                error=None,
                local_id=699,
                created=time.monotonic(),
            )

            original_call = core.call_formal_reply
            core.call_formal_reply = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("image edit must bypass Vision/Hermes")
            )
            try:
                GroupWorker._answer(worker, event, image_ref=initial_ref)
            finally:
                core.call_formal_reply = original_call

            assert started == [(
                event["local_id"],
                str(reference),
                "quoted" if quoted else "adjacent",
            )]
            assert cleaned == [], "reference ownership must transfer to async edit job"
            if expected_override_terms:
                override = str(overrides[-1] or "")
                for term in expected_override_terms:
                    assert term in override, (term, override)
            if explicit_command_prompt is not None:
                assert overrides[-1] == explicit_command_prompt, overrides[-1]

        run_case(quoted=False)
        run_case(quoted=True)
        run_case(
            quoted=True,
            text="@阿卫 把这张图里最左边的人物P掉",
        )
        run_case(
            quoted=True,
            text="@阿卫 修改图片，把图中最左边的人物P掉",
        )
        # Regression for the 2026-09-15 live miss: an exact quoted image plus
        # a material-restoration instruction is a direct reference edit.  It
        # must bypass Vision instead of spending a full Vision turn before
        # image generation.
        run_case(
            quoted=True,
            text="@小卫机器人 把杯盖的花纹还原成纯色，其他不变",
        )
        # Regression for live local_id=47703: replying directly to the bot's
        # generated image is already explicit addressing. A concrete garment
        # edit must not require a second @ mention or fall through to Vision.
        live_followup = "裤子改成灰色，外套更加休闲风格一点，加上一些口袋拼接的设计"
        assert core.image_edit_intent(live_followup), live_followup
        assert core.image_edit_followup_intent(live_followup), live_followup
        run_case(
            quoted=True,
            text=live_followup,
            trigger_kind="quote",
            quote_gate_passed=True,
        )
        # Regression for the 2026-09-16 live miss in 示例群聊壹号测试:
        # a generic "modify this image" execution command must actually enter
        # the edit route, and because the command itself omits the edit target,
        # the exact quote window must carry the already-stated modification
        # plan into the image-generation prompt.
        generic_live = "那你修改这个图片@小卫机器人"
        generic_context = (
            generic_live
            + "\n\n最近引用上下文：\n"
            + "用户：这蕾丝有点太密了吧，我密集恐惧症犯了\n"
            + "机器人：把花纹密度降下来，领子和口袋留白出来"
        )
        assert core.image_edit_intent(generic_live), generic_live
        assert core.image_edit_followup_intent(generic_live), generic_live
        assert core.image_edit_needs_recent_context(generic_live), generic_live
        run_case(
            quoted=True,
            text=generic_live,
            provider_context=generic_context,
            expected_override_terms=("花纹密度降下来", "领子和口袋留白"),
        )
        for wording in (
            "把口袋盖恢复成衣服本布，其他不变",
            "把口袋盖还原为衣服本布，其他不变",
            "把口袋盖恢复原样，其他不变",
        ):
            assert core.image_edit_intent(wording), wording
            assert core.image_edit_followup_intent(wording), wording
        run_case(
            quoted=True,
            text="@小卫机器人 生成图片：图里的人物大战机器人",
            explicit_command_prompt="图里的人物大战机器人",
        )
        assert core.image_edit_intent("生成图片：图里的人物大战机器人") is True
        assert core.image_edit_followup_intent("生成图片：图里的人物大战机器人") is True

        worker = GroupWorker.__new__(GroupWorker)
        worker.chat_name = "__group_vision_preserved__"
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
            AssertionError("Vision question must not route to image edit")
        )
        cleaned = []
        worker._cleanup_image_ref = lambda ref: cleaned.append(ref.path)
        worker._v315_is_no_reply = lambda reply: False
        worker._followup_after_success = lambda event: None
        worker._v392_send_lock = threading.Lock()
        worker._v39_last_bot_sent_mono = 0.0
        vision_calls = []
        vision_event = {
            "local_id": 703,
            "sender_username": "wxid_fixture",
            "sender_display": "Owner",
            "text": "@演示bot\u2005这张图里的人穿的是西装吗",
            "timestamp": 0,
            "_v36_trigger_kind": "explicit",
        }
        vision_ref = ImageRef(
            sender="wxid_fixture",
            path=str(reference),
            error=None,
            local_id=698,
            created=time.monotonic(),
        )
        original_call = core.call_formal_reply
        original_send = core.focus_and_send
        original_save = core.save_message
        core.call_formal_reply = lambda *args, **kwargs: (
            vision_calls.append(kwargs.get("image_path")) or "是的，看起来穿着西装。"
        )
        core.focus_and_send = lambda *args, **kwargs: 0.01
        core.save_message = lambda *args, **kwargs: None
        try:
            GroupWorker._answer(worker, vision_event, image_ref=vision_ref)
        finally:
            core.call_formal_reply = original_call
            core.focus_and_send = original_send
            core.save_message = original_save
        assert vision_calls == [str(reference)]
        assert cleaned == [str(reference)]
    print("GROUP_IMAGE_EDIT_ADJACENT=PASS")
    print("GROUP_IMAGE_EDIT_QUOTED=PASS")
    print("V3153_EXPLICIT_IMAGEGEN_GROUP_QUOTED_EDIT=PASS")
    print("GROUP_VISION_PRESERVED=PASS")


class _PrivateImagePipeline:
    def __init__(self, path):
        self.path = str(path)
        self.decode_calls = []
        self.cleanup_calls = []

    def decode_event(self, event):
        self.decode_calls.append(int(event.get("local_id") or 0))
        return self.path

    def cleanup(self, path):
        self.cleanup_calls.append(str(path))


def private_image_edit_route_smoke():
    with tempfile.TemporaryDirectory() as temp_dir:
        reference = Path(temp_dir) / "reference.png"
        reference.write_bytes(PNG_1X1)

        def make_worker():
            worker = PrivateWorker.__new__(PrivateWorker)
            worker.chat_name = "Owner"
            worker.peer_wxid = "wxid_fixture"
            worker.context_name = "private:wxid_fixture"
            worker.private_cfg = {"name": worker.context_name}
            worker.profiles = {}
            worker.bot_wxid = "wxid_bot"
            worker.stop_event = threading.Event()
            worker.image_pipeline = _PrivateImagePipeline(reference)
            worker.emoticon_pipeline = object()
            worker._is_emoticon = lambda event: False
            worker._v321_app_detail = lambda event: (0, "")
            worker._quote_prompt = lambda current, meta, target: current
            worker._v323_start_image_edit = (
                lambda event, current_text, image_path, source: started.append(
                    (event["local_id"], current_text, image_path, source)
                )
            )
            return worker

        started = []
        worker = make_worker()
        worker._parse_quote_payload = lambda raw: {"svrid": "123"}
        worker._resolve_quote_target = lambda meta: {
            "local_id": 710,
            "local_type": 3,
            "msg_type": "图片",
            "text": "[图片]",
        }
        quote_event = {
            "local_id": 711,
            "local_type": 1,
            "msg_type": "文本",
            "sender_username": "wxid_fixture",
            "text": "生成一张这个人穿西装的图片",
            "raw_content": "<msg />",
            "timestamp": 0,
        }

        original_call = core.call_formal_reply
        original_save = core.save_message
        core.call_formal_reply = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("quoted private image edit must bypass Vision/Hermes")
        )
        core.save_message = lambda *args, **kwargs: None
        try:
            PrivateWorker._handle(worker, quote_event)
        finally:
            core.call_formal_reply = original_call
            core.save_message = original_save
        assert started == [
            (711, "生成一张这个人穿西装的图片", str(reference), "quoted")
        ]
        assert worker.image_pipeline.cleanup_calls == [], (
            "quoted reference ownership must transfer to async edit job"
        )

        # v3.153: private chat gets the same deterministic explicit prefix.
        # With no quoted image it goes straight to text-to-image; with an exact
        # quoted image it goes straight to edit and strips the command prefix.
        worker = make_worker()
        worker._parse_quote_payload = lambda raw: None
        explicit_private_started = []
        worker._v322_start_imagegen = (
            lambda event, current_text, routed_reply=None, prompt_override=None:
            explicit_private_started.append(prompt_override) or True
        )
        direct_command_event = {
            "local_id": 712,
            "local_type": 1,
            "msg_type": "文本",
            "sender_username": "wxid_fixture",
            "text": "生成图片：一只戴墨镜的猫",
            "raw_content": "",
            "timestamp": 0,
        }
        original_save = core.save_message
        core.save_message = lambda *args, **kwargs: None
        try:
            PrivateWorker._handle(worker, direct_command_event)
        finally:
            core.save_message = original_save
        assert explicit_private_started == ["一只戴墨镜的猫"]

        started.clear()
        worker = make_worker()
        worker._parse_quote_payload = lambda raw: {"svrid": "123"}
        worker._resolve_quote_target = lambda meta: {
            "local_id": 713,
            "local_type": 3,
            "msg_type": "图片",
            "text": "[图片]",
        }
        quoted_command_event = {
            "local_id": 714,
            "local_type": 1,
            "msg_type": "文本",
            "sender_username": "wxid_fixture",
            "text": "生成图片：把衣服换成黑色西装",
            "raw_content": "<msg />",
            "timestamp": 0,
        }
        original_call = core.call_formal_reply
        original_save = core.save_message
        core.call_formal_reply = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("explicit quoted private edit must bypass Vision/Hermes")
        )
        core.save_message = lambda *args, **kwargs: None
        try:
            PrivateWorker._handle(worker, quoted_command_event)
        finally:
            core.call_formal_reply = original_call
            core.save_message = original_save
        assert started == [
            (714, "把衣服换成黑色西装", str(reference), "quoted")
        ], started

        started.clear()
        worker = make_worker()
        worker._parse_quote_payload = lambda raw: None
        worker._v323_recent_image = {
            "event": {
                "local_id": 720,
                "local_type": 3,
                "msg_type": "图片",
                "sender_username": "wxid_fixture",
            },
            "seen": time.monotonic(),
        }
        adjacent_event = {
            "local_id": 721,
            "local_type": 1,
            "msg_type": "文本",
            "sender_username": "wxid_fixture",
            "text": "把这个人改成穿黑色西装",
            "raw_content": "",
            "timestamp": 0,
        }
        original_call = core.call_formal_reply
        original_save = core.save_message
        core.call_formal_reply = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("adjacent private image edit must bypass normal Hermes")
        )
        core.save_message = lambda *args, **kwargs: None
        try:
            PrivateWorker._handle(worker, adjacent_event)
        finally:
            core.call_formal_reply = original_call
            core.save_message = original_save
        assert started == [
            (721, "把这个人改成穿黑色西装", str(reference), "adjacent")
        ]
        assert worker.image_pipeline.cleanup_calls == []

        started.clear()
        worker = make_worker()
        worker._parse_quote_payload = lambda raw: None
        direct_event = {
            "local_id": 730,
            "local_type": 3,
            "msg_type": "图片",
            "sender_username": "wxid_fixture",
            "text": "[图片]",
            "raw_content": "",
            "timestamp": 0,
        }
        original_call = core.call_formal_reply
        original_save = core.save_message
        original_send = core._private_focus_and_send_unlocked
        original_freeze = core.freeze_llm_context_bounded
        core.freeze_llm_context_bounded = lambda *a, **kw: {'context': '', 'current_sender': 'fixture'}
        worker.stop_event = threading.Event()
        core.call_formal_reply = lambda *args, **kwargs: "收到图片"
        core.save_message = lambda *args, **kwargs: None
        core._private_focus_and_send_unlocked = lambda *args, **kwargs: 0.01
        try:
            PrivateWorker._handle(worker, direct_event)
        finally:
            core.call_formal_reply = original_call
            core.save_message = original_save
            core._private_focus_and_send_unlocked = original_send
            core.freeze_llm_context_bounded = original_freeze
        assert worker._v323_recent_image["event"]["local_id"] == 730
        assert worker.image_pipeline.cleanup_calls == [str(reference)]
    print("PRIVATE_IMAGE_EDIT_ADJACENT=PASS")
    print("PRIVATE_IMAGE_EDIT_QUOTED=PASS")
    print("V3153_EXPLICIT_IMAGEGEN_PRIVATE_DIRECT_AND_EDIT=PASS")
    print("PRIVATE_RECENT_IMAGE_CACHE=PASS")


PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
)


class ImagegenHandler(BaseHTTPRequestHandler):
    auth = ""
    seen = None
    error_kind = ""

    def log_message(self, *args):
        return

    def do_POST(self):
        if self.path != "/v1/generate":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != f"Bearer {self.auth}":
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length") or "0")
        self.__class__.seen = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.__class__.error_kind:
            body = json.dumps(
                {"ok": False, "error": {"kind": self.__class__.error_kind}}
            ).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG_1X1)))
        self.send_header("X-Imagegen-Provider", "fake-provider")
        self.send_header("X-Imagegen-Model", "fake-image-model")
        self.send_header("X-Imagegen-Aspect", "portrait")
        self.end_headers()
        self.wfile.write(PNG_1X1)


def imagegen_http_smoke():
    token = "imagegen-test-key"
    with tempfile.TemporaryDirectory() as temp_dir:
        key_path = Path(temp_dir) / "key"
        key_path.write_text(token, encoding="utf-8")
        ImagegenHandler.auth = token
        ImagegenHandler.seen = None
        ImagegenHandler.error_kind = ""
        server = ThreadingHTTPServer(("127.0.0.1", 0), ImagegenHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        old_endpoint = core.IMAGE_GEN_ENDPOINT
        old_key_file = core.IMAGE_GEN_KEY_FILE
        try:
            core.IMAGE_GEN_ENDPOINT = (
                f"http://127.0.0.1:{server.server_port}/v1/generate"
            )
            core.IMAGE_GEN_KEY_FILE = key_path
            result = core.call_image_gen(
                "一只戴墨镜的猫",
                aspect_ratio="portrait",
            )
            image_path = Path(result["path"])
            assert image_path.read_bytes() == PNG_1X1
            assert result["provider"] == "fake-provider"
            assert result["model"] == "fake-image-model"
            assert result["aspect_ratio"] == "portrait"
            assert ImagegenHandler.seen == {
                "prompt": "一只戴墨镜的猫",
                "aspect_ratio": "portrait",
            }
            image_path.unlink()

            reference_path = Path(temp_dir) / "reference.png"
            reference_path.write_bytes(PNG_1X1)
            ImagegenHandler.seen = None
            edited = core.call_image_gen(
                "把这个人换成黑色西装",
                aspect_ratio="portrait",
                reference_image_path=str(reference_path),
            )
            edited_path = Path(edited["path"])
            assert edited_path.read_bytes() == PNG_1X1
            assert ImagegenHandler.seen["prompt"] == "把这个人换成黑色西装"
            assert ImagegenHandler.seen["aspect_ratio"] == "portrait"
            assert ImagegenHandler.seen["reference_image_mime"] == "image/png"
            assert base64.b64decode(
                ImagegenHandler.seen["reference_image_b64"],
                validate=True,
            ) == PNG_1X1
            edited_path.unlink()

            ImagegenHandler.error_kind = "request_rejected"
            try:
                core.call_image_gen("provider should reject this request")
            except core.ImageGenerationRejectedError:
                pass
            else:
                raise AssertionError("request_rejected was not classified")

            ImagegenHandler.error_kind = "quota_exhausted"
            try:
                core.call_image_gen("quota probe")
            except core.ImageGenerationQuotaError:
                pass
            else:
                raise AssertionError("quota_exhausted was not classified")
            ImagegenHandler.error_kind = ""
        finally:
            core.IMAGE_GEN_ENDPOINT = old_endpoint
            core.IMAGE_GEN_KEY_FILE = old_key_file
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main():
    assert core.is_image_gen_marker("[开始生图]") is True
    assert core.is_image_gen_marker(" [开始生图] \n") is True
    assert core.is_image_gen_marker("[开始调查]") is False
    assert core.image_gen_ack_text() == "我画一下～"

    assert core.image_gen_aspect_ratio("帮我画一张横版 16:9 的海报") == "landscape"
    assert core.image_gen_aspect_ratio("生成一张竖图，9:16") == "portrait"
    assert core.image_gen_aspect_ratio("画一只戴墨镜的猫") == "square"
    assert core.image_gen_prompt("@演示bot  帮我画一只猫") == "帮我画一只猫"
    edit_prompt = core.image_edit_prompt(
        "@演示bot 生成一张这个人穿西装的图片"
    )
    assert "保持参考图中主体的身份" in edit_prompt
    assert edit_prompt.endswith("生成一张这个人穿西装的图片")
    assert core.image_edit_intent("生成一张这个人穿西装的图片") is True
    assert core.image_edit_intent("把这个人改成穿黑色西装") is True
    assert core.image_edit_intent("把背景换成办公室") is True
    assert core.image_edit_intent("以这个人为参考，生成一张在咖啡馆里的照片") is True
    assert core.image_edit_intent("把这张图里最左边的人物P掉") is True
    assert core.image_edit_intent("修改图片，把图中最左边的人物P掉") is True
    assert core.image_edit_intent("把路人删掉") is True
    assert core.image_edit_intent("把水印抹掉") is True
    assert core.image_edit_intent("这张图里的人穿的是西装吗") is False
    assert core.image_edit_intent("帮我看看这张图是什么") is False
    assert core.image_edit_intent("这张图P掉背景会好看吗") is False
    assert core.image_edit_intent("P图是什么意思") is False
    assert core.image_edit_intent("把这个表情改成穿西装") is False
    assert core.image_edit_followup_intent("生成一个朋友聚会的图") is False
    assert core.image_edit_intent("画一张图人狗脸调换的图") is True

    old_disable = core.IMAGE_GEN_DISABLE_FILE
    with tempfile.TemporaryDirectory() as temp_dir:
        disable = Path(temp_dir) / "imagegen.disabled"
        core.IMAGE_GEN_DISABLE_FILE = disable
        assert core.image_gen_enabled() is True
        disable.write_text("disabled\n", encoding="utf-8")
        assert core.image_gen_enabled() is False
        try:
            core.call_image_gen("should not run")
        except RuntimeError as exc:
            assert "disabled" in str(exc)
        else:
            raise AssertionError("runtime kill switch did not block image generation")
    core.IMAGE_GEN_DISABLE_FILE = old_disable

    imagegen_http_smoke()
    imagegen_route_regression_smoke()
    group_image_edit_route_smoke()
    private_image_edit_route_smoke()
    image_clipboard_foreground_smoke()
    imagegen_independent_key_path_smoke()

    print("IMAGEGEN_MARKER=PASS")
    print("IMAGEGEN_ASPECT=PASS")
    print("IMAGEGEN_KILL_SWITCH=PASS")
    print("IMAGEGEN_HTTP_CLIENT=PASS")
    print("IMAGEGEN_REFERENCE_CLIENT=PASS")
    print("IMAGEGEN_EDIT_INTENT=PASS")
    print("IMAGEGEN_PROVIDER_ERROR_CLASSIFICATION=PASS")


if __name__ == "__main__":
    main()
