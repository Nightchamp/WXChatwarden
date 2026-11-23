#!/usr/bin/env python3
"""the decision service shadow acceptance: frozen data, privacy, capacity and legacy isolation.

All providers are mocked. No real messages, cloud calls or production DB writes.
"""
import copy
from dataclasses import FrozenInstanceError, replace
from contextlib import contextmanager
import json
import os
import queue
import threading
import time
from unittest.mock import patch

os.environ['CW_DECISION_SHADOW'] = '0'
from chatwarden import core, decision_client as dc
from chatwarden.worker import GroupWorker

CFG = {'name': 'fixture-chat', 'chat_username': '123456789@chatroom',
       'member_labels': {'wxid_sender': '小张', 'wxid_peer': '小李'}}


def frozen_and_historical():
    rows = [('bot', '我在讲缓存失效。'), ('wxid_sender', '我明白了'), ('wxid_peer', '下班吃饭去')]
    reads, prompts = [], []

    class Database:
        def execute(self, sql, args):
            assert 'group_messages' in sql and args[0] == 'fixture-chat'
            reads.append(1)
            return self

        def fetchall(self):
            return list(reversed(rows))

    @contextmanager
    def database():
        yield Database()

    def auxiliary(kind, prompt):
        assert kind == 'classifier'
        prompts.append(prompt)
        return {'structured': {'addressed': False}}

    with patch.object(core, 'open_db', database), patch.object(core, 'call_llm_auxiliary', auxiliary):
        assert core.call_followup_gate(CFG, '那你继续讲缓存', 'wxid_sender') is False
        old_prompt = prompts[-1]
        reads.clear()
        capture = {'bot_identity': 'wxid_bot', 'bot_display_name': '小卫机器人'}
        assert core.call_followup_gate(CFG, '那你继续讲缓存', 'wxid_sender', shadow_capture=capture) is False
        snap = capture['snapshot']
        assert len(reads) == 1, 'frozen request must use one DB read'
        assert prompts[-1] == old_prompt, 'shadow must not rewrite the legacy prompt'
        reads.clear()
        historical = core.build_followup_snapshot(
            CFG, '那你继续讲缓存', 'wxid_sender', tuple(rows),
            bot_identity='wxid_bot', bot_display_name='小卫机器人',
            created_at=123.5,
        )
        assert not reads, 'historical builder must not reread the live DB'
        assert historical.current_message == snap.current_message
        assert historical.legacy_context == snap.legacy_context
        assert historical.legacy_sender == snap.legacy_sender
        assert historical.recent_context == snap.recent_context
        assert historical.aliases == snap.aliases
        assert historical.chat_identity == snap.chat_identity
        assert historical.sender_identity == snap.sender_identity
        assert historical.created_at == 123.5
        rows[:] = [('bot', 'A completely different later topic')]
        state = dc.sanitized_state(snap)
        assert len(reads) == 0 and len(state['recent_context']) == 3
        assert state['recent_context'][0] == {'role': 'BOT', 'text': '我在讲缓存失效。'}
        assert state['recent_context'][1]['role'] == 'CURRENT_SENDER'
        assert state['recent_context'][2]['role'] == 'OTHER_1'
        try:
            snap.current_message = 'changed'
            raise AssertionError('snapshot was mutable')
        except FrozenInstanceError:
            pass
        # Historical v3.29 rule: positive keywords must never shortcut YES.
        assert len(prompts) == 2 and '那你继续讲缓存' in prompts[-1]
        before = len(prompts)
        for text, mode, expected in [('哈哈', 'followup', False),
                ('为什么这么说', 'quoted-bot', True), ('😂', 'quoted-bot', False)]:
            cap = {}
            assert core.call_followup_gate(CFG, text, 'wxid_sender', gate_mode=mode, shadow_capture=cap) is expected
            assert 'snapshot' not in cap
        assert len(prompts) == before
    print('V3162_FROZEN_SINGLE_READ_LEGACY_PROMPT_PARITY=PASS')
    print('V3162_HISTORICAL_REPLAY_SNAPSHOT_PARITY=PASS')
    print('V3162_HISTORICAL_TINY_QUOTE_NO_KEYWORD_YES=PASS')
    return snap


