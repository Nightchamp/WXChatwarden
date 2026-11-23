"""Conversational style guidance and its logging.

Split out of ``core``; ``core`` re-exports every name here. Anything that
still lives in ``core`` is reached as ``core.NAME`` so it stays late-bound.
"""
import re
import statistics

__all__ = [
    '_wechat_human_feel_context',
    '_wechat_human_feel_turn_block',
    '_log_wechat_human_style',
]


def _wechat_human_feel_context(context, recent_bot_texts=None):
    messages = core._wechat_context_messages(context)
    human_lengths = []
    bot_texts = []
    for label, text in messages:
        current = text.split('↳ 回复', 1)[0].strip()
        if not current:
            continue
        if label == '机器人':
            bot_texts.append(current)
            continue
        human_lengths.append(len(current))

    if human_lengths:
        median_chars = max(1, round(statistics.median(human_lengths)))
        short_ratio = round(
            100 * sum(length <= 20 for length in human_lengths) / len(human_lengths)
        )
        voice = (
            '[群聊语体指纹]\n'
            f'最近真人消息样本 {len(human_lengths)} 条，中位长度约 {median_chars} 字，'
            f'其中约 {short_ratio}% 不超过 20 字。'
            '普通接梗、情绪回应、轻问答优先贴近这种短而不完整的群聊节奏；'
            '这不是硬字数上限，事实、技术、搜索、调查或安全问题仍以必要信息优先。'
        )
    else:
        voice = (
            '[群聊语体指纹]\n'
            '当前没有足够真人样本可量化；普通接梗、情绪回应、轻问答仍优先一两句短回复，'
            '信息型任务按需要展开。'
        )

    if recent_bot_texts is None:
        recent_bot_texts = bot_texts[-6:]
    else:
        cleaned_bot_texts = []
        for text in recent_bot_texts:
            current = str(text or '').split('↳ 回复', 1)[0].strip()
            if current and not current.startswith('['):
                cleaned_bot_texts.append(current)
        recent_bot_texts = cleaned_bot_texts[-6:]
    recent_bot = '\n'.join(recent_bot_texts)
    cooled = [
        label
        for label, pattern in core._CW_HUMAN_FEEL_COOLDOWN_PATTERNS
        if pattern.search(recent_bot)
    ]
    if cooled:
        cooldown = (
            '[近期表达冷却]\n'
            '最近机器人已经用过：'
            + '、'.join(cooled)
            + '。本轮优先换一种更普通的自然说法；这是短期去重复，不是永久禁词。'
        )
    else:
        cooldown = ''

    structure_cooled = []
    for label, pattern in core._CW_HUMAN_FEEL_STRUCTURE_PATTERNS:
        hits = sum(
            1
            for text in recent_bot_texts
            if pattern.search(text)
        )
        if hits >= 2:
            structure_cooled.append(label)
    if structure_cooled:
        structure_cooldown = (
            '[近期句式冷却]\n'
            '最近机器人连续回复里已经重复了这些表达骨架：'
            + '、'.join(structure_cooled)
            + '。本轮不要换名词继续套同一骨架，也不要为了维持人格强行再造一个同类比喻或反问收尾；'
            '普通聊天优先改成直白陈述、短应答、自然省略或一句普通话就停。'
            '如果当前是事实、技术、搜索、调查或安全任务，只换表达骨架，不减少必要信息、证据或结构。'
            '这里冷却的是近期句式，不是永久禁词。'
        )
    else:
        structure_cooldown = ''

    return '\n\n'.join(
        part
        for part in (
            voice,
            cooldown,
            structure_cooldown,
            core._CW_HUMAN_FEEL_EXAMPLES,
        )
        if part
    )


def _wechat_human_feel_turn_block(question):
    current = str(question or '').split('↳ 回复', 1)[0].strip()
    if not current or len(current) > 18:
        return ''
    if core._CW_HUMAN_FEEL_INFO_REQUEST_RE.search(current):
        return ''
    if not core._CW_HUMAN_FEEL_MICRO_TURN_RE.search(current):
        return ''
    return (
        '[本轮短接话]\n'
        '当前更像群友间的短接话/情绪回应，不是信息任务。优先用 5-15 字顺着接一句就结束；'
        '不要新开比喻、段子、建议或反问，也不用主动展示完整人格。只有上下文真的需要时才多说。'
    )


def _log_wechat_human_style(chat_name, content):
    """Emit cheap style telemetry for user-visible replies; never rewrites text."""
    text = str(content or '').strip()
    if not text or text.startswith((
        core.AGENT_REACH_MARKER,
        core.VISION_AGENT_REACH_MARKER,
        core.IMAGE_GEN_MARKER,
    )):
        return

    paragraph_count = len([
        part for part in re.split(r'\n\s*\n+', text) if part.strip()
    ])
    cooled = [
        label
        for label, pattern in core._CW_HUMAN_FEEL_COOLDOWN_PATTERNS
        if pattern.search(text)
    ]
    core.log(
        '[v3.65-human-style] '
        f'group={str(chat_name)!r} chars={len(text)} '
        f'paragraphs={paragraph_count} newlines={text.count(chr(10))} '
        f'catchphrases={",".join(cooled) if cooled else "none"}'
    )


from . import core  # noqa: E402  (bottom import: core re-exports this module)
