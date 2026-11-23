#!/usr/bin/env python3
import subprocess
import sys
import time

import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi


GROUP = sys.argv[1] if len(sys.argv) > 1 else ''
TEXT = sys.stdin.buffer.read().decode('utf-8')

if not GROUP:
    raise SystemExit('missing group name')


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def role(acc):
    return (
        (safe(acc.get_role_name, '') or '')
        .strip()
        .lower()
        .replace('_', '-')
        .replace(' ', '-')
    )


def name(acc):
    return safe(acc.get_name, '') or ''


def children(acc):
    n = safe(acc.get_child_count, 0) or 0
    for i in range(n):
        child = safe(lambda i=i: acc.get_child_at_index(i))
        if child is not None:
            yield child


def walk(acc, depth=0, max_depth=40):
    if depth > max_depth:
        return
    yield acc
    for child in children(acc):
        yield from walk(child, depth + 1, max_depth)


Atspi.init()
desktop = Atspi.get_desktop(0)

frames = []
# db-native-v3.173-group-fast-top-level-frame
# Same fast path as v3.11.4 private text send and the image sender: WeChat
# chat windows are top-level frames directly under an application accessible.
# The former full desktop walk (depth 12, including every open chat message
# list) cost ~4.9s per group send on the production UI. Exact title
# uniqueness / ACTIVE disambiguation below is unchanged; the full walk stays
# only as a fallback when no top-level frame matches.
for app in children(desktop):
    for acc in children(app):
        if role(acc) == 'frame' and name(acc).strip() == GROUP:
            frames.append(acc)
if not frames:
    for acc in walk(desktop, max_depth=12):
        if role(acc) == 'frame' and name(acc).strip() == GROUP:
            frames.append(acc)

if not frames:
    raise SystemExit(
        f'refusing to send: AT-SPI frame not found for {GROUP!r}'
    )

if len(frames) == 1:
    frame = frames[0]
else:
    active = []
    for candidate in frames:
        states = safe(candidate.get_state_set)
        if states is not None and safe(
            lambda states=states: states.contains(Atspi.StateType.ACTIVE),
            False,
        ):
            active.append(candidate)
    if len(active) != 1:
        raise SystemExit(
            f'refusing to send: exact frame count={len(frames)} '
            f'active_count={len(active)} for {GROUP!r}'
        )
    frame = active[0]

editors = []
for acc in walk(frame):
    if role(acc) not in ('text', 'entry'):
        continue
    interfaces = [
        str(x) for x in (safe(acc.get_interfaces, []) or [])
    ]
    if 'EditableText' in interfaces:
        editors.append(acc)

if len(editors) != 1:
    raise SystemExit(
        f'refusing to send: editable input count={len(editors)} '
        f'for {GROUP!r}'
    )

editor = editors[0]

# Focus the exact editor through AT-SPI if its Action interface exposes
# SetFocus. This is not coordinate based.
action = safe(editor.get_action_iface)
if action is not None:
    n_actions = safe(action.get_n_actions, 0) or 0
    for i in range(n_actions):
        action_name = safe(
            lambda i=i: action.get_action_name(i), ''
        ) or ''
        if action_name.lower() == 'setfocus':
            safe(lambda i=i: action.do_action(i), False)
            break

editable = safe(editor.get_editable_text_iface)
if editable is None:
    raise SystemExit(
        f'refusing to send: EditableText unavailable for {GROUP!r}'
    )

ok = editable.set_text_contents(TEXT)
if ok is False:
    raise SystemExit(
        f'refusing to send: set_text_contents failed for {GROUP!r}'
    )

time.sleep(0.04)

# WeChat's Send(S) accessible object exposes SetFocus but no press action,
# so Return is the only synthetic key left in the send path.
result = subprocess.run(
    ['/opt/tools/key', 'Return'],
    capture_output=True,
    timeout=5,
)

if result.returncode != 0:
    err = result.stderr.decode('utf-8', errors='replace').strip()
    raise SystemExit(
        f'Return key failed for {GROUP!r}: {err[:300]}'
    )
