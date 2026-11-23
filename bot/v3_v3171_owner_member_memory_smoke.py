#!/usr/bin/env python3
"""v3.171 owner member/memory settings; temp groups.json + temp DB only."""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

os.environ['CW_DECISION_SHADOW'] = '0'

from chatwarden import core
from chatwarden import owner_settings as osm


A = 'wxid_fixture_alpha'
B = 'wxid_fixture_beta'
C = 'wxid_fixture_gamma'
BOT = 'wxid_fixture_bot'
GROUP = {
    'name': 'A群', 'enabled': True, 'session_key': 'wechat:group:a',
    'chat_username': '111@chatroom', 'personality': 'p', 'member_labels': {},
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def read_group(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))['groups'][0]


def main():
    saved = {k: getattr(core, k) for k in (
        'GROUPS_FILE', 'DB_FILE', 'log', '_DB_PRIMARY_BOT_WXID', '_V3171_SCHEMA_READY')}
    core.log = lambda *args, **kwargs: None
    core._DB_PRIMARY_BOT_WXID = BOT
    with tempfile.TemporaryDirectory() as tmp:
        groups = Path(tmp) / 'groups.json'
        groups.write_text(json.dumps({'version': 1, 'groups': [GROUP]}, ensure_ascii=False),
                          encoding='utf-8')
        db = Path(tmp) / 'bot.sqlite3'
        old = sqlite3.connect(db)
        old.execute(
            'CREATE TABLE v317_user_memory (chat TEXT NOT NULL, sender_wxid TEXT NOT NULL,'
            'recent_json TEXT NOT NULL, facts_json TEXT NOT NULL, message_count INTEGER NOT NULL,'
            'first_seen REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(chat, sender_wxid))')
        old.execute("INSERT INTO v317_user_memory VALUES ('A群', ?, '[\"旧发言\"]', "
                    "'[\"我是老师\"]', 5, 1, 1)", (B,))
        old.commit()
        old.close()
        core.GROUPS_FILE = groups
        core.DB_FILE = db
        core._V3171_SCHEMA_READY = False
        core._V3170_SETTINGS_CACHE['mtime'] = None
        try:
            run(groups)
        finally:
            for key, value in saved.items():
                setattr(core, key, value)
            core._V3170_SETTINGS_CACHE['mtime'] = None
            core._V3170_SETTINGS_CACHE['raw'] = None


