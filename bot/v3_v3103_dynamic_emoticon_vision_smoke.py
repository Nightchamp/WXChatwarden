from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path


BOT_ROOT = Path(__file__).resolve().parent
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))

from chatwarden.emoticon_pipeline import EmoticonPipeline  # noqa: E402


def _ffmpeg_bytes(args):
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(
            "fixture ffmpeg failed: "
            + proc.stderr.decode("utf-8", errors="replace")[-300:]
        )
    return proc.stdout


def _animated_gif():
    return _ffmpeg_bytes(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=40x32:rate=4:duration=1",
            "-frames:v",
            "4",
            "-f",
            "gif",
            "pipe:1",
        ]
    )


def _static_png():
    return _ffmpeg_bytes(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=40x32:rate=1:duration=1",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ]
    )


def _event(md5):
    return {
        "local_id": 3103,
        "local_type": 47,
        "raw_content": (
            f'<msg><emoji md5="{md5}" cdnurl="https://example.invalid/x" />'
            "</msg>"
        ),
    }


def _jpeg_dimensions(path):
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=10,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-300:])
    width, height = proc.stdout.strip().split("x", 1)
    return int(width), int(height)


def main():
    with tempfile.TemporaryDirectory(prefix="v3103-emoticon-") as tmp:
        base = Path(tmp)
        pipeline = EmoticonPipeline(
            cache_dir=base / "cache",
            runtime_dir=base / "runtime",
        )

        animated = _animated_gif()
        animated_md5 = hashlib.md5(animated).hexdigest()

        # Simulate a pre-v3.103 first-frame cache. Dynamic support must not
        # silently keep returning this stale single-frame JPEG.
        legacy_cache = pipeline.cache_dir / f"{animated_md5}.jpg"
        legacy_cache.write_bytes(pipeline._ffmpeg_jpeg(animated))

        original_recover = pipeline._recover_original
        pipeline._recover_original = lambda meta: (
            animated,
            "gif",
            "fixture",
        )
        try:
            path = Path(pipeline.decode_event(_event(animated_md5)))
        finally:
            pipeline._recover_original = original_recover

        width, height = _jpeg_dimensions(path)
        assert width > height * 3, (width, height)
        print("V3103_DYNAMIC_EMOTICON_CONTACT_SHEET=PASS")

        static = _static_png()
        static_md5 = hashlib.md5(static).hexdigest()
        pipeline._recover_original = lambda meta: (
            static,
            "png",
            "fixture",
        )
        try:
            static_path = Path(pipeline.decode_event(_event(static_md5)))
        finally:
            pipeline._recover_original = original_recover

        static_width, static_height = _jpeg_dimensions(static_path)
        assert static_width < static_height * 2, (
            static_width,
            static_height,
        )
        leaked_probes = list(pipeline.runtime_dir.glob("probe-*.media"))
        assert leaked_probes == [], leaked_probes
        print("V3103_STATIC_EMOTICON_STAYS_SINGLE_FRAME=PASS")
        print("V3103_EMOTICON_PROBE_TEMP_CLEANUP=PASS")


if __name__ == "__main__":
    main()
