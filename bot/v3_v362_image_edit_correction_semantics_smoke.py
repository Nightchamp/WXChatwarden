#!/usr/bin/env python3
"""Regression for corrective wording on an already-resolved image reference."""

from chatwarden import core


def main():
    positives = (
        "@阿卫\u2005春季街舞挑战赛，这几个字你改错了",
        "你写错了，应该是春季街舞挑战赛",
        "不对，正确的是春季街舞挑战赛",
        "春季街舞挑战赛才对",
        "这张图标题写错了，应为春季街舞挑战赛",
        "重写",
        "重做一版",
        "再来一版",
    )
    for text in positives:
        assert core.image_edit_followup_intent(text) is True, text

    negatives = (
        "你是不是把这几个字改错了？",
        "这几个字为什么写成春季街舞挑战赛？",
        "正确的是春季街舞挑战赛吗？",
        "春季街舞挑战赛才对？",
        "要重写吗？",
        "这个接口响应为200",
        "我觉得这几个字可能写错了",
        "春季街舞挑战赛这个名字怎么样？",
    )
    for text in negatives:
        assert core.image_edit_followup_intent(text) is False, text

    prompt = core.image_edit_prompt(
        "@阿卫\u2005春季街舞挑战赛，这几个字你改错了"
    )
    assert "春季街舞挑战赛" in prompt
    assert "你改错了" in prompt

    print("V362_IMAGE_EDIT_CORRECTION_SEMANTICS=PASS")


if __name__ == "__main__":
    main()
