from chatwarden.worker import GroupWorker


def _worker():
    worker = object.__new__(GroupWorker)
    worker.chat_name = "repro"
    worker.pending_image_edit = {}
    worker.pending = {}
    worker.recent = {}
    worker.recent_text = {}
    worker.recent_app = {}
    worker.answers = []
    worker.image_max_age = 15.0
    worker.pending_wait = 15.0
    worker.profiles = {}

    worker._v313_true_mention = lambda event: True
    worker._v37_quote_metadata = lambda event: (
        {
            "svrid": 1,
            "refer_type": 49,
        },
        None,
    )
    worker._cleanup_image_ref = lambda ref: None
    worker._answer = lambda event, image_ref=None: worker.answers.append(
        (dict(event), image_ref)
    )
    return worker


def _event(title="@演示bot 给我骂demo_user"):
    return {
        "local_id": 1023,
        "sender_username": "wxid_tester",
        "sender_display": "tester",
        "local_type": (1 << 32) | 49,
        "msg_type": "文本",
        "text": (
            "@演示bot 给我骂demo_user\n"
            "  ↳ 回复 Owner: Owner与demo_user的聊天记录"
            "Owner: [图片]\n"
            "Owner: 怎么现在瑞幸上都是耄耋\n"
            "demo_user: 你有一部分原因"
        ),
        "raw_content": (
            "<msg><appmsg>"
            f"<title>{title}</title>"
            "<refermsg>"
            "<type>49</type><svrid>1</svrid>"
            "<fromusr>fixture@chatroom</fromusr>"
            "<content>&lt;msg&gt;&lt;appmsg&gt;"
            "&lt;type&gt;19&lt;/type&gt;"
            "&lt;des&gt;Owner: [图片]&lt;/des&gt;"
            "&lt;/appmsg&gt;&lt;/msg&gt;</content>"
            "</refermsg>"
            "</appmsg></msg>"
        ),
    }


def main():
    worker = _worker()
    worker._handle_text(_event())

    assert len(worker.answers) == 1, (
        "quoted Type49 preview leaked [图片] into current media intent and "
        "suppressed the explicit reply"
    )
    assert worker.pending == {}, (
        "quoted historical [图片] incorrectly opened next-image pending"
    )

    answered_event, image_ref = worker.answers[0]
    assert answered_event["local_id"] == 1023
    assert image_ref is None

    # Current-instruction media intent still works. Only the historical
    # refermsg preview is excluded from routing decisions.
    media_worker = _worker()
    media_event = _event("@演示bot 看下一张图片")
    media_event["text"] = (
        "@演示bot 看下一张图片\n"
        "  ↳ 回复 Owner: 历史记录里也有 [图片]"
    )
    media_worker._handle_text(media_event)
    assert media_worker.answers == []
    assert "wxid_tester" in media_worker.pending

    print("V343_TYPE49_QUOTE_MEDIA_INTENT=PASS")


if __name__ == "__main__":
    main()
