#!/usr/bin/env python3
"""Phase-4 synthetic regressions. No live provider, Bot DB writes or UI sends."""
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from chatwarden import llm_client, core
from chatwarden.worker import GroupWorker

CFG = {'name': 'aux-fixture', 'chat_username': '12345@chatroom',
       'personality': 'PERSONALITY_KEEP', 'default_personality': 'DEFAULT_KEEP'}
VALUES = {
    'classifier': {'addressed': False},
    'history_metadata': {'mode': 'none', 'start': '', 'end': ''},
    'personality': {'personality': '新的人格但不更改权限'},
    'personality_random': {'personality': '随机人格', 'summary': '克制而幽默'},
    'welcome': {'text': '欢迎新朋友加入群聊'},
    'proactive': {'decision': 'SILENT', 'draft': '', 'reason': 'no_messages'},
    'history_summary': {'summary': 'HISTORY_SUMMARY_COMPLETE'},
}


def result(kind, data=None):
    return {'ok': True, 'text': '', 'structured': dict(VALUES[kind] if data is None else data),
            'request_id': 'test-' + kind, 'task_kind': kind, 'session_turn': 1,
            'duration_ms': 1, 'tool_count': 1, 'web_used': False, 'usage': {}}


class ClientTests(unittest.TestCase):
    def test_auxiliary_contract_timeouts_and_no_session(self):
        calls = []
        corrupt = [False]
        def send(request, timeout):
            payload = json.loads(request.data)
            kind = payload['task_kind']
            calls.append(payload)
            self.assertNotIn('session_key', payload)
            self.assertEqual(timeout, llm_client.AUX_TIMEOUTS[kind])
            self.assertTrue(llm_client.busy())
            answer = dict(result(kind), request_id=payload['request_id'])
            if corrupt[0]:
                answer['structured']['toolAction'] = 'DO_NOT_EXPOSE'
            response = io.BytesIO(json.dumps(answer).encode())
            response.status = 200
            return response
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'fixture.key'
            key.write_text('synthetic-key-' * 4)
            with patch.object(llm_client, 'KEY_FILE', key), \
                 patch.object(llm_client.urllib.request, 'build_opener',
                              return_value=SimpleNamespace(open=send)):
                for kind in VALUES:
                    output = llm_client.execute_auxiliary(kind, 'synthetic')
                    self.assertEqual(output['structured'], VALUES[kind])
                corrupt[0] = True
                with self.assertRaises(llm_client.LLMExecutionError):
                    llm_client.execute_auxiliary('personality', 'bad')
        self.assertEqual(len({c['request_id'] for c in calls}), len(calls))
        self.assertFalse(llm_client.busy())

    def test_only_bounded_known_machine_error_is_exposed(self):
        cases = [(502, {'kind': 'invalid_structured_output'}, 'invalid_structured_output'),
                 (502, {'kind': '/private/DO_NOT_EXPOSE'}, 'upstream_http'),
                 # v3.201: Gateway kinds outside the old mirror list survive.
                 (502, {'kind': 'empty_response'}, 'empty_response'),
                 (502, {'kind': 'fallback_empty_response'}, 'fallback_empty_response'),
                 (502, {'kind': 'Bad Kind'}, 'upstream_http'),
                 (502, {'kind': 'x' * 49}, 'upstream_http'),
                 (502, {'kind': 7}, 'upstream_http'),
                 (504, {'kind': 'fallback_timeout'}, 'timeout'),
                 (503, {'kind': 'invalid_structured_output'}, 'unavailable')]
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'fixture.key'
            key.write_text('synthetic-key-' * 4)
            for status, error, expected in cases:
                failure = urllib.error.HTTPError('local-test', status, '', {},
                    io.BytesIO(json.dumps({'error': error}).encode()))
                with patch.object(llm_client, 'KEY_FILE', key), \
                     patch.object(llm_client.urllib.request, 'build_opener',
                                  return_value=SimpleNamespace(open=Mock(side_effect=failure))):
                    with self.assertRaises(llm_client.LLMExecutionError) as caught:
                        llm_client.execute_auxiliary('proactive', 'fixture')
                    self.assertEqual(caught.exception.kind, expected)


