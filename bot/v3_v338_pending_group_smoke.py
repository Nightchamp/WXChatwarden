#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from chatwarden import core
from chatwarden import runtime as runtime_module


class _FakeWorker:
    def __init__(self, resolved, image_pipeline, profiles, bot_wxid, owner_wxid):
        del image_pipeline, profiles, bot_wxid, owner_wxid
        self.resolved = resolved
        self.stopped = False

    def stop(self):
        self.stopped = True

    def update(self, resolved=None, profiles=None):
        del profiles
        if resolved is not None:
            self.resolved = resolved


class _FailingWorker:
    def __init__(self, *args, **kwargs):
        del args, kwargs
        raise RuntimeError("synthetic-worker-failure")


class _FailIfWaitWatcher:
    available = True

    def scan_for_changes(self):
        return set()

    def wait(self, timeout):
        raise AssertionError(
            f"pending discovery should wake before watcher wait timeout={timeout}"
        )

    def consume_force_full_scan(self):
        return False


class _FakeState:
    def __init__(self):
        self.checkpoints = []
        self.by_name = {}

    def set_checkpoint(self, chat_name, chat_username, table_name, cursor):
        self.by_name[chat_name] = {
            "chat_name": chat_name,
            "chat_username": chat_username,
            "table_name": table_name,
            "last_local_id": cursor,
        }
        self.checkpoints.append(
            (chat_name, chat_username, table_name, cursor)
        )

    def get_checkpoint(self, chat_name):
        return self.by_name.get(chat_name)

    def delete_group(self, chat_name):
        del chat_name


class _FakeDb:
    account_wxid = "wxid_bot"

    def __init__(self):
        self.pending_visible = False
        self.pending_table_stale = False
        self.force_message_refresh_calls = 0
        self.force_names_calls = 0
        self.pending_visible_after_force_names = False

    def resolve_group(self, group_cfg, snapshot):
        name = group_cfg["name"]
        if name == "现有群":
            return {
                "chat_name": name,
                "chat_username": "existing@chatroom",
                "table_name": "Msg_existing",
                "group_cfg": group_cfg,
            }
        if name == "星河漫游" and self.pending_visible:
            if self.pending_table_stale:
                raise RuntimeError(
                    "group '星河漫游': message table 'Msg_new' "
                    "not found in keyed shards"
                )
            return {
                "chat_name": name,
                "chat_username": "new@chatroom",
                "table_name": "Msg_new",
                "group_cfg": group_cfg,
            }
        raise RuntimeError(
            f"group {name!r}: could not uniquely resolve DB chatroom identity"
        )

    def snapshot(self, force_message_refresh=False, **kwargs):
        force_names = bool(kwargs.get("force_names", False))
        if force_names:
            self.force_names_calls += 1
            if self.pending_visible_after_force_names:
                self.pending_visible = True
        if force_message_refresh:
            self.force_message_refresh_calls += 1
            self.pending_table_stale = False
        return {}

    def max_local_id(self, resolved, snapshot):
        del snapshot
        return 42 if resolved["chat_name"] == "星河漫游" else 10


