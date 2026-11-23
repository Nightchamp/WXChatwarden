#!/usr/bin/env python3
from __future__ import annotations

"""Regression smoke for explicit research wording -> Agent Reach.

No real provider call and no WeChat send occur.
"""

import sys
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden import core


def main() -> None:
    assert hasattr(core, "explicit_agent_reach_intent")

    strong = (
        "@Owner-flash-JARVIS 调查一下这个是什么水平\n"
        "  ↳ 回复 星灬: 是纳帕公牛皮"
    )
    assert core.explicit_agent_reach_intent(strong) is True
    assert core.explicit_agent_reach_intent("深入调查一下这个牌子的真实口碑") is True
    assert core.explicit_agent_reach_intent("深挖一下这家公司") is True
    assert core.explicit_agent_reach_intent("多方核实一下这个说法") is True
    assert core.explicit_agent_reach_intent("多来源查证一下") is True

    # Weak lookup wording keeps semantic routing instead of forcing Reach.
    assert core.explicit_agent_reach_intent("查一下今天美元汇率") is False
    assert core.explicit_agent_reach_intent("搜一下这个品牌官网") is False
    assert core.explicit_agent_reach_intent("看看这个材质是什么") is False

    # Historical quote previews must not become current instructions.
    assert core.explicit_agent_reach_intent(
        "好的\n  ↳ 回复 某人: 调查一下这个品牌"
    ) is False

    original_execute = core.llm_client.execute_formal
    routes = []

    def fake_execute(group_cfg, prompt, **kwargs):
        routes.append('native')
        return {'text': 'ordinary-ok', 'request_id': 'research-fixture', 'session_turn': 1,
                'duration_ms': 1, 'tool_count': 0, 'web_used': False}

    core.llm_client.execute_formal = fake_execute
    try:
        cfg = {
            "name": "v349-explicit-research",
            "session_key": "v349-session",
        }

        reply = core.call_formal_reply(
            cfg,
            strong,
            "wxid_fixture",
            profiles=None,
            raw_question=strong,
        )
        assert reply == core.AGENT_REACH_MARKER, reply
        assert routes == [], routes

        weak = "查一下今天美元汇率"
        reply = core.call_formal_reply(
            cfg,
            weak,
            "wxid_fixture",
            profiles=None,
            raw_question=weak,
            context_snapshot={'context': '', 'current_sender': 'fixture'},
        )
        assert reply == "ordinary-ok", reply
        assert routes == ["native"], routes
    finally:
        core.llm_client.execute_formal = original_execute

    print("V349_EXPLICIT_RESEARCH_REACH=PASS")


if __name__ == "__main__":
    main()
