"""Independent observational decision lane. Stdlib only; never sends WeChat."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ENDPOINT = os.environ.get('CW_DECISION_ENDPOINT', 'http://127.0.0.1:8645/v1/decide')
HEALTH_ENDPOINT = os.environ.get(
    'CW_DECISION_HEALTH_ENDPOINT',
    'http://127.0.0.1:8645/healthz',
)
KEY_FILE = Path('/etc/chatwarden/secrets/decision_gateway.key')
CONFIG_FILE = Path(__file__).resolve().parents[1] / 'decision-shadow.json'
CURRENT_DECISIONS = ('followup_v2', 'history_intent_v1', 'media_route_v3')


@dataclass(frozen=True)
class FollowupSnapshot:
    current_message: str
    legacy_context: str
    legacy_sender: str
    recent_context: tuple[tuple[str, str], ...]
    aliases: tuple[tuple[str, str], ...]
    chat_identity: str
    sender_identity: str
    created_at: float


@dataclass(frozen=True)
class HistoryIntentSnapshot:
    current_message: str
    recent_context: tuple[tuple[str, str], ...]
    aliases: tuple[tuple[str, str], ...]
    chat_identity: str
    sender_identity: str
    created_at: float


@dataclass(frozen=True)
class MediaRouteSnapshot:
    current_message: str
    recent_context: tuple[tuple[str, str], ...]
    aliases: tuple[tuple[str, str], ...]
    chat_identity: str
    sender_identity: str
    created_at: float
    visual_available: bool
    visual_source: str
    explicit_imagegen_prefix: bool
    explicit_address: bool


class DecisionClientError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_PRIVATE = re.compile(
    r'wxid_[\w-]+|\d{5,}@chatroom|apikey_[\w-]+|sk-[\w-]{12,}'
    r'|Bearer\s+\S+|\b[0-9a-f]{32,}\b'
    r'|(?:/home/|/opt/|/etc/|/tmp/|/mnt/|[A-Za-z]:[\\/])[^\s，。；]*'
    r'|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', re.I)
_SECRET_ASSIGN = re.compile(
    r'(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[=:]\s*[^\s,，;；]+')


def sanitized_state(snapshot):
    aliases = {}
    for alias, role in snapshot.aliases:
        if alias and alias not in {'bot', 'me', 'unknown', 'other', 'group'}:
            aliases[alias] = role if alias not in aliases or aliases[alias] == role else 'MEMBER'
    pattern = re.compile('|'.join(re.escape(s) for s in sorted(aliases, key=len, reverse=True))) if aliases else None

    def clean(value, limit):
        if pattern:
            value = pattern.sub(lambda match: aliases[match.group()], value)
        value = _SECRET_ASSIGN.sub('[REDACTED]', value)
        value = _PRIVATE.sub('[REDACTED]', value)
        value = re.sub(r'@(?!(?:BOT|CURRENT_SENDER|OTHER_[1-8])\b)[^\s，。:：]+', '@OTHER', value)
        return value[:limit]

    return {'current_message': clean(snapshot.current_message, 4000),
            'current_sender_role': 'CURRENT_SENDER',
            'recent_context': [{'role': role, 'text': clean(text, 600)}
                               for role, text in snapshot.recent_context]}


def sanitized_media_state(snapshot):
    state = sanitized_state(snapshot)
    state['media_context'] = {
        'visual_available': bool(snapshot.visual_available),
        'visual_source': str(snapshot.visual_source),
        'explicit_imagegen_prefix': bool(snapshot.explicit_imagegen_prefix),
        'explicit_address': bool(snapshot.explicit_address),
    }
    return state


def _reference(key, value):
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()[:24]


def _base_telemetry(snapshot, metadata, key):
    telemetry = {
        'chat_ref': _reference(key, snapshot.chat_identity),
        'sender_ref': _reference(key, snapshot.chat_identity + ':' + snapshot.sender_identity),
        'snapshot_created_at': snapshot.created_at,
    }
    if metadata.get('local_id') is not None:
        telemetry['message_ref'] = str(max(0, int(metadata.get('local_id') or 0)))
    return telemetry


def payload_for(snapshot, legacy_result, metadata, key, *, mode='shadow'):
    telemetry = _base_telemetry(snapshot, metadata, key)
    telemetry.update({
        'legacy_result': bool(legacy_result), 'stale': bool(metadata.get('stale')),
        'generation': max(0, int(metadata.get('generation') or 0)),
    })
    return {'request_id': ('probe-' if mode == 'probe' else 'bot-') + secrets.token_hex(16),
            'decision_kind': 'followup_v1', 'mode': mode, 'state': sanitized_state(snapshot),
            'telemetry': telemetry}


def payload_for_followup_v2(snapshot, legacy_result, metadata, key, *, mode='shadow'):
    telemetry = _base_telemetry(snapshot, metadata, key)
    if legacy_result is not None:
        telemetry['legacy_result'] = bool(legacy_result)
    telemetry.update({
        'stale': bool(metadata.get('stale')),
        'generation': max(0, int(metadata.get('generation') or 0)),
    })
    return {'request_id': ('probe-' if mode == 'probe' else 'bot-') + secrets.token_hex(16),
            'decision_kind': 'followup_v2', 'mode': mode, 'state': sanitized_state(snapshot),
            'telemetry': telemetry}


def payload_for_history_intent(snapshot, actual_outcome, metadata, key, *, mode='shadow'):
    telemetry = _base_telemetry(snapshot, metadata, key)
    if actual_outcome is not None:
        telemetry['actual_outcome'] = dict(actual_outcome)
    return {'request_id': ('probe-' if mode == 'probe' else 'bot-') + secrets.token_hex(16),
            'decision_kind': 'history_intent_v1', 'mode': mode,
            'state': sanitized_state(snapshot), 'telemetry': telemetry}


def payload_for_media_route(snapshot, actual_outcome, metadata, key, *, mode='shadow'):
    telemetry = _base_telemetry(snapshot, metadata, key)
    telemetry['actual_outcome'] = dict(actual_outcome)
    return {'request_id': ('probe-' if mode == 'probe' else 'bot-') + secrets.token_hex(16),
            'decision_kind': 'media_route_v1', 'mode': mode,
            'state': sanitized_media_state(snapshot), 'telemetry': telemetry}


def payload_for_media_route_v2(snapshot, actual_outcome, metadata, key, *, mode='shadow'):
    telemetry = _base_telemetry(snapshot, metadata, key)
    telemetry['actual_outcome'] = dict(actual_outcome)
    return {'request_id': ('probe-' if mode == 'probe' else 'bot-') + secrets.token_hex(16),
            'decision_kind': 'media_route_v2', 'mode': mode,
            'state': sanitized_media_state(snapshot), 'telemetry': telemetry}


def payload_for_media_route_v3(snapshot, actual_outcome, metadata, key, *, mode='shadow'):
    telemetry = _base_telemetry(snapshot, metadata, key)
    if actual_outcome is not None:
        telemetry['actual_outcome'] = dict(actual_outcome)
    return {'request_id': ('probe-' if mode == 'probe' else 'bot-') + secrets.token_hex(16),
            'decision_kind': 'media_route_v3', 'mode': mode,
            'state': sanitized_media_state(snapshot), 'telemetry': telemetry}


def validate_response(data, request):
    if not isinstance(data, dict) or data.get('ok') is not True:
        kind = data.get('error_kind') if isinstance(data, dict) else None
        known = {'auth_failed', 'invalid_request', 'unknown_decision_kind', 'upstream_timeout',
                 'upstream_auth', 'upstream_rate_limit', 'upstream_error', 'model_unavailable',
                 'response_invalid', 'busy', 'shutdown'}
        raise DecisionClientError(kind if kind in known else 'response_invalid')
    kind = request.get('decision_kind')
    contracts = {
        'followup_v1': (1, {'addressed'}),
        'followup_v2': (2, {'addressed_to_bot', 'needs_reply'}),
        'history_intent_v1': (1, {'needs_persisted_history', 'wants_summary',
                                  'wants_targeted_lookup', 'wants_count',
                                  'wants_previous_window'}),
        'media_route_v1': (1, {'wants_generate_new_image', 'wants_edit_existing_image',
                               'wants_visual_understanding', 'wants_text_only'}),
        'media_route_v2': (2, {'wants_generate_new_image', 'wants_edit_existing_image',
                               'wants_visual_understanding', 'wants_text_only'}),
        'media_route_v3': (3, {'wants_generate_new_image', 'wants_edit_existing_image',
                               'wants_visual_understanding', 'wants_text_only'}),
    }
    contract = contracts.get(kind)
    if contract is None:
        raise DecisionClientError('response_invalid')
    version, expected_answers = contract
    if (data.get('request_id') != request['request_id'] or data.get('decision_kind') != kind
            or data.get('decision_version') != version or data.get('mode') != request['mode']
            or not isinstance(data.get('model'), str) or not data['model']):
        raise DecisionClientError('response_invalid')
    answers = data.get('answers')
    if not isinstance(answers, dict) or set(answers) != expected_answers:
        raise DecisionClientError('response_invalid')
    for answer in answers.values():
        if (not isinstance(answer, dict) or set(answer) != {'type', 'probability'}
                or answer['type'] != 'noul'):
            raise DecisionClientError('response_invalid')
        p = answer['probability']
        if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
            raise DecisionClientError('response_invalid')
    if request.get('mode') == 'active':
        judged = data.get('judged_outcome')
        if not isinstance(judged, dict) or len(judged) != 1:
            raise DecisionClientError('response_invalid')
        if kind == 'followup_v2':
            if judged.get('action') not in {'reply', 'silent', 'host_fallback'}:
                raise DecisionClientError('response_invalid')
        elif kind == 'history_intent_v1':
            if judged.get('route') not in {
                'none', 'summary', 'targeted_lookup', 'count_lookup',
                'previous_window', 'host_fallback',
            }:
                raise DecisionClientError('response_invalid')
        elif kind == 'media_route_v3':
            if judged.get('route') not in {
                'imagegen', 'image_edit', 'vision', 'text', 'host_fallback',
            }:
                raise DecisionClientError('response_invalid')
        else:
            raise DecisionClientError('response_invalid')
    return data


def execute(payload, key, endpoint=ENDPOINT):
    parts = urlsplit(endpoint)
    if (parts.scheme != 'http' or parts.hostname not in {'127.0.0.1', '127.0.0.1'}
            or parts.path != '/v1/decide' or parts.username or parts.password or parts.query or parts.fragment):
        raise DecisionClientError('invalid_endpoint')
    request = urllib.request.Request(endpoint, data=json.dumps(payload, allow_nan=False).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        try:
            response = opener.open(request, timeout=9)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read(16385)
        if len(raw) > 16384:
            raise DecisionClientError('response_invalid')
        return validate_response(json.loads(raw), payload)
    except DecisionClientError:
        raise
    except Exception:
        raise DecisionClientError('transport_error') from None


def discover_modes(endpoint=HEALTH_ENDPOINT):
    parts = urlsplit(endpoint)
    if (parts.scheme != 'http' or parts.hostname not in {'127.0.0.1', '127.0.0.1'}
            or parts.path != '/healthz' or parts.username or parts.password
            or parts.query or parts.fragment):
        raise DecisionClientError('invalid_endpoint')
    request = urllib.request.Request(endpoint, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=2) as response:
            raw = response.read(16385)
        if len(raw) > 16384:
            raise DecisionClientError('response_invalid')
        data = json.loads(raw)
    except DecisionClientError:
        raise
    except Exception:
        raise DecisionClientError('transport_error') from None
    modes = data.get('decision_modes') if isinstance(data, dict) else None
    if data.get('ok') is not True or not isinstance(modes, dict):
        raise DecisionClientError('response_invalid')
    out = {}
    for kind in CURRENT_DECISIONS:
        value = modes.get(kind)
        if value not in {'off', 'shadow', 'active'}:
            raise DecisionClientError('response_invalid')
        out[kind] = value
    return out


class ShadowLane:
    """One global bounded queue; no callback into the legacy gate or sender."""
    def __init__(self, key, *, send=execute, capacity=16, workers=2):
        self.key, self.send = key, send
        self.queue = queue.Queue(maxsize=capacity)
        self.stop = threading.Event()
        self.threads = []
        self.completed = self.errors = self.dropped = 0
        for i in range(workers):
            thread = threading.Thread(target=self._run, name=f'wechat-decision-shadow-{i}', daemon=True)
            thread.start()
            self.threads.append(thread)

    def submit(self, snapshot, legacy_result, metadata):
        try:
            self.queue.put_nowait(('followup_v2', snapshot, bool(legacy_result), dict(metadata)))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def submit_history_intent(self, snapshot, actual_outcome, metadata):
        try:
            self.queue.put_nowait(('history_intent_v1', snapshot, dict(actual_outcome), dict(metadata)))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def submit_media_route(self, snapshot, actual_outcome, metadata):
        try:
            self.queue.put_nowait(('media_route_v3', snapshot, dict(actual_outcome), dict(metadata)))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _run(self):
        while not self.stop.is_set():
            try:
                kind, snapshot, outcome, metadata = self.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            rid = None
            try:
                if kind == 'followup_v1':
                    payload = payload_for(snapshot, outcome, metadata, self.key)
                    behavior = 'legacy'
                elif kind == 'followup_v2':
                    payload = payload_for_followup_v2(snapshot, outcome, metadata, self.key)
                    behavior = 'legacy'
                elif kind == 'history_intent_v1':
                    payload = payload_for_history_intent(snapshot, outcome, metadata, self.key)
                    behavior = 'history-legacy'
                elif kind == 'media_route_v1':
                    payload = payload_for_media_route(snapshot, outcome, metadata, self.key)
                    behavior = 'media-legacy'
                elif kind == 'media_route_v2':
                    payload = payload_for_media_route_v2(snapshot, outcome, metadata, self.key)
                    behavior = 'media-legacy'
                elif kind == 'media_route_v3':
                    payload = payload_for_media_route_v3(snapshot, outcome, metadata, self.key)
                    behavior = 'media-legacy'
                else:
                    raise DecisionClientError('unknown_decision_kind')
                rid = payload['request_id']
                response = self.send(payload, self.key)
                if response.get('telemetry_recorded') is not True:
                    raise DecisionClientError('telemetry_unavailable')
                self.completed += 1
                print(f'[v3.163-decision-shadow] kind={kind} request_id={rid} '
                      f'recorded=yes mode=shadow behavior={behavior}', flush=True)
            except Exception as exc:
                self.errors += 1
                error_kind = str(exc) if isinstance(exc, DecisionClientError) else type(exc).__name__
                print(f'[v3.163-decision-shadow] kind={kind} request_id={rid} '
                      f'error={error_kind} behavior=unchanged', flush=True)
            finally:
                self.queue.task_done()


_LANE = None
_KEY = None
_MODES = {}
_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def initialize():
    global _LANE, _KEY, _MODES, _INITIALIZED
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        _INITIALIZED = True
        try:
            override = os.environ.get('CW_DECISION_SHADOW')
            enabled = override == '1' if override is not None else json.loads(CONFIG_FILE.read_text()).get('enabled') is True
            if not enabled:
                return
            key = KEY_FILE.read_text().strip()
            if len(key) < 32:
                raise ValueError('invalid_client_key')
            modes = discover_modes()
            _KEY = key
            _MODES = modes
            if any(value == 'shadow' for value in modes.values()):
                _LANE = ShadowLane(key)
            rendered = '+'.join(
                f'{kind}:{modes[kind]}' for kind in CURRENT_DECISIONS
            )
            print('[v3.167-decision-decision] enabled=yes '
                  f'decisions={rendered} '
                  f'shadow_workers={2 if _LANE is not None else 0} '
                  'fallback=legacy', flush=True)
        except FileNotFoundError:
            print('[v3.167-decision-decision] enabled=no reason=config-or-key-missing '
                  'behavior=legacy', flush=True)
        except Exception as exc:
            _KEY = None
            _MODES = {}
            print(f'[v3.167-decision-decision] enabled=no error={type(exc).__name__} '
                  'behavior=legacy', flush=True)


def mode(kind):
    return _MODES.get(str(kind or ''), 'off')


def enabled(kind=None):
    if _KEY is None:
        return False
    if kind is None:
        return any(value in {'shadow', 'active'} for value in _MODES.values())
    return mode(kind) in {'shadow', 'active'}


def _active_decide(kind, snapshot, metadata=None):
    if _KEY is None or snapshot is None or mode(kind) != 'active':
        return None
    metadata = metadata or {}
    if kind == 'followup_v2':
        payload = payload_for_followup_v2(
            snapshot, None, metadata, _KEY, mode='active',
        )
        field = 'action'
    elif kind == 'history_intent_v1':
        payload = payload_for_history_intent(
            snapshot, None, metadata, _KEY, mode='active',
        )
        field = 'route'
    elif kind == 'media_route_v3':
        payload = payload_for_media_route_v3(
            snapshot, None, metadata, _KEY, mode='active',
        )
        field = 'route'
    else:
        raise DecisionClientError('unknown_decision_kind')
    response = execute(payload, _KEY)
    if response.get('telemetry_recorded') is not True:
        raise DecisionClientError('telemetry_unavailable')
    judged = response.get('judged_outcome') or {}
    result = judged.get(field)
    print(f'[v3.167-decision-active] kind={kind} request_id={payload["request_id"]} '
          f'judged={result} fallback=no', flush=True)
    return result


def decide_followup(snapshot, metadata=None):
    return _active_decide('followup_v2', snapshot, metadata)


def decide_history_intent(snapshot, metadata=None):
    return _active_decide('history_intent_v1', snapshot, metadata)


def decide_media_route(snapshot, metadata=None):
    return _active_decide('media_route_v3', snapshot, metadata)


def submit(snapshot, legacy_result, metadata):
    if _LANE is None or snapshot is None or mode('followup_v2') != 'shadow':
        return False
    return _LANE.submit(snapshot, legacy_result, metadata)


def submit_history_intent(snapshot, actual_outcome, metadata=None):
    if _LANE is None or snapshot is None or mode('history_intent_v1') != 'shadow':
        return False
    return _LANE.submit_history_intent(snapshot, actual_outcome, metadata or {})


def submit_media_route(snapshot, actual_outcome, metadata=None):
    if _LANE is None or snapshot is None or mode('media_route_v3') != 'shadow':
        return False
    return _LANE.submit_media_route(snapshot, actual_outcome, metadata or {})
