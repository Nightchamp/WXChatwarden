#!/usr/bin/env python3
"""v3.202: private-session poll memo and single-query group membership.

Offline fixtures only (plain SQLite files, no WeChat DB, no network).
"""
from __future__ import annotations

import os
import contextlib
import sqlite3
import tempfile
import time
from pathlib import Path

from chatwarden import db_reader
from chatwarden.db_reader import MessageDb


def _session_db(path):
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE SessionTable (username TEXT, unread_count INT, "
            "summary TEXT, last_timestamp INT, last_msg_type INT, "
            "last_msg_sender TEXT)"
        )
        conn.executemany(
            "INSERT INTO SessionTable VALUES (?,?,?,?,?,?)",
            [
                ("wxid_alice", 1, "hi", 100, 1, "wxid_alice"),
                ("wxid_me", 0, "self", 90, 1, "wxid_me"),
                ("123@chatroom", 0, "group", 80, 1, "wxid_bob"),
            ],
        )


def private_sessions_memo_smoke(tmp):
    path = str(tmp / "session.db")
    _session_db(path)
    db = MessageDb.__new__(MessageDb)
    db.account_wxid = "wxid_me"
    db._v310_session_db_path = lambda: path
    db.decompress_content = lambda value, kind: value
    names = {"wxid_alice": "Alice"}
    snapshot = {"names": names}

    connects = []
    real_connect = db_reader.sqlite3.connect

    def counting_connect(*args, **kwargs):
        connects.append(args[0])
        return real_connect(*args, **kwargs)

    db_reader.sqlite3.connect = counting_connect
    try:
        first = db.v310_private_sessions(snapshot)
        assert list(first) == ["wxid_alice"], first
        assert first["wxid_alice"]["display_name"] == "Alice"
        first["wxid_alice"]["display_name"] = "mutated-by-caller"
        second = db.v310_private_sessions(snapshot)
        assert len(connects) == 1, connects  # memo hit: no second query
        assert second["wxid_alice"]["display_name"] == "Alice"  # memo not mutated

        # A new names object (refresh) invalidates the memo.
        db.v310_private_sessions({"names": {"wxid_alice": "Alice 2"}})
        assert len(connects) == 2, connects

        # A changed session.db invalidates the memo.
        time.sleep(0.01)
        with contextlib.closing(real_connect(path)) as conn, conn:
            conn.execute("UPDATE SessionTable SET last_timestamp=200 WHERE username='wxid_alice'")
        os.utime(path)
        third = db.v310_private_sessions(snapshot)
        assert len(connects) == 3, connects
        assert third["wxid_alice"]["last_timestamp"] == 200
    finally:
        db_reader.sqlite3.connect = real_connect
    print("V3202_PRIVATE_SESSIONS_MEMO=PASS")


def group_members_single_query_smoke(tmp):
    path = str(tmp / "contact.db")
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE chat_room (id INT, username TEXT, ext_buffer BLOB)")
        conn.execute("CREATE TABLE chatroom_member (room_id INT, member_id INT)")
        conn.execute("CREATE TABLE contact (id INT, username TEXT)")
        conn.executemany("INSERT INTO chat_room VALUES (?,?,?)",
                         [(1, "111@chatroom", b""), (2, "222@chatroom", b""),
                          (3, "333@chatroom", b"")])
        conn.executemany("INSERT INTO contact VALUES (?,?)",
                         [(10, "wxid_a"), (11, "wxid_b"), (12, "")])
        conn.executemany("INSERT INTO chatroom_member VALUES (?,?)",
                         [(1, 10), (1, 11), (2, 11), (2, 12)])
    db = MessageDb.__new__(MessageDb)
    db._v333_contact_db_path = lambda: path
    seen = []
    original_parse = MessageDb._v333_parse_group_member_nicknames
    MessageDb._v333_parse_group_member_nicknames = classmethod(
        lambda cls, ext, members: seen.append(frozenset(members)) or {}
    )
    old_flag = db_reader._V333_GROUP_NICKNAME_ENABLED
    db_reader._V333_GROUP_NICKNAME_ENABLED = True
    try:
        result = db._v333_load_group_member_names()
    finally:
        MessageDb._v333_parse_group_member_nicknames = original_parse
        db_reader._V333_GROUP_NICKNAME_ENABLED = old_flag
    assert set(result) == {"111@chatroom", "222@chatroom", "333@chatroom"}, result
    assert db._v3171_member_ids_by_chat == {
        "111@chatroom": frozenset({"wxid_a", "wxid_b"}),
        "222@chatroom": frozenset({"wxid_b"}),
        "333@chatroom": frozenset(),
    }, db._v3171_member_ids_by_chat
    print("V3202_GROUP_MEMBERS_SINGLE_QUERY=PASS")


def main():
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        private_sessions_memo_smoke(tmp)
        group_members_single_query_smoke(tmp)


if __name__ == "__main__":
    main()
