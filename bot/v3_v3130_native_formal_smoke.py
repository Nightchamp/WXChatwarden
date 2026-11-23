#!/usr/bin/env python3
"""Native formal regressions. Synthetic state only; no Google or WeChat UI."""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from chatwarden import llm_client, core
from chatwarden.worker import GroupWorker

CFG = {'name': 'ab-fixture', 'chat_username': '12345@chatroom',
       'session_key': 'unused-by-native',
       'personality': 'PERSONALITY_SENTINEL'}
SNAP = {'context': 'RECENT_SENTINEL', 'style_bot_texts': [],
        'current_sender': 'uid:00112233 | not-owner | PERSON_SENTINEL',
        'memory_context': 'USER_MEMORY_SENTINEL GROUP_MEMORY_SENTINEL',
        'history_context': ''}


def result(text='NORMAL'):
    return {'ok': True, 'text': text, 'request_id': 'test-result',
            'session_turn': 1, 'duration_ms': 1, 'tool_count': 0, 'web_used': False}


class FormalTests(unittest.TestCase):
    def setUp(self):
        self.log = patch.object(core, 'log').start()
        self.addCleanup(patch.stopall)

    def test_shared_context_and_no_legacy_policy(self):
        with patch.object(llm_client, 'execute_formal', return_value=result()) as execute, \
             patch.object(core, 'recent_context', side_effect=AssertionError('future context')), \
             patch.object(core, 'image_gen_enabled', return_value=True):
            text = core.call_formal_reply(CFG, 'CURRENT_QUESTION', 'person', {}, context_snapshot=SNAP)
            self.assertEqual(text, 'NORMAL')
            prompt = execute.call_args.args[1]
            for marker in ('PERSONALITY_SENTINEL', 'RECENT_SENTINEL', 'PERSON_SENTINEL',
                           'USER_MEMORY_SENTINEL', 'GROUP_MEMORY_SENTINEL',
                           'CURRENT_QUESTION', '[开始生图]'):
                self.assertIn(marker, prompt)
            self.assertNotIn('普通 bot2 快速响应规则', prompt)
            self.assertNotIn('轻量实时搜索要求', prompt)
            # Persisted-history rendering is now covered by v3.132's dedicated
            # sessionless history-summary profile regression.

    def test_ordinary_chat_strips_decorative_bold_but_explicit_markdown_survives(self):
        markdown_answer = (
            '按梯队分先后：\n\n'
            '1. **上海**：断层第一。\n'
            '2. **成都**：包容度天花板。'
        )
        with patch.object(
            llm_client,
            'execute_formal',
            return_value=result(markdown_answer),
        ):
            normal = core.call_formal_reply(
                CFG,
                '给我排一下城市时尚度',
                'person',
                {},
                raw_question='给我排一下城市时尚度',
                context_snapshot=SNAP,
            )
            self.assertIn('1. 上海：断层第一。', normal)
            self.assertNotIn('**', normal)

            literal = core.call_formal_reply(
                CFG,
                '请用 Markdown 格式输出',
                'person',
                {},
                raw_question='请用 Markdown 格式输出',
                context_snapshot=SNAP,
            )
            self.assertIn('**上海**', literal)

    def test_default_native_vision_and_business_routes(self):
        with patch.object(core, 'call_vision_reply', return_value='VISION') as vision, \
             patch.object(llm_client, 'execute_formal', return_value=result()) as execute:
            self.assertEqual(core.call_formal_reply(CFG, 'hi', 'a', {}, context_snapshot=SNAP), 'NORMAL')
            self.assertEqual(core.call_formal_reply(CFG, 'look', 'a', {}, image_path='/tmp/exact.png'), 'VISION')
            self.assertEqual((execute.call_count, vision.call_count), (1, 1))
            self.assertEqual(core.call_formal_reply(CFG, '深入调查这个问题', 'a', {}), core.AGENT_REACH_MARKER)
            self.assertEqual((execute.call_count, vision.call_count), (1, 1))

    def test_gateway_fallback_result_is_used_without_bot_side_retry(self):
        fallback_result = dict(
            result('RECOVERED_BY_LUNA'),
            fallback_used=True,
            fallback_provider='backup-provider',
            model='backup/model-b',
            primary_error_kind='timeout',
        )
        with patch.object(
            llm_client, 'execute_formal',
            return_value=fallback_result,
        ) as execute:
            answer = core.call_formal_reply(CFG, 'hi', 'a', {}, context_snapshot=SNAP)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(answer, 'RECOVERED_BY_LUNA')

        for failure in ('timeout', 'upstream_failed', 'fallback_timeout'):
            with self.subTest(failure=failure), patch.object(
                llm_client, 'execute_formal',
                side_effect=llm_client.LLMExecutionError(failure),
            ) as execute:
                answer = core.call_formal_reply(CFG, 'hi', 'a', {}, context_snapshot=SNAP)
                self.assertEqual(execute.call_count, 1)
                self.assertNotIn(failure, answer)
                self.assertIn('稍后', answer)

    def test_no_migration_selector_or_legacy_runtime_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'groups.json'
            with patch.object(core, 'GROUPS_FILE', path):
                path.write_text(json.dumps({'groups': [dict(CFG), {'name': 'new'}]}))
                groups = core.load_groups_config()
                self.assertTrue(all('ai_backend' not in g for g in groups))
                self.assertFalse(hasattr(core, 'call_hermes'))
                self.assertFalse(hasattr(core, 'call_llm'))
                self.assertFalse(hasattr(core, '_v35_session_cfg_for_call'))

    def test_group_worker_preserves_send_save_and_stale_guard(self):
        worker = GroupWorker.__new__(GroupWorker)
        worker.chat_name = CFG['name']
        worker.stop_event = threading.Event()
        worker._reply_state_lock = threading.RLock()
        worker._reply_generation = 0
        worker._v392_send_lock = threading.Lock()
        worker._v39_note_bot_sent = lambda: None
        worker._v380_provider_question = lambda event: event['text']
        worker._followup_after_success = lambda event: None
        event = {'local_id': 42, 'timestamp': 0, 'text': 'hello', '_v36_trigger_kind': 'explicit'}
        work = SimpleNamespace(generation=0, event=event, image_path=None, provider_prompt='hello',
                               sender='person', display='person', legacy_sender='other',
                               group_cfg=dict(CFG), profiles={}, context_snapshot=dict(SNAP),
                               queued_at=time.monotonic())
        full = 'A' * 3400
        with patch.object(llm_client, 'execute_formal', return_value=result(full)) as execute, \
             patch.object(core, 'focus_and_send', return_value=.01) as send, \
             patch.object(core, 'save_message') as save:
            worker._run_reply_job(work)
            self.assertEqual(execute.call_count, 1)
            self.assertGreater(send.call_count, 1)
            self.assertTrue(all(c.args[0] == CFG['name'] for c in send.call_args_list))
            self.assertEqual(''.join(c.args[1] for c in send.call_args_list), full)
            self.assertEqual(save.call_args.args, (CFG['name'], 'bot', full))
            send.reset_mock(); save.reset_mock()
            def stale(*args, **kwargs):
                worker._reply_generation += 1
                return result('DO_NOT_SEND')
            execute.side_effect = stale
            worker._run_reply_job(work)
            send.assert_not_called(); save.assert_not_called()


