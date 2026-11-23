#!/usr/bin/env python3
"""v3.178 owner natural-language /设置; temp groups.json + fake translator only."""
import json
import os
import tempfile
from pathlib import Path

os.environ['CW_DECISION_SHADOW'] = '0'
os.environ.pop('CW_IGNORE_LIVE_OWNER_SETTINGS', None)

from chatwarden import llm_client, core
from chatwarden import owner_settings as osm


GROUPS = {
    'version': 1,
    'groups': [
        {'name': 'A群', 'enabled': True, 'session_key': 'wechat:group:a',
         'chat_username': '111@chatroom', 'personality': '原人格A',
         'default_personality': '默认A', 'member_labels': {}},
        {'name': 'B群', 'enabled': True, 'session_key': 'wechat:group:b',
         'chat_username': '222@chatroom', 'personality': '原人格B', 'member_labels': {}},
    ],
}


class Translator:
    def __init__(self):
        self.calls = []
        self.answer = {'commands': '', 'reply': '好的'}
        self.fail = False

    def __call__(self, kind, prompt, *, explicit=False, image_path=None):
        self.calls.append((kind, prompt, explicit))
        if self.fail:
            raise llm_client.LLMExecutionError('transport')
        return {'structured': dict(self.answer)}


def reset_cache():
    core._V3170_SETTINGS_CACHE['mtime'] = None
    core._V3170_SETTINGS_CACHE['raw'] = None


def group(path, name):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return next(g for g in data['groups'] if g['name'] == name)


