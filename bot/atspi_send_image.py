#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi


CHAT = sys.argv[1] if len(sys.argv) > 1 else ""
IMAGE_PATH = sys.argv[2] if len(sys.argv) > 2 else ""
VERIFY_ONLY = "--verify-only" in sys.argv[3:]

MAX_IMAGE_BYTES = 25 * 1024 * 1024
IMAGE_OBJECT_TEXT = "\ufffc"

import atspi_send_common as _common  # noqa: E402  (script dir is on sys.path)

_common.configure(CHAT, "image")
from atspi_send_common import (  # noqa: E402
    fail, xdotool, exact_visible_windows, active_window_id,
    find_exact_frame, find_editor, editor_text, focus_editor,
    clear_paste_preview,
)


def main():
    if not CHAT:
        fail("missing chat name")
    if not IMAGE_PATH:
        fail("missing image path")

    image = Path(IMAGE_PATH)
    try:
        size = image.stat().st_size
        header = image.read_bytes()[:8]
    except Exception as exc:
        fail(f"image input unavailable: {type(exc).__name__}")
    if size <= 0 or size > MAX_IMAGE_BYTES:
        fail(f"invalid image size={size}")
    if header != b"\x89PNG\r\n\x1a\n":
        fail("phase-1 image send accepts PNG only")

    Atspi.init()
    frame = find_exact_frame()
    windows = exact_visible_windows(CHAT)
    if not windows:
        fail(
            f"refusing image send: exact visible X11 window count=0 for {CHAT!r}"
        )
    if len(windows) == 1:
        target_raw = windows[0]
    else:
        active_id = active_window_id()
        by_id = {
            int(window_id, 0): window_id
            for window_id in windows
        }
        target_raw = by_id.get(active_id)
        if target_raw is None:
            fail(
                f"refusing image send: duplicate exact windows={len(windows)} "
                f"but active window is not target for {CHAT!r}"
            )
    target = int(target_raw, 0)
    xdotool(["windowactivate", "--sync", target_raw])
    time.sleep(0.08)
    if active_window_id() != target:
        fail(f"refusing image send: exact X11 window not active for {CHAT!r}")

    editor = find_editor(frame)
    initial_text = editor_text(editor)
    if initial_text is None:
        fail(f"refusing image send: editor text unavailable for {CHAT!r}")
    if initial_text:
        fail(f"refusing image send: editor is not empty for {CHAT!r}")
    focus_editor(editor)
    if active_window_id() != target:
        fail(f"refusing image send: X11 focus changed before paste for {CHAT!r}")

    clip = subprocess.Popen(
        [
            "xclip",
            # xclip defaults to -silent, which forks into the background.
            # Keep it in the foreground so this process can reliably wait for
            # the one clipboard consumer and, on timeout, reap the exact child
            # instead of leaving an orphan that still owns stderr/the X11
            # selection.
            "-quiet",
            "-selection",
            "clipboard",
            "-t",
            "image/png",
            "-i",
            str(image),
            "-loops",
            "1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.20)
    paste = subprocess.run(
        ["/opt/tools/key", "ctrl+v"],
        capture_output=True,
        timeout=5,
    )
    if paste.returncode != 0:
        clip.kill()
        fail("refusing image send: Ctrl+V failed")
    try:
        _, clip_stderr = clip.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        clip.kill()
        clip.communicate()
        fail("refusing image send: clipboard image was not consumed")
    if clip.returncode != 0:
        fail(
            "refusing image send: xclip failed: "
            + clip_stderr.decode("utf-8", errors="replace")[:300]
        )

    # WeChat exposes a successfully pasted image as exactly one U+FFFC object
    # replacement character in the same EditableText. Poll briefly because the
    # accessibility tree can lag behind the clipboard event.
    deadline = time.monotonic() + 2.0
    pasted_text = ""
    while time.monotonic() < deadline:
        pasted_text = editor_text(editor)
        if pasted_text:
            break
        time.sleep(0.10)
    if pasted_text != IMAGE_OBJECT_TEXT:
        if pasted_text:
            clear_paste_preview(editor)
        fail(
            "refusing image send: pasted image object was not confirmed; "
            f"editor_text={pasted_text!r}"
        )

    if VERIFY_ONLY:
        clear_paste_preview(editor)
        print("IMAGE_PASTE_VERIFY=PASS object=U+FFFC")
        return

    # Re-prove exact frame/editor and the image object immediately before the
    # only send keystroke. No coordinate click or screenshot/OCR is involved.
    frame = find_exact_frame()
    windows = exact_visible_windows(CHAT)
    if len(windows) != 1 or int(windows[0], 0) != target:
        clear_paste_preview(editor)
        fail("refusing image send: target X11 window changed before Return")
    if active_window_id() != target:
        clear_paste_preview(editor)
        fail("refusing image send: target X11 window lost focus before Return")
    editor = find_editor(frame)
    pre_return_text = editor_text(editor)
    if pre_return_text != IMAGE_OBJECT_TEXT:
        if pre_return_text:
            clear_paste_preview(editor)
        fail("refusing image send: image object disappeared before Return")
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
        fail("refusing image send: X11 focus changed after editor refocus")
    result = subprocess.run(
        ["/opt/tools/key", "Return"],
        capture_output=True,
        timeout=5,
    )
    if result.returncode != 0:
        try:
            clear_paste_preview(editor)
        except SystemExit:
            pass
        fail(
            "Return key failed: "
            + result.stderr.decode("utf-8", errors="replace")[:300]
        )

    # Successful submission should clear the image object from the editor.
    # Never press Return a second time: if WeChat does not confirm the clear,
    # surface an explicit unconfirmed-send failure to the caller.
    deadline = time.monotonic() + 3.0
    last = None
    while time.monotonic() < deadline:
        last = editor_text(editor)
        if last == "":
            print("IMAGE_SEND=PASS object=U+FFFC editor_empty=yes")
            return
        time.sleep(0.10)
    if last is None:
        detail = "editor_text_unavailable"
    else:
        detail = f"editor_not_empty text={last!r}"
    fail(f"IMAGE_SEND_UNCONFIRMED {detail}", code=43)


if __name__ == "__main__":
    main()
