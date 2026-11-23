#!/usr/bin/env python3
from __future__ import annotations

from chatwarden import core
from chatwarden.worker import GroupWorker


LOGICAL = "测试の游乐园🍺🍻🥂🍸🥃🍹"
CURRENT = "削肾裤监狱"


def main() -> None:
    original_activate = core.activate_named_window
    original_ensure = core.ensure_group_window
    original_send_text = core.send_text_to_chat
    original_image_send = core._image_send_attempt

    visible = {LOGICAL: "old-window"}
    activated = []
    ensured = []
    text_sends = []
    image_sends = []

    def fake_activate(title, required=False):
        activated.append((title, required))
        wid = visible.get(title)
        if required and not wid:
            raise RuntimeError(f"missing synthetic window: {title}")
        return wid

    def fake_ensure(title, timeout=12.0):
        ensured.append((title, timeout))
        visible[title] = "opened-current-window"
        return visible[title]

    def fake_send_text(title, text):
        text_sends.append((title, text))

    def fake_image_send(title, path):
        image_sends.append((title, path))
        return 0.01, "sent"

    core.clear_group_ui_title(LOGICAL)
    core.activate_named_window = fake_activate
    core.ensure_group_window = fake_ensure
    core.send_text_to_chat = fake_send_text
    core._image_send_attempt = fake_image_send

    try:
        # The stable logical identity must remain unchanged while the DB-derived
        # current UI title changes. A still-open detached window may retain the
        # immediately previous trusted title, so the transition must not break
        # sending before WeChat refreshes that window title.
        core.update_group_ui_title(LOGICAL, CURRENT)
        assert core.group_ui_title(LOGICAL) == CURRENT
        assert core.group_ui_title_candidates(LOGICAL) == [CURRENT, LOGICAL]

        core.focus_and_send(LOGICAL, "rename-transition")
        assert text_sends == [(LOGICAL, "rename-transition")], text_sends
        assert ensured == [], ensured
        assert core.looks_like_our_message(LOGICAL, "rename-transition") is True
        assert core.looks_like_our_message(CURRENT, "rename-transition") is False

        # Once the exact current title becomes visible it must win immediately,
        # and the previous title must be discarded for future sends.
        visible[CURRENT] = "current-window"
        core.focus_and_send(LOGICAL, "current-title")
        assert text_sends[-1] == (CURRENT, "current-title"), text_sends
        assert core.group_ui_title_candidates(LOGICAL) == [CURRENT]

        core.focus_and_send_image(LOGICAL, "/synthetic/reference.png")
        assert image_sends == [
            (CURRENT, "/synthetic/reference.png")
        ], image_sends

        # A live group event carries the mutable title from the same DB name
        # snapshot, so the worker can refresh send routing immediately even if
        # no config reload occurs.
        worker = GroupWorker.__new__(GroupWorker)
        worker.chat_name = LOGICAL
        worker._expire = lambda: None
        worker.bot_wxid = "wxid_bot"
        worker.owner_wxid = "wxid_owner"
        worker._v3100_handle_system_message = lambda event: None
        worker._v39_note_ordinary_human = lambda event: None
        worker.recent = {}
        worker.pending = {}
        worker.recent_text = {}
        worker.recent_app = {}
        event = {
            "chat_display_name": "削肾裤监狱二次改名",
            "sender_username": "wxid_bot",
            "text": "ignored-self-event",
            "local_type": 1,
        }
        worker._handle(event)
        assert core.group_ui_title(LOGICAL) == "削肾裤监狱二次改名"

        # If there is no visible current/previous window, only the current
        # trusted title is opened from the chat list.
        core.update_group_ui_title(LOGICAL, CURRENT)
        visible.clear()
        core.focus_and_send(LOGICAL, "open-current")
        assert ensured[-1][0] == CURRENT, ensured
        assert text_sends[-1] == (CURRENT, "open-current"), text_sends
    finally:
        core.activate_named_window = original_activate
        core.ensure_group_window = original_ensure
        core.send_text_to_chat = original_send_text
        core._image_send_attempt = original_image_send
        core.clear_group_ui_title(LOGICAL)

    print("V3124_STABLE_ID_DYNAMIC_GROUP_TITLE=PASS")
    print("V3124_RENAME_TRANSITION_SEND_FALLBACK=PASS")
    print("V3124_CURRENT_TITLE_RETIRES_PREVIOUS=PASS")
    print("V3124_EVENT_SNAPSHOT_TITLE_REFRESH=PASS")


if __name__ == "__main__":
    main()
