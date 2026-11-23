#!/usr/bin/env python3
from __future__ import annotations

"""Regression smoke for single-pass natural WeChat reply style guidance."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden import core


def main() -> None:
    captured = []
    original_execute = core.llm_client.execute_formal

    def fake_execute(cfg, prompt, **kwargs):
        captured.append(prompt)
        return {'text': '收到', 'request_id': 'style-fixture', 'session_turn': 1,
                'duration_ms': 1, 'tool_count': 0, 'web_used': False}

    try:
        core.llm_client.execute_formal = fake_execute

        reply = core.call_formal_reply(
            {
                "name": "fixture-group",
                "session_key": "fixture-session",
                "personality": "像熟人群友一样自然说话。",
            },
            "你觉得这个怎么样",
            "wxid_fixture_user",
            {},
            context_snapshot={
                "context": "[uid:aaaa 张三] 我觉得还行",
                "current_sender": "uid:bbbb 李四",
                "memory_context": "",
                "history_context": "",
            },
        )
    finally:
        core.llm_client.execute_formal = original_execute

    assert reply == "收到"
    assert len(captured) == 1, "natural-style integration must not add a second LLM call"

    content = captured[0]
    assert isinstance(content, str)
    assert "[中文微信自然表达规则]" in content
    assert "不要默认套用“结论-原因-建议”" in content
    assert "不要为了显得完整而强行列三点" in content
    assert "能一句说清就不要扩成三句" in content
    assert "保留口语感、短句、接话感" in content
    assert "内部路由标记和严格格式协议不受本规则影响" in content
    assert "准确、必要信息和安全边界优先" in content

    policy = core._CW_NATURAL_STYLE_POLICY
    assert len(policy) <= 1400, "style policy must stay compact for low latency"

    print("V361_NATURAL_CHAT_STYLE=PASS")


if __name__ == "__main__":
    main()
