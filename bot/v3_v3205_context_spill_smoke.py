#!/usr/bin/env python3
"""v3.205 inline/spill context; temp DB only, no cloud or sends.

- group_messages keeps a per-chat window, so a busy group no longer pushes a
  quiet one out; a total ceiling and an age limit bound the file; 0 restores
  the legacy global cap.
- a private message that points back in time pulls matching folded rows back
  into the prompt verbatim (no model call).
"""
import json
import os
import tempfile
import time
from pathlib import Path

os.environ['CW_DECISION_SHADOW'] = '0'
os.environ['CW_V3194_PRIVATE_THREAD'] = '1'
os.environ.pop('CW_IGNORE_LIVE_OWNER_SETTINGS', None)

from chatwarden import core
from chatwarden import private_thread as pt

OWNER = 'wxid_demo_owner'
PEER = 'wxid_demo_peer'
CHAT = 'private:' + PEER
GROUPS = {'version': 1, 'groups': [], 'private_contacts': {PEER: {'enabled': True, 'name': '小林'}}}
CFG = {'name': CHAT, 'chat_username': PEER, 'enabled': True,
       'session_key': 'wechat:private:demo', 'personality': 'p',
       'member_labels': {PEER: '小林'}}
BUSY, QUIET = '20000000001@chatroom-busy', '20000000002@chatroom-quiet'


def count(chat=None):
    with core.open_db() as conn:
        if chat is None:
            return conn.execute('SELECT COUNT(*) FROM group_messages').fetchone()[0]
        return conn.execute('SELECT COUNT(*) FROM group_messages WHERE chat=?', (chat,)).fetchone()[0]


def check_retention():
    saved = {k: getattr(core, k) for k in (
        'GROUP_MESSAGES_KEEP', 'GROUP_MESSAGES_PER_CHAT',
        'GROUP_MESSAGES_MAX_TOTAL', 'GROUP_MESSAGES_MAX_AGE_DAYS',
        '_TRIM_CHAT_EVERY_SEC', '_TRIM_GLOBAL_EVERY_SEC')}
    try:
        # Trim on every insert so caps are exact in the fixture.
        core._TRIM_CHAT_EVERY_SEC = 0.0
        core._TRIM_GLOBAL_EVERY_SEC = 0.0
        # Legacy: one global cap, the busy group evicts the quiet one.
        core.GROUP_MESSAGES_PER_CHAT = 0
        core.GROUP_MESSAGES_KEEP = 20
        for i in range(3):
            core.save_message(QUIET, 'dbwxid:wxid_demo_q', f'安静群第{i}句')
        for i in range(30):
            core.save_message(BUSY, 'dbwxid:wxid_demo_b', f'热闹群第{i}句')
        assert count(QUIET) == 0 and count() == 20, (count(QUIET), count())
        print('V3205_RETENTION_LEGACY_GLOBAL_CAP=PASS')

        with core.open_db() as conn:
            conn.execute('DELETE FROM group_messages')
        core.GROUP_MESSAGES_PER_CHAT = 10
        core.GROUP_MESSAGES_MAX_TOTAL = 1000
        core.GROUP_MESSAGES_MAX_AGE_DAYS = 0
        for i in range(3):
            core.save_message(QUIET, 'dbwxid:wxid_demo_q', f'安静群第{i}句')
        for i in range(30):
            core.save_message(BUSY, 'dbwxid:wxid_demo_b', f'热闹群第{i}句')
        assert count(QUIET) == 3 and count(BUSY) == 10, (count(QUIET), count(BUSY))
        with core.open_db() as conn:
            newest = conn.execute(
                'SELECT content FROM group_messages WHERE chat=? ORDER BY id DESC LIMIT 1',
                (BUSY,)).fetchone()[0]
        assert newest == '热闹群第29句'
        print('V3205_RETENTION_PER_CHAT=PASS')

        core.GROUP_MESSAGES_MAX_TOTAL = 8
        core.save_message(QUIET, 'dbwxid:wxid_demo_q', '触发总量上限')
        assert count() == 8, count()
        print('V3205_RETENTION_TOTAL_CEILING=PASS')

        core.GROUP_MESSAGES_MAX_TOTAL = 1000
        core.GROUP_MESSAGES_MAX_AGE_DAYS = 30
        with core.open_db() as conn:
            conn.execute(
                'INSERT INTO group_messages (chat, sender_id, content, created_at) VALUES (?,?,?,?)',
                (QUIET, 'dbwxid:wxid_demo_q', '四十天前的话', time.time() - 40 * 86400))
        core.save_message(QUIET, 'dbwxid:wxid_demo_q', '今天的话')
        with core.open_db() as conn:
            old = conn.execute(
                "SELECT COUNT(*) FROM group_messages WHERE content='四十天前的话'").fetchone()[0]
        assert old == 0
        print('V3205_RETENTION_MAX_AGE=PASS')

        # Throttled (production) mode: one trim per chat per interval.
        core._TRIM_CHAT_EVERY_SEC = 3600.0
        core._TRIM_GLOBAL_EVERY_SEC = 3600.0
        core._TRIM_STATE['chat'].clear()
        core._TRIM_STATE['global'] = 0.0
        core.GROUP_MESSAGES_PER_CHAT = 5
        for i in range(12):
            core.save_message(BUSY, 'dbwxid:wxid_demo_b', f'限流第{i}句')
        assert count(BUSY) > 5, count(BUSY)
        core._TRIM_STATE['chat'].clear()
        core.save_message(BUSY, 'dbwxid:wxid_demo_b', '限流后一句')
        assert count(BUSY) == 5, count(BUSY)
        print('V3205_RETENTION_TRIM_THROTTLED=PASS')
    finally:
        for k, v in saved.items():
            setattr(core, k, v)


