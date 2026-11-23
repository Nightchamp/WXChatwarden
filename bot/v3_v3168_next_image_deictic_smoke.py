#!/usr/bin/env python3
"""v3.168 explicit deictic image question before its image; no cloud, DB writes or sends."""
import os

os.environ['CW_DECISION_SHADOW'] = '0'

from chatwarden.worker import (
    _adjacent_media_reference_intent,
    _v3168_explicit_deictic_image_question,
    _wants_next_image,
)


WAITS = [
    '这图是什么',
    '这个图是啥',
    '这图啥意思',
    '这截图什么意思？',
    '那个照片是哪里',
    '这图能看出是谁吗',
    '看看这图',
    '帮我识别一下这个图片',
]

TEXT_ONLY = [
    '这图书馆几点开门',
    '这个图表怎么做',
    '这个图标好丑吗',
    '这图纸谁画的',
    '这图也太好笑了',
    '你觉得AI画图以后会取代画师吗',
    '今晚吃火锅还是烧烤',
    '画一幅猫狗大战',
    '这图是什么' + '，' * 40,
]


def main():
    for text in WAITS:
        assert _v3168_explicit_deictic_image_question(text), text
    print('V3168_EXPLICIT_DEICTIC_IMAGE_QUESTION_MATCHES=PASS')

    for text in TEXT_ONLY:
        assert not _v3168_explicit_deictic_image_question(text), text
    print('V3168_COMPOUND_NOUNS_LONG_AND_TEXT_DO_NOT_MATCH=PASS')

    # The shared direct/adjacent predicates are intentionally unchanged, so
    # follow-up Vision continuation and adjacency routing keep their paths.
    assert not _wants_next_image('这图是什么')
    assert not _wants_next_image('看看这图')
    assert _adjacent_media_reference_intent('这图是什么')
    print('V3168_SHARED_MEDIA_PREDICATES_UNCHANGED=PASS')


if __name__ == '__main__':
    main()
