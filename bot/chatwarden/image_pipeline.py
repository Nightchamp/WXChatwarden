from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from . import core
from .image_codec import load_codec


TOKEN_RE = re.compile(
    rb"[0-9a-fA-F]{32}"
)


class ImageResourceIncompleteError(RuntimeError):
    """The exact WeChat image exists only as an unreadably small thumbnail."""


_JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


class ImagePipeline:
    def __init__(
        self,
        db,
        cache_dir=(
            "/var/lib/chatwarden/"
            "db-image-cache-v3"
        ),
    ):
        self.db = db
        self.cache_dir = Path(
            cache_dir
        )
        self.cache_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        try:
            self.cache_dir.chmod(
                0o700
            )
        except Exception:
            pass

        self.max_age = float(
            os.environ.get(
                "CW_V3_IMAGE_CACHE_MAX_AGE",
                "600",
            )
        )
        self.resource_wait = float(
            os.environ.get(
                "CW_V3_RESOURCE_WAIT",
                "8",
            )
        )
        self._last_gc = 0.0

    def gc(
        self,
        force=False,
    ):
        now = time.time()
        if (
            not force
            and now - self._last_gc
            < 300
        ):
            return

        self._last_gc = now
        removed = 0

        for path in self.cache_dir.glob(
            "*"
        ):
            if not path.is_file():
                continue

            try:
                age = (
                    now
                    - path.stat().st_mtime
                )
                if age <= self.max_age:
                    continue
                path.unlink(
                    missing_ok=True
                )
                removed += 1
            except Exception:
                continue

        if removed:
            core.log(
                "image-cache GC removed "
                f"{removed} stale file(s)"
            )

    def cleanup(self, path):
        if not path:
            return
        try:
            Path(path).unlink(
                missing_ok=True
            )
        except Exception:
            pass

    def _resource_token(
        self,
        event,
    ):
        deadline = (
            time.monotonic()
            + self.resource_wait
        )
        attempt = 0

        while (
            time.monotonic()
            < deadline
        ):
            attempt += 1
            path = (
                self.db.resource_db_path()
            )

            conn = sqlite3.connect(path)
            try:
                chat_row = conn.execute(
                    """
                    SELECT rowid
                    FROM ChatName2Id
                    WHERE user_name=?
                    ORDER BY rowid DESC
                    LIMIT 1
                    """,
                    (
                        event[
                            "chat_username"
                        ],
                    ),
                ).fetchone()

                row = None

                if chat_row is not None:
                    chat_id = int(
                        chat_row[0]
                    )

                    if event[
                        "server_id"
                    ]:
                        row = conn.execute(
                            """
                            SELECT packed_info
                            FROM MessageResourceInfo
                            WHERE chat_id=?
                              AND message_local_id=?
                              AND message_svr_id=?
                            ORDER BY rowid DESC
                            LIMIT 1
                            """,
                            (
                                chat_id,
                                event[
                                    "local_id"
                                ],
                                event[
                                    "server_id"
                                ],
                            ),
                        ).fetchone()

                    if row is None:
                        row = conn.execute(
                            """
                            SELECT packed_info
                            FROM MessageResourceInfo
                            WHERE chat_id=?
                              AND message_local_id=?
                            ORDER BY rowid DESC
                            LIMIT 1
                            """,
                            (
                                chat_id,
                                event[
                                    "local_id"
                                ],
                            ),
                        ).fetchone()

                elif event[
                    "server_id"
                ]:
                    row = conn.execute(
                        """
                        SELECT packed_info
                        FROM MessageResourceInfo
                        WHERE message_svr_id=?
                          AND message_local_id=?
                        ORDER BY rowid DESC
                        LIMIT 1
                        """,
                        (
                            event[
                                "server_id"
                            ],
                            event[
                                "local_id"
                            ],
                        ),
                    ).fetchone()
            finally:
                conn.close()

            if (
                row
                and row[0] is not None
            ):
                packed = row[0]
                if isinstance(
                    packed,
                    (
                        bytearray,
                        memoryview,
                    ),
                ):
                    packed = bytes(
                        packed
                    )

                if isinstance(
                    packed,
                    bytes,
                ):
                    match = (
                        TOKEN_RE.search(
                            packed
                        )
                    )
                    if match:
                        if attempt > 1:
                            core.log(
                                "resource token "
                                "became ready after "
                                f"{attempt} "
                                "attempt(s); "
                                f"group="
                                f"{event['chat_name']!r} "
                                f"local_id="
                                f"{event['local_id']}"
                            )
                        return (
                            match.group(0)
                            .decode(
                                "ascii"
                            )
                            .lower()
                        )

            time.sleep(0.35)

        raise RuntimeError(
            "message_resource token "
            "not ready before timeout"
        )

    def _resource_paths(
        self,
        event,
        token,
    ):
        chat_hash = (
            hashlib.md5(
                event[
                    "chat_username"
                ].encode("utf-8")
            ).hexdigest()
        )
        month = time.strftime(
            "%Y-%m",
            time.localtime(
                event["timestamp"]
            ),
        )

        img_dir = (
            self.db.account_root
            / "msg"
            / "attach"
            / chat_hash
            / month
            / "Img"
        )

        original = (
            img_dir
            / f"{token}.dat"
        )
        thumbnail = (
            img_dir
            / f"{token}_t.dat"
        )

        return original, thumbnail

    def _dat_path(
        self,
        event,
        token,
    ):
        original, thumbnail = self._resource_paths(
            event,
            token,
        )

        started = time.monotonic()
        deadline = started + self.resource_wait
        original_grace = min(
            self.resource_wait,
            max(
                0.0,
                float(
                    os.environ.get(
                        "CW_V3_ORIGINAL_GRACE",
                        "1.5",
                    )
                ),
            ),
        )

        while (
            time.monotonic()
            < deadline
        ):
            if original.is_file():
                return original
            if (
                thumbnail.is_file()
                and time.monotonic() - started
                >= original_grace
            ):
                core.log(
                    "image resource original pending; "
                    f"group={event['chat_name']!r} "
                    f"local_id={event['local_id']} "
                    "using exact-token thumbnail fallback"
                )
                return thumbnail
            time.sleep(0.2)

        if thumbnail.is_file():
            core.log(
                "image resource original unavailable; "
                f"group={event['chat_name']!r} "
                f"local_id={event['local_id']} "
                "using exact-token thumbnail fallback"
            )
            return thumbnail

        raise RuntimeError(
            f"original image DAT "
            f"not ready for token={token}"
        )

    def _wait_for_original_dat(
        self,
        event,
        token,
        timeout,
    ):
        original, _thumbnail = self._resource_paths(
            event,
            token,
        )
        deadline = (
            time.monotonic()
            + max(0.0, float(timeout))
        )
        while time.monotonic() < deadline:
            if original.is_file():
                return original
            time.sleep(0.2)
        if original.is_file():
            return original
        return None

    def _vision_dimensions(
        self,
        data,
    ):
        data = bytes(data or b"")
        if (
            len(data) >= 24
            and data.startswith(
                b"\x89PNG\r\n\x1a\n"
            )
        ):
            width = int.from_bytes(
                data[16:20],
                "big",
            )
            height = int.from_bytes(
                data[20:24],
                "big",
            )
            if width > 0 and height > 0:
                return width, height
            return None

        if not data.startswith(b"\xff\xd8"):
            return None

        pos = 2
        size = len(data)
        while pos + 4 <= size:
            if data[pos] != 0xFF:
                pos += 1
                continue
            while pos < size and data[pos] == 0xFF:
                pos += 1
            if pos >= size:
                break

            marker = data[pos]
            pos += 1
            if marker in {0x01, 0xD8, 0xD9}:
                continue
            if pos + 2 > size:
                break

            segment_len = int.from_bytes(
                data[pos:pos + 2],
                "big",
            )
            if (
                segment_len < 2
                or pos + segment_len > size
            ):
                break
            if (
                marker in _JPEG_SOF_MARKERS
                and segment_len >= 7
            ):
                height = int.from_bytes(
                    data[pos + 3:pos + 5],
                    "big",
                )
                width = int.from_bytes(
                    data[pos + 5:pos + 7],
                    "big",
                )
                if width > 0 and height > 0:
                    return width, height
                return None
            pos += segment_len
        return None

    def _vision_bytes_are_tiny(
        self,
        data,
    ):
        dims = self._vision_dimensions(data)
        if not dims:
            return False
        width, height = dims
        min_long_edge = max(
            1,
            int(
                os.environ.get(
                    "CW_V396_THUMB_MIN_LONG_EDGE",
                    "160",
                )
            ),
        )
        min_pixels = max(
            1,
            int(
                os.environ.get(
                    "CW_V396_THUMB_MIN_PIXELS",
                    "12000",
                )
            ),
        )
        return (
            max(width, height) < min_long_edge
            or width * height < min_pixels
        )

    def vision_file_is_tiny(
        self,
        path,
    ):
        try:
            data = Path(path).read_bytes()
        except Exception:
            return False
        return self._vision_bytes_are_tiny(data)

    def _format(
        self,
        data,
    ):
        if data.startswith(
            b"\xff\xd8\xff"
        ):
            return "jpg"

        if data.startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            return "png"

        if data.startswith(
            (b"GIF87a", b"GIF89a")
        ):
            return "gif"

        if (
            len(data) >= 12
            and data[:4] == b"RIFF"
            and data[8:12]
            == b"WEBP"
        ):
            return "webp"

        if data.startswith(
            b"wxgf"
        ):
            return "wxgf"

        if (
            len(data) >= 12
            and data[4:8]
            == b"ftyp"
        ):
            brand = data[8:12]

            if brand in (
                b"avif",
                b"avis",
            ):
                return "avif"

            if brand in (
                b"heic",
                b"heix",
                b"hevc",
                b"hevx",
                b"heim",
                b"heis",
                b"hevm",
                b"hevs",
                b"mif1",
                b"msf1",
                b"avic",
                b"avcs",
            ):
                return "heif"

        return None

    def _wxgf_partition(
        self,
        data,
    ):
        if (
            len(data) < 15
            or not data.startswith(
                b"wxgf"
            )
        ):
            raise RuntimeError(
                "not a WXGF payload"
            )

        header_len = data[4]
        if header_len >= len(data):
            raise RuntimeError(
                "invalid WXGF header"
            )

        for pattern in (
            b"\x00\x00\x00\x01",
            b"\x00\x00\x01",
        ):
            parts = []
            offset = 0

            while (
                header_len + offset
                <= len(data)
            ):
                idx = data.find(
                    pattern,
                    header_len
                    + offset,
                )

                if idx < 0:
                    break

                if idx < 4:
                    offset += 1
                    continue

                length = (
                    int.from_bytes(
                        data[
                            idx - 4:idx
                        ],
                        "big",
                    )
                )

                if (
                    length <= 0
                    or idx + length
                    > len(data)
                ):
                    offset = (
                        idx
                        - header_len
                        + 1
                    )
                    continue

                parts.append(
                    (
                        idx,
                        length,
                    )
                )
                offset = (
                    idx
                    - header_len
                    + length
                )

            if parts:
                start, size = max(
                    parts,
                    key=lambda item:
                        item[1],
                )
                return data[
                    start:
                    start + size
                ]

        raise RuntimeError(
            "WXGF HEVC partition "
            "not found"
        )

    def _ffmpeg_jpeg(
        self,
        data,
        input_kind,
    ):
        # db-native-v3.3-fastvision
        # Vision does not need lossless PNG. Keep upload size small
        # while retaining high visual quality.
        ffmpeg = shutil.which(
            "ffmpeg"
        )
        if not ffmpeg:
            raise RuntimeError(
                "ffmpeg not found"
            )

        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
        ]

        if input_kind == "hevc":
            cmd += [
                "-f",
                "hevc",
            ]

        cmd += [
            "-i",
            "pipe:0",
            "-map_metadata",
            "-1",
            "-frames:v",
            "1",
            "-c:v",
            "mjpeg",
            "-q:v",
            os.environ.get(
                "CW_V3_JPEG_QSCALE",
                "3",
            ),
            "-f",
            "image2",
            "pipe:1",
        ]

        proc = subprocess.run(
            cmd,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

        if (
            proc.returncode != 0
            or not proc.stdout
            or not proc.stdout.startswith(
                b"\xff\xd8\xff"
            )
        ):
            err = (
                proc.stderr.decode(
                    "utf-8",
                    errors="replace",
                )[-300:]
            )
            raise RuntimeError(
                "ffmpeg JPEG conversion "
                f"failed: {err}"
            )

        return proc.stdout

    def _to_vision_jpeg(
        self,
        decoded,
        fmt,
    ):
        # db-native-v3.3-fastvision
        # A normal WeChat JPEG is already compact. Avoid a pointless
        # JPEG -> PNG expansion and avoid recompression when possible.
        direct_limit = int(
            os.environ.get(
                "CW_V3_DIRECT_JPEG_MAX_BYTES",
                "1000000",
            )
        )

        if (
            fmt == "jpg"
            and len(decoded) <= direct_limit
        ):
            return (
                decoded,
                "direct-jpeg",
            )

        if fmt == "wxgf":
            hevc = (
                self._wxgf_partition(
                    decoded
                )
            )
            return (
                self._ffmpeg_jpeg(
                    hevc,
                    "hevc",
                ),
                "wxgf-hevc-jpeg",
            )

        return (
            self._ffmpeg_jpeg(
                decoded,
                "auto",
            ),
            f"{fmt}-jpeg",
        )

    def _decode_dat_for_vision(
        self,
        dat_path,
    ):
        raw = dat_path.read_bytes()
        codec = getattr(self, "codec", None)
        if codec is None:
            codec = self.codec = load_codec(self)
        decoded, fmt, mode = codec.decode(raw)
        vision, vision_mode = self._to_vision_jpeg(
            decoded,
            fmt,
        )
        return (
            decoded,
            fmt,
            mode,
            vision,
            vision_mode,
        )

    def decode_event(
        self,
        event,
        require_vision_quality=False,
    ):
        # db-native-v3.3-fastvision
        self.gc()

        token = self._resource_token(
            event
        )
        dat_path = self._dat_path(
            event,
            token,
        )
        resource_variant = (
            "thumbnail"
            if dat_path.name == f"{token}_t.dat"
            else "original"
        )
        (
            decoded,
            fmt,
            mode,
            vision,
            vision_mode,
        ) = self._decode_dat_for_vision(
            dat_path
        )

        if (
            require_vision_quality
            and resource_variant == "thumbnail"
            and self._vision_bytes_are_tiny(vision)
        ):
            dims = self._vision_dimensions(vision)
            wait_cap = max(
                0.0,
                float(
                    os.environ.get(
                        "CW_V396_TINY_THUMB_ORIGINAL_WAIT_SEC",
                        "35",
                    )
                ),
            )
            arrival_window = max(
                0.0,
                float(
                    os.environ.get(
                        "CW_V396_ORIGINAL_ARRIVAL_WINDOW_SEC",
                        "35",
                    )
                ),
            )
            try:
                message_ts = float(
                    event.get("timestamp") or 0.0
                )
            except (TypeError, ValueError):
                message_ts = 0.0
            message_age = (
                max(0.0, time.time() - message_ts)
                if message_ts > 0.0
                else 0.0
            )
            wait_seconds = (
                min(
                    wait_cap,
                    max(0.0, arrival_window - message_age),
                )
                if message_ts > 0.0
                else wait_cap
            )
            core.log(
                "[v3.96-vision-quality] "
                f"group={event['chat_name']!r} "
                f"local_id={event['local_id']} "
                f"thumbnail_dims={dims} tiny=yes "
                f"message_age={message_age:.1f}s "
                f"wait_original={wait_seconds:.1f}s"
            )
            original_path = self._wait_for_original_dat(
                event,
                token,
                wait_seconds,
            )
            if original_path is None:
                raise ImageResourceIncompleteError(
                    "tiny thumbnail is not sufficient for Vision and "
                    "the original image is still unavailable"
                )

            dat_path = original_path
            resource_variant = "original"
            (
                decoded,
                fmt,
                mode,
                vision,
                vision_mode,
            ) = self._decode_dat_for_vision(
                dat_path
            )
            core.log(
                "[v3.96-vision-quality] "
                f"group={event['chat_name']!r} "
                f"local_id={event['local_id']} "
                "original_ready=yes upgraded=yes"
            )

        # Each consumer owns its decoded Vision file. Never reuse one
        # deterministic path for the same local_id: two queued replies may
        # reference the same WeChat image concurrently, and cleanup from the
        # first completed job must not unlink the second job's input.
        fd, out_name = tempfile.mkstemp(
            prefix=(
                f"{event['chat_name']}"
                f"-{event['local_id']}"
                f"-{token}-"
            ),
            suffix=".jpg",
            dir=self.cache_dir,
        )
        os.close(fd)
        out = Path(out_name)
        out.write_bytes(vision)
        try:
            out.chmod(0o600)
        except Exception:
            pass

        ratio = (
            len(vision)
            / max(1, len(decoded))
        )

        core.log(
            f"image ready group="
            f"{event['chat_name']!r} "
            f"local_id="
            f"{event['local_id']} "
            f"source_format={fmt} "
            f"mode={mode} "
            f"vision_format=jpeg "
            f"vision_mode={vision_mode} "
            f"bytes={len(vision)} "
            f"decoded_bytes={len(decoded)} "
            f"ratio={ratio:.3f} "
            "source=message_resource; "
            f"resource_variant={resource_variant}; "
            "screenshot=no"
        )

        return str(out)
