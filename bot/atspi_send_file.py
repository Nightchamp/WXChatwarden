#!/usr/bin/env python3
"""v3.186: fail-closed WeChat file send via the native Send File button."""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi


CHAT = sys.argv[1] if len(sys.argv) > 1 else ""
FILE_PATH = sys.argv[2] if len(sys.argv) > 2 else ""

MAX_FILE_BYTES = 20 * 1024 * 1024
FILE_OBJECT_TEXT = "\ufffc"
# WeChat's Qt build runs in the POSIX locale and cannot open non-ASCII paths.
SAFE_PATH = re.compile(r"/home/wechat/\.cache/bot-files/[A-Za-z0-9_-]+/[A-Za-z0-9][A-Za-z0-9._-]{0,99}")

import atspi_send_common as _common  # noqa: E402  (script dir is on sys.path)

_common.configure(CHAT, "file")
from atspi_send_common import (  # noqa: E402
    fail, safe, role, name, children, walk, xdotool,
    exact_visible_windows, active_window_id, find_exact_frame,
    find_editor, editor_text, focus_editor, clear_paste_preview,
)


def open_dialogs():
    found = []
    for app in children(Atspi.get_desktop(0)):
        if "wechat" not in name(app).lower():
            continue
        for acc in children(app):
            if role(acc) == "dialog" and name(acc).strip() == "Open":
                found.append(acc)
    return found


def bounds(acc):
    component = safe(acc.get_component_iface)
    rect = safe(lambda: component.get_extents(Atspi.CoordType.SCREEN)) if component else None
    if rect is None or rect.width <= 0 or rect.height <= 0:
        return None
    return rect.x, rect.y, rect.width, rect.height


def find_send_file_button(frame):
    frame_box = bounds(frame)
    buttons = []
    for acc in walk(frame):
        if role(acc) == "push-button" and name(acc).strip() == "Send File":
            box = bounds(acc)
            if box is not None:
                buttons.append(box)
    if len(buttons) != 1 or frame_box is None:
        fail(f"refusing file send: visible Send File button count={len(buttons)} for {CHAT!r}")
    x, y, w, h = buttons[0]
    fx, fy, fw, fh = frame_box
    cx, cy = x + w // 2, y + h // 2
    if not (fx <= cx < fx + fw and fy <= cy < fy + fh):
        fail(f"refusing file send: Send File button outside exact frame for {CHAT!r}")
    return cx, cy


def close_dialog_if_open():
    if open_dialogs():
        xdotool(["key", "Escape"], check=False)
        time.sleep(0.5)


