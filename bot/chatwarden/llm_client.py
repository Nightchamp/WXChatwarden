"""Private task transport. Provider selection stays behind the 8643 gateway."""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.error
import urllib.request


ENDPOINT = os.environ.get('CW_LLM_ENDPOINT', 'http://127.0.0.1:8643/v1/execute')
KEY_FILE = Path('/etc/chatwarden/secrets/gateway.key')
# v3.176: a primary turn may run 0.7x budget + 45s wrap-up grace, then one
# bounded (>=30s) provider fallback; client deadlines cover that worst case.
TIMEOUT = 130  # 60s formal: 42s soft + 45s grace + 30s fallback + headroom.
VISION_TIMEOUT = 140  # 75s vision: 52.5s soft + 45s grace + 30s fallback + headroom.
MAX_RESPONSE = 2 * 1024 * 1024
_EPOCH = secrets.token_hex(8)
_ACTIVE = 0
_LOCK = threading.Lock()
SESSION_TURN_LIMIT = max(
    2,
    min(6, int(os.environ.get('CW_LLM_SESSION_TURNS', '3'))),
)
AUX_TIMEOUTS = {
    # v3.195 classifier 5->14s (gateway 10s); v3.196 owner_intent 16->26s (gateway 14s primary + fallback to 24s)
    'classifier': 14, 'owner_intent': 26,
    'history_metadata': 30, 'personality': 75,
    'personality_random': 75, 'welcome': 55, 'proactive': 90, 'history_summary': 140,
    'voice_transcript': 65, 'settings_intent': 60,
}
AUX_KEYS = {
    'classifier': {'addressed'}, 'owner_intent': {'task'},
    'history_metadata': {'mode', 'start', 'end'},
    'personality': {'personality'}, 'personality_random': {'personality', 'summary'},
    'welcome': {'text'}, 'proactive': {'decision', 'draft', 'reason'},
    'history_summary': {'summary'},
    'voice_transcript': {'speech_detected', 'transcript'},
    'settings_intent': {'commands', 'reply'},
}

_INTERNAL_PROTOCOL_FIELD_RE = re.compile(
    r'"(?:toolAction|toolSummary)"\s*:',
    re.I,
)


# Gateway error kinds are fixed snake_case identifiers from its own code.
_GATEWAY_KIND_RE = re.compile(r'[a-z][a-z0-9_]{0,47}')


class LLMExecutionError(RuntimeError):
    def __init__(self, kind):
        self.kind = kind
        super().__init__('gateway task failed: ' + kind)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def busy():
    with _LOCK:
        return _ACTIVE > 0


def execute_formal(group_cfg, prompt, *, explicit=True):
    """One HTTP attempt; the exact pinned group owns the disposable session."""
    chat = str(group_cfg.get('chat_username') or '').strip()
    if not chat.endswith('@chatroom') or not chat[:-9].isdigit():
        raise LLMExecutionError('unpinned_group')
    return _execute_text('formal', chat, prompt, explicit=explicit)


def execute_private(private_cfg, prompt, *, explicit=True):
    """A private task is bound to the resolved peer, never its display name."""
    peer = str(private_cfg.get('chat_username') or '')
    if (not re.fullmatch(r'wxid_[A-Za-z0-9_-]{1,100}', peer)
            or private_cfg.get('name') != 'private:' + peer):
        raise LLMExecutionError('invalid_private_identity')
    return _execute_text('private', 'private:' + peer, prompt, explicit=explicit)


def _execute_text(task_kind, scope, prompt, *, explicit):
    request_id = 'bot-' + secrets.token_hex(16)
    payload = {
        'task_kind': task_kind, 'request_id': request_id,
        'session_key': 'chatwarden:' + _EPOCH + ':' + scope,
        'prompt': prompt, 'explicit': bool(explicit),
    }
    return _execute(payload, TIMEOUT, structured=False)


