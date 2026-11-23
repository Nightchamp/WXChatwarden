#!/usr/bin/env python3
"""v3.172 consolidated memory docs; temp groups.json + temp DB + stub model."""
import json
import os
import tempfile
import time
from pathlib import Path

os.environ['CW_DECISION_SHADOW'] = '0'
os.environ['CW_MEMORY_DOCS_SCHEDULER'] = '0'

from chatwarden import llm_client
from chatwarden import core
from chatwarden import memory_docs as md
from chatwarden import owner_settings as osm
from chatwarden import worker


A = 'wxid_fixture_alpha'
B = 'wxid_fixture_beta'
BOT = 'wxid_fixture_bot'


def group(name, room):
    return {
        'name': name, 'enabled': True, 'session_key': 'wechat:group:' + room,
        'chat_username': room + '@chatroom', 'personality': 'p', 'member_labels': {},
    }


GROUPS = [group('A群', '111'), group('B群', '222')]

MEMBER_OK = (
    "## 身份背景\n- 自称后端程序员\n"
    "## 兴趣偏好\n- 喜欢猫\n- 手机号13800000000\n"
    "## 说话风格\n- 爱用反讽\n"
    "## 群内关系和梗\n- [G1] 在A群外号卷王\n- [G2] 在B群常和老王互怼\n- [G9] 不存在的群\n"
)
GROUP_OK = (
    "## 群概况\n- 游戏开黑群\n"
    "## 常聊话题\n- 原神抽卡\n"
    "## 群梗和黑话\n- 卷王指加班多的人\n"
    "## 约定和近期大事\n- 09-26 约定每周五开黑\n"
)


class Stub:
    def __init__(self):
        self.outputs = []
        self.prompts = []

    def __call__(self, task_kind, prompt, *, explicit=False, image_path=None):
        assert task_kind == 'history_summary', task_kind
        self.prompts.append((prompt, explicit))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return {'structured': {'summary': out}}


def main():
    saved_core = {k: getattr(core, k) for k in (
        'GROUPS_FILE', 'DB_FILE', 'log', '_DB_PRIMARY_BOT_WXID', '_V3171_SCHEMA_READY')}
    saved_exec = llm_client.execute_auxiliary
    stub = Stub()
    core.log = lambda *args, **kwargs: None
    core._DB_PRIMARY_BOT_WXID = BOT
    llm_client.execute_auxiliary = stub
    with tempfile.TemporaryDirectory() as tmp:
        groups = Path(tmp) / 'groups.json'
        groups.write_text(json.dumps({'version': 1, 'groups': GROUPS}, ensure_ascii=False),
                          encoding='utf-8')
        core.GROUPS_FILE = groups
        core.DB_FILE = Path(tmp) / 'bot.sqlite3'
        core._V3171_SCHEMA_READY = False
        core._V3170_SETTINGS_CACHE['mtime'] = None
        md._SERVICE = None
        try:
            run(stub)
        finally:
            for key, value in saved_core.items():
                setattr(core, key, value)
            llm_client.execute_auxiliary = saved_exec
            core._V3170_SETTINGS_CACHE['mtime'] = None
            core._V3170_SETTINGS_CACHE['raw'] = None
            md._SERVICE = None


