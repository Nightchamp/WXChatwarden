#!/usr/bin/env python3
from __future__ import annotations

"""Regression smoke for unmentioned replies that quote the bot.

No real provider call and no WeChat send occur.
"""

import json
import sys
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden import core
from chatwarden.worker import GroupWorker


class _GateResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _limit=262144):
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": core.FOLLOWUP_GATE_YES,
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 80,
                    "completion_tokens": 2,
                },
            }
        ).encode("utf-8")


class _NoGateResponse(_GateResponse):
    def read(self, _limit=262144):
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": core.FOLLOWUP_GATE_NO,
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 80,
                    "completion_tokens": 2,
                },
            }
        ).encode("utf-8")


def _quote_event(chatusr: str) -> dict:
    return {
        "local_id": 350,
        "sender_username": "wxid_fixture_user",
        "sender_display": "fixture-user",
        "text": "你确定吗？\n  ↳ 回复 Owner-flash-JARVIS: [AI]这个说法需要再核一下。",
        "mentioned_wxids": [],
        "raw_content": f'''wxid_fixture_user:
<?xml version="1.0"?>
<msg>
  <appmsg>
    <title>你确定吗？</title>
    <type>57</type>
    <refermsg>
      <type>1</type>
      <svrid>123456789</svrid>
      <fromusr>fixture@chatroom</fromusr>
      <chatusr>{chatusr}</chatusr>
      <displayname>Owner-flash-JARVIS</displayname>
      <content>[AI]这个说法需要再核一下。</content>
    </refermsg>
  </appmsg>
</msg>''',
    }


def _p_image_rumor_quote_event(chatusr: str) -> dict:
    return {
        "local_id": 352,
        "sender_username": "wxid_fixture_user",
        "sender_display": "fixture-user",
        "text": (
            "那对这个P图恶搞的人该怎么处理\n"
            "  ↳ 回复 阿卫: 像，至少是截出来拼过的；"
            "气泡和表情比例有点不对，原生聊天截图不像这么规整"
        ),
        "mentioned_wxids": [],
        "raw_content": f'''wxid_fixture_user:
<?xml version="1.0"?>
<msg>
  <appmsg>
    <title>那对这个P图恶搞的人该怎么处理</title>
    <type>57</type>
    <refermsg>
      <type>1</type>
      <svrid>8250699562478218527</svrid>
      <fromusr>fixture@chatroom</fromusr>
      <chatusr>{chatusr}</chatusr>
      <displayname>阿卫</displayname>
      <content>像，至少是截出来拼过的；气泡和表情比例有点不对，原生聊天截图不像这么规整</content>
    </refermsg>
  </appmsg>
</msg>''',
    }


