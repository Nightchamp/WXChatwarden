#!/usr/bin/env python3
"""v3.193 per-contact private switch; temp groups.json only, no cloud or sends."""
import json
import os
import tempfile
from pathlib import Path

os.environ['CW_DECISION_SHADOW'] = '0'
# Exercise the real private gates against a temp config.
os.environ.pop('CW_IGNORE_LIVE_OWNER_SETTINGS', None)

from chatwarden import llm_client, core
from chatwarden import owner_settings as osm
from chatwarden.private_worker import PrivateWorker


OWNER = 'wxid_fixture_owner'
BOT = str(core._DB_PRIMARY_BOT_WXID or '').strip()
ZHANG = 'wxid_fixture_zhangsan'
LI = 'wxid_fixture_lisi'
WANG1 = 'wxid_fixture_wang1'
WANG2 = 'wxid_fixture_wang2'
STRANGER = 'wxid_fixture_nosession'
GROUPS = {
    'version': 1,
    'groups': [
        {'name': 'A群', 'enabled': True, 'session_key': 'wechat:group:a',
         'chat_username': '111@chatroom', 'personality': 'p', 'member_labels': {}},
    ],
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def reset_cache():
    core._V3170_SETTINGS_CACHE['mtime'] = None
    core._V3170_SETTINGS_CACHE['raw'] = None


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def bump(path):
    # mtime_ns granularity: force the cache to reload after each write.
    reset_cache()


def main():
    original = {
        'file': core.GROUPS_FILE, 'log': core.log, 'owner': core.owner_wxid,
        'send': core.private_focus_and_send, 'aux': llm_client.execute_auxiliary,
    }
    core.log = lambda *a, **k: None
    core.owner_wxid = lambda default='': OWNER
    with core._NAMES_LOCK:
        saved_names = dict(core._NAMES)
        core._NAMES.update({
            OWNER: '主人', ZHANG: '张三丰', LI: '李四', WANG1: '王小明',
            WANG2: '王小敏', STRANGER: '赵六', '999@chatroom': '某群',
        })
    with core._V3193_PRIVATE_PEERS_LOCK:
        saved_peers = dict(core._V3193_PRIVATE_PEERS)
        core._V3193_PRIVATE_PEERS.clear()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'groups.json'
        path.write_text(json.dumps(GROUPS, ensure_ascii=False), encoding='utf-8')
        os.chmod(path, 0o644)
        core.GROUPS_FILE = path
        reset_cache()
        try:
            run(path)
        finally:
            core.GROUPS_FILE = original['file']
            core.log = original['log']
            core.owner_wxid = original['owner']
            core.private_focus_and_send = original['send']
            llm_client.execute_auxiliary = original['aux']
            with core._NAMES_LOCK:
                core._NAMES.clear()
                core._NAMES.update(saved_names)
            with core._V3193_PRIVATE_PEERS_LOCK:
                core._V3193_PRIVATE_PEERS.clear()
                core._V3193_PRIVATE_PEERS.update(saved_peers)
            reset_cache()


def no_raw_wxid(texts):
    for text in texts:
        assert 'wxid_' not in text, text


def run(path):
    sent = []
    clock = Clock()
    settings = osm.OwnerSettings(send=sent.append, clock=clock)
    sessions = {
        OWNER: {'last_timestamp': 500}, ZHANG: {'last_timestamp': 300},
        LI: {'last_timestamp': 400}, WANG1: {'last_timestamp': 100},
        WANG2: {'last_timestamp': 90},
    }
    if BOT:
        sessions[BOT] = {'last_timestamp': 600}
    core.note_private_sessions(sessions)

    # Defaults: everyone follows the global switch; no overrides yet.
    assert core.private_reply_enabled(ZHANG) is True
    assert settings.handle('/设置 私聊') is True
    overview = sent[-1]
    assert '默认：开' in overview and '单独设置：无' in overview, overview
    assert '主人' not in overview.split('\n', 1)[1] or '主人自己' in overview
    assert overview.index('李四') < overview.index('张三丰'), overview  # most recent first
    print('V3193_OVERVIEW_RECENT_PEERS=PASS')

    # #n from the overview switches exactly that peer off.
    lines = overview.splitlines()
    li_ref = next(l.split()[0] for l in lines if '李四' in l)
    settings.handle(f'/设置 私聊 {li_ref} 关')
    bump(path)
    assert read(path)['private_contacts'][LI] == {'enabled': False, 'name': '李四'}
    assert core.private_reply_enabled(LI) is False
    assert core.private_reply_enabled(ZHANG) is True
    assert '李四' in sent[-1] and '单独关闭' in sent[-1], sent[-1]
    print('V3193_REF_SWITCH_OFF_ONE_PEER=PASS')

    # Global default off; per-contact on wins; global switch reports overrides.
    settings.handle('/设置 私聊 关')
    bump(path)
    assert core.private_nonowner_enabled() is False
    assert '另有 1 人单独设置' in sent[-1], sent[-1]
    settings.handle('/设置 私聊 【张三峰】 开')  # typo tolerated
    bump(path)
    assert read(path)['private_contacts'][ZHANG]['enabled'] is True
    assert core.private_reply_enabled(ZHANG) is True
    assert core.private_reply_enabled(WANG1) is False
    assert core.private_reply_enabled(LI) is False
    print('V3193_GLOBAL_OFF_ALLOWLIST_ON=PASS')

    # Ambiguous name shows candidates and changes nothing; #n then applies.
    before = read(path)
    settings.handle('/设置 私聊 【王小】 开')
    assert '你指的是哪位' in sent[-1], sent[-1]
    assert read(path) == before
    settings.handle('/设置 私聊 #2 开')
    bump(path)
    contacts = read(path)['private_contacts']
    assert sum(1 for w in (WANG1, WANG2) if w in contacts) == 1
    print('V3193_AMBIGUOUS_NAME_FAIL_CLOSED=PASS')

    # Keyword search reaches contacts with no private session yet.
    assert not osm._is_structured(osm._tokens('/设置 私聊 别回陌生人了'))
    settings.handle('/设置 私聊 搜 赵六')
    assert '#1 赵六' in sent[-1], sent[-1]
    settings.handle('/设置 私聊 #1 开')
    bump(path)
    assert core.private_reply_enabled(STRANGER) is True
    # 默认 removes the override; that peer follows the global default again.
    settings.handle('/设置 私聊 【赵六】 默认')
    bump(path)
    assert STRANGER not in read(path)['private_contacts']
    assert core.private_reply_enabled(STRANGER) is False
    print('V3193_SEARCH_AND_DEFAULT_CLEARS=PASS')

    # Owner and bot are never candidates and cannot be overridden.
    cands = core.private_contact_candidates(include_contacts=True)
    assert OWNER not in cands and '999@chatroom' not in cands
    assert not BOT or BOT not in cands
    try:
        core.update_private_contact(OWNER, False)
    except RuntimeError:
        pass
    else:
        raise AssertionError('owner override accepted')
    print('V3193_OWNER_AND_BOT_EXCLUDED=PASS')

    # Config validation rejects malformed private_contacts.
    for bad in ([], {ZHANG: True}, {ZHANG: {'enabled': 'yes'}}, {' x': {'enabled': True}}):
        try:
            core._normalize_groups_config(dict(GROUPS, private_contacts=bad))
        except RuntimeError:
            continue
        raise AssertionError(f'accepted {bad!r}')
    core._normalize_groups_config(read(path))
    print('V3193_CONFIG_VALIDATION=PASS')

    # Private worker gate: switched-off peer is dropped before any processing;
    # late-send guard follows the live switch; owner always passes.
    private_sent = []
    core.private_focus_and_send = lambda chat, text: private_sent.append(text) or 0.0
    import threading
    li_worker = PrivateWorker.__new__(PrivateWorker)
    li_worker.peer_wxid = LI
    li_worker.chat_name = '李四'
    li_worker.stop_event = threading.Event()

    def must_not_process(*a, **k):
        raise AssertionError('switched-off peer was processed')

    li_worker._current_text = must_not_process
    li_worker._handle({'sender_username': LI, 'local_type': 1, 'text': '在吗', 'raw_content': ''})
    assert private_sent == []
    assert li_worker._v3193_is_current() is False
    zhang = PrivateWorker.__new__(PrivateWorker)
    zhang.peer_wxid = ZHANG
    zhang.stop_event = threading.Event()
    assert zhang._v3193_is_current() is True
    owner = PrivateWorker.__new__(PrivateWorker)
    owner.peer_wxid = OWNER
    owner.stop_event = threading.Event()
    assert owner._v3193_is_current() is True
    print('V3193_PRIVATE_WORKER_GATE_AND_LATE_SEND=PASS')

    # Natural language: translator output is bound to an exact wxid, then confirmed.
    calls = []

    def fake_aux(kind, prompt, explicit=False):
        calls.append((kind, prompt))
        return {'structured': {'commands': '/设置 私聊 【李四】 开', 'reply': '单独打开李四的私聊'}}

    llm_client.execute_auxiliary = fake_aux
    settings.handle('/设置 让李四私聊能用')
    assert calls and '/设置 私聊 【名字】' in calls[-1][1]
    assert '待确认' in sent[-1] and '【李四】' in sent[-1], sent[-1]
    settings.handle('确认')
    bump(path)
    assert core.private_reply_enabled(LI) is True
    print('V3193_NATURAL_LANGUAGE_BIND_AND_CONFIRM=PASS')

    # Status line keeps the v3.170 prefix and summarizes overrides.
    settings.handle('/设置 状态')
    assert '私聊（除主人外）：关（默认），另有' in sent[-1], sent[-1]
    no_raw_wxid(sent)
    print('V3193_NO_RAW_WXID_IN_REPLIES=PASS')


if __name__ == '__main__':
    main()
