#!/usr/bin/env python3
from __future__ import annotations

from chatwarden import core


def main() -> None:
    calls = []
    original = core.run_as_client_user

    def fake_run(args, timeout=20, input_bytes=None):
        calls.append((list(args), timeout, input_bytes))
        return None

    core.run_as_client_user = fake_run
    try:
        core.double_click_node(
            {
                "bounds": {
                    "x": 100,
                    "y": 200,
                    "width": 80,
                    "height": 40,
                }
            }
        )
    finally:
        core.run_as_client_user = original

    assert len(calls) == 1, calls
    args, timeout, input_bytes = calls[0]
    assert args[:2] == ["xdotool", "mousemove"], args
    assert "--sync" not in args, args
    assert timeout <= 3, timeout
    assert input_bytes is None

    print("V373_SEND_WINDOW_MOUSEMOVE_NONBLOCKING=PASS")
    print("V373_SEND_WINDOW_CLICK_TIMEOUT_BOUNDED=PASS")


if __name__ == "__main__":
    main()
