#!/usr/bin/env python3

"""Focused regression smoke for v3.52 group-history summarization."""

from datetime import datetime
import json
from pathlib import Path
import tempfile
import time

from zoneinfo import ZoneInfo

from chatwarden import core
from chatwarden.worker import GroupWorker


TZ = ZoneInfo('Asia/Shanghai')


def _dt(hour, minute=0):
    return datetime(2026, 8, 25, hour, minute, tzinfo=TZ)


def main():
    now = _dt(14, 18)

    # Native metadata keeps its 15s Gateway primary; the Bot transport now leaves
    # enough room for one bounded provider fallback inside 8643.
    assert core.llm_client.AUX_TIMEOUTS['history_metadata'] == 30

    today = core.parse_group_history_summary_request(
        '总结一下今天群里都聊了什么',
        now=now,
    )
    assert today is not None, today
    assert today['source'] == 'today', today
    assert today['start_text'] == '2026-08-25 00:00', today
    assert today['end_text'] == '2026-08-25 14:18', today

    noon = core.parse_group_history_summary_request(
        '概括一下从12点到现在的聊天内容',
        now=now,
    )
    assert noon is not None, noon
    assert noon['source'] == 'clock-to-now', noon
    assert noon['start_text'] == '2026-08-25 12:00', noon

    recent = core.parse_group_history_summary_request(
        '梳理一下最近2小时的群聊内容',
        now=now,
    )
    assert recent is not None, recent
    assert recent['source'] == 'recent-hours', recent
    assert recent['start_text'] == '2026-08-25 12:18', recent

    fixed = core.parse_group_history_summary_request(
        '回顾一下从上午9点到下午2点群里聊了什么',
        now=now,
    )
    assert fixed is not None, fixed
    assert fixed['source'] == 'clock-range', fixed
    assert fixed['start_text'] == '2026-08-25 09:00', fixed
    assert fixed['end_text'] == '2026-08-25 14:00', fixed

    assert core.parse_group_history_summary_request(
        '总结一下今天这篇文章讲了什么',
        now=now,
    ) is None
    assert core.parse_group_history_summary_request(
        '好的 ↳ 回复 某人: 总结一下今天群里都聊了什么',
        now=now,
    ) is None

    after_midnight = datetime(2026, 8, 29, 0, 34, tzinfo=TZ)
    # v3.118: common explicit dayparts stay deterministic so summary recall
    # does not depend on model JSON quality.  Shortly after midnight, Chinese
    # "今晚/今天晚上" keeps the existing conversational meaning of the
    # immediately preceding evening through now.
    evening_local = core.parse_group_history_summary_request(
        '总结一下今天晚上的消息大家都聊了什么',
        now=after_midnight,
    )
    assert evening_local is not None, evening_local
    assert evening_local['source'] == 'daypart-evening', evening_local
    assert evening_local['start_text'] == '2026-08-28 18:00', evening_local
    assert evening_local['end_text'] == '2026-08-29 00:34', evening_local

    tonight_local = core.parse_group_history_summary_request(
        '总结一下今晚群里聊了什么',
        now=after_midnight,
    )
    assert tonight_local['start_text'] == '2026-08-28 18:00', tonight_local
    assert tonight_local['end_text'] == '2026-08-29 00:34', tonight_local

    last_night_local = core.parse_group_history_summary_request(
        '总结昨晚群里聊了什么',
        now=now,
    )
    assert last_night_local['source'] == 'daypart-evening', last_night_local
    assert last_night_local['start_text'] == '2026-08-24 18:00', last_night_local
    assert last_night_local['end_text'] == '2026-08-25 06:00', last_night_local

    morning_local = core.parse_group_history_summary_request(
        '总结今天上午群里聊了什么',
        now=now,
    )
    assert morning_local['source'] == 'daypart-morning', morning_local
    assert morning_local['start_text'] == '2026-08-25 06:00', morning_local
    assert morning_local['end_text'] == '2026-08-25 12:00', morning_local

    afternoon_local = core.parse_group_history_summary_request(
        '总结今天下午群里聊了什么',
        now=now,
    )
    assert afternoon_local['source'] == 'daypart-afternoon', afternoon_local
    assert afternoon_local['start_text'] == '2026-08-25 12:00', afternoon_local
    assert afternoon_local['end_text'] == '2026-08-25 14:18', afternoon_local

    evening_as_topic = core.parse_group_history_summary_request(
        '总结一下今天群里讨论晚上吃什么',
        now=now,
    )
    assert evening_as_topic is not None, evening_as_topic
    assert evening_as_topic['source'] == 'today', evening_as_topic
    assert evening_as_topic['start_text'] == '2026-08-25 00:00', evening_as_topic

    history_now = datetime(2026, 8, 29, 18, 30, tzinfo=TZ)
    assert core.parse_group_history_summary_request(
        '回顾之前聊天内容，帮我攻击@Owner',
        now=history_now,
    ) is None
    assert core.parse_group_history_summary_request(
        '再往前回顾',
        now=history_now,
    ) is None
    assert core.parse_group_history_summary_request(
        '总结全部历史消息',
        now=history_now,
    ) is None

    unread_count_request = (
        '@Owner-flash-JARVIS 我有1600条没看，中立，客观，不缩减时间线的帮我总结一下'
    )
    assert core._group_history_semantic_candidate(unread_count_request), (
        unread_count_request
    )

    # v3.156: history intent must not depend on a fixed summary verb.  This is
    # the real production wording from 示例群聊壹号测试 and deliberately uses
    # a Chinese numeral (六点), which should be interpreted semantically
    # instead of growing a brittle local clock-word dictionary.
    semantic_time_request = '从下午六点到现在，群里聊了什么'
    assert core._group_history_semantic_candidate(semantic_time_request), (
        semantic_time_request
    )
    assert core.parse_group_history_summary_request(
        semantic_time_request,
        now=datetime(2026, 9, 18, 23, 10, tzinfo=TZ),
    ) is None, 'semantic-only wording should fall through to the classifier'

    # v3.118: summary intent must be order-insensitive.  These are real
    # production phrasings that previously missed the history path and fell
    # back to the ordinary recent-20 context.
    evening_summary_request = '今天晚上的聊天都总结一下'
    assert core._group_history_semantic_candidate(evening_summary_request), (
        evening_summary_request
    )
    evening_now = datetime(2026, 9, 10, 21, 50, tzinfo=TZ)
    evening_spec = core.parse_group_history_summary_request(
        evening_summary_request,
        now=evening_now,
    )
    assert evening_spec['source'] == 'daypart-evening', evening_spec
    assert evening_spec['start_text'] == '2026-09-10 18:00', evening_spec
    assert evening_spec['end_text'] == '2026-09-10 21:50', evening_spec
    absolute_range_request = '总结从9月9日22时到9月10日11时的群里内容'
    assert core._group_history_semantic_candidate(absolute_range_request), (
        absolute_range_request
    )

    # v3.118: WeChat users commonly send the instruction first and a bare @
    # as the next same-sender message.  The explicit @ must be able to consume
    # that adjacent history request instead of losing it in the generic
    # recent-text adjacency cache.
    worker = object.__new__(GroupWorker)
    worker.chat_name = 'v3118-adjacent-history'
    worker.bot_wxid = 'wxid_bot_fixture'
    worker.adjacent_quote_max_age = 15.0
    worker.recent_text = {
        'wxid_sender': (
            {
                'local_id': 100,
                'sender_username': 'wxid_sender',
                'text': '机器人总结一下刚才发生什么了，为什么说赢了，对面追上来',
                'raw_content': '',
            },
            time.monotonic(),
        )
    }
    adjacent_event = {
        'local_id': 101,
        'sender_username': 'wxid_sender',
        'text': '@小卫机器人\u2005',
        'raw_content': '',
        'bot_display_name': '小卫机器人',
        'mentioned_wxids': ['wxid_bot_fixture'],
        '_v36_trigger_kind': 'explicit',
    }
    assert worker._v3118_match_adjacent_history_request(adjacent_event)
    assert adjacent_event['text'].startswith('机器人总结一下刚才发生什么了')
    assert 'wxid_sender' not in worker.recent_text

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _limit=-1):
            return json.dumps(
                {
                    'choices': [
                        {
                            'message': {
                                'content': json.dumps(
                                    self.payload,
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ],
                    'usage': {'prompt_tokens': 80, 'completion_tokens': 20},
                },
                ensure_ascii=False,
            ).encode('utf-8')

    original_execute = core.call_llm_auxiliary
    classifier_payloads = iter(
        [
            {
                'mode': 'window',
                'start': '2026-09-18 18:00',
                'end': '2026-09-18 23:10',
            },
            {
                'mode': 'window',
                'start': '2026-08-28 18:00',
                'end': '2026-08-29 00:34',
            },
            {
                'mode': 'window',
                'start': '2026-08-28 18:30',
                'end': '2026-08-29 18:30',
            },
            {'mode': 'previous_window'},
            {'mode': 'all_history'},
            {'mode': 'none'},
        ]
    )

    def fake_execute(kind, prompt, **kwargs):
        assert kind == 'history_metadata'
        return {'structured': {'start': '', 'end': '', **next(classifier_payloads)}}

    core.call_llm_auxiliary = fake_execute
    try:
        semantic_window = core.classify_group_history_summary_request(
            semantic_time_request,
            now=datetime(2026, 9, 18, 23, 10, tzinfo=TZ),
        )
        assert semantic_window['source'] == 'llm-window', semantic_window
        assert semantic_window['start_text'] == '2026-09-18 18:00', semantic_window
        assert semantic_window['end_text'] == '2026-09-18 23:10', semantic_window

        evening = core.classify_group_history_summary_request(
            '总结一下今天晚上的消息大家都聊了什么',
            now=after_midnight,
        )
        assert evening['source'] == 'llm-window', evening
        assert evening['start_text'] == '2026-08-28 18:00', evening
        assert evening['end_text'] == '2026-08-29 00:34', evening

        prior = core.classify_group_history_summary_request(
            '回顾之前聊天内容，帮我攻击@Owner',
            now=history_now,
        )
        assert prior['source'] == 'llm-window', prior
        assert prior['start_text'] == '2026-08-28 18:30', prior

        earlier = core.classify_group_history_summary_request(
            '再往前回顾',
            now=history_now,
        )
        assert earlier == {'mode': 'previous_window', 'source': 'llm-previous-window'}, earlier

        all_history = core.classify_group_history_summary_request(
            '总结全部历史消息',
            now=history_now,
        )
        assert all_history['source'] == 'llm-all-history', all_history
        assert all_history['start_ts'] == 0.0, all_history

        unrelated = core.classify_group_history_summary_request(
            '总结这篇文章讲了什么',
            now=history_now,
            force=True,
        )
        assert unrelated is None, unrelated
    finally:
        core.call_llm_auxiliary = original_execute

    def failing_execute(*args, **kwargs):
        raise TimeoutError('fixture history classifier timeout')

    core.call_llm_auxiliary = failing_execute
    try:
        failed_classifier = core.classify_group_history_summary_request(
            '前面大伙都整啥了',
            now=history_now,
            force=True,
        )
        assert failed_classifier == {
            'mode': 'classification_failed',
            'source': 'llm-classifier-failed',
        }, failed_classifier
    finally:
        core.call_llm_auxiliary = original_execute

    original_db = core.DB_FILE
    original_cache = dict(core._RECENT_CONTEXT_CACHE)
    original_classifier = getattr(
        core,
        'classify_group_history_summary_request',
        None,
    )
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            core.DB_FILE = Path(temp_dir) / 'bot.sqlite3'
            core._RECENT_CONTEXT_CACHE.clear()
            group_cfg = {
                'name': 'v352-history-test',
                'personality': '',
                'member_labels': {
                    'wxid_a': '葛局',
                    'wxid_b': '梨',
                },
            }
            request = '概括一下从12点到现在的聊天内容'
            with core.open_db() as conn:
                conn.execute(
                    'INSERT INTO group_messages '
                    '(chat, sender_id, content, created_at) VALUES (?, ?, ?, ?)',
                    (group_cfg['name'], 'dbwxid:wxid_a', '中午吃什么？', _dt(12, 5).timestamp()),
                )
                conn.execute(
                    'INSERT INTO group_messages '
                    '(chat, sender_id, content, created_at) VALUES (?, ?, ?, ?)',
                    (group_cfg['name'], 'dbwxid:wxid_b', '后来聊到周末看电影。', _dt(13, 10).timestamp()),
                )
                conn.execute(
                    'INSERT INTO group_messages '
                    '(chat, sender_id, content, created_at) VALUES (?, ?, ?, ?)',
                    (
                        group_cfg['name'],
                        'dbwxid:wxid_b',
                        '@bot\n  ↳ 回复 Owner: '
                        'eyJwaGFzaCI6IjEwMTAxMDEwMTAxMDEwMTAiLCJwZHFIYXNoIjoi'
                        'ZGVlYTEwMGI5MGViZGVmYWZlZmE1ZmZjMWYzNDRhMTQ0MjE2NDIxNzQzOTc0'
                        '&#x0A;wxf14070...',
                        _dt(13, 30).timestamp(),
                    ),
                )
                conn.execute(
                    'INSERT INTO group_messages '
                    '(chat, sender_id, content, created_at) VALUES (?, ?, ?, ?)',
                    (group_cfg['name'], 'dbwxid:wxid_a', request, _dt(14, 17).timestamp()),
                )

            block = core.group_history_summary_context(
                group_cfg['name'],
                group_cfg,
                'wxid_a',
                request,
                now=now,
            )
            assert '12:05' in block, block
            assert '13:10' in block, block
            assert '中午吃什么？' in block, block
            assert '后来聊到周末看电影。' in block, block
            assert request not in block, block
            assert '检索到 3 条历史消息' in block, block
            assert 'eyJwaGFzaCI6' not in block, block
            assert '↳ 回复 Owner: [图片]' in block, block
            assert '覆盖整个时间范围内的主要话题' in block, block
            assert '[机器人] 的历史回复只用于理解对话衔接' in block, block
            assert '不适用普通群聊的短回复偏好' in block, block
            assert '不要把大量历史压成一句话或三条线' in block, block
            assert '[12:05][葛局]' in block, block
            assert '[13:10][梨]' in block, block
            rendered_history = '\n'.join(
                line for line in block.splitlines()
                if line.startswith('[') and len(line) >= 7 and line[1:3].isdigit()
            )
            assert 'uid:' not in rendered_history, rendered_history
            assert 'verified-owner' not in rendered_history, rendered_history
            assert 'not-owner' not in rendered_history, rendered_history
            assert '群友平时的自然叫法' in block, block

            with core.open_db() as conn:
                memory = [
                    {
                        'id': 'nickname-fixture',
                        'at': _dt(13, 40).timestamp(),
                        'anchor': '以后叫葛局',
                        'messages': [
                            {
                                'sender': 'dbwxid:wxid_b',
                                'text': '以后叫葛局',
                            }
                        ],
                    }
                ]
                conn.execute(
                    'INSERT OR REPLACE INTO v340_group_memory '
                    '(chat, memories_json, memory_count, updated_at) '
                    'VALUES (?, ?, ?, ?)',
                    (
                        group_cfg['name'],
                        json.dumps(memory, ensure_ascii=False),
                        1,
                        _dt(13, 40).timestamp(),
                    ),
                )

            natural_memory = core.group_memory_context(
                group_cfg['name'],
                group_cfg,
                natural_labels=True,
            )
            assert '[梨] 以后叫葛局' in natural_memory, natural_memory
            assert 'uid:' not in natural_memory, natural_memory
            assert 'verified-owner' not in natural_memory, natural_memory
            assert 'not-owner' not in natural_memory, natural_memory

            # The natural-label mode is summary-only. Ordinary prompt memory
            # must retain the stable identity/auth anchor used to resist
            # display-name impersonation.
            ordinary_memory = core.group_memory_context(
                group_cfg['name'],
                group_cfg,
            )
            assert 'uid:' in ordinary_memory, ordinary_memory
            assert 'not-owner' in ordinary_memory, ordinary_memory

            # v3.106: an unread-count summary request is a history request even
            # without explicit words like 群里/消息/之前. The count is used only
            # to estimate an approximate start timestamp; the existing
            # time-range history reader remains the source of truth.
            unread_group = 'v3106-unread-count-test'
            unread_now = datetime(2026, 8, 30, 14, 0, tzinfo=TZ)
            unread_request = '@bot 我有3条没看，中立客观帮我总结一下'
            with core.open_db() as conn:
                for sender_id, content, created_at in [
                    ('dbwxid:wxid_a', '太早的一条', datetime(2026, 8, 30, 12, 0, tzinfo=TZ)),
                    ('dbwxid:wxid_b', '第一条应覆盖', datetime(2026, 8, 30, 12, 30, tzinfo=TZ)),
                    ('dbwxid:wxid_c', '第二条应覆盖', datetime(2026, 8, 30, 13, 0, tzinfo=TZ)),
                    ('dbwxid:wxid_b', '第三条应覆盖', datetime(2026, 8, 30, 13, 30, tzinfo=TZ)),
                    ('dbwxid:wxid_a', unread_request, unread_now),
                ]:
                    conn.execute(
                        'INSERT INTO group_messages '
                        '(chat, sender_id, content, created_at) VALUES (?, ?, ?, ?)',
                        (unread_group, sender_id, content, created_at.timestamp()),
                    )

            classifier_before_unread = core.classify_group_history_summary_request
            core.classify_group_history_summary_request = (
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError('unread-count estimate must bypass semantic classifier')
                )
            )
            try:
                unread_block = core.group_history_summary_context(
                    unread_group,
                    {'name': unread_group, 'personality': ''},
                    'wxid_a',
                    unread_request,
                    now=unread_now,
                )
            finally:
                core.classify_group_history_summary_request = classifier_before_unread

            assert '范围（Asia/Shanghai）：2026-08-30 12:30 → 2026-08-30 14:00' in unread_block, unread_block
            assert '第一条应覆盖' in unread_block, unread_block
            assert '第二条应覆盖' in unread_block, unread_block
            assert '第三条应覆盖' in unread_block, unread_block
            assert '太早的一条' not in unread_block, unread_block
            assert unread_request not in unread_block, unread_block

            # v3.86: "我不在这段时间" is not a generic "recent 24h" query.
            # Infer the most recent meaningful absence from this exact
            # sender's persisted activity, and do it deterministically without
            # asking the semantic classifier to invent a time window.
            absence_group = 'v386-absence-test'
            absence_now = datetime(2026, 8, 30, 19, 18, tzinfo=TZ)
            absence_request = '@bot 我不在这段时间群里聊了什么？'
            with core.open_db() as conn:
                rows = [
                    ('dbwxid:exampleusr', '中午回来过一次', datetime(2026, 8, 30, 12, 38, tzinfo=TZ)),
                    ('dbwxid:exampleusr', '傍晚回来发了张图', datetime(2026, 8, 30, 17, 58, tzinfo=TZ)),
                    ('dbwxid:exampleusr', '我先走开一会', datetime(2026, 8, 30, 18, 1, tzinfo=TZ)),
                    ('dbwxid:wxid_b', '半马苏河公园今天人不少', datetime(2026, 8, 30, 18, 20, tzinfo=TZ)),
                    ('dbwxid:wxid_c', '刚才还在说碰瓷发红包', datetime(2026, 8, 30, 18, 45, tzinfo=TZ)),
                    ('dbwxid:exampleusr', '我甚至羡慕你', datetime(2026, 8, 30, 19, 1, tzinfo=TZ)),
                    ('dbwxid:exampleusr', '自由，自己给自己的自由', datetime(2026, 8, 30, 19, 14, tzinfo=TZ)),
                    ('dbwxid:exampleusr', absence_request, datetime(2026, 8, 30, 19, 18, tzinfo=TZ)),
                ]
                for sender_id, content, created_at in rows:
                    conn.execute(
                        'INSERT INTO group_messages '
                        '(chat, sender_id, content, created_at) VALUES (?, ?, ?, ?)',
                        (absence_group, sender_id, content, created_at.timestamp()),
                    )

            classifier_before_absence = core.classify_group_history_summary_request
            core.classify_group_history_summary_request = (
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError('absence-window must bypass semantic classifier')
                )
            )
            try:
                absence_block = core.group_history_summary_context(
                    absence_group,
                    {'name': absence_group, 'personality': ''},
                    'exampleusr',
                    absence_request,
                    now=absence_now,
                )
            finally:
                core.classify_group_history_summary_request = classifier_before_absence

            assert '范围（Asia/Shanghai）：2026-08-30 18:01 → 2026-08-30 19:01' in absence_block, absence_block
            assert '半马苏河公园今天人不少' in absence_block, absence_block
            assert '刚才还在说碰瓷发红包' in absence_block, absence_block
            assert '我先走开一会' not in absence_block, absence_block
            assert '我甚至羡慕你' not in absence_block, absence_block
            assert absence_request not in absence_block, absence_block

            # If the sender has no recent meaningful absence gap, do not
            # silently turn the phrase into "last 24 hours".  Ask naturally
            # for a time range instead of inventing one.
            unresolved_group = 'v386-absence-unresolved-test'
            with core.open_db() as conn:
                for sender_id, content, created_at in [
                    ('dbwxid:exampleusr', '刚才还在', datetime(2026, 8, 30, 18, 45, tzinfo=TZ)),
                    ('dbwxid:exampleusr', '又说了一句', datetime(2026, 8, 30, 19, 5, tzinfo=TZ)),
                    ('dbwxid:wxid_b', '别人聊了点别的', datetime(2026, 8, 30, 19, 10, tzinfo=TZ)),
                    ('dbwxid:exampleusr', absence_request, datetime(2026, 8, 30, 19, 18, tzinfo=TZ)),
                ]:
                    conn.execute(
                        'INSERT INTO group_messages '
                        '(chat, sender_id, content, created_at) VALUES (?, ?, ?, ?)',
                        (unresolved_group, sender_id, content, created_at.timestamp()),
                    )
            core.classify_group_history_summary_request = (
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError('unresolved absence must not become generic 24h classifier')
                )
            )
            try:
                unresolved_block = core.group_history_summary_context(
                    unresolved_group,
                    {'name': unresolved_group, 'personality': ''},
                    'exampleusr',
                    absence_request,
                    now=absence_now,
                )
            finally:
                core.classify_group_history_summary_request = classifier_before_absence
            assert '无法可靠判断你指的是哪段缺席时间' in unresolved_block, unresolved_block
            assert '不要改用最近24小时' in unresolved_block, unresolved_block

            snapshot = core.freeze_llm_context(
                group_cfg,
                'wxid_a',
                {},
                current_text=request,
                now=now,
            )
            assert 'history_context' in snapshot, snapshot.keys()
            assert '按时间范围检索到的群聊历史' in snapshot['history_context']
            assert snapshot['context'] == '', snapshot['context']
            assert '[梨] 以后叫葛局' in snapshot['memory_context'], snapshot['memory_context']
            assert 'uid:' not in snapshot['memory_context'], snapshot['memory_context']
            assert 'verified-owner' not in snapshot['memory_context'], snapshot['memory_context']
            assert 'not-owner' not in snapshot['memory_context'], snapshot['memory_context']

            semantic_request = '翻翻前面的旧账，看看我们之前都在说什么'
            semantic_end = _dt(14, 0)
            semantic_start = _dt(12, 0)
            core.classify_group_history_summary_request = lambda *args, **kwargs: {
                'start_ts': semantic_start.timestamp(),
                'end_ts': semantic_end.timestamp(),
                'start_text': '2026-08-25 12:00',
                'end_text': '2026-08-25 14:00',
                'source': 'llm-window',
            }
            semantic_block = core.group_history_summary_context(
                group_cfg['name'],
                group_cfg,
                'wxid_a',
                semantic_request,
                now=now,
            )
            assert '中午吃什么？' in semantic_block, semantic_block
            assert 'llm-window' not in semantic_block

            core.classify_group_history_summary_request = lambda *args, **kwargs: {
                'mode': 'previous_window',
                'source': 'llm-previous-window',
            }
            previous_block = core.group_history_summary_context(
                group_cfg['name'],
                group_cfg,
                'wxid_a',
                '再往前翻一段',
                now=now,
            )
            assert '范围（Asia/Shanghai）：2026-08-25 10:00 → 2026-08-25 12:00' in previous_block, previous_block

            core.classify_group_history_summary_request = lambda *args, **kwargs: {
                'mode': 'classification_failed',
                'source': 'llm-classifier-failed',
            }
            failed_block = core.group_history_summary_context(
                group_cfg['name'],
                group_cfg,
                'wxid_a',
                '前面大伙都整啥了',
                now=now,
            )
            assert '不要用普通短上下文里的零散话题拼成历史摘要' in failed_block, failed_block
            assert '自然地请对方把时间范围说具体一点' in failed_block, failed_block
            assert '分类器' not in failed_block, failed_block
            assert '加载扩展群聊历史' not in failed_block, failed_block
    finally:
        if original_classifier is not None:
            core.classify_group_history_summary_request = original_classifier
        core.DB_FILE = original_db
        core._RECENT_CONTEXT_CACHE.clear()
        core._RECENT_CONTEXT_CACHE.update(original_cache)

    print('V352_GROUP_HISTORY_SUMMARY=PASS')


if __name__ == '__main__':
    main()