def main() -> None:
    worker = GroupWorker.__new__(GroupWorker)
    worker.bot_wxid = "wxid_fixture_bot"
    worker.chat_name = "v350-quote-bot"
    worker.group_cfg = {"name": worker.chat_name}
    worker.profiles = {}

    bot_quote = _quote_event(worker.bot_wxid)
    human_quote = _quote_event("wxid_other_human")

    # Real DB-native quote metadata must use exact refermsg/chatusr identity,
    # not a display-name or [AI] text guess.
    assert worker._event_quotes_bot(bot_quote) is True
    assert worker._event_quotes_bot(human_quote) is False

    original_gate = core.call_followup_gate
    calls = []
    ordinary = []
    answered = []
    decision = {"value": False}

    def fake_gate(group_cfg, trigger_message, sender_identity, profiles=None, *, gate_mode="followup"):
        calls.append(
            {
                "message": str(trigger_message),
                "sender": sender_identity,
                "gate_mode": gate_mode,
            }
        )
        return decision["value"]

    worker.pending_image_edit = {}
    worker.pending = {}
    worker.pending_wait = 8.0
    worker.recent_text = {}
    worker.recent_app = {}
    worker.recent = {}
    worker._remember_adjacent_text = lambda event: None
    worker._v39_note_ordinary_human = lambda event: ordinary.append(event["local_id"])
    worker._answer = lambda event, image_ref=None: answered.append(
        (event["local_id"], event.get("_v36_trigger_kind"))
    )
    core.call_followup_gate = fake_gate
    try:
        # A quoted-bot reply is only a candidate. Gate=NO must stay silent and
        # never enter the normal answer path.
        no_event = _quote_event(worker.bot_wxid)
        worker._handle_text(no_event)
        assert ordinary == [350]
        assert answered == []
        assert calls[-1]["gate_mode"] == "quoted-bot"
        assert "你确定吗" in calls[-1]["message"]

        # Gate=YES allows the real _handle_text route to enter _answer().
        decision["value"] = True
        yes_event = _quote_event(worker.bot_wxid)
        yes_event["local_id"] = 351
        worker._handle_text(yes_event)
        assert answered == [(351, "quote")]
        assert yes_event.get("_v350_quote_gate_passed") is True
        assert calls[-1]["gate_mode"] == "quoted-bot"

        # Real v3.87 incident shape: the current instruction asks what to do
        # about a P-image rumor, while the quoted bot text happens to contain
        # the historical word "表情". Historical quote text must not become
        # current media intent or open a next-emoticon/image wait.
        rumor_event = _p_image_rumor_quote_event(worker.bot_wxid)
        worker._handle_text(rumor_event)
        assert answered[-1] == (352, "quote"), answered
        assert worker.pending == {}, worker.pending
        assert rumor_event.get("_v350_quote_gate_passed") is True
    finally:
        core.call_followup_gate = original_gate

    # Exact quoted-bot replies now use the local high-reply policy instead of
    # paying a semantic-provider round trip. Substantive text defaults YES.
    original_context = core._followup_gate_context
    original_execute = core.call_llm_auxiliary
    captured = {}

    def fake_execute(kind, prompt, **kwargs):
        captured['task'] = kind
        return {'structured': {'addressed': True}}

    core._followup_gate_context = lambda chat_name, group_cfg: "[机器人] fixture previous reply"
    core.call_llm_auxiliary = fake_execute
    try:
        assert core.call_followup_gate(
            {"name": "v350-quote-bot"},
            "你确定吗？\n  ↳ 回复 Owner-flash-JARVIS: [AI]这个说法需要再核一下。",
            "wxid_fixture_user",
            {},
            gate_mode="quoted-bot",
        ) is True
    finally:
        core._followup_gate_context = original_context
        core.call_llm_auxiliary = original_execute

    assert captured == {}, captured

    # A real quoted-bot reply is already a strong direct-conversation signal.
    # Substantive current text should default to a reply even if the semantic
    # provider would conservatively answer NO. Pure reaction-only text may
    # remain silent.
    provider_calls = []

    def fake_no_execute(kind, prompt, **kwargs):
        provider_calls.append(kind)
        return {'structured': {'addressed': False}}

    core.call_llm_auxiliary = fake_no_execute
    try:
        assert core.call_followup_gate(
            {"name": "v350-quote-bot"},
            "你是最可爱的小猫吗\n  ↳ 回复 Owner-flash-JARVIS: 直接拔插头吧",
            "wxid_fixture_user",
            {},
            gate_mode="quoted-bot",
        ) is True
        assert core.call_followup_gate(
            {"name": "v350-quote-bot"},
            "没吃过这个馅的\n  ↳ 回复 Owner-flash-JARVIS: 排骨芸豆馅确实顶",
            "wxid_fixture_user",
            {},
            gate_mode="quoted-bot",
        ) is True
        assert core.call_followup_gate(
            {"name": "v350-quote-bot"},
            "……\n  ↳ 回复 Owner-flash-JARVIS: 行啊，组个酒局",
            "wxid_fixture_user",
            {},
            gate_mode="quoted-bot",
        ) is False
        assert core.call_followup_gate(
            {"name": "v350-quote-bot"},
            "哈哈哈哈哈\n  ↳ 回复 Owner-flash-JARVIS: 自己手残还赖教练",
            "wxid_fixture_user",
            {},
            gate_mode="quoted-bot",
        ) is False
        assert core.call_followup_gate(
            {"name": "v350-quote-bot"},
            "哈哈哈哈😂\n  ↳ 回复 Owner-flash-JARVIS: 自己手残还赖教练",
            "wxid_fixture_user",
            {},
            gate_mode="quoted-bot",
        ) is False
    finally:
        core.call_llm_auxiliary = original_execute

    assert provider_calls == [], provider_calls
    print("V389_QUOTED_BOT_DEFAULT_REPLY=PASS")

    print("V350_QUOTED_BOT_FOLLOWUP_GATE=PASS")


if __name__ == "__main__":
    main()
