#!/usr/bin/env python3
"""v3.206 stale NEXT-image waits; offline, no cloud or sends.

- an explicit @ from the same sender supersedes an older NEXT-image wait, so
  the stale wait can't expire afterwards with "我还没收到你说的那张图";
- a wait opened by a message that quotes an existing image expires silently;
- a plain wait with no image still gets the notice (legacy behaviour).
"""
from chatwarden import core
from chatwarden.worker import GroupWorker

SENDER = "wxid_demo_alice"
NOTICE = "我还没收到你说的那张图。"


def _worker(explicit_ids, quote_meta_by_id=None):
    quote_meta_by_id = quote_meta_by_id or {}
    worker = object.__new__(GroupWorker)
    worker.chat_name = "demo-group"
    worker.bot_wxid = "wxid_demo_bot"
    worker.pending_image_edit = {}
    worker.pending = {}
    worker.recent = {}
    worker.recent_text = {}
    worker.recent_app = {}
    worker.answers = []
    worker.sent = []
    worker.image_max_age = 15.0
    worker.adjacent_quote_max_age = 15.0
    worker.pending_wait = 8.0
    worker.profiles = {}

    worker._v313_true_mention = lambda event: event["local_id"] in explicit_ids
    worker._v37_quote_metadata = lambda event: (
        quote_meta_by_id.get(event["local_id"]), None)
    worker._cleanup_image_ref = lambda ref: None
    worker._v396_previous_vision_image_ref = lambda event: None
    worker._v3118_match_adjacent_history_request = lambda event: False
    worker._match_adjacent_app_context = lambda event: None
    worker._match_adjacent_quoted_image = lambda event: None
    worker._consume_queued_forward_image = lambda event: None
    worker._followup_prune = lambda now: None
    worker._v396_prune_last_vision = lambda now: None
    worker._send_fixed = lambda text, *a, **k: worker.sent.append(text)
    worker._answer = lambda event, image_ref=None: worker.answers.append(
        dict(event))
    return worker


def _plain(local_id, text):
    return {
        "local_id": local_id,
        "sender_username": SENDER,
        "sender_display": "Alice",
        "local_type": 1,
        "msg_type": "文本",
        "text": text,
        "raw_content": text,
    }


def _quoting_image(local_id, title):
    return {
        "local_id": local_id,
        "sender_username": SENDER,
        "sender_display": "Alice",
        "local_type": (57 << 32) | 49,
        "msg_type": "文本",
        "text": f"{title}\n  ↳ 回复 Bob: [图片]",
        "raw_content": (
            "<msg><appmsg>"
            f"<title>{title}</title>"
            "<refermsg>"
            "<type>3</type><svrid>7</svrid>"
            "<fromusr>wxid_demo_bob</fromusr>"
            "<chatusr>wxid_demo_bob</chatusr>"
            "<displayname>Bob</displayname>"
            "<content>demo</content>"
            "</refermsg>"
            "</appmsg></msg>"
        ),
    }


def _age_pending(worker):
    for pending in worker.pending.values():
        pending.created -= worker.pending_wait + 1.0


def main():
    logs = []
    original_log = core.log
    core.log = lambda *a, **k: logs.append(" ".join(str(x) for x in a))
    try:
        # Incident shape: a wait is open, then the same sender re-sends the
        # request with @ and a quoted image. The new request is answered and
        # the old wait is gone, so no "never arrived" notice follows.
        worker = _worker({2, 3}, {3: {"svrid": 7, "refer_type": 3}})
        worker._handle_text(_plain(2, "@bot 看看这张图"))
        assert SENDER in worker.pending
        worker._handle_text(_quoting_image(3, "把图中人物的衣服改为黑色 @bot"))
        assert worker.pending == {}, worker.pending
        assert [a["local_id"] for a in worker.answers] == [3]
        worker._expire()
        assert worker.sent == [], worker.sent
        assert any("[v3.206-pending-superseded]" in line
                   and "old_local_id=2" in line for line in logs), logs
        print("V3206_EXPLICIT_SUPERSEDES_PENDING=PASS")

        # A wait opened by a message that itself quotes an image (quote
        # metadata unavailable) expires without the misleading notice.
        logs.clear()
        worker = _worker({4})
        worker._handle_text(_quoting_image(4, "@bot 把图中人物的衣服改为黑色"))
        assert worker.pending[SENDER].event.get("_v3206_quoted_image") is True
        _age_pending(worker)
        worker._expire()
        assert worker.pending == {}
        assert worker.sent == [], worker.sent
        assert any("[v3.206-pending-expired]" in line for line in logs), logs
        print("V3206_QUOTED_IMAGE_EXPIRY_SILENT=PASS")

        # Legacy: a plain wait with no image still tells the sender.
        worker = _worker({5})
        worker._handle_text(_plain(5, "@bot 看看这张图"))
        assert "_v3206_quoted_image" not in worker.pending[SENDER].event
        _age_pending(worker)
        worker._expire()
        assert worker.sent == [NOTICE], worker.sent
        print("V3206_PLAIN_EXPIRY_NOTICE=PASS")
    finally:
        core.log = original_log
    print("V3206_PENDING_IMAGE_SUPERSEDE_SMOKE=PASS")


if __name__ == "__main__":
    main()
