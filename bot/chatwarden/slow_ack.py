"""v3.205 "still working" notice for slow model calls.

Image recognition usually answers in 15-40 s but can take over 90 s when the
primary model times out before the fallback runs. The reply lanes already
keep other chats and other senders flowing during that time; what the asker
lacked was any sign that the bot had seen the image.

``SlowAck`` sends one short notice if the guarded call is still running after
a delay, and never after the call has finished: ``finish()`` waits for an
in-progress notice, so the notice always lands before the real reply.

``CW_VISION_SLOW_ACK_SEC`` sets the delay for image recognition (default 20;
0 turns the notice off). Log tag: ``[v3.205-slow-ack]``.
"""
from __future__ import annotations

import os
import secrets
import threading

VISION_ACK_TEXTS = (
    "图我在看了，稍等一下~",
    "收到图了，正在看，马上回你~",
    "这张图我看一下，稍等~",
)


def vision_delay():
    raw = str(os.environ.get("CW_VISION_SLOW_ACK_SEC", "20") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 20.0
    return value if value > 0 else 0.0


def vision_text():
    return secrets.choice(VISION_ACK_TEXTS)


class SlowAck:
    """Run ``send()`` once after ``delay`` seconds unless finished first."""

    def __init__(self, delay, send, *, log=None, label=""):
        self._delay = float(delay or 0.0)
        self._send = send
        self._log = log
        self._label = str(label or "")
        self._lock = threading.Lock()
        self._done = False
        self._timer = None
        self.acked = False

    def start(self):
        if self._delay <= 0:
            return self
        self._timer = threading.Timer(self._delay, self._fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def _fire(self):
        # Hold the lock while sending, so finish() (and therefore the real
        # reply) waits for a notice that has already started.
        with self._lock:
            if self._done:
                return
            self.acked = True
            try:
                self._send()
                outcome = "sent"
            except Exception as exc:
                outcome = f"failed:{type(exc).__name__}"
        if self._log is not None:
            try:
                self._log(
                    f"[v3.205-slow-ack] {self._label} "
                    f"after={self._delay:.0f}s notice={outcome}"
                )
            except Exception:
                pass

    def finish(self):
        """Stop the timer; returns True if the notice was sent."""
        timer = self._timer
        if timer is not None:
            timer.cancel()
        with self._lock:
            self._done = True
        return self.acked

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.finish()
        return False
