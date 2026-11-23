#!/usr/bin/env python3
"""Replay a JSONL transcript into a plain SQLite store the bot can read.

This is the reference "message source" for development and demos: it writes
ordinary, unencrypted SQLite files in the table layout the bot expects (see
docs/ARCHITECTURE.md, "Store layout"). Point ``CW_PLAIN_DB_DIR`` at the store
and the bot treats appended lines as incoming messages.

  replay_jsonl.py init   STORE --self wxid_bot
  replay_jsonl.py append STORE transcript.jsonl [--pace 1.5]
  replay_jsonl.py say    STORE --chat 123@chatroom --sender wxid_alice "hello"

One JSON object per line::

  {"chat": "20000000001@chatroom", "chat_name": "Demo group",
   "sender": "wxid_alice", "sender_name": "Alice", "text": "@bot hi"}

``chat`` is a group id (ends with ``@chatroom``) or a contact id for a private
chat. Optional keys: ``type`` (default 1, plain text), ``time`` (unix seconds,
default now), ``owner`` (group owner id, used when the group is first seen).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

MESSAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS Name2Id(user_name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS TimeStamp(timestamp INTEGER);
"""
MSG_TABLE = """
CREATE TABLE IF NOT EXISTS [{t}](
  local_id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER,
  local_type INTEGER, sort_seq INTEGER, real_sender_id INTEGER,
  create_time INTEGER, status INTEGER, upload_status INTEGER,
  download_status INTEGER, server_seq INTEGER, origin_source INTEGER,
  source TEXT, message_content TEXT, compress_content TEXT,
  packed_info_data BLOB, WCDB_CT_message_content INTEGER DEFAULT NULL,
  WCDB_CT_source INTEGER DEFAULT NULL);
CREATE INDEX IF NOT EXISTS [{t}_SORTSEQ] ON [{t}](sort_seq);
"""
CONTACT_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact(
  id INTEGER PRIMARY KEY, username TEXT, local_type INTEGER, alias TEXT,
  encrypt_username TEXT, flag INTEGER, delete_flag INTEGER,
  verify_flag INTEGER, remark TEXT, remark_quan_pin TEXT,
  remark_pin_yin_initial TEXT, nick_name TEXT, pin_yin_initial TEXT,
  quan_pin TEXT, big_head_url TEXT, small_head_url TEXT, head_img_md5 TEXT,
  chat_room_notify INTEGER, is_in_chat_room INTEGER, description TEXT,
  extra_buffer BLOB, chat_room_type INTEGER);
CREATE TABLE IF NOT EXISTS chat_room(
  id INTEGER PRIMARY KEY, username TEXT, owner TEXT, ext_buffer BLOB);
CREATE TABLE IF NOT EXISTS chatroom_member(
  room_id INTEGER, member_id INTEGER,
  CONSTRAINT room_member UNIQUE(room_id, member_id));
"""
SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS SessionTable(
  username TEXT PRIMARY KEY, type INTEGER, unread_count INTEGER,
  unread_first_msg_srv_id INTEGER, unread_first_pat_msg_local_id INTEGER,
  unread_first_pat_msg_sort_seq INTEGER, is_hidden INTEGER, summary TEXT,
  draft TEXT, status INTEGER, last_timestamp INTEGER, sort_timestamp INTEGER,
  last_clear_unread_timestamp INTEGER, last_msg_locald_id INTEGER,
  last_msg_type INTEGER, last_msg_sub_type INTEGER, last_msg_sender TEXT,
  last_sender_display_name TEXT, last_msg_ext_type INTEGER);
"""


