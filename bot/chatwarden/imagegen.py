"""Image generation and editing: intent detection, prompts and the backend call.

Split out of ``core``; ``core`` re-exports every name here. Anything that
still lives in ``core`` is reached as ``core.NAME`` so it stays late-bound.
"""
from pathlib import Path
import json
import os
import re
import tempfile
import time
import urllib.request

__all__ = [
    'is_image_gen_marker',
    'is_internal_image_gen_marker_like',
    'image_gen_enabled',
    'image_gen_ack_text',
    'image_edit_ack_text',
    'image_gen_aspect_ratio',
    'image_gen_explicit_prompt',
    'image_gen_prompt',
    'image_gen_routed_prompt',
    'image_edit_prompt',
    'image_edit_intent',
    '_image_edit_correction_intent',
    'image_edit_followup_intent',
    'image_edit_needs_recent_context',
    'image_edit_contextual_prompt',
    'call_image_gen',
]


def is_image_gen_marker(text):
    value = str(text or '').strip()
    if value == core.IMAGE_GEN_MARKER:
        return True
    if core._IMAGE_GEN_MULTILINE_PREFIX_RE.match(value) is not None:
        return True
    if core._IMAGE_GEN_INLINE_MARKER_RE.fullmatch(value) is not None:
        return True
    prefixed = core._IMAGE_GEN_PREFIX_PROMPT_RE.fullmatch(value)
    return bool(prefixed and str(prefixed.group('prompt') or '').strip())


def is_internal_image_gen_marker_like(text):
    """Detect a whole-reply internal marker shape without routing it."""
    value = str(text or '').replace('\x00', '').strip()
    if is_image_gen_marker(value):
        return False
    return (
        core._IMAGE_GEN_MARKER_LIKE_RE.fullmatch(value) is not None
        or core._IMAGE_GEN_MARKER_TRAILING_TEXT_LIKE_RE.fullmatch(value) is not None
    )


def image_gen_enabled():
    return not core.IMAGE_GEN_DISABLE_FILE.exists()


def image_gen_ack_text():
    return core.IMAGE_GEN_ACK


def image_edit_ack_text():
    return core.IMAGE_EDIT_ACK


def image_gen_aspect_ratio(text):
    value = str(text or '').lower()
    if re.search(r'(横版|横图|横向|landscape|16\s*[:：x×]\s*9)', value):
        return 'landscape'
    if re.search(r'(竖版|竖图|竖向|portrait|9\s*[:：x×]\s*16)', value):
        return 'portrait'
    return 'square'


def image_gen_explicit_prompt(text, *bot_names):
    """Parse a deterministic leading ``生成图片：`` command.

    Ignore only leading mentions that match the exact current bot identity or
    display name. Mentions of other users remain part of the body and cannot
    accidentally turn an ordinary sentence into an image command.

    ``None`` means the prefix is absent; ``''`` means it is present but empty.
    """
    value = str(text or '').replace('\x00', '').strip()
    known = []
    for name in bot_names:
        name = str(name or '').strip()
        if name and name not in known:
            known.append(name)

    while value:
        removed = False
        for name in known:
            for sigil in ('@', '＠'):
                mention = sigil + name
                if not value.startswith(mention):
                    continue
                rest = value[len(mention):]
                if rest and rest[0] not in core._IMAGE_GEN_MENTION_SPACES:
                    continue
                value = rest.lstrip(core._IMAGE_GEN_MENTION_SPACES)
                removed = True
                break
            if removed:
                break
        if not removed:
            break

    match = core._IMAGE_GEN_EXPLICIT_COMMAND_RE.fullmatch(value)
    if match is None:
        return None
    prompt = str(match.group('prompt') or '').strip()
    if len(prompt) > 4000:
        prompt = prompt[:4000].rstrip()
    return prompt


def image_gen_prompt(text):
    """Return the phase-1 text-to-image prompt from the current user message."""
    value = str(text or '').replace('\x00', '').strip()
    # WeChat text normally includes the visible @mention at the front. It is
    # routing metadata, not part of the desired image description.
    value = re.sub(r'^(?:@\S+[\s\u2005\u2009\u202f]*)+', '', value).strip()
    if len(value) > 4000:
        value = value[:4000].rstrip()
    return value


