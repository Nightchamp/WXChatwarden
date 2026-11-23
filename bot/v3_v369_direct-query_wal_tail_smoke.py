#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden.db_reader import MessageDb
from smoke_backend import StubBackend


class _Cache:
    def __init__(self, path: str):
        self.path = path
        self.calls = 0

    def get(self, _rel_key: str):
        self.calls += 1
        return self.path


def _make_plaintext_db(path: Path, table: str) -> None:
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        con.execute(
            f"CREATE TABLE [{table}] ("
            "local_id INTEGER, server_id INTEGER, local_type INTEGER, "
            "create_time INTEGER, real_sender_id INTEGER, "
            "message_content BLOB, WCDB_CT_message_content INTEGER, "
            "source BLOB, WCDB_CT_source INTEGER)"
        )
        con.execute("CREATE TABLE Name2Id (user_name TEXT)")
        con.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (?, ?)", (7, "wxid_user"))
        con.execute(
            f"INSERT INTO [{table}] VALUES (?,?,?,?,?,?,?,?,?)",
            (13757, 42, 3, 123, 7, b"hello", 0, b"", 0),
        )


def _make_newer_wal_marker(root: Path) -> None:
    encrypted = root / "message" / "message_0.db"
    encrypted.parent.mkdir(parents=True)
    encrypted.write_bytes(b"db")
    Path(str(encrypted) + "-wal").write_bytes(b"w" * 64)


def events_after_wal_tail_smoke() -> None:
    table = "Msg_0123456789abcdef0123456789abcdef"
    with tempfile.TemporaryDirectory(prefix="v369-wal-tail-") as tmp:
        root = Path(tmp)
        path = root / "plaintext.db"
        _make_plaintext_db(path, table)
        _make_newer_wal_marker(root)

        db = MessageDb.__new__(MessageDb)
        db.direct_query_enabled = True
        db.cache_lock = threading.RLock()
        db.cache = _Cache(str(path))
        db.db_dir = root
        db.backend = StubBackend(cache=db.cache, db_dir=root, direct_query=lambda *_args, **_kwargs: [])
        db._display = lambda value: value
        # Reproduce production: encrypted-main direct SQL succeeds but sees no
        # row because the newest message exists only in the WAL tail.  The
        # v3.69 chooser must avoid accepting that stale successful result.
        db.decompress_content = lambda content, _ct: content.decode("utf-8")
        db.format_message_text = lambda *_args, **_kwargs: ("wxid_user", "hello")
        db.format_msg_type = lambda local_type: "图片" if int(local_type) == 3 else "文本"
        db.group_member_display = lambda *_args, **_kwargs: "User"
        db._v313_mentions_from_source = lambda *_args, **_kwargs: ()

        snapshot = {
            "names": {},
            "dbs": [
                {
                    "rel_key": "message/message_0.db",
                    "path": str(path),
                    "tables": {table},
                    "id_to_username": {7: "wxid_user"},
                }
            ],
        }
        resolved = {
            "table_name": table,
            "chat_username": "room@chatroom",
            "chat_name": "room",
        }

        events = db.events_after(resolved, 13756, snapshot)
        assert [event["local_id"] for event in events] == [13757], events
        assert db.cache.calls == 1, db.cache.calls

    print("V369_DIRECT_QUERY_WAL_TAIL_EVENTS=PASS")


def quoted_image_wal_tail_smoke() -> None:
    table = "Msg_0123456789abcdef0123456789abcdef"
    with tempfile.TemporaryDirectory(prefix="v369-wal-quote-") as tmp:
        root = Path(tmp)
        path = root / "plaintext.db"
        _make_plaintext_db(path, table)
        _make_newer_wal_marker(root)

        db = MessageDb.__new__(MessageDb)
        db.direct_query_enabled = True
        db.cache_lock = threading.RLock()
        db.cache = _Cache(str(path))
        db.db_dir = root
        db.backend = StubBackend(cache=db.cache, db_dir=root, direct_query=lambda *_args, **_kwargs: [])
        db.numeric_msg_keys = lambda: ["message/message_0.db"]
        # Same production failure mode for the quoted-image stale-snapshot
        # fallback: the direct encrypted-main query returns an empty result.

        event = db.v354_direct_group_image_by_server_id(
            {
                "table_name": table,
                "chat_name": "room",
                "chat_username": "room@chatroom",
            },
            42,
        )
        assert event is not None, event
        assert event["local_id"] == 13757, event
        assert event["server_id"] == 42, event
        assert event["local_type"] == 3, event
        # Image-row lookup plus Name2Id lookup both stay on the same
        # WAL-aware cache rather than consulting the stale encrypted main DB.
        assert db.cache.calls == 2, db.cache.calls

    print("V369_DIRECT_QUERY_WAL_TAIL_QUOTED_IMAGE=PASS")


def main() -> None:
    events_after_wal_tail_smoke()
    quoted_image_wal_tail_smoke()

    print("V369_DIRECT_QUERY_WAL_TAIL=PASS")


if __name__ == "__main__":
    main()
