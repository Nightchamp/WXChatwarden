#!/usr/bin/env python3
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden import core
from chatwarden.emoticon_pipeline import EmoticonPipeline
from chatwarden.worker import GroupWorker, ImageRef


def _worker():
    worker = object.__new__(GroupWorker)
    worker.chat_name = "repro"
    worker.group_cfg = {"name": "repro"}
    worker.profiles = {}
    worker.stop_event = __import__("threading").Event()
    worker.answers = []
    worker.edits = []

    worker._legacy_sender = lambda sender, display: display
    worker._v37_prompt_for_event = lambda event: str(event.get("text") or "")
    worker._v315_followup_prompt = lambda event, prompt: prompt
    worker._v38_quote_image_ref = lambda event: None
    worker._v312_quote_emoticon_ref = lambda event: ImageRef(
        sender="wxid_fixture",
        path="/tmp/quoted-type47.webp",
        error=None,
        local_id=6846,
        created=time.monotonic(),
    )
    worker._cleanup_image_ref = lambda ref: None
    worker._v323_start_image_edit = (
        lambda event, image_ref, reference_source="adjacent", instruction_override=None: worker.edits.append(
            (dict(event), image_ref, reference_source)
        )
    )
    worker._run_reply_job = lambda work: worker.answers.append(work)
    return worker


def _event(local_id, instruction):
    quoted_preview = (
        "4828927734547602802N0_V1_SQesq9i9|"
        "v1_7/fOTQsx17873829601788592580"
    )
    return {
        "local_id": local_id,
        "sender_username": "wxid_tester",
        "sender_display": "tester",
        "bot_display_name": "Owner-flash-JARVIS",
        "text": (
            instruction
            + "\n  ↳ 回复 🍐: "
            + quoted_preview
        ),
        "raw_content": (
            "<msg><appmsg><title>"
            + instruction
            + "</title><type>57</type>"
            "<refermsg><type>47</type><svrid>4557102896340484519</svrid>"
            "<fromusr>fixture@chatroom</fromusr><chatusr>wxid_fixture</chatusr>"
            "<displayname>🍐</displayname><content>&lt;msg&gt;&lt;emoji/&gt;&lt;/msg&gt;"
            "</content></refermsg></appmsg></msg>"
        ),
        "timestamp": time.time(),
        "_v36_trigger_kind": "explicit",
    }


def _assert_edit(text, local_id):
    worker = _worker()
    original_enabled = core.image_gen_enabled
    core.image_gen_enabled = lambda: True
    try:
        event = _event(local_id, text)
        assert worker._v343_current_instruction_text(event) == text
        worker._answer(event)
    finally:
        core.image_gen_enabled = original_enabled

    assert len(worker.edits) == 1, (
        f"quoted Type47 edit intent did not enter image-edit: {text!r}"
    )
    assert worker.answers == [], (
        f"quoted Type47 edit intent fell through to normal reply: {text!r}"
    )
    _, ref, source = worker.edits[0]
    assert ref.path == "/tmp/quoted-type47.webp"
    assert source == "quoted-emoticon"


def _type49_app8_target():
    md5 = "0123456789abcdef0123456789abcdef"
    url = (
        "http://vweixinf.tc.qq.com/stodownload?m="
        + md5
        + "&filekey=fixture"
    )
    emojiinfo = base64.b64encode(("\x00" + url + "\x00").encode()).decode()
    return {
        "local_id": 6860,
        "server_id": 5032910831311826512,
        "local_type": (8 << 32) + 49,
        "sender_username": "wxid_fixture",
        "raw_content": (
            "<msg><appmsg><type>8</type><appattach>"
            "<totallen>6925962</totallen>"
            f"<emoticonmd5>{md5}</emoticonmd5>"
            "<fileext>pic</fileext>"
            "<cdnthumbwidth>300</cdnthumbwidth>"
            "<cdnthumbheight>281</cdnthumbheight>"
            f"<emojiinfo>{emojiinfo}</emojiinfo>"
            "</appattach></appmsg></msg>"
        ),
    }


def _assert_type49_app8_is_exact_quoted_emoticon():
    target = _type49_app8_target()
    assert EmoticonPipeline.is_emoticon(target) is True
    meta = EmoticonPipeline._parse_meta(target)
    assert meta["md5"] == "0123456789abcdef0123456789abcdef"
    assert meta["type49_app8"] is True
    assert meta["declared_length"] == 6925962
    assert "vweixinf.tc.qq.com" in meta["cdnurl"]

    ordinary_link = dict(target)
    ordinary_link["raw_content"] = (
        "<msg><appmsg><type>4</type><url>https://example.com</url>"
        "</appmsg></msg>"
    )
    assert EmoticonPipeline.is_emoticon(ordinary_link) is False

    class FakePipeline:
        is_emoticon = staticmethod(EmoticonPipeline.is_emoticon)

        @staticmethod
        def decode_event(event):
            assert event["local_id"] == 6860
            return "/tmp/quoted-type49-app8.jpg"

    worker = object.__new__(GroupWorker)
    worker.chat_name = "repro"
    worker.emoticon_pipeline = FakePipeline()
    event = {
        "local_id": 6861,
        "_v312_quote_emoticon_target_event": target,
    }
    ref = worker._v312_quote_emoticon_ref(event)
    assert ref is not None
    assert ref.local_id == 6860
    assert ref.path == "/tmp/quoted-type49-app8.jpg"
    print("V3142_QUOTED_TYPE49_APP8_EMOTICON=PASS")


def _assert_bare_mention_quoted_media_beats_chat_context():
    worker = _worker()
    event = _event(6862, "@Owner-flash-JARVIS")
    worker._answer(event)
    assert len(worker.answers) == 1
    work = worker.answers[0]
    assert "被引用的图片/表情本身就是当前用户要你看的对象" in work.provider_prompt
    assert "加入当前群聊讨论" not in work.provider_prompt
    assert work.image_path == "/tmp/quoted-type47.webp"
    print("V3142_BARE_MENTION_QUOTED_MEDIA_PRECEDENCE=PASS")


def main():
    first = "@Owner-flash-JARVIS 给图中这个人物戴上帽子"
    second = "@Owner-flash-JARVIS 生成一张图中人物戴上帽子的图片"

    assert core.image_edit_intent(first) is True
    assert core.image_edit_intent(second) is True, (
        "strong quoted-image wording '图中人物' must count as image edit intent"
    )

    _assert_edit(first, 6848)
    _assert_edit(second, 6850)
    _assert_type49_app8_is_exact_quoted_emoticon()
    _assert_bare_mention_quoted_media_beats_chat_context()
    print("V344_QUOTED_EMOTICON_IMAGE_EDIT=PASS")


if __name__ == "__main__":
    main()
