#!/usr/bin/env python3
from __future__ import annotations

"""Regression smoke for Human Feel v2 prompt behavior."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden import core  # noqa: E402


def main() -> None:
    captured = []
    logs = []
    original_execute = core.llm_client.execute_formal
    original_aux = core.llm_client.execute_auxiliary
    original_log = core.log

    def fake_execute(cfg, prompt, **kwargs):
        captured.append(prompt)
        return {'text': '确实，有点绷不住', 'request_id': 'human-feel-fixture', 'session_turn': 1,
                'duration_ms': 1, 'tool_count': 0, 'web_used': False}

    def fake_aux(kind, prompt, **kwargs):
        assert kind == 'history_summary'
        captured.append(prompt)
        return {'structured': {'summary': '完整历史摘要'}, 'request_id': 'history-style-fixture',
                'duration_ms': 1, 'tool_count': 1, 'web_used': False}

    try:
        core.llm_client.execute_formal = fake_execute
        core.log = logs.append

        reply = core.call_formal_reply(
            {
                "name": "示例群聊壹号测试",
                "session_key": "fixture-v365",
                "personality": "幽默有趣、腹黑毒舌，紧跟互联网潮流。",
            },
            "笑死",
            "wxid_fixture",
            {},
            context_snapshot={
                "context": (
                    "[uid:a 甲] 哈哈哈\n"
                    "[uid:b 乙] 确实\n"
                    "[机器人] [AI]这个本质上就是上班受刑，主打一个纯纯修仙。\n"
                    "[uid:c 丙] 绷不住了\n"
                    "[机器人] [AI]看你是想继续熬还是直接跑路，懂得都懂。\n"
                    "[uid:a 甲] 行\n"
                    "[uid:b 乙] 有点\n"
                ),
                "current_sender": "uid:a 甲",
                "memory_context": "",
                "history_context": "",
            },
        )
    finally:
        core.llm_client.execute_formal = original_execute
        core.log = original_log

    assert reply == "确实，有点绷不住"
    assert len(captured) == 1, "Human Feel v2 must stay single-pass"

    content = captured[0]
    assert "[中文微信自然表达规则]" in content
    assert "[群聊语体指纹]" in content
    assert "最近真人消息" in content
    assert "普通接梗、情绪回应、轻问答" in content
    assert "不是每条都需要梗、毒舌、反转或收尾金句" in content
    assert "允许一句很普通的话就结束" in content
    assert "[微信对话示例]" in content
    assert "只示范节奏和行为" in content
    assert "[近期表达冷却]" in content
    assert "主打一个" in content
    assert "本质上" in content
    assert "修仙" in content
    assert "受刑" in content
    assert "懂得都懂" in content
    assert "看你是想…还是…" in content
    assert "不是永久禁词" in content
    assert "[本轮短接话]" in content
    assert "5-15 字" in content
    assert "不要新开比喻、段子、建议或反问" in content
    assert any("[v3.65-human-style]" in line for line in logs), logs
    assert any("chars=8" in line for line in logs), logs

    # Informational work must still be allowed to expand when necessary.
    assert "事实、技术、搜索、调查或安全问题" in content
    assert "必要信息优先" in content

    # v3.91: repeated *bot* sentence families must create a structural
    # cooldown. This reproduces the production sequence where the bot kept
    # slot-filling the same roast shapes ("...是吧", "把我当X", cyber/
    # payment metaphors) even though the lexical catchphrase cooldown stayed
    # silent.
    # The normal mixed context may contain only one recent bot line in an
    # active group. Structural cooldown must therefore accept an independent
    # bot-only window instead of forgetting the bot's own last few replies.
    repeated_bot_context = (
        "[uid:a 甲] 我教你\n"
        "[uid:a 甲] 像这样就好了\n"
        "[uid:b 乙] 拍了拍机器人\n"
        "[uid:b 乙] 没投币\n"
        "[uid:b 乙] 白嫖的\n"
        "[机器人] 拿我当免费苦力呢，你自己怎么不去\n"
    )
    independent_bot_window = [
        "拿我当免费苦力呢，你自己怎么不去",
        "大周一的去公园跟晨练大爷打太极？",
        "拍我干嘛，手痒去给王老师捶捶肩膀",
        "白嫖还点上连击了，真当我是赛博木鱼啊。",
        "再拍真要按次收费了，手长包浆了是吧。",
        "拍什么拍，真把我当投币式聊天机了。",
    ]
    repeated_block = core._wechat_human_feel_context(
        repeated_bot_context,
        recent_bot_texts=independent_bot_window,
    )
    assert "[近期句式冷却]" in repeated_block, repeated_block
    assert "把X当Y" in repeated_block, repeated_block
    assert "收费/投币类比" in repeated_block, repeated_block
    assert "反问「是吧」骨架" not in repeated_block, repeated_block
    assert "赛博/工具人类比" not in repeated_block, repeated_block
    assert "不要换名词继续套同一骨架" in repeated_block, repeated_block
    assert "只换表达骨架，不减少必要信息、证据或结构" in repeated_block, repeated_block

    # The same constructions spoken by humans are the chat's voice, not bot
    # repetition. They must not cool the bot down by themselves.
    human_only_context = (
        "[uid:a 甲] 拿我当赛博低保是吧\n"
        "[uid:b 乙] 真把我当投币式聊天机了\n"
        "[uid:c 丙] 再拍就收费了\n"
        "[uid:d 丁] 白嫖还点上连击了\n"
        "[机器人] 行，知道了。\n"
    )
    human_only_block = core._wechat_human_feel_context(human_only_context)
    assert "[近期句式冷却]" not in human_only_block, human_only_block

    captured.clear()
    try:
        core.llm_client.execute_formal = fake_execute
        core.call_formal_reply(
            {
                "name": "示例群聊壹号测试",
                "session_key": "fixture-v365-info",
                "personality": "幽默有趣、腹黑毒舌，紧跟互联网潮流。",
            },
            "黄金怎么看",
            "wxid_fixture",
            {},
            context_snapshot={
                "context": (
                    "[uid:a 甲] 主打一个观望\n"
                    "[uid:b 乙] 我也没买\n"
                    "[机器人] [AI]先看看今天行情再说。\n"
                ),
                "current_sender": "uid:a 甲",
                "memory_context": "",
                "history_context": "",
            },
        )
    finally:
        core.llm_client.execute_formal = original_execute

    assert len(captured) == 1, captured
    info_content = captured[0]
    assert "[本轮短接话]" not in info_content
    assert "[近期表达冷却]" not in info_content, (
        "human catchphrases must not be treated as bot repetition"
    )

    # v3.118: once persisted group history has been loaded, summary quality
    # takes precedence over the ordinary short-chat Human Feel prior.  Keep
    # the natural-style policy, but replace the voice/few-shot block with an
    # explicit long-form summary instruction.
    captured.clear()
    try:
        core.llm_client.execute_auxiliary = fake_aux
        core.call_formal_reply(
            {
                "name": "示例群聊壹号测试",
                "session_key": "fixture-v3118-history",
                "personality": "幽默有趣、腹黑毒舌，紧跟互联网潮流。",
            },
            "今天晚上的聊天都总结一下",
            "wxid_fixture",
            {},
            context_snapshot={
                "context": "",
                "style_bot_texts": ["一句很短的旧回复"],
                "current_sender": "uid:a 甲",
                "memory_context": "",
                "history_context": (
                    "【按时间范围检索到的群聊历史】\n"
                    "范围（Asia/Shanghai）：2026-09-10 18:00 → 2026-09-10 21:00\n"
                    "该范围内检索到 800 条历史消息。\n"
                    "[18:01][甲] 第一件事\n"
                    "[20:58][乙] 最后一件事"
                ),
            },
        )
    finally:
        core.llm_client.execute_auxiliary = original_aux

    assert len(captured) == 1, captured
    history_content = captured[0]
    assert "[本轮群历史总结输出要求]" in history_content, history_content
    assert "通常控制在约350～700字" in history_content, history_content
    assert "任何情况下不要超过1000字" in history_content, history_content
    assert "不要逐条复述聊天记录" in history_content, history_content
    assert "大家熟悉的简称、外号或自然叫法" in history_content, history_content
    assert "不要因为看到 verified-owner 就写成 owner、群主或主人" in history_content, history_content
    assert "[群聊语体指纹]" not in history_content, history_content
    assert "[微信对话示例]" not in history_content, history_content

    print("V365_HUMAN_FEEL_V2=PASS")


if __name__ == "__main__":
    main()
