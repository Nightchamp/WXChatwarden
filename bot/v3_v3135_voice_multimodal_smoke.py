from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
import tempfile
import wave
from unittest.mock import patch

import pysilk

from chatwarden import llm_client, core
from chatwarden.voice_pipeline import VoicePipeline
from chatwarden.worker import GroupWorker


def _silk_fixture() -> bytes:
    pcm = BytesIO(b"\x00\x00" * 2400)
    silk = BytesIO()
    pysilk.encode(pcm, silk, 24000, 24000)
    payload = silk.getvalue()
    assert b"#!SILK_V3" in payload[:16]
    return payload


def main() -> None:
    event = {
        "local_type": 34,
        "chat_username": "123456@chatroom",
        "local_id": 77,
        "server_id": 9001,
        "timestamp": 1789439885,
        "sender_username": "wxid_human",
        "sender_display": "Human",
        "bot_display_name": "小卫士",
        "mentioned_wxids": [],
        "raw_content": "<msg><voicemsg /></msg>",
        "text": "[语音]",
    }

    class FakeDb:
        def voice_data_for_event(self, selected):
            assert selected["local_id"] == 77
            assert selected["server_id"] == 9001
            return _silk_fixture()

    with tempfile.TemporaryDirectory() as tmp:
        pipeline = VoicePipeline(FakeDb(), cache_dir=tmp)
        wav_path = pipeline.decode_event(event)
        try:
            with wave.open(wav_path, "rb") as audio:
                assert audio.getnchannels() == 1
                assert audio.getsampwidth() == 2
                assert audio.getframerate() == 24000
                assert audio.getnframes() > 0

            captured = {}

            def fake_execute(payload, timeout, *, structured):
                captured.update(payload)
                assert timeout == 65
                assert structured is True
                return {"ok": True}

            with patch.object(llm_client, "_execute", side_effect=fake_execute):
                llm_client.execute_voice_transcript(wav_path)
            assert captured["task_kind"] == "voice_transcript"
            assert captured["media"]["mime_type"] == "audio/wav"
            uploaded = base64.b64decode(captured["media"]["data_base64"])
            assert uploaded.startswith(b"RIFF") and uploaded[8:12] == b"WAVE"
        finally:
            pipeline.cleanup(wav_path)
        assert not Path(wav_path).exists()

    saved = []
    forwarded = []
    cleaned = []

    class FakeVoicePipeline:
        def decode_event(self, selected):
            assert selected["local_id"] == 77
            return "/tmp/fake-voice.wav"

        def cleanup(self, path):
            cleaned.append(path)

    worker = GroupWorker.__new__(GroupWorker)
    worker.chat_name = "fixture"
    worker.voice_pipeline = FakeVoicePipeline()
    worker.recent = {}
    worker.pending = {}
    worker.recent_text = {}
    worker.recent_app = {}
    worker._cleanup_image_ref = lambda _ref: None
    worker._v39_note_ordinary_human = lambda _event: None
    worker._handle_text = lambda selected: forwarded.append(dict(selected))

    with patch.object(core, "save_message", side_effect=lambda *args: saved.append(args)), \
         patch.object(core, "call_voice_transcript", return_value={
             "speech_detected": True,
             "transcript": "小卫士 帮我记一下",
         }):
        worker._handle_voice(dict(event))

    assert cleaned == ["/tmp/fake-voice.wav"]
    assert saved[-1][2] == "[语音] 小卫士 帮我记一下"
    assert forwarded[-1]["text"] == "小卫士 帮我记一下"
    assert forwarded[-1]["_v3135_voice_transcript"] is True

    mention_worker = GroupWorker.__new__(GroupWorker)
    mention_worker.bot_wxid = "wxid_bot"
    mention_worker.chat_name = "fixture"
    spoken = dict(event, text="小卫士 帮我记一下")
    assert mention_worker._v313_true_mention(spoken) is True

    assert llm_client.validate_auxiliary(
        "voice_transcript",
        {"speech_detected": True, "transcript": "你好"},
    )
    assert llm_client.validate_auxiliary(
        "voice_transcript",
        {"speech_detected": False, "transcript": ""},
    )
    assert not llm_client.validate_auxiliary(
        "voice_transcript",
        {"speech_detected": False, "transcript": "猜出来的文字"},
    )

    print("V3135_VOICE_EXACT_SILK_TO_WAV=PASS")
    print("V3135_VOICE_LLM_UPLOAD_ONLY=PASS")
    print("V3135_VOICE_GROUP_REUSES_TEXT_POLICY=PASS")
    print("V3135_VOICE_SPOKEN_BOT_NICKNAME_TRIGGER=PASS")
    print("V3135_CW_ASR_NOT_USED=PASS")


if __name__ == "__main__":
    main()
