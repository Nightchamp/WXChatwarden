#!/usr/bin/env python3
"""v3.163 history-intent Shadow acceptance; no cloud, DB writes or sends."""
from contextlib import contextmanager
from datetime import datetime
import json
import os
import time
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ['CW_DECISION_SHADOW'] = '0'
from chatwarden import core, decision_client as dc


CFG = {
    'name': '示例群聊壹号测试',
    'chat_username': '90000000002@chatroom',
    'member_labels': {'wxid_fixture': '林晓雨'},
}
FALSE_SUMMARY_CASE = (
    '之前群聊里让你生成过几张王叔送月饼的照片\n'
    '  ↳ 回复 小卫机器人: 我记忆里根本没有王叔的照片'
)
NOW = datetime(2026, 9, 26, 8, 16, tzinfo=ZoneInfo('Asia/Shanghai'))
LEGACY_SPEC = {
    'start_ts': NOW.timestamp() - 24 * 3600,
    'end_ts': NOW.timestamp(),
    'start_text': '2026-09-25 08:16',
    'end_text': '2026-09-26 08:16',
    'source': 'llm-window',
}


@contextmanager
def history_db():
    class Database:
        def execute(self, sql, args):
            assert 'group_messages' in sql
            assert args[0] == CFG['name']
            return self

        def fetchall(self):
            return [
                (100, 'bot', '[已生成图片] 王叔送月饼', NOW.timestamp() - 7200),
                (101, 'wxid_other', '这张有点像', NOW.timestamp() - 7100),
            ]

    yield Database()


def false_summary_fixture():
    captured = []

    def submit(snapshot, actual_outcome, metadata=None):
        captured.append((snapshot, actual_outcome, metadata or {}))
        return True

    assert core._history_intent_shadow_candidate(FALSE_SUMMARY_CASE) is True
    assert core.parse_group_history_summary_request(FALSE_SUMMARY_CASE, NOW) is None
    with patch.object(core, 'open_db', history_db), \
         patch.object(core, 'classify_group_history_summary_request', return_value=dict(LEGACY_SPEC)), \
         patch.object(core, '_group_history_cursor_save', lambda *args, **kwargs: None), \
         patch.object(dc, 'enabled', return_value=True), \
         patch.object(dc, 'mode', return_value='shadow'), \
         patch.object(dc, 'submit_history_intent', side_effect=submit):
        block = core.group_history_summary_context(
            CFG['name'], CFG, 'wxid_fixture', FALSE_SUMMARY_CASE, now=NOW,
        )

    # Lock the observed production bug in as a calibration fixture: legacy
    # routes this narrow count/lookup question through the broad summary path.
    assert '当前是明确的群历史总结或回顾任务' in block
    assert len(captured) == 1
    snapshot, outcome, metadata = captured[0]
    assert outcome == {'legacy_route': 'summary'}
    assert metadata == {}
    assert snapshot.current_message == '之前群聊里让你生成过几张王叔送月饼的照片'
    assert snapshot.recent_context == ()

    payload = dc.payload_for_history_intent(snapshot, outcome, {}, 'k' * 48)
    assert payload['decision_kind'] == 'history_intent_v1'
    assert payload['telemetry']['actual_outcome'] == {'legacy_route': 'summary'}
    assert payload['state']['current_message'] == snapshot.current_message
    assert payload['state']['recent_context'] == []
    assert '90000000002@chatroom' not in json.dumps(payload, ensure_ascii=False)

    answers = {
        'needs_persisted_history': .99,
        'wants_summary': .02,
        'wants_targeted_lookup': .99,
        'wants_count': .98,
        'wants_previous_window': .01,
    }
    response = {
        'ok': True, 'request_id': payload['request_id'],
        'decision_kind': 'history_intent_v1', 'decision_version': 1,
        'mode': 'shadow', 'model': 'decision-fixture',
        'answers': {name: {'type': 'noul', 'probability': p}
                    for name, p in answers.items()},
    }
    assert dc.validate_response(response, payload) is response
    print('V3163_HISTORY_FALSE_SUMMARY_GOLDEN_FIXTURE=PASS')
    print('V3163_HISTORY_TARGETED_LOOKUP_COUNT_CONTRACT=PASS')
    return snapshot


def shadow_failure_does_not_change_legacy(snapshot):
    baseline = None
    with patch.object(core, 'open_db', history_db), \
         patch.object(core, 'classify_group_history_summary_request', return_value=dict(LEGACY_SPEC)), \
         patch.object(core, '_group_history_cursor_save', lambda *args, **kwargs: None), \
         patch.object(dc, 'enabled', return_value=False):
        baseline = core.group_history_summary_context(
            CFG['name'], CFG, 'wxid_fixture', FALSE_SUMMARY_CASE, now=NOW,
        )

    with patch.object(core, 'open_db', history_db), \
         patch.object(core, 'classify_group_history_summary_request', return_value=dict(LEGACY_SPEC)), \
         patch.object(core, '_group_history_cursor_save', lambda *args, **kwargs: None), \
         patch.object(dc, 'enabled', return_value=True), \
         patch.object(dc, 'mode', return_value='shadow'), \
         patch.object(dc, 'submit_history_intent', return_value=False):
        shadow_dropped = core.group_history_summary_context(
            CFG['name'], CFG, 'wxid_fixture', FALSE_SUMMARY_CASE, now=NOW,
        )
    assert shadow_dropped == baseline
    print('V3163_HISTORY_SHADOW_DROP_BEHAVIOR_UNCHANGED=PASS')


def synthetic_bare_mention_prompt_not_sampled():
    calls = []
    synthetic = (
        '这条消息只有明确@你，没有附加正文。把它理解为邀请你加入当前群聊讨论：'
        '结合最近群聊中正在持续的主要话题和多人上下文，自然接话。'
    )
    with patch.object(core, 'group_history_summary_context', side_effect=lambda *a, **k: calls.append(a[3]) or ''), \
         patch.object(core, 'group_memory_context', return_value=''), \
         patch.object(core, 'user_memory_context', return_value=''), \
         patch.object(core, 'recent_context', return_value=''), \
         patch.object(core, '_wechat_recent_bot_reply_texts', return_value=[]), \
         patch.object(core, 'sender_context_label', return_value='CURRENT_SENDER'):
        core.freeze_llm_context(
            CFG, 'wxid_fixture', {}, current_text=synthetic,
            history_request_text='@BOT', now=NOW,
        )
    assert calls == ['@BOT']
    print('V3163_HISTORY_SHADOW_USES_RAW_USER_TEXT=PASS')


def main():
    snapshot = false_summary_fixture()
    shadow_failure_does_not_change_legacy(snapshot)
    synthetic_bare_mention_prompt_not_sampled()


if __name__ == '__main__':
    main()