def main():
    if not CHAT:
        fail("missing chat name")
    if not FILE_PATH or not SAFE_PATH.fullmatch(FILE_PATH):
        fail("refusing file send: file path must be a staged ASCII bot-files path")
    path = Path(FILE_PATH)
    try:
        if path.is_symlink() or not path.is_file():
            fail("refusing file send: staged file is not a regular file")
        size = path.stat().st_size
    except OSError as exc:
        fail(f"file input unavailable: {type(exc).__name__}")
    if size <= 0 or size > MAX_FILE_BYTES:
        fail(f"invalid file size={size}")

    Atspi.init()
    if open_dialogs():
        fail("refusing file send: an Open dialog is already present")
    frame = find_exact_frame()
    windows = exact_visible_windows(CHAT)
    if not windows:
        fail(f"refusing file send: exact visible X11 window count=0 for {CHAT!r}")
    if len(windows) == 1:
        target_raw = windows[0]
    else:
        active_id = active_window_id()
        by_id = {int(window_id, 0): window_id for window_id in windows}
        target_raw = by_id.get(active_id)
        if target_raw is None:
            fail(f"refusing file send: duplicate exact windows={len(windows)} "
                 f"but active window is not target for {CHAT!r}")
    target = int(target_raw, 0)
    xdotool(["windowactivate", "--sync", target_raw])
    time.sleep(0.15)
    if active_window_id() != target:
        fail(f"refusing file send: exact X11 window not active for {CHAT!r}")

    editor = find_editor(frame)
    initial_text = editor_text(editor)
    if initial_text is None:
        fail(f"refusing file send: editor text unavailable for {CHAT!r}")
    if initial_text:
        fail(f"refusing file send: editor is not empty for {CHAT!r}")

    # WeChat's own "Send File" toolbar button opens a Qt "Open" file dialog.
    # The button exposes no AT-SPI action, so click the centre of its exact
    # accessible extents inside the verified, active target frame only.
    cx, cy = find_send_file_button(frame)
    if active_window_id() != target:
        fail(f"refusing file send: X11 focus changed before button click for {CHAT!r}")
    xdotool(["mousemove", str(cx), str(cy), "click", "1"])

    dialog = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        time.sleep(0.10)
        dialogs = open_dialogs()
        if len(dialogs) == 1:
            dialog = dialogs[0]
            break
        if len(dialogs) > 1:
            close_dialog_if_open()
            fail("refusing file send: more than one Open dialog")
    if dialog is None:
        fail(f"refusing file send: Open dialog did not appear for {CHAT!r}")
    try:
        fields = [
            acc for acc in walk(dialog)
            if role(acc) in ("text", "entry") and name(acc).strip() == "File name:"
            and "EditableText" in [str(v) for v in (safe(acc.get_interfaces, []) or [])]
        ]
        if len(fields) != 1:
            fail(f"refusing file send: file-name field count={len(fields)}")
        editable = safe(fields[0].get_editable_text_iface)
        if editable is None or editable.set_text_contents(FILE_PATH) is False:
            fail("refusing file send: cannot fill file-name field")
        if editor_text(fields[0]) != FILE_PATH:
            fail("refusing file send: file-name field not confirmed")
        active = xdotool(["getactivewindow", "getwindowname"], check=False).stdout.strip()
        if active != "Open":
            fail("refusing file send: Open dialog is not the active window")
        subprocess.run(["/opt/tools/key", "Return"], capture_output=True, timeout=5)
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and open_dialogs():
            time.sleep(0.10)
        if open_dialogs():
            fail("refusing file send: Open dialog did not accept the file")
    except SystemExit:
        close_dialog_if_open()
        raise

    # Selecting a file places exactly one attachment object in the editor;
    # nothing is sent yet. Poll briefly for the accessibility tree to catch up.
    frame = find_exact_frame()
    editor = find_editor(frame)
    deadline = time.monotonic() + 3.0
    attached = ""
    while time.monotonic() < deadline:
        attached = editor_text(editor)
        if attached:
            break
        time.sleep(0.10)
    if attached != FILE_OBJECT_TEXT:
        if attached:
            clear_paste_preview(editor)
        fail(f"refusing file send: attachment object not confirmed; editor_text={attached!r}")

    # Re-prove exact window/editor/object immediately before the only Return.
    windows = exact_visible_windows(CHAT)
    if target_raw not in windows:
        clear_paste_preview(editor)
        fail("refusing file send: target X11 window changed before Return")
    xdotool(["windowactivate", "--sync", target_raw])
    time.sleep(0.10)
    if active_window_id() != target:
        clear_paste_preview(editor)
        fail("refusing file send: target X11 window lost focus before Return")
    if editor_text(editor) != FILE_OBJECT_TEXT:
        clear_paste_preview(editor)
        fail("refusing file send: attachment object disappeared before Return")
    try:
        focus_editor(editor)
    except SystemExit:
        try:
            clear_paste_preview(editor)
        except SystemExit:
            pass
        raise
    if active_window_id() != target:
        clear_paste_preview(editor)
        fail("refusing file send: X11 focus changed after editor refocus")
    result = subprocess.run(["/opt/tools/key", "Return"], capture_output=True, timeout=5)
    if result.returncode != 0:
        try:
            clear_paste_preview(editor)
        except SystemExit:
            pass
        fail("Return key failed: " + result.stderr.decode("utf-8", errors="replace")[:300])

    # Never press Return twice. An editor that does not clear is reported as an
    # unconfirmed send so the caller never retries automatically.
    deadline = time.monotonic() + 3.0
    last = None
    while time.monotonic() < deadline:
        last = editor_text(editor)
        if last == "":
            break
        time.sleep(0.10)
    if last != "":
        detail = "editor_text_unavailable" if last is None else f"editor_not_empty text={last!r}"
        fail(f"FILE_SEND_UNCONFIRMED {detail}", code=43)

    # Best effort: watch the chat bubble for this file name until the
    # "Uploading" marker disappears. Submission already happened either way.
    base = path.name
    uploaded = "unknown"
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        names = [name(acc) for acc in walk(find_exact_frame()) if role(acc) == "list-item"]
        mine = [n for n in names if base in n]
        if mine:
            last_item = mine[-1]
            if "Uploading" in last_item or "上传中" in last_item:
                uploaded = "uploading"
            elif "Fail" in last_item or "失败" in last_item:
                uploaded = "failed"
                break
            else:
                uploaded = "yes"
                break
        time.sleep(0.5)
    print(f"FILE_SEND=PASS editor_empty=yes upload={uploaded} size={size}")


if __name__ == "__main__":
    main()
