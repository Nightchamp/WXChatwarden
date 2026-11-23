#!/usr/bin/env python3
"""v3.167 the decision service Active routing acceptance; no cloud, DB writes or WeChat sends."""
from contextlib import contextmanager
from datetime import datetime
import os
import time
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ['CW_DECISION_SHADOW'] = '0'

from chatwarden import core, decision_client as dc
from chatwarden import worker as worker_mod
from chatwarden.private_worker import PrivateWorker


CFG = {
    'name': '示例群聊壹号测试',
    'chat_username': '90000000002@chatroom',
    'personality': '',
    'member_labels': {'wxid_fixture': '林晓雨'},
}
NOW = datetime(2026, 9, 26, 19, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
COUNT_CASE = '之前群聊里让你生成过几张王叔送月饼的照片'
WINDOW = {
    'start_ts': NOW.timestamp() - 24 * 3600,
    'end_ts': NOW.timestamp(),
    'start_text': '2026-09-25 19:12',
    'end_text': '2026-09-26 19:12',
    'source': 'fixture-window',
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
                (101, 'wxid_other', '这张也算一张', NOW.timestamp() - 7100),
                (102, 'bot', '[已生成图片] 王叔送月饼', NOW.timestamp() - 7000),
            ]

    yield Database()


def followup_active_and_fallback():
    worker = worker_mod.GroupWorker(
        {'group_cfg': dict(CFG), 'chat_name': CFG['name']},
        None,
        {},
        'wxid_bot',
        'wxid_owner',
    )
    sender = 'wxid_fixture'
    answered = []
    original_answer = worker._answer
    worker._answer = lambda event, *a, **k: answered.append(dict(event))

    def event(local_id, text):
        return {
            'local_id': local_id,
            'server_id': local_id,
            'timestamp': 0,
            'local_type': 1,
            'sender_username': sender,
            'sender_display': '林晓雨',
            'text': text,
            'msgsource': '',
        }

    legacy_calls = []
    try:
        worker.followups[sender] = {'last_success': time.monotonic()}
        with patch.object(dc, 'mode', side_effect=lambda kind: 'active' if kind == 'followup_v2' else 'off'), \
             patch.object(dc, 'decide_followup', return_value='reply'), \
             patch.object(core, 'freeze_followup_snapshot', return_value=SimpleNamespace()), \
             patch.object(core, 'call_followup_gate', side_effect=lambda *a, **k: legacy_calls.append(1) or True):
            worker._handle_text(event(16701, '那你继续说'))
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not answered:
                time.sleep(0.01)
        assert len(answered) == 1
        assert answered[-1].get('_v324_followup_gate_passed') is True
        assert legacy_calls == []
        print('V3167_FOLLOWUP_ACTIVE_PRIMARY=PASS')

        answered.clear()
        worker.followups[sender] = {'last_success': time.monotonic()}
        with patch.object(dc, 'mode', side_effect=lambda kind: 'active' if kind == 'followup_v2' else 'off'), \
             patch.object(dc, 'decide_followup', side_effect=dc.DecisionClientError('transport_error')), \
             patch.object(core, 'freeze_followup_snapshot', return_value=SimpleNamespace()), \
             patch.object(core, 'call_followup_gate', side_effect=lambda *a, **k: legacy_calls.append(1) or True):
            worker._handle_text(event(16702, '那这个呢'))
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not answered:
                time.sleep(0.01)
        assert len(answered) == 1
        assert legacy_calls == [1]
        print('V3167_FOLLOWUP_ACTIVE_FAILURE_FALLS_BACK=PASS')
    finally:
        worker._answer = original_answer
        worker.stop()
        worker.thread.join(timeout=2.0)


def history_count_is_not_summary():
    route = {}
    with patch.object(dc, 'mode', return_value='active'), \
         patch.object(dc, 'decide_history_intent', return_value='count_lookup'), \
         patch.object(core, 'open_db', history_db), \
         patch.object(core, 'classify_group_history_summary_request', return_value=dict(WINDOW)), \
         patch.object(core, '_group_history_cursor_save', lambda *a, **k: None):
        block = core.group_history_summary_context(
            CFG['name'], CFG, 'wxid_fixture', COUNT_CASE,
            now=NOW, route_capture=route,
        )
    assert route == {'route': 'count_lookup'}, route
    assert '当前是定向历史计数任务' in block
    assert '当前是明确的群历史总结' not in block
    print('V3167_HISTORY_COUNT_ROUTE_NOT_SUMMARY=PASS')

    formal_result = {
        'text': '按这段历史能确认到两张',
        'fallback_used': False,
        'request_id': 'fixture-formal',
        'session_turn': 1,
        'duration_ms': 1,
        'tool_count': 0,
        'web_used': False,
        'model': 'fixture',
        'primary_error_kind': '',
    }
    snapshot = {
        'context': '',
        'style_bot_texts': [],
        'current_sender': '林晓雨',
        'memory_context': '',
        'history_context': block,
        'history_route': 'count_lookup',
    }
    with patch.object(core, 'prepare_formal_prompt', return_value=('prepared', None)), \
         patch.object(core.llm_client, 'execute_formal', return_value=formal_result), \
         patch.object(core, 'call_llm_auxiliary', side_effect=AssertionError('count lookup must not use history_summary')):
        reply = core.call_formal_reply(
            CFG, COUNT_CASE, 'wxid_fixture', {},
            raw_question=COUNT_CASE,
            context_snapshot=snapshot,
            allow_imagegen_route=False,
        )
    assert '两张' in reply
    print('V3167_HISTORY_COUNT_USES_NORMAL_FORMAL=PASS')


def media_active_group_and_private_contract():
    group = worker_mod.GroupWorker.__new__(worker_mod.GroupWorker)
    group.chat_name = CFG['name']
    group.group_cfg = dict(CFG)
    group.bot_wxid = 'wxid_bot'
    event = {
        'local_id': 16703,
        'local_type': 1,
        'text': '画一只骑自行车的鹈鹕',
        'sender_username': 'wxid_fixture',
        'sender_display': '林晓雨',
        'bot_display_name': 'BOT',
        '_v36_trigger_kind': 'explicit',
    }
    captured = []

    def group_decide(snapshot, metadata=None):
        captured.append((snapshot, metadata or {}))
        return 'imagegen'

    with patch.object(dc, 'mode', return_value='active'), \
         patch.object(dc, 'decide_media_route', side_effect=group_decide), \
         patch.object(group, '_v343_current_instruction_text', return_value=event['text']), \
         patch.object(group, '_v380_provider_question', return_value=event['text']):
        route = group._v3167_media_route_active_decide(event)
    assert route == 'imagegen'
    assert captured[0][0].visual_available is False
    assert captured[0][1] == {'local_id': 16703}
    print('V3167_GROUP_MEDIA_ACTIVE_CONTRACT=PASS')

    private = PrivateWorker.__new__(PrivateWorker)
    private.chat_name = '联系人'
    private.peer_wxid = 'wxid_peer'
    private.bot_wxid = 'wxid_bot'
    private_event = {
        'local_id': 16704,
        'local_type': 1,
        'text': '这张图是什么？',
        'bot_display_name': '',
    }
    private_seen = []

    def private_decide(snapshot, metadata=None):
        private_seen.append(snapshot)
        return 'vision'

    with patch.object(dc, 'mode', return_value='active'), \
         patch.object(dc, 'decide_media_route', side_effect=private_decide), \
         patch.object(private, '_current_text', return_value=private_event['text']):
        route = private._v3167_media_route_active_decide(
            private_event,
            visual_available=True,
            visual_source='quoted',
        )
    assert route == 'vision'
    assert private_seen[0].visual_available is True
    assert private_seen[0].visual_source == 'quoted'
    print('V3167_PRIVATE_MEDIA_ACTIVE_CONTRACT=PASS')


def main():
    followup_active_and_fallback()
    history_count_is_not_summary()
    media_active_group_and_private_contract()


if __name__ == '__main__':
    main()
