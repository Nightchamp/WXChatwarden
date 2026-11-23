#!/usr/bin/env python3
from __future__ import annotations

from chatwarden import core


def main():
    # Real v3.71 production false-positive shape: the three loose keywords
    # exist in unrelated parts of one pasted article and must not be merged
    # into a synthetic "what is your framework" identity question.
    pasted = (
        "以下是kimi k3针对我最初提问提示词的答复。你的问题触及了江苏地域文化研究中的议题。"
        + ("历史分析内容。" * 30)
        + "四、更完整的历史解释框架。"
        + ("继续讨论历史。" * 30)
        + "并非因为流民具有什么先天的匪性。"
    )
    assert core.is_runtime_identity_query(pasted) is False

    positives = (
        "你是什么模型",
        "你的框架是什么",
        "能不能说一下你的底层用的什么模型",
        "确认一下你的供应商是哪家",
    )
    for text in positives:
        assert core.is_runtime_identity_query(text) is True, text

    print("V372_RUNTIME_IDENTITY_GUARD=PASS")


if __name__ == "__main__":
    main()