def pending_config_load_smoke():
    original_groups_file = core.GROUPS_FILE
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "groups.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "groups": [
                            {
                                "name": "现有群",
                                "enabled": True,
                            },
                            {
                                "name": "星河漫游",
                                "enabled": True,
                                "pending": True,
                            },
                            {
                                "name": "测试の游乐园🍺🍻🥂🍸🥃🍹",
                                "enabled": True,
                                "pending": True,
                                "name_prefix": "测试の游乐园",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            core.GROUPS_FILE = path
            groups = core.load_groups_config()
    finally:
        core.GROUPS_FILE = original_groups_file

    by_name = {item["name"]: item for item in groups}
    assert by_name["星河漫游"]["pending"] is True, by_name
    assert by_name["星河漫游"]["enabled"] is True, by_name
    assert by_name["测试の游乐园🍺🍻🥂🍸🥃🍹"]["name_prefix"] == "测试の游乐园", by_name
    print("V338_PENDING_CONFIG_LOAD=PASS")


def duplicate_pinned_chatroom_config_rejected_smoke():
    original_groups_file = core.GROUPS_FILE
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "groups.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "groups": [
                            {
                                "name": "群A",
                                "enabled": True,
                                "chat_username": "same-room@chatroom",
                            },
                            {
                                "name": "群B",
                                "enabled": True,
                                "chat_username": "same-room@chatroom",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            core.GROUPS_FILE = path
            try:
                core.load_groups_config()
            except RuntimeError as exc:
                assert "duplicate chat_username" in str(exc), exc
            else:
                raise AssertionError(
                    "duplicate pinned chat_username must fail closed"
                )
    finally:
        core.GROUPS_FILE = original_groups_file

    print("V393_DUPLICATE_PINNED_CHATROOM_REJECTED=PASS")


def pending_unique_prefix_resolution_smoke():
    from chatwarden.db_reader import MessageDb

    db = MessageDb.__new__(MessageDb)
    db._resolved_group_names_by_chat = {}
    db.group_member_names_by_chat = {}

    cfg = {
        "name": "测试の游乐园🍺🍻🥂🍸🥃🍹",
        "name_prefix": "测试の游乐园",
    }
    username = "pants-kindergarten@chatroom"
    import hashlib

    table_name = "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()
    snapshot = {
        "names": {
            username: "测试の游乐园🍺🍻",
            "other@chatroom": "别的群",
        },
        "dbs": [{"tables": {table_name}}],
    }
    resolved = db.resolve_group(cfg, snapshot)
    assert resolved["chat_username"] == username, resolved

    ambiguous_snapshot = {
        "names": {
            username: "测试の游乐园🍺🍻",
            "pants-kindergarten-2@chatroom": "测试の游乐园🥂🍸",
        },
        "dbs": [{"tables": {table_name}}],
    }
    try:
        db.resolve_group(cfg, ambiguous_snapshot)
    except RuntimeError as exc:
        assert "could not uniquely resolve" in str(exc), exc
    else:
        raise AssertionError("ambiguous name_prefix must fail closed")

    print("V338_PENDING_UNIQUE_PREFIX_RESOLUTION=PASS")


def pinned_production_identity_config_smoke():
    from chatwarden.db_reader import MessageDb

    groups = core.load_groups_config()
    assert groups, "production groups config must not be empty"

    pinned = {}
    for cfg in groups:
        name = str(cfg.get("name") or "").strip()
        if cfg.get("pending") is True:
            continue
        chat_username = str(cfg.get("chat_username") or "").strip()
        assert chat_username.endswith("@chatroom"), (
            f"confirmed production group {name!r} must pin exact chat_username"
        )
        assert cfg.get("pending") is False, (
            f"confirmed production group {name!r} must not remain pending"
        )
        assert chat_username not in pinned, (
            f"duplicate production chat_username {chat_username!r}: "
            f"{pinned.get(chat_username)!r} and {name!r}"
        )
        pinned[chat_username] = name

    # Once the exact chatroom identity is pinned, the live WeChat display name
    # is allowed to drift without changing the logical group identity.
    sample = groups[0]
    sample_username = sample["chat_username"]
    import hashlib

    sample_table = "Msg_" + hashlib.md5(
        sample_username.encode("utf-8")
    ).hexdigest()
    renamed_snapshot = {
        "names": {
            sample_username: "这是已经改过的新群名",
        },
        "dbs": [{"tables": {sample_table}}],
    }
    db = MessageDb.__new__(MessageDb)
    db._resolved_group_names_by_chat = {}
    db.group_member_names_by_chat = {}
    resolved = db.resolve_group(sample, renamed_snapshot)
    assert resolved["chat_username"] == sample_username, resolved
    assert resolved["chat_name"] == sample["name"], resolved

    print("V393_PINNED_PRODUCTION_CHATROOM_IDENTITY=PASS")


def pending_wait_then_activate_smoke():
    original_worker = runtime_module.GroupWorker
    runtime_module.GroupWorker = _FakeWorker
    runtime = runtime_module.Runtime.__new__(runtime_module.Runtime)
    runtime.db = _FakeDb()
    runtime.state = _FakeState()
    runtime.images = object()
    runtime.profiles = {}
    runtime.entries = {}
    runtime.groups = [
        {
            "name": "现有群",
            "enabled": True,
            "pending": False,
            "session_key": "existing-session",
            "member_labels": {},
            "personality": "",
        },
        {
            "name": "星河漫游",
            "enabled": True,
            "pending": True,
            "session_key": "pending-session",
            "member_labels": {},
            "personality": "",
        },
    ]

    try:
        runtime.sync_groups({}, reason="startup-baseline")
        assert "现有群" in runtime.entries, runtime.entries
        assert "星河漫游" not in runtime.entries, runtime.entries

        runtime.db.pending_visible = True
        runtime.sync_pending_groups({}, reason="pending-discovery")

        assert "星河漫游" in runtime.entries, runtime.entries
        assert runtime.entries["星河漫游"]["cursor"] == 41, runtime.entries
        assert runtime.state.checkpoints[-1] == (
            "星河漫游",
            "new@chatroom",
            "Msg_new",
            41,
        ), runtime.state.checkpoints
    finally:
        for entry in runtime.entries.values():
            entry["worker"].stop()
        runtime_module.GroupWorker = original_worker

    print("V338_PENDING_WAIT_THEN_ACTIVATE=PASS")


def pending_startup_preserves_first_message_smoke():
    original_worker = runtime_module.GroupWorker
    runtime_module.GroupWorker = _FakeWorker
    runtime = runtime_module.Runtime.__new__(runtime_module.Runtime)
    runtime.db = _FakeDb()
    runtime.db.pending_visible = True
    runtime.state = _FakeState()
    runtime.images = object()
    runtime.profiles = {}
    runtime.entries = {}
    runtime.groups = [
        {
            "name": "星河漫游",
            "enabled": True,
            "pending": True,
            "session_key": "pending-session",
            "member_labels": {},
            "personality": "",
        },
    ]

    try:
        runtime.sync_groups({}, reason="startup-baseline")
        assert runtime.entries["星河漫游"]["cursor"] == 41, runtime.entries
        assert runtime.state.checkpoints[-1] == (
            "星河漫游",
            "new@chatroom",
            "Msg_new",
            41,
        ), runtime.state.checkpoints
    finally:
        for entry in runtime.entries.values():
            entry["worker"].stop()
        runtime_module.GroupWorker = original_worker

    print("V338_PENDING_STARTUP_PRESERVES_FIRST_MESSAGE=PASS")


def pending_restart_does_not_replay_activation_message_smoke():
    original_worker = runtime_module.GroupWorker
    runtime_module.GroupWorker = _FakeWorker
    state = _FakeState()
    state.set_checkpoint(
        "星河漫游",
        "new@chatroom",
        "Msg_new",
        42,
    )
    runtime = runtime_module.Runtime.__new__(runtime_module.Runtime)
    runtime.db = _FakeDb()
    runtime.db.pending_visible = True
    runtime.state = state
    runtime.images = object()
    runtime.profiles = {}
    runtime.entries = {}
    runtime.groups = [
        {
            "name": "星河漫游",
            "enabled": True,
            "pending": True,
            "session_key": "pending-session",
            "member_labels": {},
            "personality": "",
        },
    ]

    try:
        runtime.sync_groups({}, reason="startup-baseline")
        assert runtime.entries["星河漫游"]["cursor"] == 42, runtime.entries
        assert runtime.state.checkpoints[-1][-1] == 42, runtime.state.checkpoints
    finally:
        for entry in runtime.entries.values():
            entry["worker"].stop()
        runtime_module.GroupWorker = original_worker

    print("V338_PENDING_RESTART_NO_DUPLICATE_REPLAY=PASS")


def pending_discovery_has_own_wake_deadline_smoke():
    runtime = runtime_module.Runtime.__new__(runtime_module.Runtime)
    runtime.db = _FakeDb()
    runtime.state = _FakeState()
    runtime.images = object()
    runtime.profiles = {}
    runtime.entries = {}
    runtime.groups = [
        {
            "name": "星河漫游",
            "enabled": True,
            "pending": True,
            "session_key": "pending-session",
            "member_labels": {},
            "personality": "",
        },
    ]
    runtime._v330_wal_enabled = True
    runtime._v330_wal_watcher = _FailIfWaitWatcher()
    now = time.monotonic()
    runtime._v330_next_stat_scan = now + 3600.0
    runtime._v330_next_sanity_scan = now + 3600.0

    runtime.sync_groups({}, reason="startup-baseline")
    changed, reason = runtime._v330_wait_for_work()
    assert changed is None, changed
    assert reason == "pending-probe", reason

    print("V338_PENDING_DISCOVERY_WAKE=PASS")


def pending_new_table_refresh_smoke():
    original_worker = runtime_module.GroupWorker
    runtime_module.GroupWorker = _FakeWorker
    runtime = runtime_module.Runtime.__new__(runtime_module.Runtime)
    runtime.db = _FakeDb()
    runtime.db.pending_visible = True
    runtime.db.pending_table_stale = True
    runtime.state = _FakeState()
    runtime.images = object()
    runtime.profiles = {}
    runtime.entries = {}
    runtime.groups = [
        {
            "name": "星河漫游",
            "enabled": True,
            "pending": True,
            "session_key": "pending-session",
            "member_labels": {},
            "personality": "",
        },
    ]
    runtime._v338_waiting_pending = {"星河漫游"}

    try:
        activated = runtime.sync_pending_groups(
            {},
            reason="pending-discovery",
        )
        assert activated == 1, activated
        assert runtime.db.force_message_refresh_calls == 1, (
            runtime.db.force_message_refresh_calls
        )
        assert runtime.entries["星河漫游"]["cursor"] == 41, runtime.entries
    finally:
        for entry in runtime.entries.values():
            entry["worker"].stop()
        runtime_module.GroupWorker = original_worker

    print("V338_PENDING_NEW_TABLE_REFRESH=PASS")


def pending_name_cache_refresh_smoke():
    original_worker = runtime_module.GroupWorker
    runtime_module.GroupWorker = _FakeWorker
    runtime = runtime_module.Runtime.__new__(runtime_module.Runtime)
    runtime.db = _FakeDb()
    runtime.db.pending_visible_after_force_names = True
    runtime.state = _FakeState()
    runtime.images = object()
    runtime.profiles = {}
    runtime.entries = {}
    runtime.groups = [
        {
            "name": "星河漫游",
            "enabled": True,
            "pending": True,
            "session_key": "pending-session",
            "member_labels": {},
            "personality": "",
        },
    ]
    runtime._v338_waiting_pending = {"星河漫游"}
    runtime._v338_name_refresh_after = {"星河漫游": 0.0}

    try:
        activated = runtime.sync_pending_groups(
            {},
            reason="pending-discovery",
        )
        assert activated == 1, activated
        assert runtime.db.force_names_calls == 1, runtime.db.force_names_calls
        assert "星河漫游" in runtime.entries, runtime.entries
    finally:
        for entry in runtime.entries.values():
            entry["worker"].stop()
        runtime_module.GroupWorker = original_worker

    print("V338_PENDING_NAME_CACHE_REFRESH=PASS")


def pending_runtime_error_not_suppressed_smoke():
    original_worker = runtime_module.GroupWorker
    runtime_module.GroupWorker = _FailingWorker
    runtime = runtime_module.Runtime.__new__(runtime_module.Runtime)
    runtime.db = _FakeDb()
    runtime.db.pending_visible = True
    runtime.state = _FakeState()
    runtime.images = object()
    runtime.profiles = {}
    runtime.entries = {}
    runtime.groups = [
        {
            "name": "星河漫游",
            "enabled": True,
            "pending": True,
            "session_key": "pending-session",
            "member_labels": {},
            "personality": "",
        },
    ]

    try:
        try:
            runtime.sync_groups({}, reason="startup-baseline")
        except RuntimeError as exc:
            assert str(exc) == "synthetic-worker-failure", exc
        else:
            raise AssertionError(
                "pending group must not suppress non-resolution runtime failures"
            )
    finally:
        runtime_module.GroupWorker = original_worker

    print("V338_PENDING_RUNTIME_ERROR_NOT_SUPPRESSED=PASS")


if __name__ == "__main__":
    pending_config_load_smoke()
    duplicate_pinned_chatroom_config_rejected_smoke()
    pending_unique_prefix_resolution_smoke()
    pending_wait_then_activate_smoke()
    pending_startup_preserves_first_message_smoke()
    pending_restart_does_not_replay_activation_message_smoke()
    pending_discovery_has_own_wake_deadline_smoke()
    pending_new_table_refresh_smoke()
    pending_name_cache_refresh_smoke()
    pending_runtime_error_not_suppressed_smoke()
