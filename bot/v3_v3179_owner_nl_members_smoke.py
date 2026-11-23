#!/usr/bin/env python3
"""v3.179 NL /设置 member names (fuzzy), follow-ups and direct views; fakes only."""
import json
import os
import tempfile
from pathlib import Path

os.environ['CW_DECISION_SHADOW'] = '0'
os.environ.pop('CW_IGNORE_LIVE_OWNER_SETTINGS', None)

from chatwarden import llm_client, core, memory_docs
from chatwarden import owner_settings as osm


GROUPS = {'version': 1, 'groups': [
    {'name': '小卫士', 'enabled': True, 'session_key': 'wechat:group:j',
     'chat_username': '111@chatroom', 'personality': 'p', 'member_labels': {}},
    {'name': '小明同学', 'enabled': True, 'session_key': 'wechat:group:t',
     'chat_username': '222@chatroom', 'personality': 'p', 'member_labels': {}},
]}


def info(nick, messages, label=''):
    return {'label': label, 'nickname': nick, 'contact': '', 'verified': '',
            'messages': messages, 'is_member': True, 'facts': 0}


MEMBERS = {
    'wxid_fixture_grace': info('Mr. Sleepless', 353),
    'wxid_fixture_ming': info('小明同学', 20),
    'wxid_fixture_min': info('小敏', 18),
}


class Translator:
    def __init__(self):
        self.answers = []
        self.prompts = []

    def __call__(self, kind, prompt, *, explicit=False, image_path=None):
        self.prompts.append(prompt)
        return {'structured': self.answers.pop(0)}


def run(path, tr):
    sent = []
    s = osm.OwnerSettings(send=sent.append, group_worker_lookup=lambda name: None)

    # The reported case: view a member's profile by (misspelled) name, no retyping.
    tr.answers.append({'commands': '/设置 2 成员 【gracless】 画像', 'reply': '查看画像'})
    s.handle('/设置 查看小明同学群里gracless的用户画像')
    assert s._live_pending() is None, 'read-only member view must not need 确认'
    assert 'Mr. Sleepless' in sent[-1] and '画像' in sent[-1], sent[-1]
    assert '#编号' not in sent[-1] and 'wxid_' not in ''.join(sent)
    print('V3179_MEMBER_PROFILE_VIEW_BY_FUZZY_NAME_DIRECT=PASS')

    # A modification by name is listed with the resolved member, then runs on 确认.
    tr.answers.append({'commands': '/设置 2 成员 【小名同学】 称呼 阿明', 'reply': '给小明起称呼'})
    s.handle('/设置 小明同学里的小名同学以后叫阿明')
    assert '【小明同学】' in sent[-1] and '待确认' in sent[-1], sent[-1]
    s.handle('确认')
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    labels = next(g for g in data['groups'] if g['name'] == '小明同学')['member_labels']
    assert labels == {'wxid_fixture_ming': '阿明'}, labels
    print('V3179_MEMBER_CHANGE_BY_NAME_CONFIRMED=PASS')

    # Ambiguous name: candidates shown; the follow-up "第2个" sees context and uses #2.
    tr.answers.append({'commands': '/设置 2 成员 【小】 画像', 'reply': '查看'})
    s.handle('/设置 看看小明同学里小那个人的画像')
    assert '哪位' in sent[-1] and '#1' in sent[-1] and '#2' in sent[-1], sent[-1]
    order = s._selection['wxids']
    tr.answers.append({'commands': '/设置 2 成员 #2 画像', 'reply': '查看第2位'})
    s.handle('/设置 第2个')
    prompt = tr.prompts[-1]
    assert '上一条设置要求：看看小明同学里小那个人的画像' in prompt and '#2 ' in prompt, prompt[-400:]
    expected = osm.OwnerSettings._member_name(MEMBERS[order[1]])
    assert expected in sent[-1] and s._live_pending() is None, sent[-1]
    print('V3179_AMBIGUOUS_NAME_FOLLOWUP_USES_LIVE_SELECTION=PASS')

    # Unknown names, stale numbers and member-less #refs never execute.
    before = Path(path).read_text(encoding='utf-8')
    for commands, needle in (('/设置 2 成员 【张三】 称呼 x', '没找到'),
                             ('/设置 1 成员 #1 称呼 x', '已失效'),
                             ('/设置 2 #2 画像', '不符合格式')):
        tr.answers.append({'commands': commands, 'reply': '试试'})
        s.handle('/设置 做点什么')
        assert needle in sent[-1] and s._live_pending() is None, (commands, sent[-1])
    assert Path(path).read_text(encoding='utf-8') == before
    print('V3179_UNKNOWN_STALE_OR_MALFORMED_MEMBER_NO_CHANGE=PASS')

    scores = osm.fuzzy_members(MEMBERS, '王大锤')
    assert scores == []
    assert osm.fuzzy_members(MEMBERS, 'sleep less')[0][2] == 'wxid_fixture_grace'
    print('V3179_FUZZY_SCORER_BOUNDS=PASS')


def main():
    saved = (core.GROUPS_FILE, core.log, llm_client.execute_auxiliary, core.group_member_candidates,
             core.member_memory_snapshot, memory_docs.get_doc)
    core.log = lambda *a, **k: None
    tr = Translator()
    llm_client.execute_auxiliary = tr
    core.group_member_candidates = lambda name, cfg: {k: dict(v) for k, v in MEMBERS.items()}
    core.member_memory_snapshot = lambda chat, wxid: {'pinned': [], 'facts': [], 'message_count': 3}
    memory_docs.get_doc = lambda scope, subject: None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'groups.json'
        path.write_text(json.dumps(GROUPS, ensure_ascii=False), encoding='utf-8')
        os.chmod(path, 0o644)
        core.GROUPS_FILE = path
        core._V3170_SETTINGS_CACHE['mtime'] = None
        try:
            run(path, tr)
        finally:
            (core.GROUPS_FILE, core.log, llm_client.execute_auxiliary, core.group_member_candidates,
             core.member_memory_snapshot, memory_docs.get_doc) = saved
            core._V3170_SETTINGS_CACHE['mtime'] = None
            core._V3170_SETTINGS_CACHE['raw'] = None


if __name__ == '__main__':
    main()
