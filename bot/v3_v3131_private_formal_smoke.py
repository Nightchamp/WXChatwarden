#!/usr/bin/env python3
"""Phase-3 regressions: synthetic peers only; no provider, production DB or UI."""
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from chatwarden import llm_client, core, private_worker
from chatwarden.private_worker import PrivateWorker

PEER = 'wxid_phase3_fixture'
CFG = {'name': 'private:' + PEER, 'chat_username': PEER,
       'session_key': 'unused-by-native',
       'personality': 'PRIVATE_PERSONA', 'member_labels': {PEER: 'Same Name'}}
SNAP = {'context': 'PRIVATE_RECENT', 'current_sender': 'uid:12345678 | not-owner | Same Name',
        'style_bot_texts': [], 'memory_context': 'PRIVATE_MEMORY', 'history_context': ''}


def result(text='PRIVATE_OK'):
    return {'ok': True, 'text': text, 'structured': None, 'request_id': 'fixture',
            'session_turn': 1, 'duration_ms': 1, 'tool_count': 0, 'web_used': False}


class PrivateClientTests(unittest.TestCase):
    def test_exact_peer_session_kind_and_response_binding(self):
        calls = []
        wrong_kind = [False]

        def send(request, timeout):
            self.assertTrue(llm_client.busy())
            self.assertEqual(timeout, 130)
            data = json.loads(request.data)
            calls.append(data)
            answer = dict(result(), request_id=data['request_id'],
                          task_kind='formal' if wrong_kind[0] else data['task_kind'])
            response = io.BytesIO(json.dumps(answer).encode())
            response.status = 200
            return response

        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'synthetic.key'
            key.write_text('synthetic-' * 5)
            with patch.object(llm_client, 'KEY_FILE', key), \
                 patch.object(llm_client.urllib.request, 'build_opener',
                              return_value=SimpleNamespace(open=send)):
                llm_client.execute_private(CFG, 'one')
                llm_client.execute_private(dict(CFG, member_labels={PEER: 'Renamed'}), 'two')
                llm_client.execute_private(dict(CFG, name='private:wxid_other', chat_username='wxid_other'), 'other')
                llm_client.execute_formal({'name': 'Same Name', 'chat_username': '12345@chatroom'}, 'group')
                self.assertEqual([x['task_kind'] for x in calls], ['private'] * 3 + ['formal'])
                self.assertEqual(calls[0]['session_key'], calls[1]['session_key'])
                self.assertEqual(len({x['session_key'] for x in calls}), 3)
                self.assertEqual(len({x['request_id'] for x in calls}), 4)
                for bad in ({'name': 'Same Name'}, dict(CFG, name='private:wxid_other'),
                            dict(CFG, chat_username='12345@chatroom'), dict(CFG, chat_username='wxid_a/../../b')):
                    with self.assertRaises(llm_client.LLMExecutionError):
                        llm_client.execute_private(bad, 'deny')
                self.assertEqual(len(calls), 4)
                wrong_kind[0] = True
                with self.assertRaises(llm_client.LLMExecutionError):
                    llm_client.execute_private(CFG, 'wrong-response-kind')
        self.assertFalse(llm_client.busy())


