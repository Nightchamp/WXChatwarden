#!/usr/bin/env python3
from __future__ import annotations

"""Regression smoke for context-aware text-to-image prompt handoff."""

import sys
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from chatwarden import core


def main() -> None:
    routed = (
        "[开始生图]\n"
        "画一个 ABC 风格人物：眼睛更细，带自然的假笑皱纹，"
        "整体保持真实人物摄影感。"
    )

    assert core.is_image_gen_marker(routed) is True
    assert core.image_gen_routed_prompt(
        routed,
        "画一个abc",
    ) == (
        "画一个 ABC 风格人物：眼睛更细，带自然的假笑皱纹，"
        "整体保持真实人物摄影感。"
    )

    # Keep compatibility with a provider that still emits only the old exact
    # marker: it must fall back to the current user instruction rather than
    # suppressing image generation.
    assert core.image_gen_routed_prompt(
        "[开始生图]",
        "@阿卫\u2005画一个abc",
    ) == "画一个abc"

    assert core.image_gen_routed_prompt(
        "普通聊天回复",
        "画一个abc",
    ) == ""

    guidance = core._IMAGE_GEN_GATE_GUIDANCE
    assert "自包含" in guidance
    assert "当前用户要求优先" in guidance
    assert "直接相关" in guidance
    assert "最近" in guidance and "上下文" in guidance
    assert "不做创意扩写" in guidance
    assert "不得新增" in guidance

    print("V358_IMAGEGEN_CONTEXT_PROMPT=PASS")


if __name__ == "__main__":
    main()
