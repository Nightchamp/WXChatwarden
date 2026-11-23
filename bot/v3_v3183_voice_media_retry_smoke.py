from __future__ import annotations

from io import BytesIO
import tempfile
from unittest.mock import patch

import pysilk

from chatwarden import voice_pipeline as voice_mod
from chatwarden.db_reader import VoiceMediaNotFound
from chatwarden.voice_pipeline import VoicePipeline


def _silk_fixture() -> bytes:
    pcm = BytesIO(b"\x00\x00" * 2400)
    silk = BytesIO()
    pysilk.encode(pcm, silk, 24000, 24000)
    return silk.getvalue()


EVENT = {
    "local_type": 34,
    "chat_username": "123456@chatroom",
    "local_id": 77,
    "server_id": 9001,
    "timestamp": 1789439885,
}


class FlakyDb:
    def __init__(self, misses, error=VoiceMediaNotFound):
        self.misses = misses
        self.error = error
        self.calls = 0

    def voice_data_for_event(self, selected):
        self.calls += 1
        if self.calls <= self.misses:
            raise self.error("voice media not found")
        return _silk_fixture()


def run(db):
    sleeps = []
    with tempfile.TemporaryDirectory() as tmp, patch.object(
        voice_mod.time, "sleep", sleeps.append
    ), patch.object(voice_mod.core, "log", lambda *_: None):
        pipeline = VoicePipeline(db, cache_dir=tmp)
        try:
            path = pipeline.decode_event(dict(EVENT))
        except Exception as exc:  # noqa: BLE001 - asserted by caller
            return exc, sleeps
        pipeline.cleanup(path)
        return path, sleeps


def main() -> None:
    delays = VoicePipeline.MEDIA_RETRY_DELAYS
    assert sum(delays) <= 10.0

    # Immediate hit: no wait at all.
    db = FlakyDb(0)
    result, sleeps = run(db)
    assert isinstance(result, str) and db.calls == 1 and sleeps == []

    # Row appears after two misses: recovered with bounded waits.
    db = FlakyDb(2)
    result, sleeps = run(db)
    assert isinstance(result, str) and db.calls == 3
    assert sleeps == list(delays[:2])

    # Never appears: exactly len(delays)+1 attempts, then the same error.
    db = FlakyDb(99)
    result, sleeps = run(db)
    assert isinstance(result, VoiceMediaNotFound)
    assert isinstance(result, RuntimeError)
    assert db.calls == len(delays) + 1 and sleeps == list(delays)

    # Other failures (missing key, bad payload) are not retried.
    db = FlakyDb(99, error=RuntimeError)
    result, sleeps = run(db)
    assert type(result) is RuntimeError and db.calls == 1 and sleeps == []

    print("V3183_VOICE_MEDIA_RETRY_SMOKE_PASS")


if __name__ == "__main__":
    main()
