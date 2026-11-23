#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden import core
from chatwarden import runtime as runtime_mod
from chatwarden.runtime import Runtime


class _Db:
    def __init__(self):
        self.calls = []

    def snapshot(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"fixture": True}


class _Watcher:
    def __init__(self):
        self.wait_calls = []
        self.consume_calls = 0

    def wait(self, timeout):
        self.wait_calls.append(timeout)
        return set()

    def consume_force_full_scan(self):
        self.consume_calls += 1
        return False


class _HistoryDb:
    def __init__(self, events):
        self.events = list(events)
        self.calls = []

    def events_after(self, resolved, cursor, snapshot, limit=200):
        self.calls.append((resolved, cursor, snapshot, limit))
        return [
            event
            for event in self.events
            if int(event["local_id"]) > int(cursor)
        ][:limit]


class _NoopWorker:
    def __init__(self, sequence):
        self.sequence = sequence

    def update(self, **kwargs):
        self.sequence.append(("worker-update", kwargs))


class _State:
    def __init__(self, sequence):
        self.sequence = sequence

    def set_checkpoint(self, chat_name, chat_username, table_name, cursor):
        self.sequence.append(("checkpoint", chat_name, int(cursor)))

    def get_checkpoint(self, chat_name):
        return None


def main() -> None:
    original_marker = core.UI_MAIN_MARKER_FILE
    original_settle = runtime_mod.UI_RELOGIN_SETTLE_SECONDS
    original_db_file = core.DB_FILE
    try:
        with tempfile.TemporaryDirectory(prefix="v370-ui-relogin-") as tmp:
            marker = Path(tmp) / "main.ready"
            core.UI_MAIN_MARKER_FILE = marker
            runtime_mod.UI_RELOGIN_SETTLE_SECONDS = 0.0

            runtime = Runtime.__new__(Runtime)
            runtime._v370_ui_paused = False
            runtime.db = _Db()
            runtime._v330_wal_enabled = True
            runtime._v330_wal_watcher = _Watcher()
            runtime._v330_next_stat_scan = 0.0
            runtime._v330_next_sanity_scan = 0.0
            sync_calls = []
            private_calls = []
            runtime.sync_groups = lambda snapshot, **kwargs: sync_calls.append(
                (snapshot, dict(kwargs))
            )
            runtime._v310_baseline_private = lambda snapshot, **kwargs: private_calls.append(
                (snapshot, dict(kwargs))
            )

            assert runtime._v370_ui_login_gate() is False
            assert runtime._v370_ui_paused is True
            assert runtime.db.calls == []

            marker.write_text("main\n", encoding="utf-8")
            assert runtime._v370_ui_login_gate() is True
            assert runtime._v370_ui_paused is False
            assert runtime.db.calls == [
                {"force_names": True, "force_message_refresh": True}
            ]
            assert sync_calls == [
                (
                    {"fixture": True},
                    {"reason": "ui-main-resume", "rebaseline_existing": True},
                )
            ]
            assert private_calls == [
                ({"fixture": True}, {"reason": "ui-main-resume"})
            ]
            assert runtime._v330_wal_watcher.wait_calls == [0.0]
            assert runtime._v330_wal_watcher.consume_calls == 1

            assert runtime._v370_ui_login_gate() is True
            assert len(runtime.db.calls) == 1

        with tempfile.TemporaryDirectory(prefix="v395-history-only-") as tmp:
            core.DB_FILE = Path(tmp) / "context.db"
            event = {
                "local_id": 101,
                "timestamp": 1_800_000_000,
                "sender_username": "wxid_fixture",
                "local_type": 1,
                "msg_type": "文本",
                "text": "离线期间的真实消息",
            }
            result1 = core.backfill_group_history_events(
                "fixture-group",
                "fixture@chatroom",
                [event],
            )
            result2 = core.backfill_group_history_events(
                "fixture-group",
                "fixture@chatroom",
                [event],
            )
            assert result1["inserted"] == 1
            assert result2["inserted"] == 0
            assert result2["duplicates"] == 1
            with core.open_db() as conn:
                rows = conn.execute(
                    "SELECT sender_id, content, created_at "
                    "FROM group_messages WHERE chat=? ORDER BY id",
                    ("fixture-group",),
                ).fetchall()
            assert rows == [
                ("dbwxid:wxid_fixture", "离线期间的真实消息", 1_800_000_000.0)
            ]
            assert core.history_backfill_seen_local_ids(
                "fixture@chatroom",
                [100, 101, 102],
            ) == {101}

            runtime = Runtime.__new__(Runtime)
            runtime.db = _HistoryDb(
                [
                    dict(event),
                    {
                        **event,
                        "local_id": 102,
                        "timestamp": 1_800_000_001,
                        "text": "第二条离线消息",
                    },
                ]
            )
            history_calls = []
            original_backfill = core.backfill_group_history_events
            core.backfill_group_history_events = (
                lambda chat_name, chat_username, events: history_calls.append(
                    (chat_name, chat_username, list(events))
                )
                or {
                    "scanned": len(events),
                    "inserted": len(events),
                    "duplicates": 0,
                    "bot_skipped": 0,
                }
            )
            try:
                result = runtime._v395_backfill_group_history(
                    {"cursor": 100},
                    {
                        "chat_name": "fixture-group",
                        "chat_username": "fixture@chatroom",
                    },
                    {"fixture": True},
                    102,
                )
            finally:
                core.backfill_group_history_events = original_backfill
            assert result["from_local_id"] == 100
            assert result["to_local_id"] == 102
            assert result["inserted"] == 2
            assert len(history_calls) == 1
            assert [
                item["local_id"] for item in history_calls[0][2]
            ] == [101, 102]

            sequence = []
            runtime = Runtime.__new__(Runtime)
            runtime.groups = [{"name": "fixture-group", "enabled": True}]
            runtime.profiles = {}
            runtime._v338_waiting_pending = set()
            runtime.entries = {
                "fixture-group": {
                    "cursor": 100,
                    "resolved": {
                        "chat_name": "fixture-group",
                        "chat_username": "fixture@chatroom",
                        "table_name": "Msg_fixture",
                    },
                    "worker": _NoopWorker(sequence),
                }
            }
            runtime.state = _State(sequence)

            class _SyncDb:
                def resolve_group(self, cfg, snapshot):
                    return {
                        "chat_name": "fixture-group",
                        "chat_username": "fixture@chatroom",
                        "table_name": "Msg_fixture",
                    }

                def max_local_id(self, resolved, snapshot):
                    return 102

            runtime.db = _SyncDb()
            runtime._v395_backfill_group_history = (
                lambda entry, resolved, snapshot, target: sequence.append(
                    ("history-backfill", int(entry["cursor"]), int(target))
                )
            )
            runtime.sync_groups(
                {"fixture": True},
                reason="ui-main-resume",
                rebaseline_existing=True,
            )
            assert sequence[-2:] == [
                ("history-backfill", 100, 102),
                ("checkpoint", "fixture-group", 102),
            ]
            assert runtime.entries["fixture-group"]["cursor"] == 102

            runtime = Runtime.__new__(Runtime)
            filtered = runtime._v395_filter_history_only_events(
                {
                    "chat_name": "fixture-group",
                    "chat_username": "fixture@chatroom",
                },
                [
                    dict(event),
                    {
                        **event,
                        "local_id": 102,
                        "timestamp": 1_800_000_001,
                        "text": "真正的新实时消息",
                    },
                ],
            )
            assert [item["local_id"] for item in filtered] == [102]
    finally:
        core.UI_MAIN_MARKER_FILE = original_marker
        runtime_mod.UI_RELOGIN_SETTLE_SECONDS = original_settle
        core.DB_FILE = original_db_file

    print("V370_UI_RELOGIN_REBASELINE=PASS")
    print("V395_HISTORY_ONLY_OFFLINE_BACKFILL=PASS")


if __name__ == "__main__":
    main()
