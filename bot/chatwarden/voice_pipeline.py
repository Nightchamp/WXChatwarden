from __future__ import annotations

from io import BytesIO
import hashlib
import os
from pathlib import Path
import tempfile
import time
import wave

import pysilk

from . import core
from .db_reader import VoiceMediaNotFound


class VoicePipeline:
    """Resolve exact WeChat Type34 SILK and decode it to bounded PCM WAV.

    This class deliberately performs no speech recognition. SILK decoding is
    only a codec/container conversion so the native multimodal model can receive
    and understand the original voice message through Gateway.
    """

    SAMPLE_RATE = 24000
    MAX_SILK_BYTES = 2 * 1024 * 1024
    MAX_PCM_BYTES = 16 * 1024 * 1024
    # v3.183: WeChat writes VoiceInfo shortly after the message row. Only an
    # exact not-found is retried; each attempt re-reads the WAL-aware cache.
    MEDIA_RETRY_DELAYS = (0.5, 1.0, 1.5, 2.0, 3.0)

    def __init__(self, db, cache_dir="/var/lib/chatwarden/db-voice-cache-v1"):
        self.db = db
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.cache_dir.chmod(0o700)
        except OSError:
            pass
        self.max_age = 600.0
        self._last_gc = 0.0

    def gc(self, force=False):
        now = time.time()
        if not force and now - self._last_gc < 300:
            return
        self._last_gc = now
        for path in self.cache_dir.glob("*.wav"):
            try:
                if now - path.stat().st_mtime > self.max_age:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def cleanup(self, path):
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    def _voice_data_with_retry(self, event):
        waited = 0.0
        attempt = 0
        while True:
            attempt += 1
            try:
                silk = self.db.voice_data_for_event(event)
            except VoiceMediaNotFound:
                if attempt > len(self.MEDIA_RETRY_DELAYS):
                    core.log(
                        "[v3.183-voice-media] "
                        f"local_id={event.get('local_id')} found=no "
                        f"attempts={attempt} waited={waited:.1f}s"
                    )
                    raise
                delay = self.MEDIA_RETRY_DELAYS[attempt - 1]
                time.sleep(delay)
                waited += delay
                continue
            if attempt > 1:
                core.log(
                    "[v3.183-voice-media] "
                    f"local_id={event.get('local_id')} found=yes "
                    f"attempts={attempt} waited={waited:.1f}s"
                )
            return silk

    def decode_event(self, event):
        self.gc()
        silk = self._voice_data_with_retry(event)
        if not isinstance(silk, bytes) or not 0 < len(silk) <= self.MAX_SILK_BYTES:
            raise RuntimeError("invalid voice media payload")
        if b"#!SILK_V3" not in silk[:16]:
            raise RuntimeError("unsupported WeChat voice codec")

        pcm_output = BytesIO()
        try:
            pysilk.decode(BytesIO(silk), pcm_output, self.SAMPLE_RATE)
        except Exception as exc:
            raise RuntimeError("WeChat SILK decode failed") from exc
        pcm = pcm_output.getvalue()
        if (
            not pcm
            or len(pcm) % 2
            or len(pcm) > self.MAX_PCM_BYTES
        ):
            raise RuntimeError("decoded voice PCM outside budget")

        chat_hash = hashlib.sha256(
            str(event.get("chat_username") or "").encode("utf-8")
        ).hexdigest()[:12]
        final = self.cache_dir / (
            f"{chat_hash}-{int(event.get('local_id') or 0)}-"
            f"{int(event.get('server_id') or 0)}.wav"
        )
        fd, stage_name = tempfile.mkstemp(
            prefix=".voice-next-",
            suffix=".wav",
            dir=self.cache_dir,
        )
        os.close(fd)
        stage = Path(stage_name)
        try:
            with wave.open(str(stage), "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(self.SAMPLE_RATE)
                target.writeframes(pcm)
            stage.chmod(0o600)
            os.replace(stage, final)
        finally:
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                pass

        core.log(
            "[v3.135-voice-media] "
            f"local_id={int(event.get('local_id') or 0)} "
            f"silk_bytes={len(silk)} pcm_bytes={len(pcm)} "
            f"duration={len(pcm)/(self.SAMPLE_RATE*2):.2f}s "
            "codec=silk-to-pcm asr=no"
        )
        return str(final)
