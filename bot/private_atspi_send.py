#!/usr/bin/env python3
import subprocess
import sys
import time

import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi

# db-native-v3.11.4-private-fast-top-level-frame
CHAT = sys.argv[1] if len(sys.argv) > 1 else ''
TEXT = sys.stdin.buffer.read().decode('utf-8')

if not CHAT:
    raise SystemExit('missing private chat name')


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def role(acc):
    return ((safe(acc.get_role_name, '') or '').strip().lower().replace('_', '-').replace(' ', '-'))


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


def xdotool(args, check=True):
    return subprocess.run(['xdotool', *args], capture_output=True, text=True, timeout=5, check=check)


def exact_visible_windows(title):
    r = xdotool(['search', '--onlyvisible', '--name', '.*'], check=False)
    if r.returncode not in (0, 1):
        raise SystemExit(f'refusing private send: xdotool search failed rc={r.returncode}')
    out = []
    for wid in r.stdout.split():
        n = xdotool(['getwindowname', wid], check=False)
        if n.returncode == 0 and n.stdout.rstrip('\r\n') == title:
            out.append(wid)
    return out


def active_window_id():
    r = xdotool(['getactivewindow'])
    raw = r.stdout.strip()
    if not raw:
        raise SystemExit('refusing private send: empty active window id')
    return int(raw, 0)


def editor_text(editor):
    iface = safe(editor.get_text_iface)
    if iface is None:
        return None
    count = safe(iface.get_character_count)
    if count is None:
        return None
    count = int(count)
    if count <= 0:
        return ''
    return safe(lambda: iface.get_text(0, count))


Atspi.init()
desktop = Atspi.get_desktop(0)

frames = []
# db-native-v3.11.4-private-fast-top-level-frame
# Top-level WeChat chat windows are direct children of an application
# accessible. Do not recursively traverse the entire desktop tree here:
# that costs ~17s on the current production UI. Exact title/role
# uniqueness is still required and all v3.11.3 X11/focus/send-confirm
# checks remain unchanged.
for app in children(desktop):
    for acc in children(app):
        if role(acc) == 'frame' and name(acc).strip() == CHAT:
            frames.append(acc)

if not frames:
    print(f'PRIVATE_FRAME_NOT_FOUND chat={CHAT!r}', file=sys.stderr)
    raise SystemExit(42)
if len(frames) != 1:
    raise SystemExit(f'refusing private send: exact frame count={len(frames)} for {CHAT!r}')

wins = exact_visible_windows(CHAT)
if len(wins) != 1:
    raise SystemExit(f'refusing private send: exact visible X11 window count={len(wins)} for {CHAT!r}')
target_raw = wins[0]
target = int(target_raw, 0)
xdotool(['windowactivate', '--sync', target_raw])
time.sleep(0.08)
if active_window_id() != target:
    raise SystemExit(f'refusing private send: exact X11 window not active for {CHAT!r}')

frame = frames[0]
editors = []
for acc in walk(frame):
    if role(acc) not in ('text', 'entry'):
        continue
    interfaces = [str(x) for x in (safe(acc.get_interfaces, []) or [])]
    if 'EditableText' in interfaces:
        editors.append(acc)
if len(editors) != 1:
    raise SystemExit(f'refusing private send: editable input count={len(editors)} for {CHAT!r}')

editor = editors[0]
component = safe(editor.get_component_iface)
if component is None:
    raise SystemExit(f'refusing private send: Component unavailable for {CHAT!r}')
if safe(component.grab_focus, False) is False:
    raise SystemExit(f'refusing private send: grab_focus failed for {CHAT!r}')

focused = False
deadline = time.monotonic() + 1.0
while time.monotonic() < deadline:
    states = safe(editor.get_state_set)
    if states is not None and safe(lambda: states.contains(Atspi.StateType.FOCUSED), False):
        focused = True
        break
    time.sleep(0.05)
if not focused:
    raise SystemExit(f'refusing private send: editor focus not confirmed for {CHAT!r}')
if active_window_id() != target:
    raise SystemExit(f'refusing private send: X11 focus changed before input for {CHAT!r}')

editable = safe(editor.get_editable_text_iface)
if editable is None:
    raise SystemExit(f'refusing private send: EditableText unavailable for {CHAT!r}')
ok = editable.set_text_contents(TEXT)
if ok is False:
    raise SystemExit(f'refusing private send: set_text_contents failed for {CHAT!r}')

observed = editor_text(editor)
if observed is None:
    raise SystemExit(f'refusing private send: editor text unavailable before send for {CHAT!r}')
if observed != TEXT:
    raise SystemExit(f'refusing private send: editor text mismatch before send for {CHAT!r}')

time.sleep(0.10)
if active_window_id() != target:
    raise SystemExit(f'refusing private send: X11 focus changed before Return for {CHAT!r}')

r = subprocess.run(['/opt/tools/key', 'Return'], capture_output=True, timeout=5)
if r.returncode != 0:
    err = r.stderr.decode('utf-8', errors='replace').strip()
    raise SystemExit(f'private Return key failed for {CHAT!r}: {err[:300]}')

# Never press Return a second time. Successful submission must clear the editor.
deadline = time.monotonic() + 3.0
last = None
while time.monotonic() < deadline:
    last = editor_text(editor)
    if last == '':
        print(f'PRIVATE_SEND_CONFIRMED chat={CHAT!r} editor_empty=yes', file=sys.stderr)
        raise SystemExit(0)
    time.sleep(0.10)

if last is None:
    detail = 'editor_text_unavailable'
else:
    detail = f'editor_not_empty chars={len(last)}'
print(f'PRIVATE_SEND_UNCONFIRMED chat={CHAT!r} {detail}', file=sys.stderr)
raise SystemExit(43)