def _open(path, schema):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(schema)
    return conn


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.msg = _open(self.root / "message" / "message_0.db", MESSAGE_SCHEMA)
        self.contact = _open(self.root / "contact" / "contact.db", CONTACT_SCHEMA)
        self.session = _open(self.root / "session" / "session.db", SESSION_SCHEMA)

    def close(self):
        for conn in (self.msg, self.contact, self.session):
            conn.commit()
            conn.close()

    def name_id(self, username):
        self.msg.execute("INSERT OR IGNORE INTO Name2Id(user_name) VALUES (?)", (username,))
        return self.msg.execute(
            "SELECT rowid FROM Name2Id WHERE user_name=?", (username,)
        ).fetchone()[0]

    def contact_id(self, username, nick_name=""):
        row = self.contact.execute(
            "SELECT id, nick_name FROM contact WHERE username=?", (username,)
        ).fetchone()
        if row:
            if nick_name and nick_name != (row[1] or ""):
                self.contact.execute(
                    "UPDATE contact SET nick_name=? WHERE id=?", (nick_name, row[0])
                )
            return row[0]
        local_type = 2 if username.endswith("@chatroom") else 1
        cur = self.contact.execute(
            "INSERT INTO contact(username, local_type, nick_name, remark, alias, "
            "delete_flag, verify_flag, flag) VALUES (?,?,?,'','',0,0,0)",
            (username, local_type, nick_name or username),
        )
        return cur.lastrowid

    def ensure_room(self, room, room_name, owner, members):
        room_id = self.contact_id(room, room_name)
        if not self.contact.execute(
            "SELECT 1 FROM chat_room WHERE username=?", (room,)
        ).fetchone():
            self.contact.execute(
                "INSERT INTO chat_room(id, username, owner) VALUES (?,?,?)",
                (room_id, room, owner or ""),
            )
        for member in members:
            self.contact.execute(
                "INSERT OR IGNORE INTO chatroom_member(room_id, member_id) VALUES (?,?)",
                (room_id, self.contact_id(member)),
            )

    def add(self, item, self_id=""):
        chat = str(item["chat"])
        sender = str(item.get("sender") or chat)
        text = str(item.get("text") or "")
        local_type = int(item.get("type") or 1)
        now = int(item.get("time") or time.time())
        is_group = chat.endswith("@chatroom")
        self.contact_id(sender, str(item.get("sender_name") or ""))
        if is_group:
            members = [sender] + ([self_id] if self_id else [])
            self.ensure_room(chat, str(item.get("chat_name") or ""), item.get("owner"), members)
        else:
            self.contact_id(chat, str(item.get("chat_name") or item.get("sender_name") or ""))
        table = "Msg_" + hashlib.md5(chat.encode("utf-8")).hexdigest()
        self.msg.executescript(MSG_TABLE.format(t=table))
        self.name_id(chat)
        sender_id = self.name_id(sender)
        content = f"{sender}:\n{text}" if is_group and sender != self_id else text
        seq = time.time_ns() // 1000
        cur = self.msg.execute(
            f"INSERT INTO [{table}](server_id, local_type, sort_seq, real_sender_id, "
            "create_time, status, upload_status, download_status, server_seq, "
            "origin_source, source, message_content) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (seq, local_type, now * 1000, sender_id, now, 3, 0, 0, seq, 0, "", content),
        )
        self.session.execute(
            "INSERT INTO SessionTable(username, type, unread_count, is_hidden, summary, "
            "status, last_timestamp, sort_timestamp, last_msg_locald_id, last_msg_type, "
            "last_msg_sub_type, last_msg_sender, last_sender_display_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
            "unread_count=unread_count+1, summary=excluded.summary, "
            "last_timestamp=excluded.last_timestamp, sort_timestamp=excluded.sort_timestamp, "
            "last_msg_locald_id=excluded.last_msg_locald_id, "
            "last_msg_type=excluded.last_msg_type, last_msg_sender=excluded.last_msg_sender, "
            "last_sender_display_name=excluded.last_sender_display_name",
            (chat, 0, 1, 0, text[:80], 0, now, now, cur.lastrowid, local_type, 0, sender,
             str(item.get("sender_name") or "")),
        )
        for conn in (self.msg, self.contact, self.session):
            conn.commit()
        return cur.lastrowid


def _self_id(root):
    path = Path(root) / "self.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("store")
    p.add_argument("--self", dest="self_id", required=True, help="the bot account id")
    p.add_argument("--self-name", default="bot")
    p = sub.add_parser("append")
    p.add_argument("store")
    p.add_argument("jsonl")
    p.add_argument("--pace", type=float, default=0.0, help="seconds between lines")
    p = sub.add_parser("say")
    p.add_argument("store")
    p.add_argument("--chat", required=True)
    p.add_argument("--sender", required=True)
    p.add_argument("--sender-name", default="")
    p.add_argument("--chat-name", default="")
    p.add_argument("text")
    args = parser.parse_args(argv)

    store = Store(args.store)
    try:
        if args.cmd == "init":
            (Path(args.store) / "self.txt").write_text(args.self_id + "\n", encoding="utf-8")
            store.contact_id(args.self_id, args.self_name)
            store.name_id(args.self_id)
            print(f"store ready: {args.store}")
            return 0
        self_id = _self_id(args.store)
        if args.cmd == "say":
            local_id = store.add({
                "chat": args.chat, "sender": args.sender, "text": args.text,
                "sender_name": args.sender_name, "chat_name": args.chat_name,
            }, self_id)
            print(f"appended local_id={local_id}")
            return 0
        source = sys.stdin if args.jsonl == "-" else open(args.jsonl, encoding="utf-8")
        count = 0
        for line in source:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            store.add(json.loads(line), self_id)
            count += 1
            if args.pace:
                time.sleep(args.pace)
        print(f"appended {count} messages")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
