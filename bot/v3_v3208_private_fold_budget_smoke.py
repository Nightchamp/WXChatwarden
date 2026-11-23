#!/usr/bin/env python3
"""Private-thread fold budget: oldest-first batches, no skipped rows, chaining.

Temp DB only; the model is a stub. No network, no sends.
"""
import os
import re
import shutil
import tempfile
from pathlib import Path

os.environ['CW_DECISION_SHADOW'] = '0'
os.environ['CW_V3194_PRIVATE_THREAD'] = '1'
os.environ.pop('CW_V3194_FOLD_MAX_CHARS', None)
os.environ.pop('CW_V3194_FOLD_CHAIN', None)

from chatwarden import core
from chatwarden import private_thread as pt


OWNER = 'wxid_demo_owner'
CHAT = 'private:' + OWNER
CFG = {'name': CHAT, 'chat_username': OWNER, 'enabled': True,
       'session_key': 'wechat:private:demo', 'personality': 'p',
       'member_labels': {OWNER: '演示主人'}}
LINE_RE = re.compile(r'(第\d+句 (?:对方|机器人)[^\n]*)')


def thread_row():
    with core.open_db() as conn:
        return conn.execute(
            'SELECT summary, through_id, compress_count FROM v3194_private_thread WHERE peer=?',
            (OWNER,)).fetchone()


def all_rows():
    with core.open_db() as conn:
        return conn.execute(
            'SELECT id, sender_id, content, created_at FROM v3194_private_messages '
            'WHERE peer=? ORDER BY id', (OWNER,)).fetchall()


def fill(n, start=0):
    for i in range(start, start + n):
        pt.record(CHAT, 'dbwxid:' + OWNER, f'第{i}句 对方随便聊聊天气和午饭')
        pt.record(CHAT, 'bot', f'第{i}句 机器人回一句家常话')


def folded_lines(prompt):
    body = prompt.split('新增对话（按时间先后）：\n', 1)[1].split('\n\n输出方式', 1)[0]
    return LINE_RE.findall(body)


def wipe():
    with core.open_db() as conn:
        conn.execute('DELETE FROM v3194_private_messages')
        conn.execute('DELETE FROM v3194_private_thread')
    pt._LAST_FAIL.clear()


def run():
    # 0. Defaults keep the old budget and one fold per trigger.
    assert pt.COMPRESS_INPUT_MAX_CHARS == 24000, pt.COMPRESS_INPUT_MAX_CHARS
    assert pt.FOLD_CHAIN_MAX == 1, pt.FOLD_CHAIN_MAX
    print('V3208_DEFAULTS_UNCHANGED=PASS')

    # 1. Under the budget nothing changes: everything but KEEP_RAW folds.
    fill(40)
    prompts = []
    assert pt.compress(CHAT, CFG, call_model=lambda p: prompts.append(p) or '早期闲聊摘要') == 'ok'
    rows = all_rows()
    assert thread_row()[1] == int(rows[-pt.KEEP_RAW - 1][0])
    assert len(folded_lines(prompts[0])) == len(rows) - pt.KEEP_RAW
    print('V3208_UNDER_BUDGET_SAME_AS_BEFORE=PASS')

    # 2. Over the budget: oldest rows go first and the cursor stops at the
    #    last row the model saw (the old code dropped the oldest rows).
    wipe()
    fill(60)  # 120 rows
    pt.COMPRESS_INPUT_MAX_CHARS = 600
    prompts.clear()
    assert pt.compress(CHAT, CFG, call_model=lambda p: prompts.append(p) or '第一批摘要') == 'ok'
    rows = all_rows()
    sent = folded_lines(prompts[0])
    assert sent[0].startswith('第0句 对方'), sent[:2]
    assert len(sent) < len(rows) - pt.KEEP_RAW, len(sent)
    assert len(prompts[0]) < 600 + 2000, len(prompts[0])
    through = thread_row()[1]
    assert through == int(rows[len(sent) - 1][0]), (through, len(sent))
    print('V3208_OVER_BUDGET_OLDEST_FIRST_NO_SKIP=PASS')

    # 3. Chaining drains the backlog; every folded row is sent exactly once.
    seen = list(sent)
    results = pt.compress_chain(
        CHAT, CFG, rounds=10,
        call_model=lambda p: seen.extend(folded_lines(p)) or '后续批次摘要')
    assert results[-1] == 'skip' and results.count('ok') >= 2, results
    through = thread_row()[1]
    expected = [str(r[2]) for r in rows if int(r[0]) <= through]
    assert seen == expected, (len(seen), len(expected))
    _summary, tail = pt.snapshot(CHAT)
    assert len(tail) <= pt.TRIGGER_COUNT, len(tail)
    assert thread_row()[2] == 1 + results.count('ok'), (thread_row(), results)
    print('V3208_CHAIN_DRAINS_BACKLOG=PASS')

    # 4. One overlong line still folds even with a tiny budget.
    one = [(1, 'bot', '长' * 3000, 0.0), (2, 'bot', '短', 0.0)]
    assert pt.fold_batch(one, CFG, budget=10) == one[:1]
    assert pt.fold_batch([], CFG, budget=10) == []
    print('V3208_SINGLE_LONG_LINE_FOLDS=PASS')

    # 5. The chain stops at the first failure and keeps state.
    fill(50, start=200)
    before = thread_row()
    results = pt.compress_chain(CHAT, CFG, rounds=5, call_model=lambda p: '长' * 5000)
    assert results == ['fail:invalid'], results
    assert thread_row() == before
    print('V3208_CHAIN_STOPS_ON_FAIL=PASS')


def main():
    names = ('DB_FILE', 'log', 'owner_wxid')
    original = {k: getattr(core, k) for k in names}
    original_budget = pt.COMPRESS_INPUT_MAX_CHARS
    core.log = lambda *a, **k: None
    core.owner_wxid = lambda default='': OWNER
    tmp = tempfile.mkdtemp(prefix='cw-v3208-')
    core.DB_FILE = Path(tmp) / 'bot.sqlite3'
    pt._SCHEMA_READY = ''
    try:
        run()
    finally:
        for k, v in original.items():
            setattr(core, k, v)
        pt.COMPRESS_INPUT_MAX_CHARS = original_budget
        pt._SCHEMA_READY = ''
        pt._LAST_FAIL.clear()
        shutil.rmtree(tmp, ignore_errors=True)
    print('V3208_PRIVATE_FOLD_BUDGET_SMOKE=PASS')


if __name__ == '__main__':
    main()