def check_recall():
    original_async = pt.maybe_compress_async
    pt.maybe_compress_async = lambda chat, cfg=None: False
    try:
        core.save_message(CHAT, 'dbwxid:' + PEER, '我家猫叫豆包，三岁了，是只橘猫')
        core.save_message(CHAT, 'bot', '豆包这名字可爱')
        core.save_message(CHAT, 'dbwxid:' + PEER, '我的车牌尾号是 A7K')
        core.save_message(CHAT, 'bot', '记下了')
        for i in range(40):
            core.save_message(CHAT, 'dbwxid:' + PEER, f'第{i}句 日常闲聊')
            core.save_message(CHAT, 'bot', f'第{i}句 机器人回复')
        assert pt.compress(CHAT, CFG, call_model=lambda p: '对方养了一只猫；聊了很多日常。') == 'ok'

        # The summary lost the cat's name; a backward-looking question gets it back.
        block = pt.memory_block(CHAT, current_text='之前跟你说过我家猫叫什么来着')
        assert '按需取回' in block and '豆包' in block, block
        assert 'A7K' not in block, block
        block = pt.memory_block(CHAT, current_text='还记得我的车牌尾号吗')
        assert 'A7K' in block, block
        print('V3205_PRIVATE_RECALL_HITS=PASS')

        # No cue, or nothing matching: nothing is added.
        assert '按需取回' not in pt.memory_block(CHAT, current_text='我家猫叫什么')
        assert '按需取回' not in pt.memory_block(CHAT, current_text='之前说的那个演唱会')
        assert '按需取回' not in pt.memory_block(CHAT)
        os.environ['CW_PRIVATE_RECALL'] = '0'
        assert '按需取回' not in pt.memory_block(CHAT, current_text='之前跟你说过我家猫叫什么来着')
        os.environ.pop('CW_PRIVATE_RECALL', None)
        print('V3205_PRIVATE_RECALL_ONLY_ON_CUE=PASS')

        snap = core.freeze_llm_context(CFG, 'dbwxid:' + PEER, None,
                                       current_text='之前跟你说过我家猫叫什么来着')
        assert '豆包' in snap['memory_context'], snap['memory_context']
        assert 'wxid_' not in snap['memory_context']
        print('V3205_PRIVATE_RECALL_IN_PROMPT=PASS')
    finally:
        os.environ.pop('CW_PRIVATE_RECALL', None)
        pt.maybe_compress_async = original_async


def main():
    names = ('GROUPS_FILE', 'DB_FILE', 'log', 'owner_wxid')
    original = {k: getattr(core, k) for k in names}
    core.log = lambda *a, **k: None
    core.owner_wxid = lambda default='': OWNER
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'groups.json'
        path.write_text(json.dumps(GROUPS, ensure_ascii=False), encoding='utf-8')
        core.GROUPS_FILE = path
        core.DB_FILE = Path(tmp) / 'bot.sqlite3'
        pt._SCHEMA_READY = ''
        core._V3170_SETTINGS_CACHE['mtime'] = None
        core._V3170_SETTINGS_CACHE['raw'] = None
        try:
            check_retention()
            check_recall()
        finally:
            for k, v in original.items():
                setattr(core, k, v)
            pt._SCHEMA_READY = ''
            core._V3170_SETTINGS_CACHE['mtime'] = None
            core._V3170_SETTINGS_CACHE['raw'] = None
    print('V3205_CONTEXT_SPILL_SMOKE=PASS')


if __name__ == '__main__':
    main()
