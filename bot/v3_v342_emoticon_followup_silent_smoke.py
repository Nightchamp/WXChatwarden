from types import SimpleNamespace

from chatwarden.worker import GroupWorker, PendingTrigger


def _worker(*, followup_active=False, pending=False):
    worker = object.__new__(GroupWorker)
    worker.chat_name = "repro"
    worker.recent_text = {}
    worker.recent = {}
    worker.pending = {}
    worker.answers = []
    worker.ordinary_notes = []

    worker._cleanup_image_ref = lambda ref: None
    worker._followup_state = (
        (lambda sender: {"active": True})
        if followup_active
        else (lambda sender: None)
    )
    worker._v312_decode_emoticon_ref = lambda event: SimpleNamespace(
        path="/tmp/fake-sticker.webp",
        sender=str(event.get("sender_username") or ""),
    )
    worker._answer = lambda event, image_ref=None: worker.answers.append(
        (dict(event), image_ref)
    )
    worker._v39_note_ordinary_human = lambda event: worker.ordinary_notes.append(
        dict(event)
    )

    if pending:
        worker.pending["wxid_tester"] = PendingTrigger(
            event={
                "local_id": 6722,
                "sender_username": "wxid_tester",
                "text": "@Owner-flash-JARVIS 看下一张图",
            },
            created=0.0,
        )

    return worker


def _event():
    return {
        "local_id": 6723,
        "sender_username": "wxid_tester",
        "sender_display": "tester",
        "local_type": 47,
        "msg_type": "表情",
        "text": "[表情]",
    }


def main():
    # Baseline: an ordinary sticker is context/source-cache only.
    ordinary = _worker()
    ordinary._handle_emoticon(_event())
    assert ordinary.answers == []
    assert "wxid_tester" in ordinary.recent

    # Regression: an active same-sender follow-up lease must NOT upgrade an
    # otherwise ordinary Type47 sticker into Vision/reply.
    followup = _worker(followup_active=True)
    followup._handle_emoticon(_event())
    assert followup.answers == [], (
        "active follow-up lease upgraded ordinary Type47 to reply"
    )
    assert "wxid_tester" in followup.recent

    # Explicit pending media intent remains allowed to consume the sticker.
    explicit = _worker(pending=True)
    explicit._handle_emoticon(_event())
    assert len(explicit.answers) == 1
    answered_event, image_ref = explicit.answers[0]
    assert answered_event["local_id"] == 6722
    assert image_ref.path == "/tmp/fake-sticker.webp"

    print("V342_EMOTICON_FOLLOWUP_SILENT=PASS")


if __name__ == "__main__":
    main()