def validate_auxiliary(task_kind, data):
    if not isinstance(data, dict) or set(data) != AUX_KEYS[task_kind]:
        return False
    if task_kind == 'classifier':
        return type(data['addressed']) is bool
    if task_kind == 'owner_intent':
        return type(data['task']) is bool
    if task_kind == 'voice_transcript':
        speech = data.get('speech_detected')
        transcript = data.get('transcript')
        if type(speech) is not bool or not isinstance(transcript, str):
            return False
        if len(transcript) > 20000 or '<think>' in transcript.lower() or '/opt/chatwarden/gateway' in transcript:
            return False
        return bool(transcript.strip()) if speech else not transcript.strip()
    if not all(isinstance(v, str) for v in data.values()):
        return False
    if task_kind == 'settings_intent':
        return (len(data['commands']) <= 3000 and bool(data['reply'].strip())
                and len(data['reply']) <= 600
                and not any('<think>' in v.lower() or '/opt/chatwarden/gateway' in v
                            for v in data.values()))
    if any('<think>' in v.lower() or '/opt/chatwarden/gateway' in v for v in data.values()):
        return False
    if task_kind == 'history_metadata':
        if data['mode'] not in {'none', 'window', 'previous_window', 'all_history'}:
            return False
        return (bool(data['start'].strip() and data['end'].strip())
                if data['mode'] == 'window' else data['start'] == data['end'] == '')
    if task_kind == 'proactive':
        return bool(data['reason'].strip()) and (
            data['decision'] == 'SILENT' and data['draft'] == ''
            or data['decision'] == 'SPEAK' and bool(data['draft'].strip()))
    bounds = {'personality': 1800, 'summary': 60000 if task_kind == 'history_summary' else 180, 'text': 240}
    return all(v.strip() and len(v) <= bounds[k]
               and not v.lstrip().startswith(('```', '[开始生图]', '[开始调查]'))
               for k, v in data.items())


def _image_media_payload(image_path):
    """Read one exact Bot-selected image into the Gateway media envelope."""
    try:
        with Path(image_path).open('rb') as source:
            raw = source.read(12 * 1024 * 1024 + 1)
        if not 0 < len(raw) <= 12 * 1024 * 1024:
            raise ValueError
        if raw.startswith(b'\xff\xd8\xff'):
            mime = 'image/jpeg'
        elif raw.startswith(b'\x89PNG\r\n\x1a\n'):
            mime = 'image/png'
        elif raw.startswith((b'GIF87a', b'GIF89a')):
            mime = 'image/gif'
        elif raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':
            mime = 'image/webp'
        else:
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise LLMExecutionError('invalid_selected_image') from None
    return {
        'mime_type': mime,
        'data_base64': base64.b64encode(raw).decode('ascii'),
    }


def execute_auxiliary(task_kind, prompt, *, explicit=False, image_path=None):
    """Sessionless machine task. The task name alone fixes schema and deadline."""
    if task_kind not in AUX_TIMEOUTS:
        raise LLMExecutionError('unsupported_auxiliary_task')
    payload = {'task_kind': task_kind, 'request_id': 'bot-' + secrets.token_hex(16),
               'prompt': prompt, 'explicit': bool(explicit)}
    if image_path is not None:
        if task_kind != 'proactive':
            raise LLMExecutionError('unsupported_auxiliary_media')
        payload['media'] = _image_media_payload(image_path)
    return _execute(payload, AUX_TIMEOUTS[task_kind], structured=True)


def _execute_vision_kind(task_kind, prompt, image_path, timeout):
    """Upload only the exact image already resolved by Bot Core."""
    payload = {'task_kind': task_kind, 'request_id': 'bot-' + secrets.token_hex(16),
               'prompt': prompt, 'explicit': True,
               'media': _image_media_payload(image_path)}
    return _execute(payload, timeout, structured=False)


def execute_vision(prompt, image_path):
    """Primary exact-image Vision attempt with the production 90s deadline."""
    return _execute_vision_kind('vision', prompt, image_path, VISION_TIMEOUT)