class AuxiliaryTests(unittest.TestCase):
    def setUp(self):
        patch.object(core, 'log').start()
        self.addCleanup(patch.stopall)

    def worker(self):
        worker = GroupWorker.__new__(GroupWorker)
        worker.group_cfg = dict(CFG)
        worker.chat_name = CFG['name']
        worker.owner_wxid = 'wxid_owner'
        worker.bot_wxid = 'wxid_bot'
        worker.welcome_enabled = True
        worker._v39_call_shadow_hermes = Mock(side_effect=AssertionError('legacy fallback'))
        worker._send_fixed = Mock()
        worker._v3109_persist_personality = Mock()
        jobs = []
        worker._reply_dispatcher = SimpleNamespace(jobs=jobs, submit=lambda job: (jobs.append(job) or True))
        return worker

    def test_followup_boolean_failure_and_local_shortcuts(self):
        with patch.object(core, '_followup_gate_context', return_value='recent bot turn'), \
             patch.object(core, 'sender_context_label', return_value='fixture'), \
             patch.object(llm_client, 'execute_auxiliary', return_value=result('classifier', {'addressed': True})) as execute:
            self.assertTrue(core.call_followup_gate(CFG, '你刚才那点再解释一下', 'wxid_fixture'))
            self.assertEqual(execute.call_args.args[0], 'classifier')
            self.assertIn('addressed', execute.call_args.args[1])
            self.assertNotIn('只允许输出一个精确标记', execute.call_args.args[1])
            execute.side_effect = llm_client.LLMExecutionError('timeout')
            self.assertFalse(core.call_followup_gate(CFG, '继续解释', 'wxid_fixture'))
            self.assertEqual(execute.call_count, 2)
            self.assertFalse(core.call_followup_gate(CFG, '哈哈', 'wxid_fixture'))
            self.assertTrue(core.call_followup_gate(CFG, '进一步解释', 'wxid_fixture', gate_mode='quoted-bot'))
            self.assertEqual(execute.call_count, 2)

    def test_history_metadata_validation_and_failure(self):
        now = datetime(2026, 9, 14, 18, 0, tzinfo=core._GROUP_HISTORY_TZ)
        fixtures = [({'mode': 'none', 'start': '', 'end': ''}, None),
                    ({'mode': 'previous_window', 'start': '', 'end': ''}, 'previous_window'),
                    ({'mode': 'window', 'start': '2026-09-14 10:00', 'end': '2026-09-14 12:00'}, 'llm-window'),
                    ({'mode': 'window', 'start': '2026-09-14 10:00', 'end': '2026-09-15 12:00'}, 'classification_failed'),
                    ({'mode': 'window', 'start': '2026-09-01 10:00', 'end': '2026-09-14 12:00'}, 'classification_failed')]
        for data, expected in fixtures:
            with patch.object(llm_client, 'execute_auxiliary', return_value=result('history_metadata', data)) as execute:
                out = core.classify_group_history_summary_request('群里之前怎么说的', now, force=True)
                self.assertEqual(None if out is None else out.get('mode', out.get('source')), expected)
                self.assertEqual(execute.call_args.args[0], 'history_metadata')
        with patch.object(llm_client, 'execute_auxiliary', side_effect=llm_client.LLMExecutionError('unavailable')) as execute:
            out = core.classify_group_history_summary_request('群里之前怎么说的', now, force=True)
            self.assertEqual(out['mode'], 'classification_failed')
            self.assertEqual(execute.call_count, 1)

    def test_history_summary_has_own_profile_and_frozen_evidence(self):
        snap = {'context': '', 'current_sender': 'not-owner | Person', 'memory_context': 'GROUP_MEMORY',
                'style_bot_texts': [], 'history_context': 'HISTORY_ONLY_EVIDENCE'}
        with patch.object(llm_client, 'execute_auxiliary', return_value=result('history_summary')) as execute, \
             patch.object(llm_client, 'execute_formal', side_effect=AssertionError('formal session polluted')), \
             patch.object(core, 'recent_context', side_effect=AssertionError('new context read')):
            text = core.call_formal_reply(CFG, '总结群里最近的聊天', 'wxid_fixture', {}, context_snapshot=snap)
            self.assertEqual(text, 'HISTORY_SUMMARY_COMPLETE')
            self.assertEqual(execute.call_args.args[0], 'history_summary')
            prompt = execute.call_args.args[1]
            for part in ('HISTORY_ONLY_EVIDENCE', 'GROUP_MEMORY', '350～700', '1000', '不要 Markdown', '不联网'):
                self.assertIn(part, prompt)
            execute.side_effect = llm_client.LLMExecutionError('timeout')
            self.assertIn('稍后', core.call_formal_reply(CFG, '总结聊天', 'wxid_fixture', {}, context_snapshot=snap))

        markdown = '# 昨日总结\n\n- **第一件事**：聊了A。\n- 第二件事：聊了B。\n\n`内部样式`'
        plain = core._normalize_wechat_history_summary(markdown)
        self.assertEqual(
            plain,
            '昨日总结\n第一件事：聊了A。\n第二件事：聊了B。\n\n内部样式',
        )
        long_text = '这是一个完整话题，包含人物和事件变化。' * 80
        self.assertEqual(core._normalize_wechat_history_summary(long_text), long_text)

        compact = '这是一份压缩后的完整摘要。' * 35
        with patch.object(
            llm_client,
            'execute_auxiliary',
            side_effect=[
                result('history_summary', {'summary': long_text}),
                result('history_summary', {'summary': compact}),
            ],
        ) as execute:
            text = core.call_formal_reply(
                CFG,
                '总结群里最近的聊天',
                'wxid_fixture',
                {},
                context_snapshot=snap,
            )
            self.assertEqual(text, compact)
            self.assertEqual(execute.call_count, 2)
            retry_prompt = execute.call_args_list[1].args[1]
            self.assertIn('绝对不要超过1000字', retry_prompt)
            self.assertIn('不要通过截断句子', retry_prompt)

    def test_welcome_uses_current_title_and_fixed_fallback(self):
        worker = self.worker()
        prompt = worker._v3100_welcome_prompt(['New Friend'], 'Renamed Group')
        self.assertIn('Renamed Group', prompt)
        self.assertNotIn('PERSONALITY_KEEP', prompt)
        with patch.object(llm_client, 'execute_auxiliary', return_value=result('welcome')) as execute:
            self.assertEqual(worker._v3100_call_welcome_generation(prompt), VALUES['welcome']['text'])
            self.assertEqual(execute.call_args.args[0], 'welcome')
            execute.side_effect = llm_client.LLMExecutionError('timeout')
            worker._v3100_queue_welcome({'local_id': 1, 'chat_display_name': 'Renamed Group'}, ['New Friend'])
            worker._reply_dispatcher.jobs.pop().run(lambda: True)
            worker._send_fixed.assert_called_once_with('欢迎New Friend加入群聊～')
        worker._v39_call_shadow_hermes.assert_not_called()

    def test_personality_custom_random_and_authority_unchanged(self):
        worker = self.worker()
        def execute(kind, prompt, **kwargs):
            return result(kind)
        with patch.object(llm_client, 'execute_auxiliary', side_effect=execute) as native:
            self.assertEqual(worker._v3109_optimize_personality('冷静'), VALUES['personality']['personality'])
            self.assertEqual(worker._v3110_random_personality(), ('随机人格', '克制而幽默'))
            event = {'local_id': 2, 'sender_username': 'wxid_intruder', 'bot_display_name': 'BOT',
                     'mentioned_wxids': ['wxid_bot']}
            worker._v3109_handle_personality_command(event, explicit=True, intent_text='@BOT /人格 冷静')
            worker._reply_dispatcher.jobs.pop().run(lambda: True)
            worker._v3109_persist_personality.assert_not_called()
            self.assertEqual(native.call_count, 2)
            native.side_effect = llm_client.LLMExecutionError('timeout')
            worker._v3109_handle_personality_command(dict(event, sender_username='wxid_owner'),
                                                   explicit=True, intent_text='@BOT /人格 冷静')
            worker._reply_dispatcher.jobs.pop().run(lambda: True)
            worker._v3109_persist_personality.assert_not_called()
            self.assertEqual(worker.group_cfg['personality'], 'PERSONALITY_KEEP')

    def test_proactive_format_retry_only_once_no_fallback(self):
        worker = self.worker()
        with patch.object(llm_client, 'execute_auxiliary', side_effect=[
            llm_client.LLMExecutionError('invalid_structured_output'), result('proactive')]) as execute:
            output = worker._v39_generate_shadow_decision('synthetic quiet chat')
            self.assertEqual(output[:3], ('SILENT', '', 'no_messages'))
            self.assertTrue(output[-1])
            self.assertEqual(execute.call_count, 2)
        with patch.object(llm_client, 'execute_auxiliary', side_effect=llm_client.LLMExecutionError('invalid_structured_output')) as execute:
            self.assertEqual(worker._v39_generate_shadow_decision('quiet')[0], 'INVALID')
            self.assertEqual(execute.call_count, 2)
        for failure in ('timeout', 'unavailable', 'transport'):
            with patch.object(llm_client, 'execute_auxiliary', side_effect=llm_client.LLMExecutionError(failure)) as execute:
                with self.assertRaises(llm_client.LLMExecutionError):
                    worker._v39_generate_shadow_decision('quiet')
                self.assertEqual(execute.call_count, 1)
        worker._v39_call_shadow_hermes.assert_not_called()
        worker._send_fixed.assert_not_called()

    def test_proactive_guard_and_same_backend_during_retry(self):
        worker = self.worker()
        for draft in ('REASON: leak', '[开始生图]\nnot a proactive action', '[开始调查]', core.VISION_AGENT_REACH_MARKER):
            with patch.object(llm_client, 'execute_auxiliary', return_value=result('proactive',
                 {'decision': 'SPEAK', 'draft': draft, 'reason': 'test'})):
                self.assertEqual(worker._v39_generate_shadow_decision('fixture')[0], 'INVALID')
        count = [0]
        def execute(*args, **kwargs):
            count[0] += 1
            if count[0] == 1:
                worker.group_cfg['personality'] = 'Changed while waiting'
                raise llm_client.LLMExecutionError('invalid_structured_output')
            return result('proactive')
        with patch.object(llm_client, 'execute_auxiliary', side_effect=execute):
            self.assertEqual(worker._v39_generate_shadow_decision('fixture')[0], 'SILENT')
        self.assertEqual(count[0], 2)
        worker._v39_call_shadow_hermes.assert_not_called()

    def test_new_group_uses_native_without_migration_flag(self):
        worker = self.worker()
        self.assertNotIn('ai_backend', worker.group_cfg)
        with patch.object(llm_client, 'execute_auxiliary', return_value=result('welcome')) as execute:
            self.assertEqual(worker._call_auxiliary_generation('welcome', 'fixture')[0], VALUES['welcome']['text'])
            self.assertEqual(execute.call_count, 1)
        worker._v39_call_shadow_hermes.assert_not_called()


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    if not outcome.wasSuccessful():
        raise SystemExit(1)
    print('V3132_LLM_AUXILIARY_SCHEMA_SCOPE_FAILURE_BUSINESS_GUARDS=PASS')