def run(stub):
    # Validation: exact layout, tags mapped, unknown tag + sensitive dropped.
    tags = {'G1': 'A群', 'G2': 'B群'}
    doc = md.validate_member_output(MEMBER_OK, tags, 2)
    assert '[群:A群] 在A群外号卷王' in doc and '[群:B群]' in doc, doc
    assert '13800000000' not in doc and '不存在的群' not in doc, doc
    for bad in ("## 身份背景\n- x\n", "随便写的\n" + MEMBER_OK,
                MEMBER_OK.replace('- 爱用反讽', '- ' + '很长' * 300)):
        try:
            md.validate_member_output(bad, tags, 2)
        except md.MemoryDocError:
            pass
        else:
            raise AssertionError('accepted invalid member doc: ' + bad[:40])
    assert md.validate_group_output('```markdown\n' + GROUP_OK + '```').startswith('## 群概况')
    print('V3172_VALIDATION_LAYOUT_TAGS_SENSITIVE_LENGTH=PASS')

    # Cross-group member doc; relation lines injected only into their group.
    for i in range(20):
        core.save_message('A群', 'dbwxid:' + A, f'今天又加班到十点，第{i}次了')
        core.save_message('B群', 'dbwxid:' + A, f'老王你又来了，第{i}回合')
    core.save_message('A群', 'dbwxid:' + B, '我也来一句')
    jobs = md.due_jobs()
    assert ('member', A, 40) in jobs and all(j[1] != B for j in jobs), jobs
    assert [j[1] for j in jobs if j[0] == 'group'] == ['A群', 'B群'], jobs
    stub.outputs.append(MEMBER_OK)
    md.rebuild_member(A)
    prompt = stub.prompts[-1][0]
    assert 'summary 字段' in prompt and '不要创建或写入任何文件' in prompt
    # Budget counts only the groups this member is active in (A群 + B群), not all.
    assert f'硬上限 {md.member_budget(2) - 30} 字' in prompt, prompt
    assert md.member_budget(1) == md.MEMBER_MAX_CHARS
    assert 'G1=「A群」' in prompt and 'G2=「B群」' in prompt and 'wxid_' not in prompt, prompt
    in_a = core.user_memory_context('A群', 'dbwxid:' + A, '你好')
    in_b = core.user_memory_context('B群', 'dbwxid:' + A, '你好')
    assert '卷王' in in_a and '老王' not in in_a and '后端程序员' in in_a, in_a
    assert '老王' in in_b and '卷王' not in in_b and '整理画像' in in_b, in_b
    assert '今天又加班' not in in_a  # legacy recent lines are not injected any more
    assert not [j for j in md.due_jobs() if j[1] == A]
    print('V3172_MEMBER_DOC_CROSS_GROUP_RELATIONS_SCOPED=PASS')

    # Invalid output keeps the previous doc and records a failure/backoff.
    before = md.get_doc('member', A)['doc']
    for i in range(30):
        core.save_message('A群', 'dbwxid:' + A, f'新的发言{i}')
    stub.outputs.append('不是 markdown')
    try:
        md.rebuild_member(A)
    except md.MemoryDocError as exc:
        assert exc.kind == 'layout'
    else:
        raise AssertionError('invalid output accepted')
    entry = md.get_doc('member', A)
    assert entry['doc'] == before and entry['fail_count'] == 1
    stub.outputs.append(llm_client.LLMExecutionError('timeout'))
    try:
        md.rebuild_member(A)
    except md.MemoryDocError as exc:
        assert exc.kind == 'provider_timeout'
    assert md.get_doc('member', A)['doc'] == before
    print('V3172_FAILURE_KEEPS_PREVIOUS_DOC=PASS')

    # Group doc + manual settings; legacy episodes no longer injected.
    core.group_memory_edit('A群', 'add', text='每周五晚上固定开黑')
    stub.outputs.append(GROUP_OK)
    md.rebuild_group('A群')
    gprompt = stub.prompts[-1][0]
    assert '每周五晚上固定开黑' in gprompt and 'wxid_' not in gprompt
    block = core.group_memory_context('A群', GROUPS[0])
    assert '群内设定' in block and '每周五晚上固定开黑' in block and '原神抽卡' in block, block
    assert '群内片段' not in block
    assert core.group_memory_context('B群', GROUPS[1]) == '' or '原神' not in core.group_memory_context('B群', GROUPS[1])
    print('V3172_GROUP_DOC_WITH_MANUAL_SETTINGS=PASS')

    # Member request: summary returned, other groups' tags stripped, cooldown.
    stub.outputs.append(MEMBER_OK + '\n@@摘要@@\n你是后端程序员，喜欢猫，[G1] 在本群外号卷王')
    _doc, summary = md.rebuild_member(A, current_chat='A群', extra_text='我养了一只橘猫',
                                      want_summary=True, explicit=True, reason='member_request')
    prompt, explicit = stub.prompts[-1]
    assert explicit is True and '我养了一只橘猫' in prompt and md.SUMMARY_MARKER in prompt
    assert summary.startswith('你是后端程序员') and '[G1]' not in summary, summary
    svc = md.service()
    assert svc.explicit_allowed(A) and not svc.explicit_allowed(A)
    assert worker._v3172_profile_request('@机器人 更新一下我的画像') == ''
    assert worker._v3172_profile_request('重新认识一下我') == ''
    assert worker._v3172_profile_request('记住我是做后端的') == '我是做后端的'
    assert worker._v3172_profile_request('记住我们周五开黑') is None
    assert worker._v3172_profile_request('这张画像好看吗') is None
    print('V3172_MEMBER_REQUEST_SUMMARY_COOLDOWN_PARSER=PASS')

    # Owner views, rebuild and clear.
    sent = []
    s = osm.OwnerSettings(send=sent.append)
    svc.run_async = lambda fn, cb: cb(*_call(fn))
    s.handle('/设置 1 群记忆')
    assert '1. 每周五晚上固定开黑' in sent[-1] and '原神抽卡' in sent[-1], sent[-1]
    stub.outputs.append(GROUP_OK.replace('原神抽卡', '星铁抽卡'))
    s.handle('/设置 1 群记忆 重建')
    assert '已重建' in sent[-1] and '星铁抽卡' in sent[-1], sent[-1]
    core.update_group_member_ids('A群', {A, B, BOT})
    core.update_group_member_name_map('A群', {A: '阿尔法', B: '贝塔'})
    s.handle('/设置 1 成员 阿尔法')
    s.handle('/设置 1 成员 #1 画像')
    assert '整理画像（跨群' in sent[-1] and 'B群' in sent[-1] and 'wxid_' not in sent[-1], sent[-1]
    s.handle('/设置 1 成员 #1 画像 清空')
    s.handle('确认')
    assert md.get_doc('member', A) is None
    assert '整理画像' not in core.user_memory_context('A群', 'dbwxid:' + A, 'hi')
    print('V3172_OWNER_VIEW_REBUILD_CLEAR=PASS')


def _call(fn):
    try:
        return fn(), None
    except Exception as exc:
        return None, exc


if __name__ == '__main__':
    main()
