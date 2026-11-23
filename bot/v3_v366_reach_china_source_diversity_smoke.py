#!/usr/bin/env python3
from __future__ import annotations

"""Regression smoke for Agent Reach China source-diversity guidance."""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from chatwarden import core


def main() -> None:
    captured = []
    original_execute = core.llm_client.execute_research
    def fake_execute(prompt):
        captured.append(prompt)
        return {'text': 'fixture reach result', 'request_id': 'source-policy-fixture',
                'duration_ms': 1, 'tool_count': 2, 'web_used': True}
    try:
        core.llm_client.execute_research = fake_execute
        reply = core.call_agent_reach(
            {'name': 'source-policy-fixture'}, '调查一下中国新能源汽车出口的最新情况',
            'wxid_fixture', profiles=None,
            context_snapshot={'context': '', 'current_sender': 'fixture', 'memory_context': ''},
        )
    finally:
        core.llm_client.execute_research = original_execute

    assert reply == "fixture reach result"
    assert len(captured) == 1, captured
    prompt = captured[0]

    assert "中国相关调查的来源多样性" in prompt
    assert "中国官方/权威来源" in prompt
    assert "境外较权威、具有编辑责任的媒体或机构" in prompt
    assert "至少一个" in prompt
    assert "不要为了凑境外来源" in prompt
    assert "分别呈现" in prompt
    assert "不要强行折中" in prompt

    print("V366_REACH_CHINA_SOURCE_DIVERSITY=PASS")


if __name__ == "__main__":
    main()
