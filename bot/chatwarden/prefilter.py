"""v3.205 cheap rule layer in front of the LLM judges.

Two judges call the model on messages that were not addressed to the bot:

- the follow-up gate (``classifier`` task) decides whether an un-@ message
  in a follow-up window is still talking to the bot;
- the proactive judge (``proactive`` task) decides whether to chime in on a
  burst of group chat.

Program rules (cooldowns, daily caps, activity minimums, busy slots, the
v3.28 banter shortcut) already run before both. This module adds content
rules that are cheap and one-sided: a rule may only say "skip the model, the
answer is no". It never says yes, so a rule can make the bot quieter but can
never make it answer something it would not have answered.

``CW_PREFILTER`` selects the mode:

- ``shadow`` (default): rules are evaluated and logged next to the model's
  real answer; behaviour is unchanged. Use the logs to measure agreement.
- ``active``: a matching rule skips the model call.
- ``off``: rules are not evaluated.

Log tag: ``[v3.205-prefilter]``.
"""
from __future__ import annotations

import os
import re

MODES = ("off", "shadow", "active")

# WeChat renders built-in emoji as short bracketed names, e.g. "[捂脸]".
_BRACKET_EMOJI_RE = re.compile(r"\[[^\[\]\s]{1,6}\]")
# Everything that is not a letter or digit: punctuation, spaces, emoji.
_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)
# Pure laughter / interjections, any length: 哈哈哈, 嗯嗯, hhh, 666...
_INTERJECTION_RE = re.compile(r"^(?:哈|呵|嘿|嘻|嗯|哦|噢|喔|啊|嗷|呜|额|呃|唉|哎|嚯|h|6)+$")
# Short reactions that never ask the bot for anything. Affirmatives such as
# 好 / 可以 / 行 / ok are deliberately absent: they can answer a question
# the bot just asked.
_REACTIONS = frozenset({
    "笑死", "笑死我了", "草", "牛", "牛逼", "nb", "绝了", "赞", "顶",
    "离谱", "好家伙", "卧槽", "wc", "lol", "xswl", "yyds",
})


def mode():
    value = str(os.environ.get("CW_PREFILTER", "shadow") or "").strip().lower()
    return value if value in MODES else "shadow"


def compact(text):
    """Lowercased text with bracket emoji, punctuation and spaces removed."""
    value = _BRACKET_EMOJI_RE.sub("", str(text or ""))
    return _NON_WORD_RE.sub("", value).lower()


def reaction_only(text):
    """True when the text carries no content beyond a reaction."""
    value = compact(text)
    if not value:
        return True
    return bool(_INTERJECTION_RE.match(value)) or value in _REACTIONS


def _other_mentions(event, bot_wxid):
    mentions = event.get("mentioned_wxids") if isinstance(event, dict) else None
    if not isinstance(mentions, (list, tuple, set)):
        return []
    own = str(bot_wxid or "").strip()
    return [
        str(item or "").strip()
        for item in mentions
        if str(item or "").strip() and str(item or "").strip() != own
    ]


def followup_rule(event, bot_wxid=""):
    """Reason the follow-up classifier can be skipped, or None.

    ``event`` is the group text event that reached the follow-up gate (the
    bot was not @-mentioned).
    """
    if not isinstance(event, dict):
        return None
    mentions = event.get("mentioned_wxids")
    if isinstance(mentions, (list, tuple, set)) and str(bot_wxid or "").strip() in {
        str(item or "").strip() for item in mentions
    }:
        return None
    if _other_mentions(event, bot_wxid):
        return "mentions-other"
    if reaction_only(event.get("text")):
        return "reaction-only"
    return None


def proactive_rule(window, has_visual=False):
    """Reason the proactive judge can be skipped, or None.

    ``window`` is the list of recent human messages (dicts with ``text``)
    that would be sent to the judge. A burst made only of reactions gives
    the model nothing to respond to.
    """
    if has_visual:
        return None
    texts = [
        str(item.get("text") or "")
        for item in (window or [])
        if isinstance(item, dict)
    ]
    if not texts:
        return None
    if all(reaction_only(text) for text in texts):
        return "reactions-only"
    return None
