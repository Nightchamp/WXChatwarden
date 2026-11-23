from __future__ import annotations

from unittest.mock import patch

from chatwarden import core
from chatwarden.runtime import Runtime


class FakeState:
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.writes = []

    def get_checkpoint(self, chat_name):
        return self.checkpoint

    def set_checkpoint(self, chat_name, chat_username, table_name, cursor):
        self.writes.append((chat_name, chat_username, table_name, int(cursor)))


class FakeDb:
    def __init__(self, *, chat_username="123@chatroom", table="Msg_fixture", target=102):
        self.resolved = {
            "chat_name": "fixture",
            "chat_username": chat_username,
            "table_name": table,
        }
        self.target = target

    def resolve_group(self, _cfg, _snapshot):
        return dict(self.resolved)

    def max_local_id(self, _resolved, _snapshot):
        return self.target


def main() -> None:
    checkpoint = {
        "chat_name": "fixture",
        "chat_username": "123@chatroom",
        "table_name": "Msg_fixture",
        "last_local_id": 100,
    }
    runtime = Runtime.__new__(Runtime)
    runtime.state = FakeState(checkpoint)
    runtime.db = FakeDb(target=102)
    created = []
    backfilled = []

    def fake_new_entry(cfg, snapshot, **kwargs):
        created.append(dict(kwargs))
        return {
            "cursor": int(kwargs.get("cursor_override") or 0),
            "resolved": dict(runtime.db.resolved),
            "worker": object(),
        }

    runtime._new_entry = fake_new_entry
    runtime._v395_backfill_group_history = (
        lambda entry, resolved, snapshot, target: backfilled.append(
            (int(entry["cursor"]), int(target), resolved["chat_username"])
        )
    )

    entry = runtime._v3139_startup_entry(
        {"name": "fixture", "enabled": True}, {"fixture": True}
    )
    assert created == [{
        "reason": "startup-recovery",
        "cursor_override": 100,
        "persist_checkpoint": False,
        "emit_ready": False,
    }]
    assert backfilled == [(100, 102, "123@chatroom")]
    assert entry["cursor"] == 102
    assert runtime.state.writes == [
        ("fixture", "123@chatroom", "Msg_fixture", 102)
    ]

    runtime = Runtime.__new__(Runtime)
    runtime.state = FakeState(None)
    runtime.db = FakeDb(target=50)
    calls = []
    runtime._new_entry = lambda cfg, snapshot, **kwargs: calls.append(kwargs) or {
        "cursor": 49,
        "resolved": runtime.db.resolved,
        "worker": object(),
    }
    runtime._v3139_startup_entry(
        {"name": "fixture", "enabled": True, "pending": True}, {"fixture": True}
    )
    assert calls == [{"reason": "startup-baseline", "include_latest": True}]

    runtime = Runtime.__new__(Runtime)
    runtime.state = FakeState(checkpoint)
    runtime.db = FakeDb(chat_username="999@chatroom", table="Msg_other", target=5)
    calls = []
    runtime._new_entry = lambda cfg, snapshot, **kwargs: calls.append(kwargs) or {
        "cursor": 5,
        "resolved": runtime.db.resolved,
        "worker": object(),
    }
    runtime._v395_backfill_group_history = lambda *_args: (_ for _ in ()).throw(
        AssertionError("identity change must not backfill old chat")
    )
    runtime._v3139_startup_entry(
        {"name": "fixture", "enabled": True}, {"fixture": True}
    )
    assert calls == [{"reason": "startup-baseline"}]

    runtime = Runtime.__new__(Runtime)
    runtime.groups = [
        {"name": "ready-group"},
        {"name": "waiting-group"},
    ]
    runtime.entries = {
        "ready-group": {
            "cursor": 7,
            "resolved": {
                "chat_username": "7@chatroom",
                "table_name": "Msg_7",
            },
        }
    }
    runtime._v338_waiting_pending = {"waiting-group"}
    logs = []
    with patch.object(core, "log", side_effect=logs.append):
        runtime._v3139_log_startup_readiness()
    assert sum("[v3.139-startup-ready] group=" in line for line in logs) == 2
    assert any("status=ready" in line for line in logs)
    assert any("status=waiting-pending" in line for line in logs)
    assert any("summary ready=1 configured=2" in line for line in logs)

    print("V3139_COLD_START_HISTORY_ONLY_BACKFILL=PASS")
    print("V3139_NEW_PENDING_GROUP_BASELINE_PRESERVED=PASS")
    print("V3139_EXACT_IDENTITY_GUARD=PASS")
    print("V3139_STARTUP_READY_OBSERVABILITY=PASS")


if __name__ == "__main__":
    main()
