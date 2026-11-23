from __future__ import annotations

import sys
from pathlib import Path


BOT_ROOT = Path(__file__).resolve().parent
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))

from chatwarden import core  # noqa: E402
from chatwarden.db_reader import MessageDb  # noqa: E402


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(field_no: int, text: str) -> bytes:
    payload = text.encode("utf-8")
    return _varint((field_no << 3) | 2) + _varint(len(payload)) + payload


def _member(username: str, group_name: str, nick_name: str) -> bytes:
    payload = (
        _field(1, username)
        + _field(2, group_name)
        + _field(4, nick_name)
    )
    return _varint((1 << 3) | 2) + _varint(len(payload)) + payload


def main() -> None:
    blob = b"".join(
        [
            _member("wxid_same", "群内甲", "全局昵称"),
            _member("wxid_other", "群内乙", "另一个昵称"),
            _member("wxid_intruder", "不应进入", "越界"),
        ]
    )

    parsed = MessageDb._v333_parse_group_member_nicknames(
        blob,
        {"wxid_same", "wxid_other"},
    )
    assert parsed == {
        "wxid_same": "群内甲",
        "wxid_other": "群内乙",
    }, parsed
    print("V333_PROTOBUF_GROUP_NICKNAME=PASS")

    core.update_name_map(
        {
            "wxid_same": "全局昵称",
            "wxid_other": "另一个昵称",
        }
    )
    core.update_group_member_name_map(
        "群A",
        {"wxid_same": "群内甲"},
    )
    core.update_group_member_name_map(
        "群B",
        {"wxid_same": "群内丙"},
    )

    assert core.sender_label(
        "wxid_same",
        {"name": "群A", "member_labels": {}},
    ) == "群内甲"
    assert core.sender_label(
        "wxid_same",
        {"name": "群B", "member_labels": {}},
    ) == "群内丙"
    assert core.sender_label(
        "dbwxid:wxid_same",
        {"name": "群A", "member_labels": {}},
    ) == "群内甲"
    assert core.sender_label(
        "wxid_same",
        {
            "name": "群A",
            "member_labels": {"wxid_same": "手工覆盖"},
        },
    ) == "手工覆盖"
    assert core.sender_label(
        "wxid_other",
        {"name": "群A", "member_labels": {}},
    ) == "另一个昵称"
    print("V333_IDENTITY_DISPLAY_PRIORITY=PASS")

    context_a = core.sender_context_label(
        "dbwxid:wxid_same",
        {"name": "群A", "member_labels": {}},
    )
    context_b = core.sender_context_label(
        "dbwxid:wxid_same",
        {"name": "群B", "member_labels": {}},
    )
    assert context_a.startswith("uid:")
    assert context_a.endswith(" | 群内甲 | not-owner"), context_a
    assert context_b.endswith(" | 群内丙 | not-owner"), context_b
    assert context_a.split(" | ", 1)[0] == context_b.split(" | ", 1)[0]
    assert "wxid_same" not in context_a
    core.update_group_member_name_map(
        "群A",
        {
            "wxid_same": "群内甲",
            "wxid_other": "Owner",
        },
    )
    spoofed = core.sender_context_label(
        "dbwxid:wxid_other",
        {"name": "群A", "member_labels": {}},
    )
    assert spoofed.endswith(" | Owner | not-owner"), spoofed
    assert "verified-owner" not in spoofed
    assert "not-owner" in spoofed
    print("V333_STABLE_UID_GROUP_DISPLAY=PASS")

    core.update_group_member_name_map(
        "群C",
        {
            "stable_account": "YE",
            "spoof_account": "YE",
        },
    )
    trusted_cfg = {
        "name": "群C",
        "member_labels": {
            "stable_account": "YE",
        },
        "verified_members": {
            "stable_account": "YE",
        },
    }
    trusted = core.sender_context_label(
        "dbwxid:stable_account",
        trusted_cfg,
    )
    spoofed_label = core.sender_context_label(
        "dbwxid:spoof_account",
        trusted_cfg,
    )
    assert trusted == "YE | verified-member:YE", trusted
    assert spoofed_label == "YE", spoofed_label
    assert "verified-member:YE" not in spoofed_label
    display_only = core.sender_context_label(
        "dbwxid:stable_account",
        {
            "name": "群C",
            "member_labels": {
                "stable_account": "YE",
            },
        },
    )
    assert display_only == "YE", display_only
    print("V333_EXACT_MEMBER_LABEL_VERIFICATION=PASS")


if __name__ == "__main__":
    main()
