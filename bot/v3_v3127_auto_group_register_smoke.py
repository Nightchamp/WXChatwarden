#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from chatwarden import core
from chatwarden.runtime import Runtime
from chatwarden.state import StateStore


class FakeDb:
    def __init__(self, rooms):
        self.rooms = dict(rooms)

    def current_joined_chatrooms(self, names=None):
        return dict(self.rooms)


def main() -> None:
    original_groups_file = core.GROUPS_FILE

    with tempfile.TemporaryDirectory(prefix="v3127-auto-group-") as temp_dir:
        root = Path(temp_dir)
        groups_file = root / "groups.json"
        groups_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "groups": [
                        {
                            "name": "现有群",
                            "enabled": True,
                            "chat_username": "111@chatroom",
                            "personality": "existing",
                            "default_personality": "existing",
                            "member_labels": {},
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        core.GROUPS_FILE = groups_file

        try:
            runtime = Runtime.__new__(Runtime)
            runtime.state = StateStore(root / "bot.sqlite3")
            runtime.groups = core.load_groups()
            runtime.db = FakeDb(
                {
                    "111@chatroom": "现有群",
                    # A pre-existing unrelated WeChat group must be baselined,
                    # never retroactively enrolled as a bot group.
                    "999@chatroom": "历史普通群",
                }
            )

            created = runtime._v3127_auto_register_groups(
                {"names": runtime.db.rooms},
                reason="startup",
            )
            assert created == 0, created
            assert runtime.state.auto_group_registry_initialized() is True
            assert set(runtime.state.seen_chatrooms()) == {
                "111@chatroom",
                "999@chatroom",
            }
            raw = json.loads(groups_file.read_text(encoding="utf-8"))
            assert len(raw["groups"]) == 1, raw

            # A genuinely new exact chatroom id is enrolled automatically.
            runtime.db.rooms["222@chatroom"] = "新拉进来的群"
            created = runtime._v3127_auto_register_groups(
                {"names": runtime.db.rooms},
                reason="wal",
            )
            assert created == 1, created
            raw = json.loads(groups_file.read_text(encoding="utf-8"))
            new_group = next(
                item
                for item in raw["groups"]
                if item.get("chat_username") == "222@chatroom"
            )
            assert new_group["name"] == "新拉进来的群", new_group
            assert new_group["enabled"] is True, new_group
            assert new_group["pending"] is True, new_group
            assert new_group["auto_registered"] is True, new_group
            assert new_group["muted"] is True, new_group  # v3.177 default off
            assert new_group["personality"] == new_group["default_personality"]
            assert new_group["member_labels"] == {}, new_group
            assert new_group["verified_members"] == {}, new_group

            # Re-scanning is idempotent, and a previously seen but unconfigured
            # historical room is not silently re-registered.
            created = runtime._v3127_auto_register_groups(
                {"names": runtime.db.rooms},
                reason="sanity",
            )
            assert created == 0, created
            raw = json.loads(groups_file.read_text(encoding="utf-8"))
            assert not any(
                item.get("chat_username") == "999@chatroom"
                for item in raw["groups"]
            ), raw

            # Same visible title on a different exact chatroom remains a new
            # group but receives a collision-safe stable logical identity.
            runtime.db.rooms["333@chatroom"] = "现有群"
            created = runtime._v3127_auto_register_groups(
                {"names": runtime.db.rooms},
                reason="wal",
            )
            assert created == 1, created
            raw = json.loads(groups_file.read_text(encoding="utf-8"))
            collision = next(
                item
                for item in raw["groups"]
                if item.get("chat_username") == "333@chatroom"
            )
            assert collision["name"].startswith("现有群 ["), collision
            assert collision["name"] != "现有群", collision

            # Core registration is exact-id idempotent even before runtime
            # state catches up.
            duplicate = core.auto_register_group_config(
                "222@chatroom",
                "新名字不应覆盖稳定身份",
            )
            assert duplicate["created"] is False, duplicate
            assert duplicate["group"]["name"] == "新拉进来的群", duplicate
        finally:
            core.GROUPS_FILE = original_groups_file

    print("V3127_EXISTING_CHATROOM_BASELINE_NO_RETRO_ENROLL=PASS")
    print("V3127_NEW_EXACT_CHATROOM_AUTO_REGISTER=PASS")
    print("V3127_SEEN_CHATROOM_NO_REREGISTER=PASS")
    print("V3127_TITLE_COLLISION_STABLE_ID=PASS")


if __name__ == "__main__":
    main()