def run(groups):
    # Schema migration keeps the pre-v3.171 row and defaults pinned to [].
    snap = core.member_memory_snapshot('A群', B)
    assert snap['facts'] == ['我是老师'] and snap['pinned'] == [], snap
    print('V3171_SCHEMA_MIGRATION_PRESERVES_OLD_ROWS=PASS')

    core.update_group_member_ids('A群', {A, B, BOT})
    core.update_group_member_name_map('A群', {A: '小明同学', BOT: '机器人'})
    core.update_name_map({B: '小明哥', C: '路人'})
    cands = core.group_member_candidates('A群', GROUP)
    assert set(cands) == {A, B}, cands
    assert cands[A]['nickname'] == '小明同学' and cands[B]['contact'] == '小明哥'
    assert cands[B]['facts'] == 1 and cands[A]['is_member'] is True

    sent = []
    clock = Clock()
    s = osm.OwnerSettings(send=sent.append, clock=clock)
    s.handle('/设置 1 成员 小明')
    assert '#1' in sent[-1] and '#2' in sent[-1] and BOT not in sent[-1], sent[-1]
    assert 'wxid_' not in sent[-1]
    order = s._selection['wxids']
    a_ref = '#%d' % (order.index(A) + 1)
    b_ref = '#%d' % (order.index(B) + 1)
    s.handle('/设置 1 成员 不存在的人')
    assert '没找到' in sent[-1]
    print('V3171_MEMBER_SEARCH_EXACT_SCOPED_NO_WXID=PASS')

    # Labels are direct; verification needs confirmation; selection expires.
    s.handle('/设置 1 成员 小明')
    s.handle(f'/设置 1 成员 {a_ref} 称呼 明哥')
    assert read_group(groups)['member_labels'] == {A: '明哥'}
    s.handle(f'/设置 1 成员 {b_ref} 认证 YE')
    assert 'verified_members' not in read_group(groups) and '待确认' in sent[-1]
    s.handle('确认')
    assert read_group(groups)['verified_members'] == {B: 'YE'}
    core._V3170_SETTINGS_CACHE['mtime'] = None
    cfg = core.load_groups_config()[0]
    assert 'verified-member:YE' in core.sender_context_label('dbwxid:' + B, cfg)
    s.handle('/设置 1 成员 小明')
    s.handle(f'/设置 1 成员 {b_ref} 认证 取消')
    s.handle('确认')
    assert read_group(groups)['verified_members'] == {}
    s.handle(f'/设置 1 成员 {a_ref} 称呼 清除')
    assert read_group(groups)['member_labels'] == {}
    clock.now += osm.SELECTION_TTL_SECONDS + 1
    before = groups.read_text(encoding='utf-8')
    s.handle(f'/设置 1 成员 {a_ref} 称呼 过期')
    assert '过期' in sent[-1] and groups.read_text(encoding='utf-8') == before
    print('V3171_LABEL_VERIFY_CONFIRM_AND_SELECTION_TTL=PASS')

    # Pinned profile facts survive automatic eviction and are injected.
    s.handle('/设置 1 成员 小明')
    s.handle(f'/设置 1 成员 {b_ref} 画像 加 喜欢猫 也养狗')
    s.handle(f'/设置 1 成员 {b_ref} 画像 加 我的密码是123')
    assert '敏感' in sent[-1]
    with core.open_db() as conn:
        for i in range(30):
            core._v317_remember_in_conn(conn, 'A群', 'dbwxid:' + B, f'我是第{i}号工程师')
    snap = core.member_memory_snapshot('A群', B)
    assert snap['pinned'] == ['喜欢猫 也养狗'], snap
    block = core.user_memory_context('A群', 'dbwxid:' + B, '你好')
    assert '主人设定的此人信息' in block and '喜欢猫 也养狗' in block, block
    s.handle(f'/设置 1 成员 {b_ref} 画像')
    assert '1. 喜欢猫 也养狗' in sent[-1]
    s.handle(f'/设置 1 成员 {b_ref} 画像 删 1')
    assert core.member_memory_snapshot('A群', B)['pinned'] == []
    s.handle(f'/设置 1 成员 {b_ref} 画像 清空')
    assert core.member_memory_snapshot('A群', B)['message_count'] > 0
    s.handle('确认')
    assert core.member_memory_snapshot('A群', B)['message_count'] == 0
    print('V3171_PINNED_PROFILE_SURVIVES_EVICTION_SENSITIVE_CLEAR=PASS')

    # Manual group settings: injected, never evicted, deletion confirmed.
    s.handle('/设置 1 群记忆 加 每周五晚上固定开黑')
    for i in range(25):
        core.save_message('A群', 'dbwxid:' + A, f'就这么定了，第{i}号方案以后统一执行')
    memories = core.group_memory_snapshot('A群')
    manual = [m for m in memories if m['manual']]
    automatic = [m for m in memories if not m['manual']]
    assert [m['anchor'] for m in manual] == ['每周五晚上固定开黑'], memories
    assert 0 < len(automatic) <= core._V340_MEMORY_MAX, len(automatic)
    block = core.group_memory_context('A群', GROUP)
    assert '群内设定' in block and '每周五晚上固定开黑' in block, block
    s.handle('/设置 1 群记忆')
    assert '1. 每周五晚上固定开黑' in sent[-1]
    s.handle('/设置 1 群记忆 删 1')
    assert '每周五晚上固定开黑' in sent[-1] and '待确认' in sent[-1]
    s.handle('确认')
    assert not [m for m in core.group_memory_snapshot('A群') if m['manual']]
    print('V3171_GROUP_SETTING_INJECTED_NOT_EVICTED_DELETE_CONFIRMED=PASS')


if __name__ == '__main__':
    main()