def run(path, tr):
    sent = []
    s = osm.OwnerSettings(send=sent.append, group_worker_lookup=lambda name: None)

    # Routing: parsable commands stay deterministic; anything else is NL.
    for text in ('/设置', '/设置 状态', '/设置 1', '/设置 1 主动发言 关', '/设置 1 主动冷却 40分钟',
                 '/设置 模型', '/设置 模型 列表 claude', '/设置 1 人格 傲娇', '/设置 99'):
        assert osm._is_structured(osm._tokens(text)), text
    for text in ('/设置 帮我把A群主动发言关了', '/设置 1 主动发言 也许', '/设置 1 总开关 帮我关掉',
                 '/设置 模型 帮我换成sonnet', '/设置 私聊 别回陌生人了', '/设置 1 不存在 关'):
        assert not osm._is_structured(osm._tokens(text)), text
    assert '/设置 <自然语言>' in osm.HELP
    s.handle('/设置 1 主动发言 关')
    assert tr.calls == [] and group(path, 'A群')['proactive_enabled'] is False
    print('V3178_NL_ROUTING_KEEPS_DETERMINISTIC_COMMANDS=PASS')

    # A modifying translation is listed and needs one 确认; nothing changes before it.
    tr.answer = {'commands': '/设置 1 主动发言 开\n/设置 2 欢迎 关', 'reply': '打开A群主动发言，关闭B群欢迎'}
    s.handle('/设置 A群主动发言打开，B群别欢迎新人了')
    kind, prompt, explicit = tr.calls[-1]
    assert kind == 'settings_intent' and explicit is True
    assert 'A群' in prompt and 'B群' in prompt and 'B群别欢迎新人了' in prompt and '/设置 <群号> 总开关' in prompt
    assert '待确认' in sent[-1] and '1. /设置 1 主动发言 开' in sent[-1], sent[-1]
    assert group(path, 'A群')['proactive_enabled'] is False and 'welcome_enabled' not in group(path, 'B群')
    s.handle('确认')
    assert 'proactive_enabled' not in group(path, 'A群') or group(path, 'A群')['proactive_enabled'] is True
    assert group(path, 'B群')['welcome_enabled'] is False
    print('V3178_NL_MODIFY_REQUIRES_CONFIRM_THEN_RUNS_BATCH=PASS')

    # Read-only translations run immediately without a pending action.
    tr.answer = {'commands': '/设置 状态', 'reply': '看看状态'}
    s.handle('/设置 现在各个群什么情况')
    assert s._live_pending() is None and '私聊（除主人外）' in sent[-1], sent[-2:]
    print('V3178_NL_READ_ONLY_RUNS_DIRECTLY=PASS')

    # A command that normally needs its own confirmation is covered by the
    # batch confirmation; a personality change still stops for its preview.
    tr.answer = {'commands': '/设置 2 总开关 关\n/设置 2 主动发言 关', 'reply': '关掉B群'}
    s.handle('/设置 B群先别处理了')
    s.handle('确认')
    assert group(path, 'B群')['muted'] is True and group(path, 'B群')['proactive_enabled'] is False
    assert s._live_pending() is None
    tr.answer = {'commands': '/设置 1 人格 默认\n/设置 1 欢迎 关', 'reply': '恢复A群默认人格并关欢迎'}
    s.handle('/设置 A群人格恢复默认，顺便关欢迎')
    s.handle('确认')
    pending = s._live_pending()
    assert pending is not None and pending['kind'] == 'personality_default', pending
    assert '默认A' in sent[-2] and '没执行' in sent[-1] and '/设置 1 欢迎 关' in sent[-1], sent[-2:]
    assert 'welcome_enabled' not in group(path, 'A群')
    s.handle('取消')
    assert group(path, 'A群')['personality'] == '原人格A'
    print('V3178_NL_BATCH_AUTOCONFIRM_EXCEPT_PERSONALITY_PREVIEW=PASS')

    # Invalid translations never execute anything.
    before = Path(path).read_text(encoding='utf-8')
    for bad in ('/设置 1 #1 认证 管理员', '确认', '/设置 确认', '/设置 1 随便说说',
                '\n'.join(['/设置 状态'] * 6), '删库跑路'):
        tr.answer = {'commands': bad, 'reply': '试试'}
        s.handle('/设置 做点什么')
        assert s._live_pending() is None and ('不符合格式' in sent[-1] or '最多' in sent[-1]), (bad, sent[-1])
    tr.answer = {'commands': '', 'reply': '你想改哪个群？'}
    s.handle('/设置 改一下')
    assert sent[-1] == '你想改哪个群？'
    tr.fail = True
    s.handle('/设置 把A群关了')
    assert '暂时没能理解' in sent[-1] and s._live_pending() is None
    s.handle('/设置 ' + '长' * (osm.NL_MAX_TEXT + 1))
    assert '太长' in sent[-1]
    assert Path(path).read_text(encoding='utf-8') == before
    assert all('wxid_' not in m for m in sent)
    print('V3178_NL_INVALID_EMPTY_AND_FAILURE_NO_CHANGE=PASS')

    # Client contract for the new auxiliary task.
    assert llm_client.AUX_TIMEOUTS['settings_intent'] == 60
    assert llm_client.validate_auxiliary('settings_intent', {'commands': '', 'reply': '要改哪个群？'})
    assert not llm_client.validate_auxiliary('settings_intent', {'commands': '/设置 状态', 'reply': ' '})
    assert not llm_client.validate_auxiliary('settings_intent', {'commands': '<think>x', 'reply': 'a'})
    print('V3178_SETTINGS_INTENT_CLIENT_CONTRACT=PASS')


def main():
    saved = (core.GROUPS_FILE, core.log, llm_client.execute_auxiliary)
    core.log = lambda *a, **k: None
    tr = Translator()
    llm_client.execute_auxiliary = tr
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'groups.json'
        path.write_text(json.dumps(GROUPS, ensure_ascii=False), encoding='utf-8')
        os.chmod(path, 0o644)
        core.GROUPS_FILE = path
        reset_cache()
        try:
            run(path, tr)
        finally:
            core.GROUPS_FILE, core.log, llm_client.execute_auxiliary = saved
            reset_cache()


if __name__ == '__main__':
    main()
