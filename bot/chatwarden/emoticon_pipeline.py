from __future__ import annotations

# db-native-v3.12-emoticon-pipeline

import base64
import hashlib
import importlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from . import core


_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_AES_RE = re.compile(r"^[0-9a-f]{32}$")


def _local_recover():
    """Optional extra recovery step from ``emoticon_local.py``, if present."""
    local_name = f"{__package__}.emoticon_local"
    try:
        local = importlib.import_module(local_name)
    except ModuleNotFoundError as exc:
        if exc.name != local_name:
            raise
        return None
    return local.recover


class EmoticonPipeline:
    _VISION_CACHE_VERSION = "v3103"
    _ANIMATION_SAMPLE_FRAMES = 4
    _ANIMATION_CELL_SIZE = 300

    def __init__(
        self,
        cache_dir="/var/lib/chatwarden/v312-emoticon-cache",
        runtime_dir="/var/lib/chatwarden/v312-emoticon-runtime",
    ):
        self.cache_dir = Path(cache_dir)
        self.runtime_dir = Path(runtime_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.cache_dir, self.runtime_dir):
            try:
                path.chmod(0o700)
            except Exception:
                pass

        self.http_timeout = float(
            os.environ.get(
                "CW_V312_EMOTICON_HTTP_TIMEOUT",
                "2.5",
            )
        )
        self.max_bytes = int(
            os.environ.get(
                "CW_V312_EMOTICON_MAX_BYTES",
                "5242880",
            )
        )
        self.type49_max_bytes = int(
            os.environ.get(
                "CW_V3142_TYPE49_EMOTICON_MAX_BYTES",
                "8388608",
            )
        )
        suffixes = os.environ.get(
            "CW_V312_EMOTICON_ALLOWED_SUFFIXES",
            "qq.com,qpic.cn,weixin.qq.com,wechat.com",
        )
        self.allowed_suffixes = tuple(
            x.strip().lower().lstrip(".")
            for x in suffixes.split(",")
            if x.strip()
        )

    @classmethod
    def is_emoticon(cls, event):
        try:
            cls._parse_meta(event)
            return True
        except Exception:
            return False

    @staticmethod
    def _format(data):
        if data.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "gif"
        if (
            len(data) >= 12
            and data[:4] == b"RIFF"
            and data[8:12] == b"WEBP"
        ):
            return "webp"
        if data.startswith(b"wxgf"):
            return "wxgf"
        if len(data) >= 12 and data[4:8] == b"ftyp":
            return "isobmff"
        return None

    @staticmethod
    def _parse_meta(event):
        try:
            local_type = int(event.get("local_type") or 0)
        except (TypeError, ValueError):
            local_type = 0
        base_type = local_type & 0xFFFFFFFF if local_type > 0xFFFFFFFF else local_type
        raw = str(event.get("raw_content") or "").strip()
        start = raw.find("<msg")
        if start < 0:
            raise RuntimeError("emoticon XML missing <msg>")

        try:
            root = ET.fromstring(raw[start:])
        except ET.ParseError as exc:
            raise RuntimeError(
                f"emoticon XML parse failed: {exc}"
            ) from exc

        if base_type == 49:
            app = root.find(".//appmsg")
            if app is None:
                raise RuntimeError("Type 49 XML missing <appmsg>")
            try:
                app_type = int(str(app.findtext("type") or "0").strip() or 0)
            except (TypeError, ValueError):
                app_type = 0
            if app_type != 8:
                raise RuntimeError("Type 49 appmsg is not emoticon attachment")
            attach = app.find("appattach")
            if attach is None:
                raise RuntimeError("Type 49 emoticon missing <appattach>")
            md5 = str(attach.findtext("emoticonmd5") or "").strip().lower()
            if not _MD5_RE.fullmatch(md5):
                raise RuntimeError("Type 49 emoticon md5 invalid")
            emojiinfo = str(attach.findtext("emojiinfo") or "").strip()
            try:
                decoded = base64.b64decode(emojiinfo + "===", validate=False)
            except Exception as exc:
                raise RuntimeError("Type 49 emojiinfo decode failed") from exc
            urls = [
                item.decode("ascii", errors="ignore")
                for item in re.findall(
                    rb"https?://[A-Za-z0-9:/?&=._%-]+",
                    decoded,
                )
            ]
            cdnurl = ""
            for item in urls:
                try:
                    query = urllib.parse.parse_qs(
                        urllib.parse.urlparse(item).query
                    )
                except Exception:
                    continue
                if str((query.get("m") or [""])[0]).lower() == md5:
                    cdnurl = item
                    break
            if not cdnurl:
                raise RuntimeError("Type 49 emoticon CDN URL missing")
            try:
                declared = int(str(attach.findtext("totallen") or "0").strip() or 0)
            except (TypeError, ValueError):
                declared = 0
            return {
                "md5": md5,
                "cdnurl": cdnurl,
                "encrypturl": "",
                "aeskey": "",
                "width": str(attach.findtext("cdnthumbwidth") or "").strip(),
                "height": str(attach.findtext("cdnthumbheight") or "").strip(),
                "desc": "",
                "productid": "",
                "declared_length": max(0, declared),
                "type49_app8": True,
            }

        if base_type != 47:
            raise RuntimeError("not a supported emoticon event")

        emoji = root.find(".//emoji")
        if emoji is None:
            raise RuntimeError("Type 47 XML missing <emoji>")

        def attr(name):
            return str(emoji.attrib.get(name) or "").strip()

        md5 = attr("md5").lower()
        if not _MD5_RE.fullmatch(md5):
            raise RuntimeError("Type 47 md5 invalid")

        aeskey = attr("aeskey").lower()
        if aeskey and not _AES_RE.fullmatch(aeskey):
            aeskey = ""

        return {
            "md5": md5,
            "cdnurl": attr("cdnurl"),
            "encrypturl": attr("encrypturl"),
            "aeskey": aeskey,
            "width": attr("width"),
            "height": attr("height"),
            "desc": attr("desc"),
            "productid": attr("productid"),
            "declared_length": 0,
            "type49_app8": False,
        }

    def _host_allowed(self, url):
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        host = str(parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            return False
        for suffix in self.allowed_suffixes:
            if host == suffix or host.endswith("." + suffix):
                return True
        return False

    def _download(self, url, max_bytes=None):
        if not self._host_allowed(url):
            raise RuntimeError("emoticon URL host rejected")

        budget = self.max_bytes if max_bytes is None else int(max_bytes)
        budget = max(1, budget)

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 WeChat-DB-native-v3.12",
                "Accept": "*/*",
            },
        )
        with urllib.request.urlopen(
            req,
            timeout=self.http_timeout,
        ) as resp:
            final_url = str(resp.geturl() or "")
            if not self._host_allowed(final_url):
                raise RuntimeError("emoticon redirect host rejected")
            data = resp.read(budget + 1)

        if len(data) > budget:
            raise RuntimeError(
                "emoticon payload exceeds max byte budget"
            )
        if not data:
            raise RuntimeError("empty emoticon payload")
        return data

    @staticmethod
    def _md5(data):
        return hashlib.md5(data).hexdigest()

    def _recover_original(self, meta):
        expected = meta["md5"]
        errors = []
        download_budget = self.max_bytes
        if meta.get("type49_app8"):
            declared = int(meta.get("declared_length") or 0)
            download_budget = min(
                self.type49_max_bytes,
                max(self.max_bytes, declared),
            )

        cdn = str(meta.get("cdnurl") or "")
        if cdn:
            try:
                data = self._download(cdn, max_bytes=download_budget)
                if self._md5(data) != expected:
                    raise RuntimeError("cdn md5 mismatch")
                fmt = self._format(data)
                if not fmt:
                    raise RuntimeError("cdn format unrecognized")
                return data, fmt, "cdn"
            except Exception as exc:
                errors.append(
                    "cdn=" + type(exc).__name__ + ":" + str(exc)[:120]
                )

        recover = _local_recover()
        if recover is not None:
            try:
                result = recover(self, meta, download_budget, expected)
                if result:
                    return result
            except Exception as exc:
                errors.append(
                    "local="
                    + type(exc).__name__
                    + ":"
                    + str(exc)[:120]
                )

        raise RuntimeError(
            "emoticon recovery failed; "
            + "; ".join(errors[-2:])
        )

    @staticmethod
    def _ffmpeg_jpeg(data):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found")

        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
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
                    "CW_V312_EMOTICON_JPEG_QSCALE",
                    "3",
                ),
                "-f",
                "image2",
                "pipe:1",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        if (
            proc.returncode != 0
            or not proc.stdout.startswith(b"\xff\xd8\xff")
        ):
            err = proc.stderr.decode(
                "utf-8",
                errors="replace",
            )[-300:]
            raise RuntimeError(
                "emoticon ffmpeg JPEG conversion failed: " + err
            )
        return proc.stdout

    def _probe_animation(self, data):
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None

        source = self.runtime_dir / (
            "probe-" + uuid.uuid4().hex[:12] + ".media"
        )
        self._atomic_bytes(source, data)
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-count_frames",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height,nb_frames,nb_read_frames",
                    "-of",
                    "default=noprint_wrappers=1",
                    str(source),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )
            values = {}
            for line in proc.stdout.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()

            raw_frames = (
                values.get("nb_read_frames")
                or values.get("nb_frames")
                or ""
            )
            try:
                frames = int(raw_frames)
            except (TypeError, ValueError):
                frames = 0
            if frames <= 1:
                source.unlink(missing_ok=True)
                return None

            return {
                "frames": frames,
                "source_path": source,
            }
        except Exception:
            source.unlink(missing_ok=True)
            return None

    def _animated_contact_sheet_jpeg(self, data):
        probe = self._probe_animation(data)
        if not probe:
            return None

        source = probe["source_path"]
        frames = int(probe["frames"])
        sample_count = min(
            self._ANIMATION_SAMPLE_FRAMES,
            frames,
        )
        try:
            if sample_count <= 1:
                return None

            indexes = []
            for index in range(sample_count):
                frame = round(
                    index
                    * (frames - 1)
                    / (sample_count - 1)
                )
                if frame not in indexes:
                    indexes.append(frame)
            if len(indexes) <= 1:
                return None

            select = "+".join(
                f"eq(n,{frame})"
                for frame in indexes
            )
            cell = self._ANIMATION_CELL_SIZE
            vf = (
                f"select='{select}',"
                f"scale={cell}:{cell}:force_original_aspect_ratio=decrease,"
                f"pad={cell}:{cell}:(ow-iw)/2:(oh-ih)/2,"
                f"tile={len(indexes)}x1:padding=4:margin=4"
            )

            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                return None
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-err_detect",
                    "ignore_err",
                    "-i",
                    str(source),
                    "-vf",
                    vf,
                    "-frames:v",
                    "1",
                    "-c:v",
                    "mjpeg",
                    "-q:v",
                    os.environ.get(
                        "CW_V312_EMOTICON_JPEG_QSCALE",
                        "3",
                    ),
                    "-f",
                    "image2",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15,
            )
            if proc.stdout.startswith(b"\xff\xd8\xff"):
                return proc.stdout, frames, len(indexes)
            return None
        finally:
            source.unlink(missing_ok=True)

    @staticmethod
    def _atomic_bytes(path, data):
        tmp = path.with_name(
            path.name + ".tmp-" + uuid.uuid4().hex[:8]
        )
        tmp.write_bytes(data)
        try:
            tmp.chmod(0o600)
        except Exception:
            pass
        os.replace(tmp, path)

    def _cache_path(self, md5, mode):
        return self.cache_dir / (
            f"{md5}-{self._VISION_CACHE_VERSION}-{mode}.jpg"
        )

    @staticmethod
    def _valid_jpeg_cache(path):
        try:
            return path.read_bytes().startswith(b"\xff\xd8\xff")
        except Exception:
            return False

    def _cached_visual(self, md5):
        for mode in ("dynamic", "static"):
            cache = self._cache_path(md5, mode)
            if self._valid_jpeg_cache(cache):
                return cache, mode
        return None, None

    def _cache_jpeg(self, md5, original, fmt):
        animated = self._animated_contact_sheet_jpeg(original)
        if animated is not None:
            vision, source_frames, sampled_frames = animated
            mode = "dynamic"
        else:
            vision = (
                original
                if fmt == "jpg"
                else self._ffmpeg_jpeg(original)
            )
            source_frames = 1
            sampled_frames = 1
            mode = "static"

        cache = self._cache_path(md5, mode)
        self._atomic_bytes(cache, vision)
        return cache, mode, source_frames, sampled_frames

    def _lease(self, cache, md5, event):
        local_id = int(event.get("local_id") or 0)
        lease = self.runtime_dir / (
            f"{md5}-{local_id}-{uuid.uuid4().hex[:10]}.jpg"
        )
        try:
            os.link(cache, lease)
        except Exception:
            shutil.copy2(cache, lease)
        try:
            lease.chmod(0o600)
        except Exception:
            pass
        return lease

    def decode_event(self, event):
        meta = self._parse_meta(event)
        md5 = meta["md5"]
        cache, mode = self._cached_visual(md5)
        cache_hit = cache is not None
        source = "cache"
        source_frames = None
        sampled_frames = None

        if not cache_hit:
            original, fmt, source = self._recover_original(meta)
            (
                cache,
                mode,
                source_frames,
                sampled_frames,
            ) = self._cache_jpeg(
                md5,
                original,
                fmt,
            )

        lease = self._lease(cache, md5, event)
        size = lease.stat().st_size
        core.log(
            "[v3.12-emoticon] "
            f"local_id={event.get('local_id')} "
            f"source={source} "
            f"cache_hit={'yes' if cache_hit else 'no'} "
            f"vision_bytes={size} "
            f"vision_mode={mode} "
            f"source_frames={source_frames if source_frames is not None else 'cached'} "
            f"sampled_frames={sampled_frames if sampled_frames is not None else 'cached'} "
            "md5_verified=yes "
            "secret_fields_logged=no"
        )
        return str(lease)

    def gc(self):
        import time
        now = time.time()
        for path in self.runtime_dir.glob("*.jpg"):
            try:
                if now - path.stat().st_mtime > 1800:
                    path.unlink(missing_ok=True)
            except Exception:
                pass
