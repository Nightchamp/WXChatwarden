#!/usr/bin/env python3
"""v3.201: shared AT-SPI send helpers keep each script's exact failure text.

core.py matches exit code 42 + IMAGE_FRAME_NOT_FOUND / FILE_FRAME_NOT_FOUND,
so the extracted module must reproduce the per-script wording byte for byte.
Offline: Atspi desktop and xdotool are faked; nothing touches the GUI.
"""
from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path
from types import SimpleNamespace

import atspi_send_common as common

BOT_DIR = Path(__file__).resolve().parent


def _failure(fn):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            fn()
        except SystemExit as exc:
            return exc.code, err.getvalue().strip()
    raise AssertionError("expected fail()")


def main():
    empty_desktop = SimpleNamespace(get_child_count=lambda: 0)
    original_atspi, original_xdotool = common.Atspi, common.xdotool
    common.Atspi = SimpleNamespace(get_desktop=lambda index: empty_desktop)
    try:
        for noun in ("image", "file"):
            common.configure("v3201-chat", noun)
            assert _failure(common.find_exact_frame) == (
                42, f"{noun.upper()}_FRAME_NOT_FOUND 'v3201-chat'"
            )
            common.xdotool = lambda args, check=True: SimpleNamespace(stdout="  ")
            assert _failure(common.active_window_id) == (
                1, f"refusing {noun} send: active X11 window id is empty"
            )
            common.xdotool = original_xdotool
    finally:
        common.Atspi, common.xdotool = original_atspi, original_xdotool

    # Each script configures the shared module with its own noun.
    for script, noun in (("atspi_send_image.py", "image"), ("atspi_send_file.py", "file")):
        source = (BOT_DIR / script).read_text(encoding="utf-8")
        assert re.search(r'_common\.configure\(CHAT, "%s"\)' % noun, source), script
        assert "def find_exact_frame" not in source, script
    print("V3201_ATSPI_SEND_COMMON=PASS")


if __name__ == "__main__":
    main()
