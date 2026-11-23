#!/usr/bin/env python3
"""v3.164 media-route Shadow contract; no cloud, DB writes or WeChat sends."""
import json
import os
from unittest.mock import patch

os.environ['CW_DECISION_SHADOW'] = '0'

from chatwarden import decision_client as dc
from chatwarden.worker import GroupWorker
from chatwarden.private_worker import PrivateWorker


def response_for(payload, probabilities):
    return {
        'ok': True,
        'request_id': payload['request_id'],
        'decision_kind': payload['decision_kind'],
        'decision_version': 3,
        'mode': payload['mode'],
        'model': 'decision-fixture',
        'answers': {
            name: {'type': 'noul', 'probability': value}
            for name, value in probabilities.items()
        },
    }


def payload_contract():
    snapshot = dc.MediaRouteSnapshot(
        current_message='@LE_MOTURE 把这张图改成红色外套',
        recent_context=(),
        aliases=(('LE_MOTURE', 'BOT'), ('测试群', 'THIS_CHAT')),
        chat_identity='123456789@chatroom',
        sender_identity='wxid_fixture',
        created_at=1.0,
        visual_available=True,
        visual_source='quoted',
        explicit_imagegen_prefix=False,
        explicit_address=True,
    )
    payload = dc.payload_for_media_route_v3(
        snapshot, {'legacy_route': 'image_edit'}, {'local_id': 164}, 'k' * 48,
    )
    assert payload['decision_kind'] == 'media_route_v3'
    assert payload['telemetry']['actual_outcome'] == {'legacy_route': 'image_edit'}
    state = payload['state']
    assert state['media_context'] == {
        'visual_available': True,
        'visual_source': 'quoted',
        'explicit_imagegen_prefix': False,
        'explicit_address': True,
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    assert '123456789@chatroom' not in encoded
    assert 'wxid_fixture' not in encoded
    assert '/tmp/' not in encoded
    response = response_for(payload, {
        'wants_generate_new_image': .02,
        'wants_edit_existing_image': .97,
        'wants_visual_understanding': .08,
        'wants_text_only': .03,
    })
    assert dc.validate_response(response, payload) is response
    print('V3164_MEDIA_ROUTE_PAYLOAD_PRIVACY=PASS')
    print('V3164_MEDIA_ROUTE_MULTI_QUESTION_CONTRACT=PASS')
    print('V3166_MEDIA_ROUTE_V3_TYPED_RESPONSE=PASS')


def group_hook_contract():
    worker = GroupWorker.__new__(GroupWorker)
    worker.chat_name = '测试群'
    worker.group_cfg = {'name': '测试群', 'chat_username': '123456789@chatroom'}
    worker.bot_wxid = 'wxid_bot_fixture'
    event = {
        'local_id': 1641,
        'local_type': 1,
        'text': '@BOT 生成一张鹈鹕骑自行车的图',
        'sender_username': 'wxid_sender_fixture',
        'sender_display': '发送者',
        'bot_display_name': 'BOT',
        '_v36_trigger_kind': 'explicit',
    }
    captured = []
    with patch.object(dc, 'enabled', return_value=True), \
         patch.object(dc, 'mode', return_value='shadow'), \
         patch.object(dc, 'submit_media_route', side_effect=lambda s, o, m: captured.append((s, o, m)) or True):
        assert worker._v3163_media_route_shadow_observe(
            event, 'imagegen', visual_available=False,
            visual_source='none', explicit_imagegen_prefix=True,
        ) is True
    assert len(captured) == 1
    snapshot, outcome, metadata = captured[0]
    assert snapshot.current_message == event['text']
    assert snapshot.explicit_imagegen_prefix is True
    assert snapshot.explicit_address is True
    assert outcome == {'legacy_route': 'imagegen'}
    assert metadata == {'local_id': 1641}
    print('V3164_GROUP_MEDIA_ROUTE_SHADOW=PASS')


def private_hook_contract():
    worker = PrivateWorker.__new__(PrivateWorker)
    worker.chat_name = '联系人'
    worker.peer_wxid = 'wxid_private_fixture'
    worker.bot_wxid = 'wxid_bot_fixture'
    event = {
        'local_id': 1642,
        'local_type': 1,
        'text': '这张图是什么品牌？',
        'sender_username': 'wxid_private_fixture',
        'bot_display_name': '',
    }
    captured = []
    with patch.object(dc, 'enabled', return_value=True), \
         patch.object(dc, 'mode', return_value='shadow'), \
         patch.object(dc, 'submit_media_route', side_effect=lambda s, o, m: captured.append((s, o, m)) or True):
        assert worker._v3163_media_route_shadow_observe(
            event, 'vision', visual_available=True, visual_source='quoted',
        ) is True
    snapshot, outcome, metadata = captured[0]
    assert snapshot.visual_available is True
    assert snapshot.visual_source == 'quoted'
    assert snapshot.explicit_address is True
    assert outcome == {'legacy_route': 'vision'}
    assert metadata == {'local_id': 1642}
    print('V3164_PRIVATE_MEDIA_ROUTE_SHADOW=PASS')


def queue_drop_is_observational_only():
    worker = GroupWorker.__new__(GroupWorker)
    worker.chat_name = '测试群'
    worker.group_cfg = {'name': '测试群', 'chat_username': '123456789@chatroom'}
    worker.bot_wxid = 'wxid_bot_fixture'
    event = {
        'local_id': 1643, 'local_type': 1, 'text': '普通文本问题',
        'sender_username': 'wxid_sender_fixture', 'sender_display': '发送者',
        'bot_display_name': 'BOT', '_v36_trigger_kind': 'explicit',
    }
    with patch.object(dc, 'enabled', return_value=True), \
         patch.object(dc, 'mode', return_value='shadow'), \
         patch.object(dc, 'submit_media_route', return_value=False):
        assert worker._v3163_media_route_shadow_observe(event, 'text') is False
    print('V3164_MEDIA_ROUTE_SHADOW_DROP_NO_ACTION=PASS')


def main():
    payload_contract()
    group_hook_contract()
    private_hook_contract()
    queue_drop_is_observational_only()


if __name__ == '__main__':
    main()
