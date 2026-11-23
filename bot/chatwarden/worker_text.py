"""Text patterns, small parsers and value types used by the group worker.

``worker`` re-exports every name here.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from . import core, decision_client, memory_docs

__all__ = [
    '_NEXT_IMAGE_SEND_INTENT',
    '_DIRECT_IMAGE_REFERENCE_INTENT',
    '_wants_next_image',
    '_V3168_EXPLICIT_DEICTIC_IMAGE_QUESTION',
    '_v3168_explicit_deictic_image_question',
    '_TEXT_TO_IMAGE_CREATE_INTENT',
    '_V3192_STICKER_MAKE_TARGET',
    '_v3192_sticker_make_target',
    '_V3109_PERSONALITY_COMMAND_RE',
    '_V3109_MENTION_SPACES',
    '_V3172_PROFILE_UPDATE_RE',
    '_V3172_REMEMBER_RE',
    '_v3172_profile_request',
    '_v3161_structured_own_mention_body',
    '_v3109_personality_command_arg',
    '_VISION_CORRECTION_FOLLOWUP_INTENT',
    '_VISION_CONTINUATION_FOLLOWUP_INTENT',
    '_ADJACENT_VISUAL_SHORT_INTENT',
    '_ADJACENT_VISUAL_OBJECT_INTENT',
    '_adjacent_media_reference_intent',
    '_V3100_QUOTED_NAME_RE',
    '_V3100_INVITE_JOIN_RE',
    '_V3100_DIRECT_JOIN_RE',
    '_V3100_THROUGH_JOIN_RE',
    '_v3100_clean_newcomer_names',
    '_v3100_newcomers_from_system_text',
    'ImageRef',
    'PendingTrigger',
    'FormalReplyWork',
]


_NEXT_IMAGE_SEND_INTENT = re.compile(
    r"(?:"
    r"(?:接下来|等下|待会|随后|一会儿?|等会儿?).{0,16}"
    r"(?:我)?(?:会|要|准备|再)?(?:发|贴|传|上传|发送|丢|甩).{0,8}"
    r"(?:一|几)?(?:张|幅|个)?(?:图片|照片|截图|图)"
    r"|(?:我|给你).{0,12}(?:发|贴|传|上传|发送|丢|甩).{0,8}"
    r"(?:一|几)?(?:张|幅|个)?(?:图片|照片|截图|图)"
    r")",
    re.I,
)

_DIRECT_IMAGE_REFERENCE_INTENT = re.compile(
    r"(?:"
    r"(?:下一张|下张|下一幅|下幅)(?:图片|照片|截图|图)?"
    r"|(?:这张|这幅)(?:图片|照片|截图|图)"
    r"|(?:图片|照片|截图|图)(?:里|中|上)"
    r"|(?:帮我|给我)?识图(?:一下|下|看看)?[。！!？?~～]*$"
    r"|(?:帮我|给我)?看(?:看|下|一下)?"
    r"(?:下图|(?:这张|这幅|下一张)(?:图片|照片|截图|图))"
    r"|(?:帮我|给我)?看(?:看|下|一下)?(?:图片|照片|截图|图)"
    r"[。！!？?~～]*$"
    r"|(?:帮我|给我|你)?看(?:看|下|一下)?(?:图片|照片|截图|图)\s*.{0,40}"
    r"(?:记一下|记住|记下来|说说|分析一下?|评价一下?|点评一下?|识别一下?|看懂|总结一下?)"
    r")",
    re.I,
)


def _wants_next_image(text):
    """Return True only for an explicit future/direct image reference.

    Long pasted prose may contain words such as ``下面``, ``接下来`` or even
    image-related terminology while still being an ordinary text request.
    Future send wording is strong enough to match anywhere; weaker direct
    image references are bounded to normal short instructions so quoted or
    pasted articles cannot accidentally open the legacy NEXT-image wait.
    """
    value = str(text or "").strip()
    if _NEXT_IMAGE_SEND_INTENT.search(value):
        return True
    if len(value) > 600:
        return False
    return bool(_DIRECT_IMAGE_REFERENCE_INTENT.search(value))

# v3.168: an explicitly addressed short deictic image question without 张/幅
# ("@bot 这图是什么", "@bot 看看这图") often arrives a second before the image
# itself. Only for explicit @ turns, open the existing bounded NEXT-image wait.
# This is deliberately not part of _DIRECT_IMAGE_REFERENCE_INTENT: follow-up
# Vision continuations and adjacent-media reuse keep their existing routing.
_V3168_EXPLICIT_DEICTIC_IMAGE_QUESTION = re.compile(
    r"(?:"
    r"(?:这个?|那个?)(?:图片|照片|截图|图)(?![书表标纸案层鉴谋腾形])"
    r"[^。！!\n]{0,12}(?:是什么|是啥|啥|什么|哪|谁|怎么|如何|吗|么|[？?])"
    r"|(?:帮我|给我|你)?(?:看看|看下|看一下|识别|分析|解释|说说)(?:一下|下)?"
    r"(?:这个?|那个?)(?:图片|照片|截图|图)(?![书表标纸案层鉴谋腾形])"
    r")",
    re.I,
)


def _v3168_explicit_deictic_image_question(text):
    value = str(text or "").strip()
    if not value or len(value) > 40:
        return False
    return bool(_V3168_EXPLICIT_DEICTIC_IMAGE_QUESTION.search(value))


# v3.22.1 imagegen/vision intent disambiguation.
# This is deliberately only a bypass for the legacy "wait for NEXT image"
# adjacency rule; it does not itself trigger image generation. The request
# still enters the native generator, where the existing semantic imagegen gate decides
# whether to return the exact [开始生图] marker.
_TEXT_TO_IMAGE_CREATE_INTENT = re.compile(
    r"(?:生成|绘制|创作|制作|设计|画).{0,80}"
    r"(?:图片|照片|图像|插画|海报|头像|壁纸|封面|图)"
    r"|(?:做|出)(?:一|几)?(?:张|幅|个)?"
    r"(?:图片|图像|插画|海报|头像|壁纸|封面|图)"
    r"|来(?:一|几)?(?:张|幅)"
    r"(?:图片|图像|插画|海报|头像|壁纸|封面|图)?",
    re.I,
)

# v3.192: "做成/转成…表情包" names the desired OUTPUT form of a generation
# request, not a reference to an upcoming sticker. A 2026-10-30 incident in a
# group quoted plain text and asked to turn it into a sticker image; the bare
# 表情 keyword opened the legacy NEXT-sticker wait and replied
# "我还没收到你说的那张图" four times. Only the make/convert-target form is
# exempted from that wait; deictic sticker references keep the old behavior.
_V3192_STICKER_MAKE_TARGET = re.compile(
    r"(?:做成|转成|转换成|转为|变成|弄成|整成|搞成|画成|改成|生成|制作|做|画|设计|出)"
    r"(?:一|几|个|张|套|组|些|下|一下)*"
    r"[^。！!？?\n]{0,12}?(?:表情包|表情图|表情贴纸|贴纸)",
    re.I,
)


def _v3192_sticker_make_target(text):
    value = str(text or "").strip()
    if not value or len(value) > 200:
        return False
    return bool(_V3192_STICKER_MAKE_TARGET.search(value))


_V3109_PERSONALITY_COMMAND_RE = re.compile(
    r"^/人格(?:[\s\u2005\u00a0]+(?P<value>[\s\S]*))?$"
)
_V3109_MENTION_SPACES = " \t\r\n\u2005\u2002\u2003\u00a0"


# db-native-v3.172-member-profile-request
_V3172_PROFILE_UPDATE_RE = re.compile(
    r"^(?:请|麻烦|帮我|给我)?\s*(?:更新|刷新|重建|重新整理|重新生成)(?:一下|下)?\s*"
    r"(?:我的?|本人的?)?\s*(?:个人)?画像[。.！!～~]*$"
    r"|^(?:请|麻烦)?\s*重新认识(?:一下)?我[。.！!～~]*$"
)
_V3172_REMEMBER_RE = re.compile(
    r"^(?:请|麻烦)?\s*记住[，,：:\s]*(?P<body>我(?!们).{1,120})$",
    re.S,
)


def _v3172_profile_request(text):
    """None = not a request; '' = plain update; str = self-statement to remember."""
    value = str(text or "").strip()
    value = re.sub(r"^(?:[@＠][^\s\u2005]+[\s\u2005]*)+", "", value).strip()
    if _V3172_PROFILE_UPDATE_RE.match(value):
        return ""
    match = _V3172_REMEMBER_RE.match(value)
    if match:
        return match.group("body").strip()
    return None


def _v3161_structured_own_mention_body(event, bot_wxid, text):
    """Strip only a proven leading own-mention from media-intent text."""
    value = str(text or "")
    own_wxid = str(bot_wxid or "").strip()
    mentions = event.get("mentioned_wxids") if isinstance(event, dict) else None
    if not (
        own_wxid
        and isinstance(mentions, (list, tuple, set))
        and any(str(item or "").strip() == own_wxid for item in mentions)
    ):
        return value

    display = str(event.get("bot_display_name") or "").strip()
    if not display:
        return value

    leading = value.lstrip(_V3109_MENTION_SPACES)
    for sigil in ("@", "＠"):
        prefix = sigil + display
        if not leading.startswith(prefix):
            continue
        remainder = leading[len(prefix):]
        if remainder and remainder[0] not in _V3109_MENTION_SPACES:
            continue
        return remainder.lstrip(_V3109_MENTION_SPACES)
    return value


def _v3109_personality_command_arg(text, bot_display_name=""):
    value = str(text or "").strip()
    display = str(bot_display_name or "").strip()
    if display:
        mention = "@" + display
        if value.startswith(mention):
            remainder = value[len(mention):]
            if remainder and remainder[0] not in _V3109_MENTION_SPACES:
                return None
            value = remainder.lstrip(_V3109_MENTION_SPACES)

    match = _V3109_PERSONALITY_COMMAND_RE.fullmatch(value)
    if match is None:
        return None
    return str(match.group("value") or "").strip()

_VISION_CORRECTION_FOLLOWUP_INTENT = re.compile(
    r"(?:"
    r"(?:你|刚才|上条|上面)?.{0,8}(?:看|识别|识图).{0,6}"
    r"(?:图|图片|截图).{0,8}(?:吗|没|没有|错|错了|不对|看清|看懂|看见|看到)"
    r"|(?:图|图片|截图).{0,8}(?:看|识别|识图).{0,6}"
    r"(?:错|错了|不对|没|没有|看清|看懂)"
    r"|(?:看错|识别错|识图错|没看图|没看图片|没识图|再看图|重新看图|重新识图)"
    r")",
    re.I,
)

_VISION_CONTINUATION_FOLLOWUP_INTENT = re.compile(
    r"(?:"
    r"(?:他|她|它|这|那|这个|那个|这人|那人|这个人|那个人|这里|那里|这儿|那儿)"
    r".{0,24}(?:在哪|哪里|哪儿|是?哪(?:里|儿)?|哪个(?:地铁)?站|哪一站|什么站|"
    r"是谁|谁|是什么|什么地方|什么位置|什么牌子|什么型号|穿(?:的)?什么|拿(?:的)?什么)"
    r"|(?:图|图片|照片|截图|画面|镜像|反转|站牌|路牌|招牌).{0,32}"
    r"(?:在哪|哪里|哪儿|哪个(?:地铁)?站|哪一站|什么站|谁|是什么|什么地方|什么位置)"
    r"|(?:在哪|哪里|哪儿|哪个(?:地铁)?站|哪一站|什么站)[？?]?$"
    r")",
    re.I,
)


_ADJACENT_VISUAL_SHORT_INTENT = re.compile(
    r"^(?:@\S+[\s\u2005\u2009\u202f]*)?"
    r"(?:"
    r"(?:帮我|给我|你)?(?:看看|看下|看一下|瞅瞅)(?:这个|这张|这幅|它)?"
    r"|(?:这张|这幅|这个|这|那张|那幅|那个|那)(?:呢|怎么样|咋样|好看吗|好不好看)"
    r"|(?:怎么样|咋样|好看吗|好不好看|像谁|像什么|什么意思|在干嘛|干嘛呢)"
    r")"
    r"[。！!？?~～]*$",
    re.I,
)

_ADJACENT_VISUAL_OBJECT_INTENT = re.compile(
    r"^(?:@\S+[\s\u2005\u2009\u202f]*)?"
    r"(?:"
    r"(?:请|帮我|给我|你)?(?:评价|点评|评评|分析|看看|看下|看一下|鉴定|辨认|识别)"
    r"(?:一下|下)?(?:这|那)(?:件|款|个|双|套|身|张|幅)?"
    r"(?:夹克|衣服|外套|上衣|裤子|鞋|包|东西|物品|设计|造型|穿搭|款式)?"
    r"|(?:这|那)(?:件|款|个|双|套|身)?"
    r"(?:夹克|衣服|外套|上衣|裤子|鞋|包|东西|物品|设计|造型|穿搭|款式)"
    r".{0,8}(?:怎么样|咋样|如何|好看吗|好不好看|值不值|适合吗)"
    r"|(?:你)?(?:还|还是)?(?:看不到|看不见|没看到|没看见)"
    r"(?:这|那|图|图片|照片|它)?(?:吗|么)?"
    r")"
    r"[。！!？?~～]*$",
    re.I,
)


def _adjacent_media_reference_intent(text):
    """Return True only when current text clearly depends on adjacent media."""
    value = str(text or "").replace("\x00", "").strip()
    if not value or len(value) > 240:
        return False
    if core.image_edit_followup_intent(value):
        return True
    return bool(
        _DIRECT_IMAGE_REFERENCE_INTENT.search(value)
        or _VISION_CORRECTION_FOLLOWUP_INTENT.search(value)
        or _VISION_CONTINUATION_FOLLOWUP_INTENT.search(value)
        or _ADJACENT_VISUAL_SHORT_INTENT.search(value)
        or _ADJACENT_VISUAL_OBJECT_INTENT.search(value)
    )


# db-native-v3.100-newcomer-welcome
# WeChat renders group membership changes as Type10000 system messages. Keep
# parsing deliberately narrow: only quoted invitee/joiner forms are accepted,
# while the bot's own "邀请你加入了群聊" activation notice is rejected.
_V3100_QUOTED_NAME_RE = re.compile(
    r'["“]([^"”\r\n]{1,80})["”]'
)
_V3100_INVITE_JOIN_RE = re.compile(
    r'^(?:["“][^"”\r\n]{1,80}["”]|你)邀请(?P<invitees>.+?)加入了?群聊'
)
_V3100_DIRECT_JOIN_RE = re.compile(
    r'^["“]([^"”\r\n]{1,80})["”](?:已)?加入了?群聊'
)
_V3100_THROUGH_JOIN_RE = re.compile(
    r'^["“]([^"”\r\n]{1,80})["”](?:已)?通过.{1,120}?加入了?群聊'
)


def _v3100_clean_newcomer_names(values):
    out = []
    seen = set()
    for value in values:
        name = str(value or "").strip()
        if not name or name in {"你", "我"}:
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= 8:
            break
    return out


def _v3100_newcomers_from_system_text(text):
    value = str(text or "").strip()
    if value.startswith("[系统]"):
        value = value[len("[系统]"):].strip()
    if not value or "群聊" not in value or "加入" not in value:
        return []

    # This is the bot itself being added to a newly discovered group. People
    # listed after "群聊参与人还有" are existing members, not newcomers.
    if "邀请你加入了群聊" in value or "邀请你加入群聊" in value:
        return []

    invited = _V3100_INVITE_JOIN_RE.search(value)
    if invited is not None:
        invitees = str(invited.group("invitees") or "").strip()
        if invitees.startswith("你"):
            return []
        return _v3100_clean_newcomer_names(
            _V3100_QUOTED_NAME_RE.findall(invitees)
        )

    through = _V3100_THROUGH_JOIN_RE.search(value)
    if through is not None:
        return _v3100_clean_newcomer_names([through.group(1)])

    direct = _V3100_DIRECT_JOIN_RE.search(value)
    if direct is not None:
        return _v3100_clean_newcomer_names([direct.group(1)])

    return []


@dataclass
class ImageRef:
    sender: str
    path: str | None
    error: str | None
    local_id: int
    created: float


@dataclass
class PendingTrigger:
    event: dict
    created: float


@dataclass(frozen=True)
class FormalReplyWork:
    generation: int
    event: dict
    image_path: str | None
    provider_prompt: str
    sender: str | None
    display: str
    legacy_sender: str
    group_cfg: dict
    profiles: object
    context_snapshot: dict | None
    queued_at: float
    image_local_id: int | None = None
    media_visual_source: str = "none"
    media_route: str = "host_fallback"