def image_gen_routed_prompt(route_reply, current_text):
    """Extract a context-compiled image prompt, with legacy marker fallback."""
    value = str(route_reply or '').replace('\x00', '').strip()
    if value == core.IMAGE_GEN_MARKER:
        return image_gen_prompt(current_text)
    inline = core._IMAGE_GEN_INLINE_MARKER_RE.fullmatch(value)
    if inline is not None:
        return image_gen_prompt(inline.group('prompt'))
    prefixed = core._IMAGE_GEN_PREFIX_PROMPT_RE.fullmatch(value)
    if prefixed is not None:
        compiled = str(prefixed.group('prompt') or '').strip()
        if compiled:
            return image_gen_prompt(compiled)
    multiline = core._IMAGE_GEN_MULTILINE_PREFIX_RE.match(value)
    if multiline is None:
        return ''
    compiled = value[multiline.end():].strip()
    if not compiled:
        return image_gen_prompt(current_text)
    return image_gen_prompt(compiled)


def image_edit_prompt(text):
    """Build a single-reference edit prompt that defaults to identity preservation."""
    request = image_gen_prompt(text)
    if not request:
        return ""
    prefix = (
        "请以提供的参考图片为基础进行编辑或生成。除非用户明确要求改变，"
        "尽量保持参考图中主体的身份、脸部特征、发型和主要外观一致，"
        "只改变用户明确指定的内容。用户要求："
    )
    return (prefix + request)[:4000].rstrip()


def image_edit_intent(text):
    """Return True only for a strong single-reference image edit/create intent."""
    value = str(text or '').replace('\x00', '').strip()
    if re.search(r'(?:表情|贴纸|sticker|emoji)', value, re.I):
        return False
    return any(pattern.search(value) for pattern in core._IMAGE_EDIT_PATTERNS)


def _image_edit_correction_intent(text):
    """Recognize actionable correction wording after an exact image is known."""
    value = image_gen_prompt(text)
    if not value:
        return False

    if core._IMAGE_EDIT_CORRECTION_EXPLICIT_RE.search(value):
        return True

    generic_targets = {
        "这个",
        "这样",
        "这张图",
        "这幅图",
        "这个图",
        "这版",
        "这一版",
    }
    for pattern in (
        core._IMAGE_EDIT_CORRECTION_TARGET_FIRST_RE,
        core._IMAGE_EDIT_CORRECTION_TARGET_SUFFIX_RE,
    ):
        match = pattern.search(value)
        if not match:
            continue
        target = str(match.group("target") or "").strip(" 「『“\"」』”")
        if target and target not in generic_targets:
            return True

    return False


def image_edit_followup_intent(text):
    """Recognize terse edit commands when an exact image reference exists.

    Phrases such as ``换成穿年轮的`` are intentionally too context-dependent
    for the global image-edit detector. They become strong edit intent only
    after the caller has already resolved one exact reference image.
    """
    value = str(text or '').replace('\x00', '').strip()
    if not value:
        return False
    # Keep advice/comparison questions out of generation even when an image is
    # quoted. This is deliberately lexical and conservative.
    if re.search(
        r'(?:哪个|哪种|哪一个|如何|怎么|怎么样|为什么|是否|会不会|'
        r'是不是|对不对|错没错|错了吗|写错了吗|改错了吗|弄错了吗|搞错了吗|'
        r'重写吗|重做吗|重新写吗|重新做吗|再来一版吗|再做一版吗|'
        r'好看吗|合适吗|建议|觉得)',
        value,
        re.I,
    ):
        return False
    if image_edit_intent(value):
        return True
    if core._IMAGE_EDIT_FOLLOWUP_ACTION_RE.search(value):
        return True
    if core._IMAGE_EDIT_REFERENCE_REPEAT_RE.fullmatch(image_gen_prompt(value)):
        return True
    correction_value = image_gen_prompt(value)
    if re.search(
        r'(?:'
        r'(?:应(?:该|当)?|正确(?:的)?|准确(?:的)?).{1,80}[？?]'
        r'|(?:才对|才正确|才是对的)\s*[？?]'
        r')\s*$',
        correction_value,
        re.I,
    ):
        return False
    return _image_edit_correction_intent(value)


def image_edit_needs_recent_context(text):
    """Return True for execute-the-previous-plan image-edit wording."""
    value = str(text or '').replace('\x00', '').strip()
    return bool(core._IMAGE_EDIT_GENERIC_EXECUTION_RE.search(value))


def image_edit_contextual_prompt(current_text, quote_context):
    """Compile a bounded edit instruction from an exact-image quote window."""
    current = image_gen_prompt(current_text)
    context = str(quote_context or '').replace('\x00', '').strip()
    if len(context) > 1800:
        context = context[-1800:]
    if not current or not context:
        return current
    return (
        '执行当前用户对这张参考图的修改请求。当前命令：'
        + current
        + '\n\n这张图附近最近对话中已经明确过修改方向；只提取其中与当前参考图直接相关、'
          '最近一次已经说清楚的视觉修改要求并落实，不要把无关聊天内容当成编辑指令。'
          '\n最近引用上下文：\n'
        + context
    )[:3800].rstrip()


