#!/usr/bin/env python3
"""v3.194 private thread memory; temp DB/config only, no cloud or sends."""
import json
import os
import tempfile
import time
from pathlib import Path

os.environ['CW_DECISION_SHADOW'] = '0'
os.environ['CW_V3194_PRIVATE_THREAD'] = '1'
os.environ.pop('CW_IGNORE_LIVE_OWNER_SETTINGS', None)

from chatwarden import llm_client, core
from chatwarden import owner_settings as osm
from chatwarden import private_thread as pt


OWNER = 'wxid_fixture_owner'
PEER = 'wxid_fixture_peer'
OFFPEER = 'wxid_fixture_offpeer'
CHAT = 'private:' + PEER
GROUPS = {
    'version': 1,
    'groups': [
        {'name': 'A群', 'enabled': True, 'session_key': 'wechat:group:a',
         'chat_username': '111@chatroom', 'personality': 'p', 'member_labels': {}},
    ],
    'private_contacts': {OFFPEER: {'enabled': False, 'name': '关掉的人'}},
}
CFG = {'name': CHAT, 'chat_username': PEER, 'enabled': True,
       'session_key': 'wechat:private:x', 'personality': 'p',
       'member_labels': {PEER: '小周'}}


def reset_cache():
    core._V3170_SETTINGS_CACHE['mtime'] = None
    core._V3170_SETTINGS_CACHE['raw'] = None


def rows_for(peer):
    with core.open_db() as conn:
        return conn.execute(
            'SELECT sender_id, content FROM v3194_private_messages WHERE peer=? ORDER BY id',
            (peer,)).fetchall()


def thread_row(peer):
    with core.open_db() as conn:
        return conn.execute(
            'SELECT summary, through_id, compress_count FROM v3194_private_thread WHERE peer=?',
            (peer,)).fetchone()


def fill(n, start=0, peer=PEER):
    chat = 'private:' + peer
    for i in range(start, start + n):
        core.save_message(chat, 'dbwxid:' + peer, f'第{i}句 对方说的话')
        core.save_message(chat, 'bot', f'第{i}句 机器人回复')


def main():
    names = ('GROUPS_FILE', 'DB_FILE', 'log', 'owner_wxid')
    original = {k: getattr(core, k) for k in names}
    original_async = pt.maybe_compress_async
    original_aux = llm_client.execute_auxiliary
    core.log = lambda *a, **k: None
    core.owner_wxid = lambda default='': OWNER
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'groups.json'
        path.write_text(json.dumps(GROUPS, ensure_ascii=False), encoding='utf-8')
        core.GROUPS_FILE = path
        core.DB_FILE = Path(tmp) / 'bot.sqlite3'
        pt._SCHEMA_READY = ''
        reset_cache()
        try:
            run()
        finally:
            for k, v in original.items():
                setattr(core, k, v)
            pt.maybe_compress_async = original_async
            llm_client.execute_auxiliary = original_aux
            pt._SCHEMA_READY = ''
            reset_cache()
    print('V3194_PRIVATE_THREAD_SMOKE=PASS')