class ClientTests(unittest.TestCase):
    def test_exact_identity_session_and_response_binding(self):
        calls = []
        broken = [False]
        leak = [False]
        third_turn = [False]
        def open_request(request, timeout):
            self.assertTrue(llm_client.busy())
            self.assertEqual(timeout, 130)
            data = json.loads(request.data)
            calls.append(data)
            answer = dict(result(), task_kind='formal', structured=None,
                          request_id='wrong' if broken[0] else data['request_id'])
            if third_turn[0]:
                answer['session_turn'] = 3
            if leak[0]:
                answer['text'] = (
                    '{"toolAction":"Replying to group","toolSummary":"Complete task"}\n'
                    'DO_NOT_EXPOSE_WRAPPER'
                )
            response = io.BytesIO(json.dumps(answer).encode())
            response.status = 200
            return response
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'test.key'; key.write_text('x' * 40)
            with patch.object(llm_client, 'KEY_FILE', key), \
                 patch.object(llm_client.urllib.request, 'build_opener', return_value=SimpleNamespace(open=open_request)):
                llm_client.execute_formal(CFG, 'one')
                llm_client.execute_formal(dict(CFG, name='renamed'), 'two')
                llm_client.execute_formal(dict(CFG, chat_username='67890@chatroom'), 'other')
                self.assertEqual(calls[0]['session_key'], calls[1]['session_key'])
                self.assertNotEqual(calls[1]['session_key'], calls[2]['session_key'])
                self.assertEqual(len({d['request_id'] for d in calls}), 3)
                third_turn[0] = True
                llm_client.execute_formal(CFG, 'third-turn')
                third_turn[0] = False
                self.assertEqual(len(calls), 4)
                broken[0] = True
                with self.assertRaises(llm_client.LLMExecutionError):
                    llm_client.execute_formal(CFG, 'bad')
                self.assertEqual(len(calls), 5)
                broken[0] = False
                leak[0] = True
                with self.assertRaises(llm_client.LLMExecutionError):
                    llm_client.execute_formal(CFG, 'wrapper-leak')
                self.assertEqual(len(calls), 6)
        self.assertFalse(llm_client.busy())
        with self.assertRaises(llm_client.LLMExecutionError):
            llm_client.execute_formal({'name': 'ambiguous'}, 'bad')

    def test_gateway_fallback_metadata_is_accepted_on_normal_formal_kind(self):
        calls = []
        def open_request(request, timeout):
            self.assertEqual(timeout, 130)
            data = json.loads(request.data)
            calls.append(data)
            self.assertEqual(data['task_kind'], 'formal')
            self.assertIn('session_key', data)
            self.assertEqual(data['prompt'], 'FROZEN_PROMPT')
            answer = dict(
                result('RECOVERED'),
                task_kind='formal',
                request_id=data['request_id'],
                structured=None,
                session_turn=1,
                fallback_used=True,
                fallback_provider='backup-provider',
                model='backup/model-b',
                primary_error_kind='upstream_failed',
            )
            response = io.BytesIO(json.dumps(answer).encode())
            response.status = 200
            return response
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'test.key'; key.write_text('x' * 40)
            with patch.object(llm_client, 'KEY_FILE', key), \
                 patch.object(llm_client.urllib.request, 'build_opener',
                              return_value=SimpleNamespace(open=open_request)):
                recovered = llm_client.execute_formal(
                    CFG, 'FROZEN_PROMPT', explicit=True,
                )
                self.assertEqual(recovered['text'], 'RECOVERED')
                self.assertTrue(recovered['fallback_used'])
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    if not outcome.wasSuccessful():
        raise SystemExit(1)
    print('V3130_LLM_FORMAL_CONTEXT_SCOPE_NO_FALLBACK_SEND_GUARDS=PASS')
