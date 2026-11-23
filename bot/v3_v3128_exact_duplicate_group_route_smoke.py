#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import sqlite3
import tempfile
from pathlib import Path

from chatwarden import core
from chatwarden.db_reader import MessageDb


TITLE = "测试の游乐园🍺🍻🍹🥂🍸🥃"
RENAMED = "测试の游乐园"
STALE_DETACHED = "示例派对の游乐园Demo🍯"
OLD = "90000000013@chatroom"
NEW = "90000000001@chatroom"
LOGICAL = TITLE


def build_route() -> dict:
    with tempfile.TemporaryDirectory(prefix="v3128-route-") as tmp:
        root = Path(tmp)
        contact = root / "contact.db"
        session = root / "session.db"

        with contextlib.closing(sqlite3.connect(contact)) as conn, conn:
            conn.execute(
                "CREATE TABLE contact ("
                "username TEXT, delete_flag INTEGER, nick_name TEXT)"
            )
            conn.executemany(
                "INSERT INTO contact VALUES (?, 0, ?)",
                [(OLD, TITLE), (NEW, TITLE)],
            )

        with contextlib.closing(sqlite3.connect(session)) as conn, conn:
            conn.execute(
                "CREATE TABLE SessionTable ("
                "username TEXT, unread_count INTEGER, summary TEXT, "
                "last_sender_display_name TEXT, last_timestamp INTEGER, "
                "last_msg_locald_id INTEGER, last_msg_type INTEGER, "
                "last_msg_sub_type INTEGER, status INTEGER, is_hidden INTEGER)"
            )
            conn.executemany(
                "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                [
                    (OLD, 346, "", "妮蝶", 1, 41806, 47, 0, 1),
                    (NEW, 100, "", "l.", 2, 100, 1, 0, 0),
                ],
            )

        db = MessageDb.__new__(MessageDb)
        db._v310_session_db_path = lambda: str(session)
        db._v333_contact_db_path = lambda: str(contact)
        db.decompress_content = lambda value, _kind: value
        return db.v3128_group_ui_route(NEW, TITLE)


