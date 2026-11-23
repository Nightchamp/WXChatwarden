#!/usr/bin/env python3
"""v3.200: group text send normalizes the final full stop exactly once.

The normalizer is not idempotent ("3.。" -> "3." -> "3"), so the group path
must not apply it twice, and the self-echo digest must match the sent text.
Offline: run_as_client_user and the window lookup are faked; nothing is sent.
"""
from __future__ import annotations

from types import SimpleNamespace

from chatwarden import core


def main():
    sent = []
    originals = (
        core.run_as_client_user,
        core.group_muted,
        core.resolve_group_send_title,
    )

    def fake_run_as_client_user(args, *, timeout, input_bytes=None):
        sent.append(input_bytes.decode("utf-8"))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    core.run_as_client_user = fake_run_as_client_user
    core.group_muted = lambda chat_name: False
    core.resolve_group_send_title = lambda chat_name: (chat_name, "0x1")
    try:
        for text, expected in (
            ("版本3.。", "版本3."),
            ("好的。", "好的"),
            ("等等...", "等等..."),
            ("真的吗？", "真的吗？"),
        ):
            core._focus_and_send_unlocked("v3200-group", text)
            assert sent[-1] == expected, (text, sent[-1])
            assert core.looks_like_our_message("v3200-group", expected), text
    finally:
        (
            core.run_as_client_user,
            core.group_muted,
            core.resolve_group_send_title,
        ) = originals
    print("V3200_OUTGOING_NORMALIZE_ONCE=PASS")


if __name__ == "__main__":
    main()
