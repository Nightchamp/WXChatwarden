#!/usr/bin/env python3
from __future__ import annotations

"""Regression smoke for external comments/feedback -> Agent Reach.

No real provider call and no WeChat send occur.
"""

import sys
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden import core


def main() -> None:
    assert hasattr(core, "external_feedback_agent_reach_intent")

    real_266 = (
        "@Owner-flash-JARVIS 总结一下评论区的内容\n"
        "  ↳ 回复 Mr. Sleepless: 示例视频标题"
    )
    quoted_card_prompt = (
        real_266
        + "\n\n【被引用分享卡片完整内容】\n"
        + "以下内容来自被引用的历史分享卡片。\n"
        + "[链接]\n标题：示例视频标题\n"
        + "来源：哔哩哔哩\nURL：https://b23.tv/example1"
    )

    assert core.external_feedback_agent_reach_intent(
        real_266,
        quoted_card_prompt,
    ) is True
    assert core.external_feedback_agent_reach_intent(
        "看看这个视频下面大家怎么评价",
        "看看这个视频下面大家怎么评价\n\n"
        "【DB-native 相邻分享卡片上下文】\n"
        "[链接]\n来源：小红书\nURL：https://example.invalid/note",
    ) is True
    assert core.external_feedback_agent_reach_intent(
        "总结B站弹幕 b23.tv/example",
        "总结B站弹幕 b23.tv/example",
    ) is True

    # Already-provided feedback should be summarized directly.
    assert core.external_feedback_agent_reach_intent(
        "总结一下我刚才贴出来的这5条评论",
        "总结一下我刚才贴出来的这5条评论\n\n"
        "最近群聊：\nA: 第一条评论\nB: 第二条评论\nC: 第三条评论",
    ) is False
    assert core.external_feedback_agent_reach_intent(
        "总结这些评论：很好看；音质不错；求第二期",
        "总结这些评论：很好看；音质不错；求第二期",
    ) is False
    assert core.external_feedback_agent_reach_intent(
        "总结这个链接的评论，评论如下：很好看；音质不错；求第二期 "
        "https://example.invalid/video",
        "总结这个链接的评论，评论如下：很好看；音质不错；求第二期 "
        "https://example.invalid/video\n\n【被引用分享卡片完整内容】",
    ) is False

    # A random old URL in broader context must not promote a local summary.
    assert core.external_feedback_agent_reach_intent(
        "总结一下这几条评论",
        "总结一下这几条评论\n\n最近群聊：\n"
        "某人：https://b23.tv/old-link\n"
        "某人：第一条评论\n某人：第二条评论",
    ) is False

    original_execute = core.llm_client.execute_formal
    routes = []

    def fake_execute(group_cfg, prompt, **kwargs):
        routes.append('native')
        return {'text': 'ordinary-ok', 'request_id': 'feedback-fixture', 'session_turn': 1,
                'duration_ms': 1, 'tool_count': 0, 'web_used': False}

    core.llm_client.execute_formal = fake_execute
    try:
        cfg = {
            "name": "v351-external-feedback",
            "session_key": "v351-session",
        }

        reply = core.call_formal_reply(
            cfg,
            quoted_card_prompt,
            "wxid_fixture",
            profiles=None,
            raw_question=real_266,
        )
        assert reply == core.AGENT_REACH_MARKER, reply
        assert routes == [], routes

        local = "总结一下我刚才贴出来的这5条评论"
        reply = core.call_formal_reply(
            cfg,
            local,
            "wxid_fixture",
            profiles=None,
            raw_question=local,
            context_snapshot={'context': '', 'current_sender': 'fixture'},
        )
        assert reply == "ordinary-ok", reply
        assert routes == ["native"], routes
    finally:
        core.llm_client.execute_formal = original_execute

    print("V351_EXTERNAL_FEEDBACK_REACH=PASS")


if __name__ == "__main__":
    main()