def call_image_gen(
    prompt,
    aspect_ratio='square',
    chat_name=None,
    reference_image_path=None,
):
    """Call the isolated host image-generation gateway and save its PNG locally."""
    if not core.image_gen_enabled():
        raise RuntimeError('image generation is disabled by runtime kill switch')
    prompt = str(prompt or '').strip()
    aspect = str(aspect_ratio or 'square').strip().lower()
    if not prompt:
        raise RuntimeError('image generation prompt is empty')
    if aspect not in {'square', 'landscape', 'portrait'}:
        raise RuntimeError(f'invalid image generation aspect ratio: {aspect!r}')
    if not core.IMAGE_GEN_ENDPOINT:
        raise RuntimeError('image generation endpoint is empty')
    if not core.IMAGE_GEN_KEY_FILE.exists():
        raise RuntimeError(
            f'image generation auth key missing: {core.IMAGE_GEN_KEY_FILE}'
        )

    key = core.IMAGE_GEN_KEY_FILE.read_text(encoding='utf-8').strip()
    if not key:
        raise RuntimeError('image generation auth key is empty')

    request_payload = {
        'prompt': prompt,
        'aspect_ratio': aspect,
    }
    if reference_image_path:
        reference_mime, reference_b64 = core._image_reference_payload(
            reference_image_path
        )
        request_payload['reference_image_mime'] = reference_mime
        request_payload['reference_image_b64'] = reference_b64

    payload = json.dumps(
        request_payload,
        ensure_ascii=False,
    ).encode('utf-8')
    request = urllib.request.Request(
        core.IMAGE_GEN_ENDPOINT,
        data=payload,
        method='POST',
        headers={
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'Accept': 'image/png',
        },
    )

    wait_started = time.monotonic()
    with core._IMAGE_GEN_SEM:
        queue_wait = time.monotonic() - wait_started
        started = time.monotonic()
        core.log(
            '[v3.22-imagegen] '
            f'chat={str(chat_name or "unknown")!r} '
            f'generate_start=yes aspect={aspect} '
            f'prompt_chars={len(prompt)} '
            f'reference={"yes" if reference_image_path else "no"} '
            f'queue_wait={queue_wait:.2f}s'
        )
        try:
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=core.IMAGE_GEN_HTTP_TIMEOUT,
                ) as response:
                    raw = response.read(core.IMAGE_GEN_MAX_BYTES + 1)
                    provider = str(
                        response.headers.get('X-Imagegen-Provider') or ''
                    ).strip()
                    model = str(
                        response.headers.get('X-Imagegen-Model') or ''
                    ).strip()
                    returned_aspect = str(
                        response.headers.get('X-Imagegen-Aspect') or aspect
                    ).strip().lower()
            except urllib.error.HTTPError as exc:
                try:
                    body = exc.read(500).decode('utf-8', errors='replace')
                except Exception:
                    body = ''
                error_kind = ''
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        error = parsed.get('error')
                        if isinstance(error, dict):
                            error_kind = str(error.get('kind') or '').strip()
                except Exception:
                    error_kind = ''
                if error_kind == 'request_rejected':
                    raise core.ImageGenerationRejectedError(
                        'image provider rejected this request'
                    ) from exc
                if error_kind == 'quota_exhausted':
                    raise core.ImageGenerationQuotaError(
                        'image provider quota is currently unavailable'
                    ) from exc
                raise RuntimeError(
                    f'image generation HTTP {exc.code}: {body[:500]}'
                ) from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(
                    f'image generation transport failed: {exc.reason}'
                ) from exc

            if len(raw) > core.IMAGE_GEN_MAX_BYTES:
                raise RuntimeError('generated image exceeds 25MB cap')
            if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                raise RuntimeError('image generation gateway returned non-PNG data')

            fd, path = tempfile.mkstemp(
                prefix='wechat-imagegen-',
                suffix='.png',
                dir='/tmp',
            )
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(raw)
                os.chmod(path, 0o644)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    Path(path).unlink()
                except FileNotFoundError:
                    pass
                raise

            elapsed = time.monotonic() - started
            core.log(
                '[v3.22-imagegen] '
                f'chat={str(chat_name or "unknown")!r} '
                f'generate_success=yes aspect={returned_aspect} '
                f'provider={provider or "unknown"} model={model or "unknown"} '
                f'bytes={len(raw)} elapsed={elapsed:.2f}s'
            )
            return {
                'path': path,
                'provider': provider,
                'model': model,
                'aspect_ratio': returned_aspect,
                'bytes': len(raw),
                'elapsed': elapsed,
                'queue_wait': queue_wait,
                'reference_image': bool(reference_image_path),
            }
        finally:
            key = ''


from . import core  # noqa: E402  (bottom import: core re-exports this module)
