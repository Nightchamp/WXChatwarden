#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
from pathlib import Path


def project_root():
    here = Path.cwd()
    if (here / 'bot-data').is_dir():
        return here
    return Path('/opt')


def default_paths():
    if Path('/var/lib/chatwarden').is_dir():
        return Path('/var/lib/chatwarden/groups.json'), Path('/var/lib/chatwarden/chat.lock')
    root = project_root()
    return root / 'bot-data/groups.json', root / 'bot-data/chat.lock'


GROUPS_FILE, CHAT_LOCK = default_paths()


def session_key_for_chat(chat_name):
    digest = hashlib.sha256(chat_name.encode('utf-8')).hexdigest()[:20]
    return f'wechat:group:{digest}'


def seed_config():
    if GROUPS_FILE.exists():
        return
    legacy = ''
    if CHAT_LOCK.exists():
        legacy = CHAT_LOCK.read_text(
            encoding='utf-8', errors='replace'
        ).strip()
    if not legacy:
        raise SystemExit(
            f'ERROR: {GROUPS_FILE} does not exist and no legacy chat.lock target was found'
        )
    session = (
        'wechat:group:jarvis'
        if legacy == '小卫士'
        else session_key_for_chat(legacy)
    )
    save({
        'version': 1,
        'groups': [{
            'name': legacy,
            'enabled': True,
            'session_key': session,
            'personality': '',
            'member_labels': {},
        }],
    })


def load():
    seed_config()
    data = json.loads(GROUPS_FILE.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('groups'), list):
        raise SystemExit('ERROR: invalid groups.json')
    return data


def save(data):
    GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = GROUPS_FILE.with_suffix('.json.tmp')
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    os.chmod(tmp, 0o600)
    tmp.replace(GROUPS_FILE)
    os.chmod(GROUPS_FILE, 0o600)


def find_group(data, name):
    for item in data['groups']:
        if str(item.get('name') or '').strip() == name:
            return item
    return None


def cmd_list(args):
    data = load()
    if not data['groups']:
        print('(no groups)')
        return
    for item in data['groups']:
        state = 'enabled' if item.get('enabled', True) else 'disabled'
        session = item.get('session_key') or session_key_for_chat(item['name'])
        labels = item.get('member_labels') or {}
        personality = str(item.get('personality') or '').strip()
        print(f"{state:8}  {item['name']}  session={session}")
        if personality:
            preview = personality.replace('\n', ' ')
            if len(preview) > 100:
                preview = preview[:100] + '…'
            print(f'          personality: {preview}')
        for person_id, label in sorted(labels.items()):
            print(f'          {person_id} -> {label}')


def cmd_add(args):
    data = load()
    if find_group(data, args.name):
        raise SystemExit(f'ERROR: group already exists: {args.name}')
    session = args.session or session_key_for_chat(args.name)
    try:
        session.encode('ascii')
    except UnicodeEncodeError:
        raise SystemExit('ERROR: --session must contain ASCII characters only')
    personality = (args.personality or '').strip()
    if len(personality) > 2000:
        raise SystemExit('ERROR: personality must be <= 2000 characters')

    data['groups'].append({
        'name': args.name,
        'enabled': True,
        'session_key': session,
        'personality': personality,
        'member_labels': {},
    })
    save(data)
    print(f'Added group: {args.name}')
    print(f'Session: {session}')
    print('The running v4 bot will reload groups.json automatically.')


def cmd_remove(args):
    data = load()
    before = len(data['groups'])
    data['groups'] = [
        g for g in data['groups']
        if str(g.get('name') or '').strip() != args.name
    ]
    if len(data['groups']) == before:
        raise SystemExit(f'ERROR: group not found: {args.name}')
    if not data['groups']:
        raise SystemExit('ERROR: refusing to remove the last group')
    save(data)
    print(f'Removed group: {args.name}')


def set_enabled(name, enabled):
    data = load()
    group = find_group(data, name)
    if not group:
        raise SystemExit(f'ERROR: group not found: {name}')
    group['enabled'] = enabled
    save(data)
    print(f'{"Enabled" if enabled else "Disabled"} group: {name}')


def cmd_enable(args):
    set_enabled(args.name, True)


def cmd_disable(args):
    set_enabled(args.name, False)


def cmd_label(args):
    data = load()
    group = find_group(data, args.group)
    if not group:
        raise SystemExit(f'ERROR: group not found: {args.group}')
    labels = group.setdefault('member_labels', {})
    labels[args.person_id] = args.label
    save(data)
    print(
        f"Group {args.group}: {args.person_id} -> {args.label}"
    )


def cmd_unlabel(args):
    data = load()
    group = find_group(data, args.group)
    if not group:
        raise SystemExit(f'ERROR: group not found: {args.group}')
    labels = group.setdefault('member_labels', {})
    if args.person_id not in labels:
        raise SystemExit(
            f'ERROR: no label for {args.person_id} in {args.group}'
        )
    del labels[args.person_id]
    save(data)
    print(
        f"Removed group label: {args.group} / {args.person_id}"
    )


def cmd_personality(args):
    data = load()
    group = find_group(data, args.group)
    if not group:
        raise SystemExit(f'ERROR: group not found: {args.group}')

    value = args.text.strip()
    if len(value) > 2000:
        raise SystemExit('ERROR: personality must be <= 2000 characters')

    group['personality'] = value
    save(data)
    if value:
        print(f'Personality updated: {args.group}')
        print(value)
    else:
        print(f'Personality cleared: {args.group}')


def cmd_clear_personality(args):
    data = load()
    group = find_group(data, args.group)
    if not group:
        raise SystemExit(f'ERROR: group not found: {args.group}')
    group['personality'] = ''
    save(data)
    print(f'Personality cleared: {args.group}')


def cmd_show(args):
    data = load()
    group = find_group(data, args.name)
    if not group:
        raise SystemExit(f'ERROR: group not found: {args.name}')
    print(json.dumps(group, ensure_ascii=False, indent=2))


def build_parser():
    p = argparse.ArgumentParser(
        description='Manage multi-group WeChat bridge configuration'
    )
    sub = p.add_subparsers(dest='command', required=True)

    s = sub.add_parser('list')
    s.set_defaults(func=cmd_list)

    s = sub.add_parser('show')
    s.add_argument('name')
    s.set_defaults(func=cmd_show)

    s = sub.add_parser('add')
    s.add_argument('name')
    s.add_argument('--session')
    s.add_argument(
        '--personality',
        default='',
        help='per-group personality/instructions (max 2000 chars)',
    )
    s.set_defaults(func=cmd_add)

    s = sub.add_parser('remove')
    s.add_argument('name')
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser('enable')
    s.add_argument('name')
    s.set_defaults(func=cmd_enable)

    s = sub.add_parser('disable')
    s.add_argument('name')
    s.set_defaults(func=cmd_disable)

    s = sub.add_parser('personality')
    s.add_argument('group')
    s.add_argument('text')
    s.set_defaults(func=cmd_personality)

    s = sub.add_parser('clear-personality')
    s.add_argument('group')
    s.set_defaults(func=cmd_clear_personality)

    s = sub.add_parser('label')
    s.add_argument('group')
    s.add_argument('person_id')
    s.add_argument('label')
    s.set_defaults(func=cmd_label)

    s = sub.add_parser('unlabel')
    s.add_argument('group')
    s.add_argument('person_id')
    s.set_defaults(func=cmd_unlabel)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