def privacy_and_validation(snap):
    dirty = replace(snap, current_message=(
        '@小卫机器人 小张 wxid_private 123456789@chatroom '
        '/home/user/private/key.txt C:\\private\\key.txt apikey_private '
        'Bearer private-value api_key=private-value ' + 'a' * 64))
    state = dc.sanitized_state(dirty)
    encoded = json.dumps(state, ensure_ascii=False)
    for private in ['小卫', '小张', 'wxid_', '@chatroom', '/home/', 'C:\\', 'apikey_', 'private-value', 'a' * 64]:
        assert private not in encoded, private
    payload = dc.payload_for(dirty, False, {'local_id': 7, 'generation': 2}, 'k' * 48)
    assert payload['telemetry']['chat_ref'] != snap.chat_identity
    assert 'telemetry' not in payload['state']
    good = {'ok': True, 'request_id': payload['request_id'], 'decision_kind': 'followup_v1',
            'decision_version': 1, 'mode': 'shadow', 'model': 'pinned-test-model',
            'answers': {'addressed': {'type': 'noul', 'probability': .9}}}
    assert dc.validate_response(good, payload) is good
    payload_v2 = dc.payload_for_followup_v2(
        dirty, False, {'local_id': 8, 'generation': 3}, 'k' * 48,
    )
    good_v2 = {
        'ok': True, 'request_id': payload_v2['request_id'],
        'decision_kind': 'followup_v2', 'decision_version': 2,
        'mode': 'shadow', 'model': 'pinned-test-model',
        'answers': {
            'addressed_to_bot': {'type': 'noul', 'probability': .9},
            'needs_reply': {'type': 'noul', 'probability': .1},
        },
    }
    assert dc.validate_response(good_v2, payload_v2) is good_v2
    assert payload_v2['decision_kind'] == 'followup_v2'
    for p in [float('nan'), float('inf'), -1, 2, True, '0.9']:
        bad = copy.deepcopy(good)
        bad['answers']['addressed']['probability'] = p
        try:
            dc.validate_response(bad, payload)
            raise AssertionError('invalid probability accepted')
        except dc.DecisionClientError:
            pass
    print('V3162_PRIVACY_TYPED_RESPONSE=PASS')
    print('V3165_FOLLOWUP_V2_TYPED_RESPONSE=PASS')


def lane_and_gate(snap):
    started, release = threading.Event(), threading.Event()
    sent = []

    def slow_send(payload, key):
        sent.append(payload)
        started.set()
        release.wait(2)
        raise dc.DecisionClientError('upstream_timeout')

    lane = dc.ShadowLane('k' * 48, send=slow_send, capacity=1, workers=1)
    worker = GroupWorker.__new__(GroupWorker)
    worker.group_cfg, worker.chat_name = CFG, CFG['name']
    worker.profiles, worker.bot_wxid = {}, 'wxid_bot'
    worker.stop_event = threading.Event()
    worker.q = queue.Queue()
    worker._followup_gate_queue = queue.Queue(maxsize=1)
    worker._followup_gate_slot = threading.BoundedSemaphore(1)
    worker._followup_gate_state_lock = threading.RLock()
    worker._followup_gate_generation = {}
    worker._v39_note_ordinary_human = lambda *_: None
    decision = [True]
    legacy_started, legacy_release = threading.Event(), threading.Event()
    block_legacy = [False]

    def legacy(*args, **kwargs):
        kwargs['shadow_capture']['snapshot'] = snap
        if block_legacy[0]:
            legacy_started.set()
            legacy_release.wait(1)
        return decision[0]

    def submit(n):
        worker._v3144_note_sender_activity('wxid_sender')
        assert worker._v3144_submit_followup_gate({'sender_username': 'wxid_sender', 'text': '继续说', 'local_id': n})

    thread = threading.Thread(target=worker._v3144_followup_gate_run, daemon=True)
    try:
        with patch.object(dc, '_LANE', lane), \
             patch.object(dc, '_MODES', {'followup_v2': 'shadow'}), \
             patch.object(core, 'call_followup_gate', legacy):
            thread.start()
            submit(1)
            assert worker.q.get(timeout=.5)['addressed'] is True
            assert started.wait(.5)
            assert worker._followup_gate_slot.acquire(blocking=False), 'the decision service retained legacy slot'
            worker._followup_gate_slot.release()
            decision[0] = False
            before = time.monotonic()
            submit(2)
            assert worker.q.get(timeout=.5)['addressed'] is False
            assert time.monotonic() - before < .5 and not release.is_set()
            worker._followup_gate_queue.join()
            deadline = time.monotonic() + .5
            while lane.queue.qsize() < 1 and time.monotonic() < deadline:
                time.sleep(.005)
            before = time.monotonic()
            assert lane.submit(snap, True, {}) is False
            assert time.monotonic() - before < .05
            # A stale positive cannot publish a control result, regardless of shadow.
            decision[0], block_legacy[0] = True, True
            submit(3)
            assert legacy_started.wait(.5)
            worker._v3144_note_sender_activity('wxid_sender')
            legacy_release.set()
            worker._followup_gate_queue.join()
            assert worker.q.empty(), 'stale legacy result escaped'
            release.set()
            lane.queue.join()
            assert lane.errors >= 1 and worker.q.empty(), 'shadow failure emitted control work'
    finally:
        release.set(); legacy_release.set()
        worker.stop_event.set(); lane.stop.set()
        thread.join(1)
        for t in lane.threads:
            t.join(1)
    print('V3162_SHADOW_QUEUE_FULL_NONBLOCKING=PASS')
    print('V3162_LEGACY_CONTROL_SLOT_RELEASE_BEFORE_SHADOW=PASS')
    print('V3162_SHADOW_TIMEOUT_STALE_NO_USER_ACTION=PASS')


def main():
    snap = frozen_and_historical()
    privacy_and_validation(snap)
    lane_and_gate(snap)


if __name__ == '__main__':
    main()
