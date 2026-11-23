#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chatwarden.image_pipeline import ImagePipeline


def _fake_jpeg(width: int, height: int) -> bytes:
    """Enough JPEG structure for the production dimension parser seam."""
    return (
        b"\xff\xd8"
        + b"\xff\xc0\x00\x11\x08"
        + int(height).to_bytes(2, "big")
        + int(width).to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        + b"\xff\xd9"
    )


def _event(chat_username: str) -> dict:
    return {
        "chat_name": "fixture",
        "chat_username": chat_username,
        "local_id": 1,
        "timestamp": time.time(),
    }


def _img_dir(root: Path, chat_username: str, timestamp: float) -> Path:
    chat_hash = hashlib.md5(chat_username.encode("utf-8")).hexdigest()
    month = time.strftime("%Y-%m", time.localtime(timestamp))
    return root / "msg" / "attach" / chat_hash / month / "Img"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chat = "fixture@chatroom"
        event = _event(chat)
        image_dir = _img_dir(root, chat, event["timestamp"])
        image_dir.mkdir(parents=True)

        pipe = object.__new__(ImagePipeline)
        pipe.db = SimpleNamespace(account_root=root)
        pipe.resource_wait = 0.15

        token = "1" * 32
        original = image_dir / f"{token}.dat"
        thumb = image_dir / f"{token}_t.dat"

        thumb.write_bytes(b"thumb")
        with mock.patch.dict(
            os.environ,
            {"CW_V3_ORIGINAL_GRACE": "0"},
            clear=False,
        ):
            selected = pipe._dat_path(event, token)
        assert selected == thumb, selected

        original.write_bytes(b"original")
        with mock.patch.dict(
            os.environ,
            {"CW_V3_ORIGINAL_GRACE": "0"},
            clear=False,
        ):
            selected = pipe._dat_path(event, token)
        assert selected == original, selected

    # v3.96 regression: the real 2026-09-03 incident had a 72x40 thumbnail
    # available quickly while the 518x40 text screenshot original arrived
    # later. A Vision request must wait for the original instead of treating
    # the unreadably tiny thumbnail as valid visual evidence.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chat = "fixture@chatroom"
        event = _event(chat)
        image_dir = _img_dir(root, chat, event["timestamp"])
        image_dir.mkdir(parents=True)
        cache_dir = root / "cache"
        cache_dir.mkdir()

        pipe = object.__new__(ImagePipeline)
        pipe.db = SimpleNamespace(account_root=root)
        pipe.cache_dir = cache_dir
        pipe.resource_wait = 0.02
        pipe.max_age = 600.0
        pipe._last_gc = 0.0
        pipe.gc = lambda *args, **kwargs: None
        pipe._resource_token = lambda _event: token
        tiny = _fake_jpeg(72, 40)
        full = _fake_jpeg(518, 40)
        pipe.codec = SimpleNamespace(
            decode=lambda raw: (
                (tiny, "jpg", "stub") if raw == b"thumb" else (full, "jpg", "stub")
            )
        )

        thumb = image_dir / f"{token}_t.dat"
        original = image_dir / f"{token}.dat"
        thumb.write_bytes(b"thumb")

        def publish_original():
            time.sleep(0.05)
            original.write_bytes(b"original")

        publisher = threading.Thread(target=publish_original, daemon=True)
        publisher.start()
        with mock.patch.dict(
            os.environ,
            {
                "CW_V3_ORIGINAL_GRACE": "0",
                "CW_V396_TINY_THUMB_ORIGINAL_WAIT_SEC": "0.25",
            },
            clear=False,
        ):
            vision_path = pipe.decode_event(
                event,
                require_vision_quality=True,
            )
        publisher.join(timeout=1.0)
        rendered = Path(vision_path).read_bytes()
        assert pipe._vision_dimensions(rendered) == (518, 40), (
            "tiny thumbnail must be replaced by the later original before Vision"
        )

        # Two queued replies may reference the same exact WeChat image. Each
        # decode must return a separately owned temp file so cleanup of one job
        # cannot invalidate the other queued job's input.
        second_vision_path = pipe.decode_event(
            event,
            require_vision_quality=True,
        )
        assert second_vision_path != vision_path, (
            "same local_id must not share one cleanup-owned Vision temp path"
        )
        assert Path(second_vision_path).read_bytes() == rendered
        Path(vision_path).unlink(missing_ok=True)
        assert Path(second_vision_path).is_file(), (
            "cleanup of one Vision consumer must not delete another consumer's input"
        )
        Path(second_vision_path).unlink(missing_ok=True)

        original.unlink()
        with mock.patch.dict(
            os.environ,
            {
                "CW_V3_ORIGINAL_GRACE": "0",
                "CW_V396_TINY_THUMB_ORIGINAL_WAIT_SEC": "0.03",
            },
            clear=False,
        ):
            try:
                pipe.decode_event(
                    event,
                    require_vision_quality=True,
                )
            except Exception as exc:
                assert "tiny thumbnail" in str(exc).lower(), str(exc)
            else:
                raise AssertionError(
                    "Vision must fail closed when only an unreadably tiny thumbnail exists"
                )

    print("V373_IMAGE_RESOURCE_THUMBNAIL_FALLBACK=PASS")
    print("V373_IMAGE_RESOURCE_ORIGINAL_PREFERRED=PASS")
    print("V396_TINY_THUMBNAIL_VISION_GUARD=PASS")
    print("V3137_VISION_TEMP_OWNERSHIP=PASS")


if __name__ == "__main__":
    main()