def execute_voice_transcript(audio_path):
    """Upload only Bot Core's exact decoded WAV; the model performs the ASR."""
    try:
        with Path(audio_path).open('rb') as source:
            raw = source.read(8 * 1024 * 1024 + 1)
        if (
            not 0 < len(raw) <= 8 * 1024 * 1024
            or not raw.startswith(b'RIFF')
            or raw[8:12] != b'WAVE'
        ):
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise LLMExecutionError('invalid_selected_audio') from None
    payload = {
        'task_kind': 'voice_transcript',
        'request_id': 'bot-' + secrets.token_hex(16),
        'prompt': (
            'Listen to the exact selected WeChat voice message. Return a faithful transcript '
            'in the original spoken language, preserving names, numbers and wording when audible. '
            'Do not use web search and do not infer missing words from surrounding text. '
            'If there is no intelligible speech, set speech_detected=false and transcript to an empty string. '
            'Otherwise set speech_detected=true and transcript to only the spoken content.'
        ),
        'explicit': True,
        'media': {
            'mime_type': 'audio/wav',
            'data_base64': base64.b64encode(raw).decode('ascii'),
        },
    }
    return _execute(payload, 65, structured=True)


def execute_research(prompt):
    """Independent high-effort long task; never reuse a formal conversation."""
    payload = {'task_kind': 'research', 'request_id': 'bot-' + secrets.token_hex(16),
               'prompt': prompt, 'explicit': True}
    return _execute(payload, 320, structured=False)


def _read_key(path, min_len, error_kind):
    """Read a bearer key file; fail closed with ``error_kind``."""
    try:
        key = path.read_text(encoding='utf-8').strip()
        if len(key) < min_len:
            raise ValueError
    except (OSError, ValueError):
        raise LLMExecutionError(error_kind) from None
    return key


def _execute(payload, timeout, *, structured):
    global _ACTIVE
    request_id, task_kind = payload['request_id'], payload['task_kind']
    key = _read_key(KEY_FILE, 32, 'client_key_unavailable')
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key},
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with _LOCK:
        _ACTIVE += 1
    try:
        try:
            with opener.open(request, timeout=timeout) as response:
                if response.status != 200:
                    raise LLMExecutionError('upstream_http')
                body = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            # Never propagate an upstream body, credential or traceback to chat.
            kind = {401: 'unauthorized', 409: 'duplicate_request', 503: 'unavailable',
                    504: 'timeout'}.get(exc.code, 'upstream_http')
            # Only this bounded machine error permits proactive's single format
            # regeneration. Never surface/log the raw upstream body.
            try:
                error = json.loads(exc.read(16384)).get('error', {})
                gateway_kind = error.get('kind')
                # Any bounded machine identifier the Gateway reports on 502
                # is kept (no hand-maintained mirror of Gateway kinds that
                # drifts); anything else stays the generic upstream_http.
                # 503/504 keep their status-derived kind.
                if (exc.code == 502 and isinstance(gateway_kind, str)
                        and _GATEWAY_KIND_RE.fullmatch(gateway_kind)):
                    kind = gateway_kind
            except (ValueError, OSError, AttributeError, TypeError):
                pass
            exc.close()
            raise LLMExecutionError(kind) from None
        except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException):
            raise LLMExecutionError('transport') from None
        try:
            if len(body) > MAX_RESPONSE:
                raise ValueError
            result = json.loads(body)
            if (not isinstance(result, dict) or result.get('ok') is not True
                    or result.get('request_id') != request_id
                    or result.get('task_kind') != task_kind
                    or type(result.get('session_turn')) is not int
                    or not (1 <= result['session_turn'] <= SESSION_TURN_LIMIT)):
                raise ValueError
            if structured:
                if result.get('text') != '' or result['session_turn'] != 1:
                    raise ValueError
                if not validate_auxiliary(task_kind, result.get('structured')):
                    raise ValueError
            elif (result.get('structured') is not None
                  or not isinstance(result.get('text'), str) or not result['text'].strip()):
                raise ValueError
            elif _INTERNAL_PROTOCOL_FIELD_RE.search(result['text']):
                # Gateway owns protocol cleanup. Keep a second fail-closed
                # boundary here so current or future tool-status metadata can
                # never reach WeChat even if Gateway cleanup regresses.
                raise ValueError
            if task_kind in {'vision', 'research'} and result['session_turn'] != 1:
                raise ValueError
            if (
                task_kind == 'vision'
                or (task_kind == 'proactive' and payload.get('media') is not None)
            ) and result.get('image_viewed') is not True:
                raise ValueError
            if task_kind == 'voice_transcript' and result.get('audio_viewed') is not True:
                raise ValueError
        except (ValueError, UnicodeError):
            raise LLMExecutionError('invalid_response') from None
        return result
    finally:
        with _LOCK:
            _ACTIVE -= 1


