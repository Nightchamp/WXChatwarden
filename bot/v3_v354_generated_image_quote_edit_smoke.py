#!/usr/bin/env python3
"""Regression for bot-generated image quote -> single-reference edit.

The live failure was caused by the historical snapshot path not resolving the
quoted image by server id even though the current encrypted DB had the exact
image row. The fallback must stay exact-chat + exact-svrid and must preserve
sender-hint validation.
"""

from chatwarden import core
from chatwarden.worker import GroupWorker


SVRID = 5630808421347258423
BOT_WXID = "wxid_bot_fixture"
CHAT = "fixture@chatroom"
TABLE = "Msg_0123456789abcdef0123456789abcdef"


class FakeDb:
    def __init__(self, sender=BOT_WXID):
        self.sender = sender
        self.calls = []

    def v354_direct_group_image_by_server_id(self, resolved, server_id):
        self.calls.append((dict(resolved), int(server_id)))
        return {
            "chat_name": "fixture-group",
            "chat_username": CHAT,
            "table_name": TABLE,
            "source_db": "message/message_0.db",
            "local_id": 700,
            "server_id": SVRID,
            "local_type": 3,
            "msg_type": "图片",
            "timestamp": 0,
            "sender_username": self.sender,
            "sender_display": "bot",
            "text": "[图片]",
            "raw_content": "",
            "is_image": True,
            "is_emoticon": False,
        }


class FakePipeline:
    def __init__(self, db):
        self.db = db
        self.decoded = []

    def decode_event(self, event):
        self.decoded.append(dict(event))
        return "/tmp/v354-reference.jpg"


def make_worker(sender=BOT_WXID):
    db = FakeDb(sender=sender)
    worker = GroupWorker.__new__(GroupWorker)
    worker.chat_name = "fixture-group"
    worker.group_cfg = {"name": worker.chat_name, "chat_username": CHAT}
    worker.resolved = {
        "chat_name": worker.chat_name,
        "chat_username": CHAT,
        "table_name": TABLE,
        "group_cfg": worker.group_cfg,
    }
    worker.image_pipeline = FakePipeline(db)

    # Reproduce the real v3.50 path: an unmentioned exact quote to the bot is
    # trigger_kind=quote, so the broader v3.7 metadata reader deliberately
    # returns no metadata. v3.50 has already parsed and cached exact refermsg
    # metadata on the event itself.
    worker._v37_quote_metadata = lambda event: (None, None)
    worker._v37_resolve_quote_window = lambda meta, snapshot: None
    return worker, db


def main():
    assert core.image_edit_followup_intent("换成穿年轮的") is True
    assert core.image_edit_followup_intent("改成穿年轮的") is True
    assert core.image_edit_followup_intent("换成黑衣服") is True
    assert core.image_edit_followup_intent("换成哪个更好？") is False
    assert core.image_edit_followup_intent("这张图换成黑衣服会好看吗？") is False

    event = {
        "local_id": 701,
        "server_id": 9001,
        "_v36_trigger_kind": "quote",
        "_v350_quote_gate_passed": True,
        "_v350_quoted_bot_meta": {
            "svrid": SVRID,
            "fromusr": CHAT,
            "chatusr": BOT_WXID,
            "displayname": "bot",
            "content": "<msg><img/></msg>",
            "refer_type": 3,
            "_v38_fromusr_mode": "chatroom",
        },
        "text": "换成穿年轮的",
    }

    worker, db = make_worker()
    ref = worker._v38_quote_image_ref(dict(event))
    assert ref is not None, (
        "stale quote snapshot must fall back to current exact-svrid image lookup"
    )
    assert ref.local_id == 700
    assert ref.sender == BOT_WXID
    assert ref.path == "/tmp/v354-reference.jpg"
    assert db.calls == [(worker.resolved, SVRID)]
    assert worker.image_pipeline.decoded[-1]["server_id"] == SVRID
    assert worker.image_pipeline.decoded[-1]["chat_username"] == CHAT

    # Same exact server id is not enough when quote sender hints disagree.
    mismatch, mismatch_db = make_worker(sender="wxid_other_fixture")
    assert mismatch._v38_quote_image_ref(dict(event)) is None
    assert mismatch_db.calls == [(mismatch.resolved, SVRID)]
    assert mismatch.image_pipeline.decoded == []

    # Exercise the real _answer seam too: an exact quoted-bot image + terse
    # edit command must bypass Vision/Hermes and transfer reference ownership
    # to the async image-edit job.
    routed, _ = make_worker()
    routed.profiles = {}
    routed.owner_wxid = "wxid_owner_fixture"
    routed.bot_wxid = BOT_WXID
    routed._legacy_sender = lambda sender, display: "fixture-user"
    routed._v37_prompt_for_event = lambda item: item["text"]
    routed._v315_followup_prompt = lambda item, prompt: prompt
    routed._v312_quote_emoticon_ref = lambda item: None
    routed._v343_current_instruction_text = lambda item: item["text"]
    routed._cleanup_image_ref = lambda ref: None
    started = []
    routed._v323_start_image_edit = (
        lambda item, image_ref, reference_source="adjacent", instruction_override=None: started.append(
            (item["local_id"], image_ref.local_id, reference_source)
        )
    )
    routed._run_reply_job = lambda work, queued_at: (_ for _ in ()).throw(
        AssertionError("quoted image edit must bypass normal Vision/Hermes reply")
    )
    routed_event = dict(event)
    routed_event.update(
        {
            "sender_username": "wxid_user_fixture",
            "sender_display": "fixture-user",
            "timestamp": 0,
        }
    )
    GroupWorker._answer(routed, routed_event)
    assert started == [(701, 700, "quoted")]

    print("V354_GENERATED_IMAGE_QUOTE_EDIT=PASS")


if __name__ == "__main__":
    main()