def run():
    calls = []
    pt.maybe_compress_async = lambda chat, cfg=None: calls.append(chat) or False

    # 1. Private rows go to the per-peer store; groups do not.
    core.save_message(CHAT, 'dbwxid:' + PEER, '你好，我下周三去上海出差')
    core.save_message(CHAT, 'bot', '好的，路上顺利')
    core.save_message('A群', 'dbwxid:wxid_fixture_g', '群消息')
    assert rows_for(PEER) == [('dbwxid:' + PEER, '你好，我下周三去上海出差'), ('bot', '好的，路上顺利')]
    with core.open_db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM v3194_private_messages').fetchone()[0] == 2
    assert calls == [CHAT], calls  # compression check only after bot reply
    print('V3194_PRIVATE_STORE_SEPARATE=PASS')

    # 2. Switched-off peer is not stored; owner always is.
    core.save_message('private:' + OFFPEER, 'dbwxid:' + OFFPEER, '不该存')
    core.save_message('private:' + OWNER, 'dbwxid:' + OWNER, '主人的话')
    assert rows_for(OFFPEER) == []
    assert rows_for(OWNER) == [('dbwxid:' + OWNER, '主人的话')]
    print('V3194_OFF_PEER_NOT_STORED_OWNER_STORED=PASS')

    # 3. Group cap eviction no longer erases private context.
    with core.open_db() as conn:
        conn.execute('DELETE FROM group_messages')
    with core._RECENT_CONTEXT_CACHE_LOCK:
        core._RECENT_CONTEXT_CACHE.clear()
    ctx = core.recent_context(CHAT, CFG)
    assert '下周三去上海出差' in ctx and '路上顺利' in ctx, ctx
    print('V3194_SURVIVES_GROUP_CAP=PASS')

    # 4. Below threshold nothing compresses; above it the old part folds.
    assert pt.compress(CHAT, CFG, call_model=lambda p: 'x') == 'skip'
    fill(40)  # 2 + 80 rows > TRIGGER_COUNT
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return '对方下周三去上海出差；聊过很多句日常。'
    assert pt.compress(CHAT, CFG, call_model=model) == 'ok'
    summary, through, count = thread_row(PEER)
    assert count == 1 and '上海出差' in summary
    s, rows = pt.snapshot(CHAT)
    assert len(rows) == pt.KEEP_RAW, len(rows)
    assert rows[-1][2] == '第39句 机器人回复'
    assert '下周三去上海出差' in prompts[0] and '已有摘要：\n（无）' in prompts[0]
    assert 'wxid_' not in prompts[0], 'raw wxid leaked into compression prompt'
    print('V3194_COMPRESS_FOLDS_KEEPS_TAIL=PASS')

    # 5. Prompt sees summary in memory and raw tail in context.
    snap = core.freeze_llm_context(CFG, 'dbwxid:' + PEER, None, current_text='还记得我去哪吗')
    assert '较早私聊摘要' in snap['memory_context'] and '上海出差' in snap['memory_context']
    assert '第39句 机器人回复' in snap['context'] and '第0句 对方' not in snap['context']
    print('V3194_PROMPT_SUMMARY_PLUS_TAIL=PASS')

    # 6. Invalid summary (identity leak / too long / empty) keeps old state.
    fill(30, start=100)
    before = thread_row(PEER)
    assert pt.compress(CHAT, CFG, call_model=lambda p: '对方是 wxid_abc') == 'fail:invalid'
    assert thread_row(PEER) == before
    pt._LAST_FAIL.clear()
    assert pt.compress(CHAT, CFG, call_model=lambda p: '长' * 2000) == 'fail:invalid'
    assert pt.compress(CHAT, CFG, call_model=lambda p: '   ') == 'fail:invalid'
    assert thread_row(PEER) == before
    print('V3194_INVALID_SUMMARY_REJECTED=PASS')

    # 7. Provider failure keeps state and backs off async retries.
    def boom(*a, **k):
        raise llm_client.LLMExecutionError('timeout')
    llm_client.execute_auxiliary = boom
    assert pt.compress(CHAT, CFG) == 'fail:provider'
    assert thread_row(PEER) == before
    print('V3194_PROVIDER_FAIL_KEEPS_STATE=PASS')

    # 8. CAS: a cursor moved meanwhile is never overwritten.
    def racing(prompt):
        with core.open_db() as conn:
            conn.execute('UPDATE v3194_private_thread SET through_id=through_id+1 WHERE peer=?', (PEER,))
        return '新摘要'
    moved_before = thread_row(PEER)
    assert pt.compress(CHAT, CFG, call_model=racing) == 'skip'
    after = thread_row(PEER)
    assert after[0] == moved_before[0] and after[1] == moved_before[1] + 1
    ok_second = pt.compress(CHAT, CFG, call_model=lambda p: '第二轮摘要：上海出差，后来聊了很多。')
    assert ok_second == 'ok' and thread_row(PEER)[2] == 2
    assert '第二轮摘要' in pt.snapshot(CHAT)[0]
    print('V3194_CURSOR_CAS=PASS')

    # 9. Time-gap hint only for long gaps.
    gap_peer = 'wxid_fixture_gap'
    now = time.time()
    pt.record('private:' + gap_peer, 'dbwxid:' + gap_peer, '晚安', timestamp=now - 9 * 3600)
    pt.record('private:' + gap_peer, 'dbwxid:' + gap_peer, '早', timestamp=now)
    block = pt.memory_block('private:' + gap_peer)
    assert '时间提示' in block and '约 9 小时' in block, block
    pt.record('private:' + gap_peer, 'bot', '早呀', timestamp=now + 5)
    assert '时间提示' not in pt.memory_block('private:' + gap_peer)
    print('V3194_TIME_GAP_HINT=PASS')

    # 10. Owner "/设置 私聊 【名字】 忘记" clears one peer only, switch unchanged.
    assert '忘记' in osm._PRIVATE_ACTIONS
    sent = []
    settings = osm.OwnerSettings(send=sent.append, clock=time.time)
    with core._NAMES_LOCK:
        saved_names = dict(core._NAMES)
        core._NAMES.update({PEER: '周小舟'})
    try:
        core.note_private_sessions({PEER: {'last_timestamp': 100}})
        assert settings.handle('/设置 私聊 【周小舟】 忘记') is True
    finally:
        with core._NAMES_LOCK:
            core._NAMES.clear()
            core._NAMES.update(saved_names)
    assert '私聊记忆已清除' in sent[-1] and '开关不变' in sent[-1], sent
    assert all('wxid_' not in t for t in sent), sent
    assert rows_for(PEER) == [] and thread_row(PEER) is None
    reset_cache()
    assert PEER not in json.loads(core.GROUPS_FILE.read_text(encoding='utf-8')).get('private_contacts', {})
    assert rows_for(OWNER)
    assert pt.forget('not-a-wxid') == 0
    print('V3194_FORGET_ONE_PEER=PASS')

    # 11. Kill switch falls back to legacy behaviour.
    os.environ['CW_V3194_PRIVATE_THREAD'] = '0'
    try:
        assert pt.record(CHAT, 'bot', 'x') is False
        assert core._v3194_private_recent(CHAT, CFG) is None
        assert pt.memory_block(CHAT) == ''
    finally:
        os.environ['CW_V3194_PRIVATE_THREAD'] = '1'
    print('V3194_KILL_SWITCH=PASS')


if __name__ == '__main__':
    main()