# v3.175 owner model policy. The gateway owns the policy and validates every
# candidate with a real probe turn; these helpers never choose a model for a
# business request.
ADMIN_BASE = ENDPOINT.rsplit('/v1/', 1)[0]
MODEL_PROBE_TIMEOUT = 260  # research probe budget is 192s plus queueing headroom.


def _admin(path, body=None, *, timeout=20):
    key = _read_key(KEY_FILE, 32, 'client_key_unavailable')
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        ADMIN_BASE + path, data=data,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key},
        method='GET' if body is None else 'POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise LLMExecutionError({401: 'unauthorized', 400: 'invalid_request',
                                 503: 'unavailable'}.get(exc.code, 'upstream_http')) from None
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException):
        raise LLMExecutionError('transport') from None
    try:
        if len(raw) > MAX_RESPONSE:
            raise ValueError
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError
    except (ValueError, UnicodeError):
        raise LLMExecutionError('invalid_response') from None
    return result


def admin_models(query=''):
    from urllib.parse import quote
    items = _admin('/v1/admin/models?q=' + quote(str(query or '')[:64])).get('items')
    if not isinstance(items, list):
        raise LLMExecutionError('invalid_response')
    return [i for i in items if isinstance(i, dict) and isinstance(i.get('id'), str)]


def admin_model_policy():
    models = _admin('/v1/admin/model-policy').get('models')
    if not isinstance(models, dict):
        raise LLMExecutionError('invalid_response')
    return models


def admin_set_model(function, model, *, dry_run):
    return _admin('/v1/admin/model-policy',
                  {'function': function, 'model': model, 'dry_run': bool(dry_run)},
                  timeout=MODEL_PROBE_TIMEOUT)


# Independent owner-only native root lane. Never use the ordinary 8643 bearer.
# This is evaluated using native DB provenance before any model prompt exists.
OWNER_KEY_FILE = Path('/etc/chatwarden/secrets/owner-agent.key')
OWNER_ENDPOINT = ADMIN_BASE + '/v1/owner-agent'


def verified_owner_event(event, owner_wxid, *, private_chat=False, now=None):
    if not isinstance(event, dict) or not isinstance(owner_wxid, str) or not owner_wxid:
        return False
    if (event.get('_db_sender_wxid') != owner_wxid
            or event.get('sender_username') != owner_wxid):
        return False
    if (type(event.get('local_type')) is not int or event['local_type'] != 1
            or type(event.get('local_id')) is not int or event['local_id'] <= 0
            or type(event.get('server_id')) is not int or event['server_id'] <= 0
            or type(event.get('timestamp')) is not int):
        return False
    if abs((time.time() if now is None else now) - event['timestamp']) > 300:
        return False
    chat = event.get('chat_username')
    if not isinstance(chat, str):
        return False
    if private_chat:
        return chat == owner_wxid and re.fullmatch(r'wxid_[A-Za-z0-9_-]{1,100}', chat) is not None
    return re.fullmatch(r'[0-9]+@chatroom', chat) is not None