class PrivateFormalTests(unittest.TestCase):
    def setUp(self):
        patch.object(core, 'log').start()
        self.addCleanup(patch.stopall)

    def test_frozen_context_persona_private_prompt_and_failure(self):
        with patch.object(llm_client, 'execute_private', return_value=result()) as execute, \
             patch.object(llm_client, 'execute_formal', side_effect=AssertionError('group lane')), \
             patch.object(core, 'recent_context', side_effect=AssertionError('unfrozen read')):
            answer = core.call_formal_reply(CFG, 'CURRENT_PRIVATE_QUESTION', PEER, {},
                                           context_snapshot=SNAP, task_kind='private')
            self.assertEqual(answer, 'PRIVATE_OK')
            prompt = execute.call_args.args[1]
            for marker in ('PRIVATE_PERSONA', 'PRIVATE_RECENT', 'PRIVATE_MEMORY',
                           'CURRENT_PRIVATE_QUESTION', '微信一对一私聊', '最近私聊上下文'):
                self.assertIn(marker, prompt)
            self.assertNotIn('这是微信群', prompt)
            self.assertNotIn('普通 bot2 快速响应规则', prompt)
            execute.side_effect = llm_client.LLMExecutionError('timeout')
            reply = core.call_formal_reply(CFG, 'again', PEER, {}, context_snapshot=SNAP, task_kind='private')
            self.assertIn('稍后', reply)
            self.assertNotIn('timeout', reply)
            self.assertEqual(execute.call_count, 2)
            with self.assertRaises(ValueError):
                core.call_formal_reply(CFG, 'bad', 'wxid_other', {}, context_snapshot=SNAP, task_kind='private')
            self.assertEqual(execute.call_count, 2)

    def test_native_vision_and_business_markers_remain(self):
        with patch.object(core, 'call_vision_reply', return_value='VISION') as vision, \
             patch.object(llm_client, 'execute_private', side_effect=AssertionError('unexpected gateway')):
            self.assertEqual(core.call_formal_reply(CFG, 'look', PEER, {}, image_path='/tmp/fixture.png',
                                                   task_kind='private'), 'VISION')
            self.assertEqual(core.call_formal_reply(CFG, '深入调查这个问题', PEER, {},
                                                   task_kind='private'), core.AGENT_REACH_MARKER)
            self.assertEqual(vision.call_count, 1)

    def test_private_identity_survives_display_refresh_without_selector(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'groups.json'
            raw = {'groups': [{'name': 'new-group'}]}
            with patch.object(core, 'GROUPS_FILE', path):
                path.write_text(json.dumps(raw))
                with patch.object(private_worker, 'EmoticonPipeline'), \
                     patch.object(threading.Thread, 'start'):
                    worker = PrivateWorker({'chat_name': 'Same Name', 'chat_username': PEER}, None, {}, 'wxid_bot')
                self.assertEqual(worker.private_cfg['chat_username'], PEER)
                worker.update(resolved={'chat_name': 'Renamed', 'chat_username': PEER})
                self.assertNotIn('ai_backend', worker.private_cfg)
                self.assertEqual(worker.private_cfg['name'], 'private:' + PEER)
                self.assertEqual(worker.private_cfg['member_labels'], {PEER: 'Renamed'})
                self.assertNotIn('ai_backend', core.load_groups_config()[0])
                with self.assertRaises(ValueError):
                    worker.update(resolved={'chat_name': 'Same Name', 'chat_username': 'wxid_other'})

    def worker(self):
        worker = PrivateWorker.__new__(PrivateWorker)
        worker.peer_wxid = PEER
        worker.chat_name = 'Same Name'
        worker.context_name = CFG['name']
        worker.private_cfg = dict(CFG)
        worker.profiles = {}
        worker.stop_event = threading.Event()
        worker._v321_app_detail = lambda event: (0, '')
        return worker

    def test_actual_private_handler_frozen_order_complete_save_and_stop(self):
        worker = self.worker()
        event = {'local_id': 1, 'local_type': 1, 'sender_username': PEER, 'text': 'hello', 'timestamp': 0}
        full = 'Z' * 3400
        with patch.object(llm_client, 'execute_private', return_value=result(full)) as execute, \
             patch.object(core, 'freeze_llm_context_bounded', return_value=SNAP) as freeze, \
             patch.object(core, '_private_focus_and_send_unlocked', return_value=.01) as send, \
             patch.object(core, 'save_message') as save:
            worker._handle(event)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(freeze.call_args.args[0]['name'], CFG['name'])
            self.assertEqual(''.join(c.args[1] for c in send.call_args_list), full)
            self.assertTrue(all(c.args[0] == worker.chat_name for c in send.call_args_list))
            self.assertEqual(save.call_args.args, (CFG['name'], 'bot', full))
            self.assertIn('PRIVATE_RECENT', execute.call_args.args[1])
            send.reset_mock(); save.reset_mock()
            worker._handle(dict(event, sender_username='wxid_other'))
            self.assertEqual(execute.call_count, 1)
            send.assert_not_called(); save.assert_not_called()

            def late(*args, **kwargs):
                worker.stop_event.set()
                return result('DO_NOT_SEND')

            execute.side_effect = late
            worker._handle(dict(event, local_id=2))
            send.assert_not_called()
            self.assertFalse(any(c.args[1] == 'bot' for c in save.call_args_list))

    def test_stale_checked_inside_ui_lock_for_each_chunk(self):
        live = [True]
        def send(*args):
            live[0] = False
            return .01
        with patch.object(core, '_private_focus_and_send_unlocked', side_effect=send) as boundary:
            self.assertIsNone(core.private_focus_and_send_many('Same Name', ['a', 'b'], is_current=lambda: live[0]))
            self.assertEqual(boundary.call_count, 1)
            boundary.reset_mock()
            self.assertIsNone(core.private_focus_and_send_many('Same Name', ['late'], is_current=lambda: False))
            boundary.assert_not_called()


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    if not outcome.wasSuccessful():
        raise SystemExit(1)
    print('V3131_PRIVATE_FORMAL_IDENTITY_CONTEXT_NO_FALLBACK_STOP=PASS')
