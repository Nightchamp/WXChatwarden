from __future__ import annotations

from chatwarden.worker import (
    _adjacent_media_reference_intent,
    _v3161_structured_own_mention_body,
)


def main() -> None:
    should_use_media = (
        "这是什么",
        "他在哪",
        "重新看图",
        "看看",
        "这张呢",
        "怎么样",
        "换成红色",
        "评价一下这件外套",
        "点评一下这款外套",
        "分析一下这双鞋",
        "这件夹克怎么样",
        "这款外套如何",
        "你还是看不到吗",
        "你没看到吗",
        "你看看图 记一下",
        "看图片分析一下内容",
    )
    should_stay_text = (
        "那俩字母是啥好难猜啊",
        "你刚才说的两个字母是什么意思",
        "继续说刚才那个",
        "为什么这么说",
        "评价一下你刚才的说法",
        "你还是不同意吗",
    )

    for value in should_use_media:
        assert _adjacent_media_reference_intent(value), value
    for value in should_stay_text:
        assert not _adjacent_media_reference_intent(value), value

    assert _adjacent_media_reference_intent("评价一下这件外套")
    assert _adjacent_media_reference_intent("你还是看不到吗")

    live_event = {
        "bot_display_name": "小卫机器人",
        "mentioned_wxids": ["wxid_bot"],
    }
    normalized = _v3161_structured_own_mention_body(
        live_event,
        "wxid_bot",
        "@小卫机器人\u2005这个裤子怎么样",
    )
    assert normalized == "这个裤子怎么样", normalized
    assert _adjacent_media_reference_intent(normalized)

    unproven = _v3161_structured_own_mention_body(
        {"bot_display_name": "小卫机器人", "mentioned_wxids": []},
        "wxid_bot",
        "@小卫机器人\u2005这个裤子怎么样",
    )
    assert unproven.startswith("@小卫"), unproven

    print("V3123_ADJACENT_VISUAL_INTENT_GATE=PASS")
    print("V3161_STRUCTURED_OWN_MENTION_MEDIA_INTENT=PASS")
    print("V3123_TEXT_FOLLOWUP_NOT_HIJACKED_BY_MEDIA=PASS")


if __name__ == "__main__":
    main()