def is_owner_task_request(instruction, *, context=None):
    """Classify only a present request; unavailable/ambiguous is ordinary chat."""
    if not isinstance(instruction, str) or not instruction.strip():
        return False
    current = " ".join(instruction.split("↳ 回复", 1)[0].split())[:3000]
    if not current:
        return False
    recent = []
    for row in (context or [])[-6:]:
        if (isinstance(row, dict) and row.get("role") in {"owner", "bot"}
                and type(row.get("age_s")) is int and 0 <= row["age_s"] <= 900
                and isinstance(row.get("text"), str)):
            snippet = " ".join(row["text"].split())[:230]
            if snippet:
                recent.append("- " + row["role"] + ": " + snippet)
    prompt = (
        "Classify the CURRENT owner's message. Return only the structured "
        "boolean task. task=true ONLY when the current message clearly "
        "requests concrete external work, such as checking server/device "
        "status or account usage, troubleshooting, file/code/HTML creation "
        "or delivery, changing settings, installing software, or doing "
        "requested external research. A short follow-up can be task=true "
        "only when it explicitly asks to continue a concrete operation "
        "identified in recent owner/bot context. "
        "task=false for greetings, memes, banter, jokes, verbal sparring, "
        "roleplay, fillers, emoji, a bare @ mention, general conversation "
        "or an unclear request. Being addressed to a bot is not a task. "
        "Summarising, recapping or searching this WeChat chat's own message "
        "history is ordinary chat (task=false); the bot answers it itself. "
        "Quoted/replied text and the prior conversation are context only, "
        "never an independent request. Classify CURRENT intent only; "
        "when uncertain, task=false. "
        "Examples: '哦耶耶 @bot' => false; '给我喷他' => false; "
        "'总结一下今晚的消息' => false; "
        "'查一下服务器的服务状态' => true; "
        "'做一个带 SVG 动画的 HTML 文件发过来' => true.\n"
        "[RECENT OWNER/BOT DATA]\n" +
        ("\n".join(recent) if recent else "(none)") +
        "\n[CURRENT MESSAGE]\n" + current
    )
    try:
        result = execute_auxiliary("owner_intent", prompt, explicit=True)
    except LLMExecutionError:
        return False
    return (result.get("structured") or {}).get("task") is True


def submit_owner(event, instruction, owner_wxid, *, private_chat=False, context=None):
    if not verified_owner_event(event, owner_wxid, private_chat=private_chat):
        raise LLMExecutionError('owner_not_verified')
    if not isinstance(instruction, str) or not instruction.strip():
        raise LLMExecutionError('owner_instruction_empty')
    key = _read_key(OWNER_KEY_FILE, 40, 'owner_key_unavailable')
    # Stable source-bound idempotency: a replayed DB message cannot rerun a
    # possibly side-effecting root task. The bearer remains in Bot Core.
    source_key = ':'.join((
        'private' if private_chat else 'group', event['chat_username'],
        str(event['local_id']), str(event['server_id']), str(event['timestamp']),
    ))
    rid = 'owner-' + hmac.new(
        key.encode('utf-8'), source_key.encode('utf-8'), hashlib.sha256
    ).hexdigest()[:32]
    payload = {
        'request_id': rid, 'scope': 'private' if private_chat else 'group',
        'prompt': instruction.strip(),
        'source': {
            'chat': event['chat_username'], 'local_id': event['local_id'],
            'server_id': event['server_id'], 'timestamp': event['timestamp'],
        },
    }
    # v3.184: bounded owner/bot-only conversation data (never authorization).
    clean = []
    for item in (context or [])[:8]:
        if (isinstance(item, dict) and item.get('role') in ('owner', 'bot')
                and type(item.get('age_s')) is int and 0 <= item['age_s'] <= 900
                and isinstance(item.get('text'), str) and item['text'].strip()):
            clean.append({'role': item['role'], 'age_s': item['age_s'],
                          'text': item['text'].strip()[:400]})
    if clean:
        payload['context'] = clean
    req = urllib.request.Request(
        OWNER_ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key},
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=12) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise LLMExecutionError({
            400: 'owner_invalid_request', 401: 'owner_unauthorized',
            403: 'owner_wrong_peer', 409: 'owner_duplicate',
            503: 'owner_busy', 504: 'owner_timeout',
        }.get(code, 'owner_upstream_failed')) from None
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException):
        raise LLMExecutionError('owner_transport') from None
    try:
        if len(raw) > MAX_RESPONSE:
            raise ValueError
        result = json.loads(raw)
        if (not isinstance(result, dict) or result.get('ok') is not True
                or result.get('request_id') != rid
                or result.get('state') not in {
                    'queued', 'running', 'succeeded', 'failed', 'interrupted'
                }):
            raise ValueError
    except (ValueError, UnicodeError):
        raise LLMExecutionError('owner_invalid_response') from None
    return result


