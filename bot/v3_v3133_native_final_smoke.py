#!/usr/bin/env python3
"""Final native transport/late-send review regressions; no network, data or UI."""
import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch

from chatwarden import llm_client, core
from chatwarden.worker import GroupWorker, FormalReplyWork
from chatwarden.private_worker import PrivateWorker

CFG = {'name': 'final-test', 'chat_username': '12345@chatroom', 'personality': 'PERSONA_SENTINEL'}
SNAP = {'context': 'FROZEN_SENTINEL', 'current_sender': 'not-owner | Fixture',
        'memory_context': 'MEMORY_SENTINEL', 'style_bot_texts': [], 'history_context': ''}


class NativeFinalTests(unittest.TestCase):
    def setUp(self):
        patch.object(core, 'log').start()
        self.addCleanup(patch.stopall)

    def test_selected_media_transport_and_view_proof(self):
        samples = {'image/jpeg': b'\xff\xd8\xfffixture', 'image/png': b'\x89PNG\r\n\x1a\nfixture',
                   'image/gif': b'GIF89afixture', 'image/webp': b'RIFF0000WEBPfixture'}
        calls = []
        proven = [True]
        def send(request, timeout):
            payload = json.loads(request.data)
            calls.append(payload)
            self.assertEqual(timeout, 140)
            self.assertNotIn('session_key', payload)
            self.assertNotIn('path', payload)
            self.assertEqual(payload['task_kind'], 'vision')
            media = payload['media']
            self.assertEqual(base64.b64decode(media['data_base64']), samples[media['mime_type']])
            result = {'ok': True, 'request_id': payload['request_id'], 'task_kind': 'vision',
                      'text': 'IMAGE_RESULT', 'structured': None, 'session_turn': 1,
                      'image_viewed': proven[0]}
            output = io.BytesIO(json.dumps(result).encode()); output.status = 200
            return output
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'test.key'; key.write_text('fixture-' * 5)
            image = Path(directory) / 'selected'
            with patch.object(llm_client, 'KEY_FILE', key), \
                 patch.object(llm_client.urllib.request, 'build_opener', return_value=SimpleNamespace(open=send)):
                for raw in samples.values():
                    image.write_bytes(raw)
                    self.assertEqual(llm_client.execute_vision('inspect', str(image))['text'], 'IMAGE_RESULT')
                image.write_bytes(b'unsupported')
                with self.assertRaises(llm_client.LLMExecutionError):
                    llm_client.execute_vision('inspect', str(image))
                self.assertEqual(len(calls), 4)
                image.write_bytes(samples['image/png']); proven[0] = False
                with self.assertRaises(llm_client.LLMExecutionError):
                    llm_client.execute_vision('inspect', str(image))
        self.assertFalse(llm_client.busy())

    def test_gateway_image_not_viewed_kind_is_preserved(self):
        def send(request, timeout):
            body = io.BytesIO(json.dumps({
                'ok': False,
                'error': {'kind': 'image_not_viewed'},
            }).encode())
            raise urllib.error.HTTPError(
                request.full_url, 502, 'Bad Gateway', {}, body,
            )
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'test.key'; key.write_text('fixture-' * 5)
            image = Path(directory) / 'selected.png'
            image.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            with patch.object(llm_client, 'KEY_FILE', key), \
                 patch.object(llm_client.urllib.request, 'build_opener',
                              return_value=SimpleNamespace(open=send)):
                with self.assertRaises(llm_client.LLMExecutionError) as raised:
                    llm_client.execute_vision('inspect', str(image))
                self.assertEqual(raised.exception.kind, 'image_not_viewed')

    def test_proactive_auxiliary_media_transport_and_view_proof(self):
        calls = []
        proven = [True]

        def send(request, timeout):
            payload = json.loads(request.data)
            calls.append((payload, timeout))
            self.assertEqual(payload['task_kind'], 'proactive')
            self.assertNotIn('session_key', payload)
            media = payload['media']
            self.assertEqual(media['mime_type'], 'image/png')
            self.assertEqual(
                base64.b64decode(media['data_base64']),
                b'\x89PNG\r\n\x1a\nfixture',
            )
            result = {
                'ok': True,
                'request_id': payload['request_id'],
                'task_kind': 'proactive',
                'text': '',
                'structured': {
                    'decision': 'SILENT',
                    'draft': '',
                    'reason': 'fixture',
                },
                'session_turn': 1,
                'image_viewed': proven[0],
                'usage': {},
                'tool_count': 1,
                'web_used': False,
            }
            output = io.BytesIO(json.dumps(result).encode())
            output.status = 200
            return output

        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'test.key'
            key.write_text('fixture-' * 5)
            image = Path(directory) / 'selected.png'
            image.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            with patch.object(llm_client, 'KEY_FILE', key), \
                 patch.object(
                     llm_client.urllib.request,
                     'build_opener',
                     return_value=SimpleNamespace(open=send),
                 ):
                result = llm_client.execute_auxiliary(
                    'proactive',
                    'judge this active group context',
                    image_path=str(image),
                )
                self.assertTrue(result['image_viewed'])
                proven[0] = False
                with self.assertRaises(llm_client.LLMExecutionError) as raised:
                    llm_client.execute_auxiliary(
                        'proactive',
                        'judge again',
                        image_path=str(image),
                    )
                self.assertEqual(raised.exception.kind, 'invalid_response')
        self.assertEqual(len(calls), 2)

    def test_gateway_fallback_metadata_stays_on_exact_vision_request(self):
        calls = []
        def send(request, timeout):
            payload = json.loads(request.data)
            calls.append((payload, timeout))
            result = {
                'ok': True, 'request_id': payload['request_id'],
                'task_kind': payload['task_kind'], 'text': 'RECOVERED',
                'structured': None, 'session_turn': 1, 'image_viewed': True,
                'fallback_used': True, 'fallback_provider': 'backup-provider',
                'model': 'backup/model-b',
                'primary_error_kind': 'timeout',
            }
            output = io.BytesIO(json.dumps(result).encode()); output.status = 200
            return output
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'test.key'; key.write_text('fixture-' * 5)
            image = Path(directory) / 'selected.png'
            image.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            with patch.object(llm_client, 'KEY_FILE', key), \
                 patch.object(llm_client.urllib.request, 'build_opener',
                              return_value=SimpleNamespace(open=send)):
                self.assertEqual(
                    llm_client.execute_vision('inspect', str(image))['text'],
                    'RECOVERED',
                )
        self.assertEqual(len(calls), 1)
        payload, timeout = calls[0]
        self.assertEqual(payload['task_kind'], 'vision')
        self.assertEqual(timeout, 140)
        self.assertNotIn('session_key', payload)
        self.assertEqual(payload['media']['mime_type'], 'image/png')

    def test_image_not_viewed_and_upstream_are_not_retried_by_bot(self):
        recovered = {
            'text': 'VISION_RECOVERED', 'request_id': 'vision-fallback-ok',
            'session_turn': 1, 'duration_ms': 1, 'tool_count': 1,
            'web_used': False, 'image_viewed': True,
            'fallback_used': True, 'fallback_provider': 'backup-provider',
            'model': 'backup/model-b',
            'primary_error_kind': 'image_not_viewed',
        }
        with patch.object(
            llm_client, 'execute_vision',
            return_value=recovered,
        ) as native:
            reply = core.call_vision_reply(
                CFG, 'look', 'wxid_fixture', {}, '/selected.png',
                context_snapshot=SNAP,
            )
            self.assertEqual(reply, 'VISION_RECOVERED')
            self.assertEqual(native.call_count, 1)

        for failure in ('image_not_viewed', 'upstream_failed', 'timeout', 'unauthorized'):
            with self.subTest(failure=failure), patch.object(
                llm_client, 'execute_vision',
                side_effect=llm_client.LLMExecutionError(failure),
            ) as native:
                with self.assertRaises(core.VisionGenerationError):
                    core.call_vision_reply(
                        CFG, 'look', 'wxid_fixture', {}, '/selected.png',
                        context_snapshot=SNAP,
                    )
                self.assertEqual(native.call_count, 1)

    def test_vision_error_is_bounded_and_uses_fixed_group_notice(self):
        with patch.object(llm_client, 'execute_vision', side_effect=llm_client.LLMExecutionError('timeout')) as native, \
             patch.object(llm_client, 'execute_formal', side_effect=AssertionError('must not fall back')):
            with self.assertRaises(core.VisionGenerationError):
                core.call_formal_reply(CFG, 'look', 'wxid_fixture', {}, image_path='/selected.png', context_snapshot=SNAP)
            self.assertEqual(native.call_count, 1)
        worker = GroupWorker.__new__(GroupWorker)
        worker.chat_name = CFG['name']; worker.stop_event = threading.Event()
        worker._reply_state_lock = threading.RLock(); worker._reply_generation = 0
        worker._v392_send_lock = threading.Lock(); worker._v39_note_bot_sent = lambda: None
        worker._v380_provider_question = lambda event: event['text']
        worker._v396_upgrade_vision_path = lambda work, path: path
        event = {'local_id': 7, 'timestamp': 0, 'text': 'inspect', '_v36_trigger_kind': 'explicit'}
        work = FormalReplyWork(generation=0, event=event, image_path='/selected.png', provider_prompt='inspect',
                               sender='wxid_fixture', display='Fixture', legacy_sender='other', group_cfg=CFG,
                               profiles={}, context_snapshot=SNAP, queued_at=time.monotonic())
        with patch.object(core, 'call_formal_reply', side_effect=core.VisionGenerationError('test')), \
             patch.object(core, 'focus_and_send', return_value=.01) as send, patch.object(core, 'save_message'):
            worker._run_reply_job(work)
            self.assertEqual(send.call_count, 1)
            self.assertIn('识图', send.call_args.args[1])

    def test_research_frozen_context_full_output_no_formal_session(self):
        full = 'RESEARCH_RESULT_' * 1000
        with patch.object(llm_client, 'execute_research', return_value={
             'text': full, 'request_id': 'research-test', 'duration_ms': 1, 'tool_count': 2, 'web_used': True}) as native, \
             patch.object(llm_client, 'execute_formal', side_effect=AssertionError('formal cache')), \
             patch.object(core, 'recent_context', side_effect=AssertionError('future rows')):
            self.assertEqual(core.call_agent_reach(CFG, 'Current research question', 'wxid_fixture', {},
                                                  context_snapshot=SNAP), full)
            prompt = native.call_args.args[0]
            for text in ('FROZEN_SENTINEL', 'MEMORY_SENTINEL', 'PERSONA_SENTINEL', '来源', '中国相关调查的来源多样性'):
                self.assertIn(text, prompt)
            native.side_effect = llm_client.LLMExecutionError('timeout')
            with self.assertRaises(llm_client.LLMExecutionError):
                core.call_agent_reach(CFG, 'question', 'wxid_fixture', {}, context_snapshot=SNAP)
            self.assertEqual(native.call_count, 2)

    def test_private_research_stop_drops_result_and_does_not_save(self):
        worker = PrivateWorker.__new__(PrivateWorker)
        peer = 'wxid_private_fixture'
        worker.peer_wxid = peer; worker.chat_name = 'Fixture'; worker.context_name = 'private:' + peer
        worker.private_cfg = {'name': worker.context_name, 'chat_username': peer}
        worker.profiles = {}; worker.stop_event = threading.Event()
        worker._v321_app_detail = lambda event: (0, '')
        event = {'local_id': 8, 'local_type': 1, 'sender_username': peer, 'text': '深入调查问题', 'timestamp': 0}
        def research(*args, **kwargs):
            self.assertEqual(kwargs['context_snapshot'], SNAP)
            worker.stop_event.set()
            return 'LATE_RESEARCH_MUST_NOT_SEND'
        with patch.object(core, 'freeze_llm_context_bounded', return_value=SNAP), \
             patch.object(core, 'call_formal_reply', return_value=core.AGENT_REACH_MARKER), \
             patch.object(core, 'call_agent_reach', side_effect=research), \
             patch.object(core, '_private_focus_and_send_unlocked', return_value=.01) as send, \
             patch.object(core, 'save_message') as save:
            worker._handle(event)
            self.assertEqual(send.call_count, 1)  # Only the pre-research acknowledgement.
            self.assertNotIn('LATE_RESEARCH', send.call_args.args[1])
            self.assertFalse(any(c.args[1] == 'bot' for c in save.call_args_list))

    def test_native_purge_and_new_group_default(self):
        for name in ('call_hermes', 'call_llm', 'read_hermes_key', 'HERMES_ENDPOINT',
                     '_v35_session_cfg_for_call', '_BOT2_FAST_POLICY', 'private_formal_backend', 'use_llm_auxiliary'):
            self.assertFalse(hasattr(core, name), name)
        self.assertFalse(hasattr(GroupWorker, '_v39_call_shadow_hermes'))
        self.assertTrue(callable(core.backend_busy))
        with patch.object(llm_client, 'execute_formal', return_value={
             'text': 'NATIVE', 'request_id': 'new-group', 'session_turn': 1, 'duration_ms': 1,
             'tool_count': 0, 'web_used': False}) as native:
            self.assertEqual(core.call_formal_reply(CFG, 'hello', 'wxid_fixture', {}, context_snapshot=SNAP), 'NATIVE')
            self.assertEqual(native.call_count, 1)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__)))
    if not result.wasSuccessful():
        raise SystemExit(1)
    print('V3133_NATIVE_VISION_RESEARCH_LATE_SEND_PURGE=PASS')
