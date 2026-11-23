#!/usr/bin/env python3
"""v3.170 owner private settings; temp groups.json only, no cloud or sends."""
import json
import os
import tempfile
from pathlib import Path

os.environ['CW_DECISION_SHADOW'] = '0'
# This smoke exercises the real private/mute gates against a temp config.
os.environ.pop('CW_IGNORE_LIVE_OWNER_SETTINGS', None)

from chatwarden import core
from chatwarden import owner_settings as osm
from chatwarden.private_worker import PrivateWorker
from chatwarden.worker import GroupWorker


OWNER = 'wxid_fixture_owner'
GROUPS = {
    'version': 1,
    'groups': [
        {'name': 'A群', 'enabled': True, 'session_key': 'wechat:group:a',
         'chat_username': '111@chatroom', 'personality': '原人格A',
         'member_labels': {}},
        {'name': 'B群', 'enabled': True, 'session_key': 'wechat:group:b',
         'chat_username': '222@chatroom', 'personality': '原人格B',
         'default_personality': '默认B', 'member_labels': {}},
    ],
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeGroupWorker:
    def __init__(self):
        self.optimized = []

    def _v3109_optimize_personality(self, raw):
        self.optimized.append(raw)
        return '整理后：' + raw

    def _v3110_random_personality(self):
        return '随机人格全文', '一个随机的人格'


def reset_cache():
    core._V3170_SETTINGS_CACHE['mtime'] = None
    core._V3170_SETTINGS_CACHE['raw'] = None


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def group(path, name):
    return next(g for g in read(path)['groups'] if g['name'] == name)


def main():
    original_file = core.GROUPS_FILE
    original_log = core.log
    core.log = lambda *args, **kwargs: None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'groups.json'
        path.write_text(json.dumps(GROUPS, ensure_ascii=False), encoding='utf-8')
        os.chmod(path, 0o644)
        core.GROUPS_FILE = path
        reset_cache()
        try:
            run(path)
        finally:
            core.GROUPS_FILE = original_file
            core.log = original_log
            reset_cache()


def run(path):
    sent = []
    clock = Clock()
    fake = FakeGroupWorker()
    settings = osm.OwnerSettings(
        send=sent.append,
        group_worker_lookup=lambda name: fake,
        clock=clock,
    )

    # Non-commands and a bare 确认 without a pending action are not consumed.
    assert settings.handle('今天吃什么') is False
    assert settings.handle('确认') is False
    assert settings.handle('/设置状态x') is False
    assert settings.handle('/设置 状态') is True
    assert 'A群' in sent[-1] and 'B群' in sent[-1] and '私聊（除主人外）：开' in sent[-1]
    print('V3170_OWNER_SETTINGS_STATUS_AND_NONCOMMAND=PASS')

    # Direct toggles and cooldowns are written atomically and validated.
    settings.handle('/设置 1 主动发言 关')
    assert group(path, 'A群')['proactive_enabled'] is False
    settings.handle('/设置 1 欢迎 关')
    assert group(path, 'A群')['welcome_enabled'] is False
    settings.handle('/设置 1 主动冷却 40分钟')
    assert group(path, 'A群')['proactive_any_bot_cooldown_sec'] == 2400.0
    settings.handle('/设置 1 主动间隔 1小时')
    assert group(path, 'A群')['proactive_send_cooldown_sec'] == 3600.0
    settings.handle('/设置 1 主动冷却 默认')
    assert 'proactive_any_bot_cooldown_sec' not in group(path, 'A群')
    before = path.read_text(encoding='utf-8')
    settings.handle('/设置 1 主动冷却 99999999')
    settings.handle('/设置 9 主动发言 关')
    settings.handle('/设置 1 不存在 关')
    assert path.read_text(encoding='utf-8') == before
    assert oct(path.stat().st_mode & 0o777) == '0o644'
    assert core.load_groups_config()[0]['proactive_enabled'] is False
    print('V3170_OWNER_SETTINGS_TOGGLES_COOLDOWNS_VALIDATED=PASS')

    # Muting needs confirmation, expires, and is identity-bound.
    settings.handle('/设置 1 总开关 关')
    assert 'muted' not in group(path, 'A群') and '待确认' in sent[-1]
    clock.now += osm.CONFIRM_TTL_SECONDS + 1
    assert settings.handle('确认') is False
    assert 'muted' not in group(path, 'A群')
    settings.handle('/设置 1 总开关 关')
    assert settings.handle('确认') is True
    assert group(path, 'A群')['muted'] is True
    reset_cache()
    assert core.group_muted('A群') is True and core.group_muted('B群') is False
    cfg = core.load_groups_config()[0]
    assert GroupWorker.proactive_enabled_for_group(cfg) is False
    assert GroupWorker.newcomer_welcome_enabled_for_group(cfg) is False
    for sender in (core._focus_and_send_unlocked, core._focus_and_send_image_unlocked):
        try:
            sender('A群', 'x')
        except core.GroupMutedError:
            pass
        else:
            raise AssertionError('muted group send was not refused')
    settings.handle('/设置 1 总开关 开')
    assert 'muted' not in group(path, 'A群')

    settings.handle('/设置 1 总开关 关')
    raw = read(path)
    raw['groups'][0]['chat_username'] = '999@chatroom'
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
    settings.handle('确认')
    assert 'muted' not in group(path, 'A群') and '没有改动' in sent[-1]
    raw['groups'][0]['chat_username'] = '111@chatroom'
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
    print('V3170_MUTE_CONFIRM_TTL_IDENTITY_AND_SEND_GUARD=PASS')

    # Personality: custom text keeps newlines, previews, cancel, confirm.
    settings.handle('/设置 2 人格 温柔一点\n偶尔吐槽')
    assert fake.optimized[-1] == '温柔一点\n偶尔吐槽', fake.optimized
    assert '整理后：温柔一点\n偶尔吐槽' in sent[-1]
    settings.handle('取消')
    assert group(path, 'B群')['personality'] == '原人格B'
    settings.handle('/设置 2 人格 温柔一点')
    settings.handle('确认')
    assert group(path, 'B群')['personality'] == '整理后：温柔一点'
    assert group(path, 'B群')['default_personality'] == '默认B'
    settings.handle('/设置 2 人格 默认')
    settings.handle('确认')
    assert group(path, 'B群')['personality'] == '默认B'
    settings.handle('/设置 2 人格 随机')
    settings.handle('确认')
    assert group(path, 'B群')['personality'] == '随机人格全文'
    print('V3170_PERSONALITY_PREVIEW_CONFIRM_DEFAULT_RANDOM=PASS')

    # Private non-owner switch.
    settings.handle('/设置 私聊 关')
    reset_cache()
    assert core.private_nonowner_enabled() is False
    assert read(path)['private_nonowner_enabled'] is False
    core.load_groups_config()

    original_owner = core.owner_wxid
    original_send = core.private_focus_and_send
    private_sent = []
    core.owner_wxid = lambda default='': OWNER
    core.private_focus_and_send = lambda chat, text: private_sent.append(text) or 0.0
    try:
        stranger = PrivateWorker.__new__(PrivateWorker)
        stranger.peer_wxid = 'wxid_fixture_stranger'
        stranger.chat_name = 'stranger'
        stranger._handle({
            'sender_username': 'wxid_fixture_stranger',
            'local_type': 1, 'text': '/设置 状态', 'raw_content': '',
        })
        assert private_sent == [], private_sent

        owner = PrivateWorker.__new__(PrivateWorker)
        owner.peer_wxid = OWNER
        owner.chat_name = 'owner'
        owner.group_worker_lookup = lambda name: fake
        owner._handle({
            'sender_username': OWNER, 'local_type': 1,
            'text': '/设置 私聊 开', 'raw_content': '',
        })
        assert private_sent and '已开启' in private_sent[-1], private_sent
        reset_cache()
        assert core.private_nonowner_enabled() is True
    finally:
        core.owner_wxid = original_owner
        core.private_focus_and_send = original_send
    print('V3170_PRIVATE_NONOWNER_GATE_AND_OWNER_ONLY=PASS')

    # v3.177 muted group: nothing is persisted or routed (runtime also stops reading it).
    saved = []
    original_save = core.save_message
    core.save_message = lambda chat, sender, text: saved.append((chat, sender, text))
    try:
        worker = GroupWorker.__new__(GroupWorker)
        worker.chat_name = 'A群'
        worker.group_cfg = {'name': 'A群', 'muted': True}
        worker.bot_wxid = 'wxid_fixture_bot'
        worker._expire = lambda: None

        def must_not_route(*args, **kwargs):
            raise AssertionError('muted group routed a message')

        worker._handle_text = must_not_route
        worker._handle_image = must_not_route
        worker._handle_voice = must_not_route
        worker._v335_defer_if_blocked = must_not_route
        worker._v3100_handle_system_message = must_not_route
        for local_type, text in (
            (1, '@bot 你好'), (3, ''), (34, ''), (47, ''), (10000, '"甲"邀请"乙"加入了群聊'),
        ):
            worker._handle({
                'sender_username': 'wxid_fixture_member', 'text': text,
                'local_type': local_type, 'msg_type': '',
                'chat_display_name': '新标题',
            })
        assert saved == [], saved
    finally:
        core.save_message = original_save
    print('V3170_MUTED_GROUP_NOT_PROCESSED=PASS')


if __name__ == '__main__':
    main()
