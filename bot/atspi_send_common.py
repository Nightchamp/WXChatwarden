#!/usr/bin/env python3
"""Shared fail-closed AT-SPI/X11 helpers for atspi_send_image / atspi_send_file.

v3.201: extracted verbatim from the two scripts (they were identical except
for the noun in failure text). Each script calls configure() once with its
exact chat name and noun so every message and exit code stays byte-identical
("refusing image send: ...", "IMAGE_FRAME_NOT_FOUND ..." exit 42, and the
same for "file").
"""
from __future__ import annotations

import subprocess
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi


CHAT = ""
NOUN = ""
FRAME_NOT_FOUND = ""


def configure(chat: str, noun: str) -> None:
    global CHAT, NOUN, FRAME_NOT_FOUND
    CHAT = chat
    NOUN = noun
    FRAME_NOT_FOUND = f"{noun.upper()}_FRAME_NOT_FOUND"


def fail(message: str, code: int = 1):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def role(acc):
    return (
        (safe(acc.get_role_name, "") or "")
        .strip()
        .lower()
        .replace("_", "-")
        .replace(" ", "-")
    )


def name(acc):
    return safe(acc.get_name, "") or ""


def children(acc):
    count = safe(acc.get_child_count, 0) or 0
    for index in range(count):
        child = safe(lambda index=index: acc.get_child_at_index(index))
        if child is not None:
            yield child


def walk(acc, depth=0, max_depth=40):
    if depth > max_depth:
        return
    yield acc
    for child in children(acc):
        yield from walk(child, depth + 1, max_depth)


def xdotool(args, check=True):
    return subprocess.run(
        ["xdotool", *args],
        capture_output=True,
        text=True,
        timeout=5,
        check=check,
    )


def exact_visible_windows(title):
    result = xdotool(["search", "--onlyvisible", "--name", ".*"], check=False)
    if result.returncode not in (0, 1):
        fail(f"refusing {NOUN} send: xdotool search failed rc={result.returncode}")
    matches = []
    for window_id in result.stdout.split():
        named = xdotool(["getwindowname", window_id], check=False)
        if named.returncode == 0 and named.stdout.rstrip("\r\n") == title:
            matches.append(window_id)
    return matches


def active_window_id():
    result = xdotool(["getactivewindow"])
    value = result.stdout.strip()
    if not value:
        fail(f"refusing {NOUN} send: active X11 window id is empty")
    return int(value, 0)


def find_exact_frame():
    desktop = Atspi.get_desktop(0)
    # Match the v3.11.4 private text-send fast path: WeChat chat windows are
    # top-level frames directly under an application accessible. Recursively
    # traversing the entire desktop tree can take tens of seconds on a busy
    # client and caused a real image send to succeed while the caller timed
    # out waiting for this script to finish.
    matches = []
    for app in children(desktop):
        for acc in children(app):
            if role(acc) == "frame" and name(acc).strip() == CHAT:
                matches.append(acc)
    if not matches:
        fail(f"{FRAME_NOT_FOUND} {CHAT!r}", code=42)
    if len(matches) == 1:
        return matches[0]
    active = []
    for candidate in matches:
        states = safe(candidate.get_state_set)
        if states is not None and safe(
            lambda states=states: states.contains(Atspi.StateType.ACTIVE),
            False,
        ):
            active.append(candidate)
    if len(active) != 1:
        fail(
            f"refusing {NOUN} send: exact frame count={len(matches)} "
            f"active_count={len(active)} for {CHAT!r}"
        )
    return active[0]


def find_editor(frame):
    editors = []
    for acc in walk(frame):
        if role(acc) not in ("text", "entry"):
            continue
        interfaces = [
            str(value) for value in (safe(acc.get_interfaces, []) or [])
        ]
        if "EditableText" in interfaces:
            editors.append(acc)
    if len(editors) != 1:
        fail(
            f"refusing {NOUN} send: editable input count={len(editors)} "
            f"for {CHAT!r}"
        )
    return editors[0]


def editor_text(editor):
    text_iface = safe(editor.get_text_iface)
    if text_iface is None:
        return None
    count = safe(text_iface.get_character_count)
    if count is None:
        return None
    count = int(count)
    if count <= 0:
        return ""
    value = safe(lambda: text_iface.get_text(0, count))
    if value is None:
        return None
    return str(value)


def focus_editor(editor):
    component = safe(editor.get_component_iface)
    if component is None:
        fail(f"refusing {NOUN} send: Component unavailable for {CHAT!r}")
    if safe(component.grab_focus, False) is False:
        fail(f"refusing {NOUN} send: grab_focus failed for {CHAT!r}")

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        states = safe(editor.get_state_set)
        if states is not None and safe(
            lambda: states.contains(Atspi.StateType.FOCUSED), False
        ):
            return
        time.sleep(0.05)
    fail(f"refusing {NOUN} send: editor focus not confirmed for {CHAT!r}")


def clear_paste_preview(editor):
    editable = safe(editor.get_editable_text_iface)
    if editable is None:
        fail(f"cannot clear {NOUN} preview: EditableText unavailable for {CHAT!r}")
    ok = editable.set_text_contents("")
    observed = editor_text(editor)
    if ok is False or observed != "":
        fail(f"cannot clear {NOUN} preview for {CHAT!r}")
