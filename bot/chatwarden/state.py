from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class StateStore:
    def __init__(
        self,
        path="/var/lib/chatwarden/bot.sqlite3",
    ):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(
            self.path,
            timeout=20.0,
        )
        conn.execute(
            "PRAGMA busy_timeout=20000"
        )
        return conn

    def _init_schema(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS
                    v3_group_checkpoint (
                        chat_name TEXT PRIMARY KEY,
                        chat_username TEXT NOT NULL,
                        table_name TEXT NOT NULL,
                        last_local_id INTEGER NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS
                    v3_seen_chatroom (
                        chat_username TEXT PRIMARY KEY,
                        last_name TEXT NOT NULL,
                        first_seen_at REAL NOT NULL,
                        last_seen_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS
                    v3_runtime_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def seen_chatrooms(self):
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT chat_username, last_name "
                    "FROM v3_seen_chatroom"
                ).fetchall()
            finally:
                conn.close()

        return {
            str(row[0]): str(row[1] or "")
            for row in rows
            if str(row[0] or "").strip()
        }

    def remember_chatrooms(self, chatrooms):
        clean = {
            str(chat_username or "").strip(): str(name or "").strip()
            for chat_username, name in dict(chatrooms or {}).items()
            if str(chat_username or "").strip()
        }
        if not clean:
            return 0

        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    """
                    INSERT INTO v3_seen_chatroom(
                        chat_username,
                        last_name,
                        first_seen_at,
                        last_seen_at
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(chat_username) DO UPDATE SET
                        last_name=excluded.last_name,
                        last_seen_at=excluded.last_seen_at
                    """,
                    [
                        (
                            chat_username,
                            name,
                            now,
                            now,
                        )
                        for chat_username, name in clean.items()
                    ],
                )
                conn.commit()
            finally:
                conn.close()
        return len(clean)

    def auto_group_registry_initialized(self):
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value FROM v3_runtime_meta "
                    "WHERE key='auto_group_registry_initialized'"
                ).fetchone()
            finally:
                conn.close()
        return bool(row and str(row[0] or "") == "1")

    def mark_auto_group_registry_initialized(self):
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO v3_runtime_meta(key, value, updated_at)
                    VALUES ('auto_group_registry_initialized', '1', ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value='1',
                        updated_at=excluded.updated_at
                    """,
                    (now,),
                )
                conn.commit()
            finally:
                conn.close()

    def set_checkpoint(
        self,
        chat_name,
        chat_username,
        table_name,
        last_local_id,
    ):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO v3_group_checkpoint(
                        chat_name,
                        chat_username,
                        table_name,
                        last_local_id,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(chat_name) DO UPDATE SET
                        chat_username=excluded.chat_username,
                        table_name=excluded.table_name,
                        last_local_id=excluded.last_local_id,
                        updated_at=excluded.updated_at
                    """,
                    (
                        chat_name,
                        chat_username,
                        table_name,
                        int(last_local_id),
                        time.time(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def get_checkpoint(self, chat_name):
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT chat_name, chat_username, table_name, last_local_id "
                    "FROM v3_group_checkpoint WHERE chat_name=?",
                    (chat_name,),
                ).fetchone()
            finally:
                conn.close()

        if row is None:
            return None
        return {
            "chat_name": str(row[0]),
            "chat_username": str(row[1]),
            "table_name": str(row[2]),
            "last_local_id": int(row[3]),
        }

    def delete_group(self, chat_name):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM v3_group_checkpoint "
                    "WHERE chat_name=?",
                    (chat_name,),
                )
                conn.commit()
            finally:
                conn.close()
