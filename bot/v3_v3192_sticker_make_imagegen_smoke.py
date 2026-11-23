from chatwarden.worker import GroupWorker, _v3192_sticker_make_target


def _worker(quote_meta=None):
    worker = object.__new__(GroupWorker)
    worker.chat_name = "repro"
    worker.bot_wxid = "wxid_bot"
    worker.pending_image_edit = {}
    worker.pending = {}
    worker.recent = {}
    worker.recent_text = {}
    worker.recent_app = {}
    worker.answers = []
    worker.image_max_age = 15.0
    worker.pending_wait = 8.0
    worker.profiles = {}

    worker._v313_true_mention = lambda event: True
    worker._v37_quote_metadata = lambda event: (quote_meta, None)
    worker._cleanup_image_ref = lambda ref: None
    worker._v396_previous_vision_image_ref = lambda event: None
    worker._v3118_match_adjacent_history_request = lambda event: False
    worker._match_adjacent_app_context = lambda event: None
    worker._match_adjacent_quoted_image = lambda event: None
    worker._consume_queued_forward_image = lambda event: None
    worker._answer = lambda event, image_ref=None: worker.answers.append(
        (dict(event), image_ref)
    )
    return worker


def _quoted_text_event(title, quoted="打死我--变态 掐死我--好 来"):
    return {
        "local_id": 131934,
        "sender_username": "wxid_tester",
        "sender_display": "tester",
        "local_type": (57 << 32) | 49,
        "msg_type": "文本",
        "text": f"{title}\n  ↳ 回复 美子: {quoted}",
        "raw_content": (
            "<msg><appmsg>"
            f"<title>{title}</title>"
            "<refermsg>"
            "<type>1</type><svrid>7</svrid>"
            "<fromusr>wxid_other</fromusr>"
            "<displayname>美子</displayname>"
            f"<content>{quoted}</content>"
            "</refermsg>"
            "</appmsg></msg>"
        ),
    }


def _plain_event(text):
    return {
        "local_id": 131922,
        "sender_username": "wxid_tester",
        "sender_display": "tester",
        "local_type": 1,
        "msg_type": "文本",
        "text": text,
        "raw_content": text,
    }


def main():
    # Predicate: make/convert-into-sticker wording is an output target.
    for text in (
        "@Warden-Demo-Bot00 把这两段文字转成适合表情包的图片",
        "@Warden-Demo-Bot00 把这个做成表情包",
        "把这个文字做成表情包@Warden-Demo-Bot00",
        "帮我生成一张猫猫表情包",
        "做个表情包",
    ):
        assert _v3192_sticker_make_target(text), text
    # Deictic sticker references keep the legacy NEXT-sticker wait.
    for text in (
        "@bot 看看这个表情",
        "@bot 下一个表情是什么意思",
        "@bot 这个贴纸好可爱",
        "@bot 表情包",
    ):
        assert not _v3192_sticker_make_target(text), text

    # Incident shape: quoted plain text + "转成适合表情包的图片" must not wait.
    for title in (
        "@Warden-Demo-Bot00 把这两段文字转成适合表情包的图片",
        "@Warden-Demo-Bot00 把这个做成表情包",
        "把这个文字做成表情包@Warden-Demo-Bot00",
    ):
        worker = _worker({"svrid": 7, "refer_type": 1})
        worker._handle_text(_quoted_text_event(title))
        assert worker.pending == {}, (title, "opened NEXT-sticker wait")
        assert len(worker.answers) == 1, title
        assert worker.answers[0][1] is None

    # Unquoted make-sticker request also answers instead of waiting.
    worker = _worker()
    worker._handle_text(_plain_event("把这个做成表情包@Warden-Demo-Bot00"))
    assert worker.pending == {}
    assert len(worker.answers) == 1

    # Legacy deictic sticker question still waits for the next sticker.
    worker = _worker()
    worker._handle_text(_plain_event("@Warden-Demo-Bot00 看看这个表情"))
    assert "wxid_tester" in worker.pending
    assert worker.answers == []

    # Quoted Type47 sticker + make wording still answers immediately.
    worker = _worker({"svrid": 7, "refer_type": 47})
    event = _quoted_text_event("@Warden-Demo-Bot00 把这个做成表情包")
    event["raw_content"] = event["raw_content"].replace(
        "<type>1</type>", "<type>47</type>"
    )
    worker._handle_text(event)
    assert worker.pending == {}
    assert len(worker.answers) == 1

    # the decision service imagegen prompt carries the exact quoted plain text.
    worker = _worker()
    prompt = worker._v3192_imagegen_prompt_with_quoted_text(
        _quoted_text_event("@Warden-Demo-Bot00 把这两段文字转成适合表情包的图片"),
        "把这两段文字转成适合表情包的图片",
    )
    assert prompt.startswith("把这两段文字转成适合表情包的图片")
    assert "打死我--变态 掐死我--好 来" in prompt
    # Non-text quotes and unquoted messages are unchanged.
    media_event = _quoted_text_event("@bot 画一张")
    media_event["raw_content"] = media_event["raw_content"].replace(
        "<type>1</type>", "<type>3</type>"
    )
    assert worker._v3192_imagegen_prompt_with_quoted_text(media_event, "画一张") == "画一张"
    assert worker._v3192_imagegen_prompt_with_quoted_text(
        _plain_event("画一只猫"), "画一只猫"
    ) == "画一只猫"

    print("V3192_STICKER_MAKE_IMAGEGEN=PASS")


if __name__ == "__main__":
    main()