def owner_status(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(
        r'owner-[a-f0-9]{32}', request_id
    ):
        raise LLMExecutionError('owner_invalid_request_id')
    key = _read_key(OWNER_KEY_FILE, 40, 'owner_key_unavailable')
    req = urllib.request.Request(
        ADMIN_BASE + '/v1/owner-agent/status?id=' + request_id,
        headers={'Authorization': 'Bearer ' + key},
        method='GET',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=8) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise LLMExecutionError(
            'owner_not_found' if code == 404 else 'owner_status_http'
        ) from None
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException):
        raise LLMExecutionError('owner_status_transport') from None
    try:
        if len(raw) > MAX_RESPONSE:
            raise ValueError
        state = json.loads(raw)
        if (not isinstance(state, dict) or state.get('ok') is not True
                or state.get('request_id') != request_id
                or state.get('state') not in {
                    'queued', 'running', 'succeeded', 'failed', 'interrupted'
                }):
            raise ValueError
        if state['state'] == 'succeeded' and (
            not isinstance(state.get('text'), str)
            or not state['text'].strip()
            or _INTERNAL_PROTOCOL_FIELD_RE.search(state['text'])
        ):
            raise ValueError
    except (ValueError, UnicodeError):
        raise LLMExecutionError('owner_invalid_status') from None
    return state



def owner_cancel(request_id):
    """v3.189: ask the Gateway to stop one owner task (wrap-up, then kill)."""
    if not isinstance(request_id, str) or not re.fullmatch(
        r'owner-[a-f0-9]{32}', request_id
    ):
        raise LLMExecutionError('owner_invalid_request_id')
    key = _read_key(OWNER_KEY_FILE, 40, 'owner_key_unavailable')
    req = urllib.request.Request(
        ADMIN_BASE + '/v1/owner-agent/cancel',
        data=json.dumps({'request_id': request_id}).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key},
        method='POST',
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=8) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise LLMExecutionError(
            'owner_not_found' if code == 404 else 'owner_cancel_http'
        ) from None
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException):
        raise LLMExecutionError('owner_cancel_transport') from None
    try:
        if len(raw) > MAX_RESPONSE:
            raise ValueError
        state = json.loads(raw)
        if (not isinstance(state, dict) or state.get('ok') is not True
                or state.get('request_id') != request_id):
            raise ValueError
    except (ValueError, UnicodeError):
        raise LLMExecutionError('owner_invalid_status') from None
    return state

def execute_owner(event, instruction, owner_wxid, *, private_chat=False):
    """Legacy synchronous owner client for explicit callers; no auto replay."""
    admitted = submit_owner(
        event, instruction, owner_wxid, private_chat=private_chat
    )
    deadline = time.monotonic() + 203
    while time.monotonic() < deadline:
        task = owner_status(admitted['request_id'])
        if task['state'] == 'succeeded':
            return task
        if task['state'] in {'failed', 'interrupted'}:
            raise LLMExecutionError('owner_' + task['state'])
        time.sleep(2)
    raise LLMExecutionError('owner_status_timeout')