def main() -> None:
    route = build_route()
    assert route["duplicate_title"] is True, route
    assert route["chat_username"] == NEW, route
    assert route["same_title_usernames"] == [NEW, OLD], route
    assert route["unread_count"] == 100, route
    assert route["last_sender_display_name"] == "l.", route

    new_item = f"{TITLE} 100 unread message(s) l.: 这么多结果 没我的份 02:57"
    old_item = f"{TITLE} 346 unread message(s) [You were mentioned] 妮蝶: [Sticker] 14:54"
    assert core._group_chat_item_fingerprint_matches(
        new_item, TITLE, route
    ) is True
    assert core._group_chat_item_fingerprint_matches(
        old_item, TITLE, route
    ) is False

    unique_route = dict(route)
    unique_route["duplicate_title"] = False
    unique_route["same_title_usernames"] = [NEW]
    assert core._group_chat_item_fingerprint_matches(
        TITLE, TITLE, unique_route
    ) is True

    original_provider = core._GROUP_EXACT_ROUTE_PROVIDER
    original_activate_id = core.activate_window_id
    original_exact_ensure = core.ensure_group_window_exact
    original_activate_named = core.activate_named_window
    calls = []
    window_titles = {"777": TITLE, "888": STALE_DETACHED}
    ensure_result = {"value": "777"}

    def provider(logical, current):
        assert logical == LOGICAL
        assert current == TITLE
        return dict(route)

    def activate_id(wid, title, required=False):
        calls.append(("activate-id", str(wid), title, required))
        if window_titles.get(str(wid)) == title:
            return str(wid)
        return None

    def exact_ensure(title, supplied_route, timeout=12.0):
        calls.append(("exact-ensure", title, supplied_route["chat_username"], timeout))
        return ensure_result["value"]

    def forbidden_title_activation(title, required=False):
        raise AssertionError(
            f"duplicate-title route must not fall back to title-only activation: {title!r}"
        )

    core.clear_group_ui_title(LOGICAL)
    core.update_group_ui_title(LOGICAL, TITLE)
    core.set_group_exact_route_provider(provider)
    core.activate_window_id = activate_id
    core.ensure_group_window_exact = exact_ensure
    core.activate_named_window = forbidden_title_activation
    try:
        title, wid = core.resolve_group_send_title(LOGICAL)
        assert (title, wid) == (TITLE, "777")
        assert calls == [("exact-ensure", TITLE, NEW, 12.0)], calls

        calls.clear()
        title, wid = core.resolve_group_send_title(LOGICAL)
        assert (title, wid) == (TITLE, "777")
        assert calls == [("activate-id", "777", TITLE, False)], calls

        # v3.129: exact chatroom routing is now the default for every
        # configured group, not only duplicate-title sessions. A later rename
        # must therefore stay on the exact-ID selector instead of regressing to
        # the title-only open path.
        core.clear_group_ui_title(LOGICAL)
        core.update_group_ui_title(LOGICAL, TITLE)

        def unique_provider(logical, current):
            assert logical == LOGICAL
            assert current == TITLE
            return dict(unique_route)

        core.set_group_exact_route_provider(unique_provider)
        calls.clear()
        title, wid = core.resolve_group_send_title(LOGICAL)
        assert (title, wid) == (TITLE, "777")
        assert calls == [("exact-ensure", TITLE, NEW, 12.0)], calls

        # v3.154: a DB title rename must not discard an exact window id that
        # was already proven to belong to this chatroom. The detached WeChat
        # window can legitimately keep its old X11 title for a while.
        core.update_group_ui_title(LOGICAL, RENAMED)

        def renamed_provider(logical, current):
            assert logical == LOGICAL
            assert current == RENAMED
            return dict(unique_route)

        core.set_group_exact_route_provider(renamed_provider)
        calls.clear()
        title, wid = core.resolve_group_send_title(LOGICAL)
        assert (title, wid) == (TITLE, "777")
        assert calls == [
            ("activate-id", "777", RENAMED, False),
            ("activate-id", "777", TITLE, False),
        ], calls

        # Restart-like recovery: there is no in-memory exact-window cache, but
        # exact-item selection focuses the already-open stale-title detached
        # window. The helper may therefore return the proven window id plus its
        # real current title, which becomes the transition send title.
        core.clear_group_ui_title(LOGICAL)
        core.update_group_ui_title(LOGICAL, RENAMED)
        ensure_result["value"] = ("888", STALE_DETACHED)
        calls.clear()
        title, wid = core.resolve_group_send_title(LOGICAL)
        assert (title, wid) == (STALE_DETACHED, "888")
        assert calls == [("exact-ensure", RENAMED, NEW, 12.0)], calls
        assert core.group_ui_title_candidates(LOGICAL) == [
            RENAMED,
            STALE_DETACHED,
        ]

        calls.clear()
        title, wid = core.resolve_group_send_title(LOGICAL)
        assert (title, wid) == (STALE_DETACHED, "888")
        assert calls == [
            ("activate-id", "888", RENAMED, False),
            ("activate-id", "888", STALE_DETACHED, False),
        ], calls
    finally:
        core.activate_window_id = original_activate_id
        core.ensure_group_window_exact = original_exact_ensure
        core.activate_named_window = original_activate_named
        core.set_group_exact_route_provider(original_provider)
        core.clear_group_ui_title(LOGICAL)

    print("V3128_DUPLICATE_TITLE_EXACT_CHATROOM_ROUTE=PASS")
    print("V3128_UI_FINGERPRINT_FAILS_CLOSED_ON_OLD_GROUP=PASS")
    print("V3128_EXACT_WINDOW_ID_CACHE=PASS")
    print("V3129_UNIQUE_TITLE_STAYS_ON_EXACT_CHATROOM_ROUTE=PASS")
    print("V3154_EXACT_RENAME_CACHE_PRESERVED=PASS")
    print("V3154_RESTART_STALE_TITLE_BINDING=PASS")


if __name__ == "__main__":
    main()
