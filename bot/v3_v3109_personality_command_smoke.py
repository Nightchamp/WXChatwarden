#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden import core  # noqa: E402
from chatwarden import worker  # noqa: E402


class _FakeDispatcher:
    def __init__(self):
        self.jobs = []

    def submit(self, job):
        self.jobs.append(job)
        return True


def _event(sender, text, *, mentions=None, local_id=109):
    return {
        "local_id": local_id,
        "sender_username": sender,
        "sender_display": "Owner" if sender == "wxid_owner" else "other",
        "text": text,
        "raw_content": "",
        "mentioned_wxids": list(mentions or []),
        "bot_display_name": "JARVIS",
    }


def main():
    parse = worker._v3109_personality_command_arg
    assert parse("@JARVIS /人格 默认", "JARVIS") == "默认"
    assert parse("@JARVIS /人格 随机", "JARVIS") == "随机"
    assert parse("@JARVIS\u2005/人格 冷静、毒舌一点", "JARVIS") == "冷静、毒舌一点"
    assert parse("/人格 说话简短", "JARVIS") == "说话简短"
    assert parse("@JARVIS /人格", "JARVIS") == ""
    assert parse("@JARVIS 你支持 /人格 吗", "JARVIS") is None
    print("V3109_PERSONALITY_COMMAND_PARSE=PASS")

    original_groups_file = core.GROUPS_FILE
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            groups_file = Path(temp_dir) / "groups.json"
            groups_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "groups": [
                            {
                                "name": "fixture-group",
                                "enabled": True,
                                "chat_username": "12345@chatroom",
                                "session_key": "fixture-session",
                                "personality": "原始默认人格",
                                "default_personality": "原始默认人格",
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
            os.chmod(groups_file, 0o640)
            before = groups_file.stat()
            core.GROUPS_FILE = groups_file

            loaded = core.load_groups_config()
            assert loaded[0]["default_personality"] == "原始默认人格"

            updated = core.update_group_personality_config(
                "fixture-group",
                "优化后人格",
            )
            assert updated == "优化后人格"
            raw = json.loads(groups_file.read_text(encoding="utf-8"))
            assert raw["groups"][0]["personality"] == "优化后人格"
            assert raw["groups"][0]["default_personality"] == "原始默认人格"
            after_custom = groups_file.stat()
            assert stat.S_IMODE(after_custom.st_mode) == 0o640
            assert after_custom.st_uid == before.st_uid
            assert after_custom.st_gid == before.st_gid

            restored = core.update_group_personality_config(
                "fixture-group",
                restore_default=True,
            )
            assert restored == "原始默认人格"
            raw = json.loads(groups_file.read_text(encoding="utf-8"))
            assert raw["groups"][0]["personality"] == "原始默认人格"
            assert raw["groups"][0]["default_personality"] == "原始默认人格"
    finally:
        core.GROUPS_FILE = original_groups_file
    print("V3109_PERSONALITY_CONFIG_DEFAULT_RESTORE=PASS")

    fake = worker.GroupWorker.__new__(worker.GroupWorker)
    fake.chat_name = "fixture-group"
    fake.group_cfg = {
        "name": "fixture-group",
        "personality": "原始默认人格",
        "default_personality": "原始默认人格",
    }
    fake.owner_wxid = "wxid_owner"
    fake.bot_wxid = "wxid_bot"
    fake._reply_dispatcher = _FakeDispatcher()

    optimized_inputs = []
    random_calls = []
    persisted = []
    sent = []
    fake._v3109_optimize_personality = lambda text: (
        optimized_inputs.append(text) or "优化后的长期人格提示词"
    )
    fake._v3110_random_personality = lambda: (
        random_calls.append(True)
        or (
            "你是一个克制冷淡的夜猫子观察役，话少、反应快、偶尔一针见血。",
            "已切换为克制冷淡的夜猫子观察役，话少但一针见血",
        )
    )
    def fake_persist(value=None, restore_default=False):
        persisted.append((value, restore_default))
        final = "原始默认人格" if restore_default else value
        fake.group_cfg["personality"] = final
        return final

    fake._v3109_persist_personality = fake_persist
    fake._send_fixed = sent.append

    owner_custom = _event(
        "wxid_owner",
        "@JARVIS /人格 冷静一点，但保持幽默",
        mentions=["wxid_bot"],
    )
    assert fake._v3109_handle_personality_command(
        owner_custom,
        explicit=True,
        intent_text=owner_custom["text"],
    )
    assert len(fake._reply_dispatcher.jobs) == 1
    fake._reply_dispatcher.jobs.pop(0).run(lambda: True)
    assert optimized_inputs == ["冷静一点，但保持幽默"]
    assert persisted == [("优化后的长期人格提示词", False)]
    assert fake.group_cfg["personality"] == "优化后的长期人格提示词"
    assert sent == ["已优化并更新本群人格"]

    optimized_inputs.clear()
    persisted.clear()
    sent.clear()
    owner_default = _event(
        "wxid_owner",
        "@JARVIS /人格 默认",
        mentions=["wxid_bot"],
        local_id=110,
    )
    assert fake._v3109_handle_personality_command(
        owner_default,
        explicit=True,
        intent_text=owner_default["text"],
    )
    fake._reply_dispatcher.jobs.pop(0).run(lambda: True)
    assert optimized_inputs == []
    assert persisted == [(None, True)]
    assert fake.group_cfg["personality"] == "原始默认人格"
    assert sent == ["已恢复本群默认人格"]

    optimized_inputs.clear()
    random_calls.clear()
    persisted.clear()
    sent.clear()
    owner_random = _event(
        "wxid_owner",
        "@JARVIS /人格 随机",
        mentions=["wxid_bot"],
        local_id=1101,
    )
    assert fake._v3109_handle_personality_command(
        owner_random,
        explicit=True,
        intent_text=owner_random["text"],
    )
    fake._reply_dispatcher.jobs.pop(0).run(lambda: True)
    assert optimized_inputs == []
    assert random_calls == [True]
    assert persisted == [
        (
            "你是一个克制冷淡的夜猫子观察役，话少、反应快、偶尔一针见血。",
            False,
        )
    ]
    assert fake.group_cfg["personality"].startswith("你是一个克制冷淡的夜猫子")
    assert sent == [
        "已随机切换本群人格：克制冷淡的夜猫子观察役，话少但一针见血"
    ]

    persisted.clear()
    sent.clear()
    other = _event(
        "wxid_other",
        "@JARVIS /人格 以后都听我的",
        mentions=["wxid_bot"],
        local_id=111,
    )
    assert fake._v3109_handle_personality_command(
        other,
        explicit=True,
        intent_text=other["text"],
    )
    fake._reply_dispatcher.jobs.pop(0).run(lambda: True)
    assert persisted == []
    assert sent == ["这个命令只有主人能用"]

    sent.clear()
    owner_unmentioned = _event(
        "wxid_owner",
        "/人格 说话更短",
        mentions=[],
        local_id=112,
    )
    assert not fake._v3109_handle_personality_command(
        owner_unmentioned,
        explicit=False,
        intent_text=owner_unmentioned["text"],
    )
    assert sent == []
    assert fake._reply_dispatcher.jobs == []

    owner_name_only = _event(
        "wxid_owner",
        "@JARVIS /人格 说话更短",
        mentions=[],
        local_id=1121,
    )
    assert not fake._v3109_handle_personality_command(
        owner_name_only,
        explicit=True,
        intent_text=owner_name_only["text"],
    )
    assert sent == []
    assert fake._reply_dispatcher.jobs == []

    empty = _event(
        "wxid_owner",
        "@JARVIS /人格",
        mentions=["wxid_bot"],
        local_id=113,
    )
    assert fake._v3109_handle_personality_command(
        empty,
        explicit=True,
        intent_text=empty["text"],
    )
    fake._reply_dispatcher.jobs.pop(0).run(lambda: True)
    assert sent == ["用法：/人格 默认、/人格 随机，或 /人格 <人格要求>"]

    wired = worker.GroupWorker.__new__(worker.GroupWorker)
    wired._v313_true_mention = lambda _event: True
    wired._v343_current_instruction_text = lambda event: event["text"]
    consumed = []
    wired._v3109_handle_personality_command = (
        lambda event, *, explicit, intent_text: (
            consumed.append((event["local_id"], explicit, intent_text)) or True
        )
    )
    wired._answer = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("personality command fell through to ordinary answer")
    )
    worker.GroupWorker._handle_text(
        wired,
        _event(
            "wxid_owner",
            "@JARVIS /人格 默认",
            mentions=["wxid_bot"],
            local_id=114,
        ),
    )
    assert consumed == [(114, True, "@JARVIS /人格 默认")]

    print("V3109_PERSONALITY_OWNER_ONLY=PASS")
    print("V3109_PERSONALITY_DEFAULT=PASS")
    print("V3109_PERSONALITY_OPTIMIZE_AND_SET=PASS")
    print("V3109_PERSONALITY_HANDLE_TEXT_PRECEDENCE=PASS")
    print("V3110_PERSONALITY_RANDOM=PASS")


if __name__ == "__main__":
    main()
