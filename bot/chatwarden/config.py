"""Module-level settings and lookup tables shared across the bot.

Pure values only: nothing here holds runtime state. ``core`` re-exports every
name, so existing ``core.NAME`` readers keep working.
"""
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

__all__ = [
    'BUS_FILE',
    'UI_MAIN_MARKER_FILE',
    'GROUPS_FILE',
    'PAUSE_FILE',
    'DB_FILE',
    'FOLLOWUP_GATE_CONTEXT_MESSAGES',
    'FOLLOWUP_GATE_CONTEXT_CHARS',
    'AGENT_REACH_MARKER',
    'VISION_AGENT_REACH_MARKER',
    'VISION_AGENT_REACH_CONTEXT_MAX_CHARS',
    'AGENT_REACH_ACK',
    'IMAGE_GEN_MARKER',
    '_EXPLICIT_AGENT_REACH_INTENT_RE',
    '_EXTERNAL_FEEDBACK_TARGET_RE',
    '_EXTERNAL_FEEDBACK_ACTION_RE',
    '_CURRENT_EXTERNAL_URL_RE',
    '_PROVIDED_FEEDBACK_INLINE_RE',
    '_EXTERNAL_FEEDBACK_CARD_MARKERS',
    '_GROUP_HISTORY_TZ',
    '_GROUP_HISTORY_SUMMARY_ACTION_RE',
    '_GROUP_HISTORY_SUMMARY_CHAT_RE',
    '_GROUP_HISTORY_FROM_TO_NOW_RE',
    '_GROUP_HISTORY_FROM_TO_CLOCK_RE',
    '_GROUP_HISTORY_RECENT_HOURS_RE',
    '_GROUP_HISTORY_DAYPART_RE',
    '_GROUP_HISTORY_SEMANTIC_HINT_RE',
    '_GROUP_HISTORY_SEMANTIC_TIME_SCOPE_RE',
    '_GROUP_HISTORY_UNREAD_COUNT_RE',
    '_GROUP_HISTORY_UNREAD_COUNT_MAX',
    '_GROUP_HISTORY_ABSENCE_SELF_RE',
    '_GROUP_HISTORY_ABSENCE_HISTORY_RE',
    '_GROUP_HISTORY_ABSENCE_MIN_GAP_SEC',
    '_GROUP_HISTORY_MEDIA_QUOTE_GARBAGE_RE',
    '_GROUP_HISTORY_MAX_ROWS',
    '_GROUP_HISTORY_MAX_CHARS',
    '_GROUP_HISTORY_MESSAGE_MAX_CHARS',
    '_GROUP_HISTORY_CLASSIFIER_MAX_WINDOW',
    '_GROUP_HISTORY_CURSOR_TTL_SEC',
    'IMAGE_GEN_ACK',
    'IMAGE_EDIT_ACK',
    'IMAGE_GEN_DISABLE_FILE',
    'IMAGE_GEN_KEY_FILE',
    'IMAGE_GEN_ENDPOINT',
    'IMAGE_GEN_HTTP_TIMEOUT',
    'IMAGE_GEN_MAX_BYTES',
    'IMAGE_GEN_REFERENCE_MAX_BYTES',
    'AGENT_REACH_SEND_CHUNK_CHARS',
    '_CW_HUMAN_FEEL_COOLDOWN_PATTERNS',
    '_CW_HUMAN_FEEL_STRUCTURE_PATTERNS',
    '_CW_HUMAN_FEEL_MICRO_TURN_RE',
    '_CW_HUMAN_FEEL_INFO_REQUEST_RE',
    'TRIGGER',
    'FORMAL_REPLY_SEND_CHUNK_CHARS',
    'BOT_PREFIX',
    '_RECENT_OUTGOING_TEXT_TTL_SEC',
    'ME_LABEL',
    'OTHER_LABEL',
    '_DB_PRIMARY_BOT_WXID',
    '_DB_PRIMARY_OWNER_WXID',
    '_V317_RECENT_MAX',
    '_V317_FACT_MAX',
    '_V317_INJECT_RECENT_MAX',
    '_V317_INJECT_FACT_MAX',
    '_V317_RECENT_MAX_CHARS',
    '_V317_FACT_MAX_CHARS',
    '_V317_FACT_MARKERS',
    '_V317_SENSITIVE_MARKERS',
    '_V340_MEMORY_MAX',
    '_V340_INJECT_MAX',
    '_V340_EPISODE_MESSAGES',
    '_V340_MESSAGE_MAX_CHARS',
    '_V340_ANCHOR_MAX_CHARS',
    '_V340_DIRECT_MARKERS',
    '_V340_COLLECTIVE_MARKERS',
    '_V340_DURABLE_MARKERS',
    '_V35_CONTEXT_MESSAGES',
    '_V35_MESSAGE_MAX_CHARS',
    '_V3170_SETTINGS_CACHE',
    '_V3170_GROUP_SETTING_KEYS',
    '_V3170_RUNTIME_SETTING_KEYS',
    '_V3193_PRIVATE_NAME_MAX',
    '_V3171_PINNED_MAX',
    '_V3171_MANUAL_GROUP_MAX',
    '_V3171_GROUP_SETTING_SENDER',
    '_V3171_MEMBER_MAPS',
    'GROUP_MESSAGES_KEEP',
    'GROUP_MESSAGES_PER_CHAT',
    'GROUP_MESSAGES_MAX_TOTAL',
    'GROUP_MESSAGES_MAX_AGE_DAYS',
    '_OPEN_DB_SCHEMA_OBJECTS',
    'RUNTIME_PRIVACY_REPLIES',
    '_CONTEXT_FREEZE_NORMAL_TIMEOUT_SEC',
    '_CONTEXT_FREEZE_HISTORY_TIMEOUT_SEC',
    '_CW_HISTORY_SUMMARY_MAX_CHARS',
    '_CW_EXPLICIT_LITERAL_FORMAT_RE',
    'OWNER_CONTEXT_LIMIT',
    'OWNER_CONTEXT_WINDOW',
    '_OWNER_OPS_TARGET_RE',
    '_OWNER_EXPLICIT_TASK_RE',
    '_IMAGE_GEN_INLINE_MARKER_RE',
    '_IMAGE_GEN_MULTILINE_PREFIX_RE',
    '_IMAGE_GEN_PREFIX_PROMPT_RE',
    '_IMAGE_GEN_MARKER_LIKE_RE',
    '_IMAGE_GEN_MARKER_TRAILING_TEXT_LIKE_RE',
    '_IMAGE_GEN_EXPLICIT_COMMAND_RE',
    '_IMAGE_GEN_MENTION_SPACES',
    '_IMAGE_EDIT_FOLLOWUP_ACTION_RE',
    '_IMAGE_EDIT_REFERENCE_REPEAT_RE',
    '_IMAGE_EDIT_CORRECTION_EXPLICIT_RE',
    '_IMAGE_EDIT_CORRECTION_TARGET_FIRST_RE',
    '_IMAGE_EDIT_CORRECTION_TARGET_SUFFIX_RE',
    '_IMAGE_EDIT_GENERIC_EXECUTION_RE',
    '_AGENT_REACH_SOURCE_HEADING_RE',
    'BOT_FILES_ROOT',
    '_SAFE_FILE_NAME',
    '_SAFE_FILE_BATCH',
    'MAX_SEND_FILE_BYTES',
]


BUS_FILE = Path('/run/wechat-dbus-address')

UI_MAIN_MARKER_FILE = Path(
    os.environ.get(
        'CW_UI_MAIN_MARKER',
        '/home/wechat/.cache/wechat-atspi/ui-main.ready',
    )
)

GROUPS_FILE = Path('/var/lib/chatwarden/groups.json')

PAUSE_FILE = Path('/var/lib/chatwarden/bot.pause')

DB_FILE = Path('/var/lib/chatwarden/bot.sqlite3')

# db-native-v3.24-followup-light-gate
FOLLOWUP_GATE_CONTEXT_MESSAGES = max(
    1,
    min(
        6,
        int(os.environ.get('CW_V315_GATE_CONTEXT_MESSAGES', '4')),
    ),
)

FOLLOWUP_GATE_CONTEXT_CHARS = max(
    80,
    min(
        500,
        int(os.environ.get('CW_V315_GATE_CONTEXT_CHARS', '240')),
    ),
)

# db-native-v3.11-agent-reach-router
AGENT_REACH_MARKER = '[开始调查]'

VISION_AGENT_REACH_MARKER = '[开始调查:图像]'

VISION_AGENT_REACH_CONTEXT_MAX_CHARS = 1600

AGENT_REACH_ACK = '等等，我先查一下看看～'

IMAGE_GEN_MARKER = '[开始生图]'

_EXPLICIT_AGENT_REACH_INTENT_RE = re.compile(
    r'(?:'
    r'调查(?:一下|下)'
    r'|深入调查'
    r'|深度调查'
    r'|深挖(?:一下|下)?'
    r'|多方(?:核实|查证|验证)'
    r'|多来源(?:核实|查证|验证)'
    r'|交叉(?:核实|查证|验证)'
    r')',
    re.I,
)

_EXTERNAL_FEEDBACK_TARGET_RE = re.compile(
    r'(?:'
    r'评论区|评论内容|评论|弹幕'
    r'|网友(?:评价|评论|反馈|看法)'
    r'|用户(?:评价|评论|反馈|看法)'
    r'|大家(?:怎么说|怎么评价|怎么看|都在说什么)'
    r')',
    re.I,
)

_EXTERNAL_FEEDBACK_ACTION_RE = re.compile(
    r'(?:'
    r'总结|概括|归纳|梳理|整理|分析|提炼'
    r'|看看|看下|看一下|查看|了解'
    r'|说了啥|都说什么|什么情况|怎么样'
    r')',
    re.I,
)

_CURRENT_EXTERNAL_URL_RE = re.compile(
    r'(?:https?://|b23\.tv/|v\.douyin\.com/)',
    re.I,
)

_PROVIDED_FEEDBACK_INLINE_RE = re.compile(
    r'(?:评论|弹幕|反馈)(?:内容)?(?:如下|\s*[:：])\s*.{12,}',
    re.I | re.S,
)

_EXTERNAL_FEEDBACK_CARD_MARKERS = (
    '【被引用分享卡片完整内容】',
    '【DB-native 相邻分享卡片上下文】',
)

# db-native-v3.52-group-history-summary
_GROUP_HISTORY_TZ = ZoneInfo('Asia/Shanghai')

_GROUP_HISTORY_SUMMARY_ACTION_RE = re.compile(
    r'(?:总结(?:一下|下)?|概括(?:一下|下)?|回顾(?:一下|下)?|'
    r'梳理(?:一下|下)?|归纳(?:一下|下)?|整理(?:一下|下)?|'
    r'复盘(?:一下|下)?)',
    re.I,
)

_GROUP_HISTORY_SUMMARY_CHAT_RE = re.compile(
    r'(?:群里|群聊|聊天(?:内容|记录)?|消息(?:内容|记录)?|'
    r'都聊(?:了)?什么|聊了(?:些)?什么|聊了啥|说了什么)',
    re.I,
)

_GROUP_HISTORY_FROM_TO_NOW_RE = re.compile(
    r'(?:从|自)?\s*'
    r'(?:(今天|今日|昨天|昨日)\s*)?'
    r'(凌晨|早上|上午|中午|下午|傍晚|晚上|晚间)?\s*'
    r'(\d{1,2})\s*'
    r'(?:(?:[:：]\s*(\d{1,2}))|(?:点\s*(半|\d{1,2}\s*分?)?))?\s*'
    r'(?:开始)?\s*(?:到|至)\s*(?:现在|目前|当前|刚刚)',
    re.I,
)

_GROUP_HISTORY_FROM_TO_CLOCK_RE = re.compile(
    r'(?:从|自)\s*'
    r'(?:(今天|今日|昨天|昨日)\s*)?'
    r'(凌晨|早上|上午|中午|下午|傍晚|晚上|晚间)?\s*'
    r'(\d{1,2})\s*'
    r'(?:(?:[:：]\s*(\d{1,2}))|(?:点\s*(半|\d{1,2}\s*分?)?))?\s*'
    r'(?:到|至)\s*'
    r'(凌晨|早上|上午|中午|下午|傍晚|晚上|晚间)?\s*'
    r'(\d{1,2})\s*'
    r'(?:(?:[:：]\s*(\d{1,2}))|(?:点\s*(半|\d{1,2}\s*分?)?))?',
    re.I,
)

_GROUP_HISTORY_RECENT_HOURS_RE = re.compile(
    r'(?:最近|过去)\s*(\d{1,2}(?:\.\d+)?)\s*个?小时',
    re.I,
)

_GROUP_HISTORY_DAYPART_RE = re.compile(
    r'(?:(今天|今日|昨天|昨日)\s*)?'
    r'(今晚|今早|今晨|昨晚|凌晨|早上|上午|中午|下午|傍晚|晚上|晚间)'
    r'(?:的)?\s*'
    r'(?=(?:群里|群聊|聊天|消息|记录|内容|大家|都聊|聊了|说了))',
    re.I,
)

_GROUP_HISTORY_SEMANTIC_HINT_RE = re.compile(
    r'(?:回顾|复盘|总结|概括|梳理|归纳|整理).{0,16}'
    r'(?:群里|群聊|聊天|消息|记录|历史|之前|以前|此前|前面|过去|最近|'
    r'今晚|昨晚|今早|今天|昨天|旧账)'
    r'|(?:之前|以前|此前|前面|前边|历史|旧账|往前|向前|更早|上文|前文|'
    r'刚才|最近|过去|这段时间|这些天|今晚|昨晚|今早).{0,16}'
    r'(?:群里|群聊|聊天|消息|记录|大家|大伙|都|说了|聊了|发生|翻|看|旧账)'
    r'|(?:大家|大伙|群里|群聊).{0,16}'
    r'(?:之前|以前|此前|前面|过去|最近|今晚|昨晚|都聊|都说|发生)'
    r'|(?:再|继续)?\s*(?:往前|向前|更早).{0,8}(?:翻|看|回顾|总结|梳理)',
    re.I,
)

_GROUP_HISTORY_SEMANTIC_TIME_SCOPE_RE = re.compile(
    r'(?:从|自).{0,32}(?:到|至).{0,16}(?:现在|目前|当前|刚刚)'
    r'|(?:从|自).{0,32}(?:以来)'
    r'|(?:刚才|之前|以前|此前|前面|前边|历史|旧账|最近|过去|这段时间|'
    r'这些天|今晚|昨晚|今早|今晨|今天|今日|昨天|昨日|凌晨|早上|上午|'
    r'中午|下午|傍晚|晚上|晚间)',
    re.I,
)

_GROUP_HISTORY_UNREAD_COUNT_RE = re.compile(
    r'(?:我\s*)?(?:有|还有)?\s*(\d{1,5})\s*条'
    r'(?:消息|聊天记录|未读消息)?\s*'
    r'(?:没看|未看|未读|没读|没来得及看)',
    re.I,
)

_GROUP_HISTORY_UNREAD_COUNT_MAX = 3000

_GROUP_HISTORY_ABSENCE_SELF_RE = re.compile(
    r'(?:我\s*(?:不在|没在|离开|不在线|没看(?:群|群聊|消息))'
    r'|我\s*(?:这段时间|这阵子|这会儿|那会儿)\s*(?:不在|没在|离开|不在线))',
    re.I,
)

_GROUP_HISTORY_ABSENCE_HISTORY_RE = re.compile(
    r'(?:群里|群聊|大家|大伙).{0,20}(?:聊|说|发生|干|整|消息|记录)'
    r'|(?:聊|说|发生|干|整).{0,10}(?:什么|啥)',
    re.I,
)

_GROUP_HISTORY_ABSENCE_MIN_GAP_SEC = 30 * 60.0

_GROUP_HISTORY_MEDIA_QUOTE_GARBAGE_RE = re.compile(
    r'eyJwaGFzaCI6[A-Za-z0-9+/=_-]{16,}'
    r'|&#x0A;[^\n]{0,400}wxf\d{3,}',
    re.I,
)

_GROUP_HISTORY_MAX_ROWS = max(
    50,
    min(3000, int(os.environ.get('CW_V352_HISTORY_MAX_ROWS', '1600'))),
)

_GROUP_HISTORY_MAX_CHARS = max(
    12000,
    min(160000, int(os.environ.get('CW_V352_HISTORY_MAX_CHARS', '90000'))),
)

_GROUP_HISTORY_MESSAGE_MAX_CHARS = max(
    100,
    min(600, int(os.environ.get('CW_V352_HISTORY_MESSAGE_MAX_CHARS', '180'))),
)

_GROUP_HISTORY_CLASSIFIER_MAX_WINDOW = timedelta(days=7)

_GROUP_HISTORY_CURSOR_TTL_SEC = max(
    300.0,
    min(
        7200.0,
        float(os.environ.get('CW_V380_HISTORY_CURSOR_TTL_SEC', '1800')),
    ),
)

IMAGE_GEN_ACK = '我画一下～'

IMAGE_EDIT_ACK = '我改一下～'

IMAGE_GEN_DISABLE_FILE = Path('/var/lib/chatwarden/imagegen.disabled')

IMAGE_GEN_KEY_FILE = Path(
    os.environ.get(
        'CW_IMAGEGEN_AUTH_KEY_FILE',
        '/etc/chatwarden/secrets/imagegen_gateway.key',
    )
)

IMAGE_GEN_ENDPOINT = os.environ.get(
    'CW_IMAGEGEN_ENDPOINT',
    'http://127.0.0.1:8652/v1/generate',
).strip()

IMAGE_GEN_HTTP_TIMEOUT = max(
    30,
    int(os.environ.get('CW_IMAGEGEN_HTTP_TIMEOUT', '240')),
)

IMAGE_GEN_MAX_BYTES = 25 * 1024 * 1024

IMAGE_GEN_REFERENCE_MAX_BYTES = 25 * 1024 * 1024

# db-native-v3.11.1-agent-reach-long-reply
AGENT_REACH_SEND_CHUNK_CHARS = max(
    800,
    int(os.environ.get('CW_V311_REACH_SEND_CHUNK_CHARS', '1800')),
)

_CW_HUMAN_FEEL_COOLDOWN_PATTERNS = (
    ('主打一个', re.compile(r'主打一个')),
    ('本质上', re.compile(r'本质上')),
    ('纯/纯纯', re.compile(r'纯纯|纯(?=[\u4e00-\u9fff])')),
    ('修仙', re.compile(r'修仙')),
    ('受刑', re.compile(r'受刑')),
    ('懂得都懂', re.compile(r'懂得都懂')),
    ('看你是想…还是…', re.compile(r'看你是想.{0,36}还是', re.S)),
)

# v3.91: lexical catchphrase cooldown is not enough. A model can keep the
# same roast template while swapping nouns, which still feels highly
# formulaic. Detect only repeated *bot* sentence families in the recent
# window; human wording remains a voice signal and never triggers this block.
_CW_HUMAN_FEEL_STRUCTURE_PATTERNS = (
    (
        '反问「是吧」骨架',
        re.compile(r'是吧(?:[，,。.!！?？~～]|$)'),
    ),
    (
        '「不然呢/难不成」反问',
        re.compile(r'(?:^|[。！？!?])\s*不然呢|难不成'),
    ),
    (
        '「把X当Y」挤兑',
        re.compile(
            r'(?:真)?(?:把|拿).{0,16}当.{1,22}(?:是吧|了|呢|啊|呀|[。！？!?]|$)',
            re.S,
        ),
    ),
    (
        '赛博/工具人类比',
        re.compile(r'赛博|电子宠物|工具人|低保|木鱼|防冷场'),
    ),
    (
        '收费/投币类比',
        re.compile(r'收费|按次|价钱|投币|白嫖|五块'),
    ),
)

_CW_HUMAN_FEEL_MICRO_TURN_RE = re.compile(
    r'(?:笑死|哈哈+|确实|是啊|对啊|绷不住|离谱|绝了|牛(?:啊)?|'
    r'有点累|累(?:死|了)?|困(?:死|了)?|烦(?:死|了)?|饿(?:死|了)?|'
    r'无聊|不想上班|算了|行吧|好吧|懂了|收到|服了)',
    re.I,
)

_CW_HUMAN_FEEL_INFO_REQUEST_RE = re.compile(
    r'(?:什么|怎么|为什么|为何|如何|哪(?:个|些|里)?|多少|几(?:个|点|块)?|'
    r'推荐|分析|调查|查(?:一下|下)?|搜|搜索|帮我|告诉我|有没有|是否|'
    r'好喝吗|能买吗|走势|行情|天气|价格|汇率|新品|评价|介绍|解释|'
    r'说说|讲讲|列(?:一下|下)?|写(?:一下|下)?|画(?:一下|下)?|改(?:一下|下)?|'
    r'生成|播放|报(?:一下|下)?)',
    re.I,
)

TRIGGER = os.getenv('BOT_TRIGGER', 'AI:')

FORMAL_REPLY_SEND_CHUNK_CHARS = max(
    800,
    int(os.getenv('BOT_REPLY_SEND_CHUNK_CHARS', '1600')),
)

# v3.80: user-visible replies intentionally carry no synthetic AI prefix.
# Keep this source-level invariant even if an older container environment still
# exports BOT_REPLY_PREFIX; the rollout must not depend on recreating container
# environment just to hide the visible prefix.
BOT_PREFIX = ''

_RECENT_OUTGOING_TEXT_TTL_SEC = 30.0

ME_LABEL = os.getenv('BOT_ME_LABEL', '用户本人').strip() or '用户本人'

OTHER_LABEL = os.getenv('BOT_OTHER_LABEL', '另一位真人').strip() or '另一位真人'

_DB_PRIMARY_BOT_WXID = os.environ.get(
    'CW_BOT_WXID',
    'wxid_example0082',
)

_DB_PRIMARY_OWNER_WXID = os.environ.get(
    'CW_OWNER_WXID',
    'wxid_example0284',
)

# db-native-v3.17-per-chat-user-memory
_V317_RECENT_MAX = 12

_V317_FACT_MAX = 8

_V317_INJECT_RECENT_MAX = 3

_V317_INJECT_FACT_MAX = 4

_V317_RECENT_MAX_CHARS = 180

_V317_FACT_MAX_CHARS = 140

_V317_FACT_MARKERS = (
    "我叫",
    "叫我",
    "你可以叫我",
    "大家叫我",
    "我的名字是",
    "我喜欢",
    "我最喜欢",
    "我更喜欢",
    "我不喜欢",
    "我讨厌",
    "我偏好",
    "我爱吃",
    "我爱喝",
    "我爱看",
    "我爱玩",
    "我从事",
    "我的工作是",
    "我的职业是",
)

_V317_SENSITIVE_MARKERS = (
    "身份证",
    "护照",
    "银行卡",
    "信用卡",
    "手机号",
    "电话号码",
    "家庭住址",
    "详细地址",
    "密码",
    "token",
    "api key",
    "apikey",
    "性取向",
    "同性恋",
    "宗教",
    "基督教",
    "伊斯兰",
    "穆斯林",
    "佛教",
    "党员",
    "政治立场",
    "确诊",
    "抑郁症",
    "焦虑症",
)

# db-native-v3.40-per-group-shared-memory
# Keep this deliberately small: shared memory is a durable set of notable
# group episodes, not another rolling transcript.  A qualifying message stores
# a compact local episode so short conclusions such as "那就这么定了" retain
# enough preceding context to make sense later without another LLM call.
_V340_MEMORY_MAX = 20

_V340_INJECT_MAX = 3

_V340_EPISODE_MESSAGES = 3

_V340_MESSAGE_MAX_CHARS = 160

_V340_ANCHOR_MAX_CHARS = 240

_V340_DIRECT_MARKERS = (
    "就这么定",
    "那就定",
    "就定这个",
    "决定了",
    "结论是",
    "统一口径",
    "统一叫",
    "以后就叫",
    "以后叫",
    "以后默认",
    "以后统一",
    "公认",
    "一致认为",
    "达成共识",
    "共识是",
    "约定好",
    "暗号是",
    "外号是",
    "简称是",
    "本群的梗是",
    "这个梗以后",
    "群梗以后",
)

_V340_COLLECTIVE_MARKERS = (
    "我们",
    "大家",
    "咱们",
    "群里",
    "这个群",
    "本群",
    "全群",
)

_V340_DURABLE_MARKERS = (
    "决定",
    "定了",
    "约定",
    "统一",
    "默认",
    "以后",
    "今后",
    "结论",
    "共识",
    "公认",
    "负责",
    "轮到",
    "组织",
    "集合",
    "聚餐",
    "约饭",
    "活动",
    "安排",
    "暗号",
    "外号",
    "昵称",
    "简称",
    "梗",
)

# db-native-v3.5-session-diet
_V35_CONTEXT_MESSAGES = max(
    20,
    int(os.environ.get("CW_V35_CONTEXT_MESSAGES", "20")),
)

_V35_MESSAGE_MAX_CHARS = max(
    120,
    int(os.environ.get("CW_V35_MESSAGE_MAX_CHARS", "500")),
)

_V3170_SETTINGS_CACHE = {"mtime": None, "raw": None}

_V3170_GROUP_SETTING_KEYS = {
    "muted": bool,
    "proactive_enabled": bool,
    "welcome_enabled": bool,
    "proactive_any_bot_cooldown_sec": float,
    "proactive_send_cooldown_sec": float,
}

_V3170_RUNTIME_SETTING_KEYS = {"private_nonowner_enabled": bool}

# db-native-v3.193-private-per-contact
# groups.json top-level ``private_contacts`` maps an exact private peer wxid to
# {"enabled": bool, "name": display-only label}. An entry overrides the global
# ``private_nonowner_enabled`` default for that one peer only; the owner's own
# private chat is never gated. Keys are exact wxids, never display names.
_V3193_PRIVATE_NAME_MAX = 64

_V3171_PINNED_MAX = 8

_V3171_MANUAL_GROUP_MAX = 10

_V3171_GROUP_SETTING_SENDER = "setting:group"

_V3171_MEMBER_MAPS = {"member_labels", "verified_members"}

GROUP_MESSAGES_KEEP = 10000


def _env_int(name, default):
    try:
        return int(str(os.environ.get(name, default)).strip())
    except ValueError:
        return int(default)


# v3.205 retention of the bot's own chat history (group_messages). The legacy
# single global cap of GROUP_MESSAGES_KEEP rows let busy groups push quiet
# ones out within about three days. With a per-chat cap every chat keeps its
# own newest rows; the total ceiling and the age limit bound the file.
# CW_GROUP_MESSAGES_PER_CHAT=0 restores the legacy global cap.
GROUP_MESSAGES_PER_CHAT = max(0, _env_int("CW_GROUP_MESSAGES_PER_CHAT", 50000))
GROUP_MESSAGES_MAX_TOTAL = max(
    GROUP_MESSAGES_KEEP,
    _env_int("CW_GROUP_MESSAGES_MAX_TOTAL", 300000),
)
GROUP_MESSAGES_MAX_AGE_DAYS = max(0, _env_int("CW_GROUP_MESSAGES_MAX_AGE_DAYS", 30))

_OPEN_DB_SCHEMA_OBJECTS = (
    "group_messages",
    "idx_group_messages_chat_id",
    "v317_user_memory",
    "v317_user_memory_meta",
    "v340_group_memory",
    "v340_group_memory_meta",
    "v380_history_cursor",
    "v395_history_backfill_seen",
)

RUNTIME_PRIVACY_REPLIES = (
    '这个就不扒底裤了 😏 底层模型和后台配置我不公开。',
    '哈哈，这个属于后台机密。你就当我是群里的 AI 搭子就行。',
    '模型和 Agent 这些幕后信息先保密哈，聊点别的 😎',
    '这个不能剧透，底层配置不对外公开。有什么事直接使唤我就行。',
    '后台是哪套就不报户口了 😂 我负责在群里干活。',
    '这个问题我就卖个关子吧，模型和运行配置不公开。',
    '想套我底细是吧 😂 底层模型、Agent 和部署信息保密。',
    '具体哪套模型我不公布哈，反正现在在这个群里给你们打工。',
)

# v3.92: prompt context is optional enrichment and must never become an
# unbounded pre-dispatch dependency. Normal snapshots are usually a few
# milliseconds. History-summary requests retain a wider budget because their
# existing semantic range classifier alone may legitimately use up to 10s.
_CONTEXT_FREEZE_NORMAL_TIMEOUT_SEC = max(
    0.5,
    min(
        8.0,
        float(os.environ.get('CW_V392_CONTEXT_FREEZE_TIMEOUT_SEC', '3')),
    ),
)

_CONTEXT_FREEZE_HISTORY_TIMEOUT_SEC = max(
    22.0,
    min(
        30.0,
        float(os.environ.get('CW_V392_HISTORY_FREEZE_TIMEOUT_SEC', '22')),
    ),
)

_CW_HISTORY_SUMMARY_MAX_CHARS = 1000

_CW_EXPLICIT_LITERAL_FORMAT_RE = re.compile(
    r'(?:markdown|\bmd\b|json|yaml|xml|代码块|代码格式|保留格式|原样输出)',
    re.I,
)

OWNER_CONTEXT_LIMIT = 6

OWNER_CONTEXT_WINDOW = 600

_OWNER_OPS_TARGET_RE = re.compile(
    r'(?:服务器|服务|日志|配置|容器|进程|端口|代码|脚本|文件|部署|安装|'
    r'额度|账号|证书|VPS|docker|nginx|systemd)',
    re.I,
)

# v3.196: an explicit owner command prefix is the owner's own routing choice;
# it needs no model judgement (the 2026-11-10 "执行管理员任务：…" and
# "タスクを実行：…" were lost to owner_intent timeouts). Checked only after exact
# sender verification and only at the start of the current message.
_OWNER_EXPLICIT_TASK_RE = re.compile(
    r'^\s*(?:@\S+\s*)*'
    r'(?:(?:执行|运行|开始)?管理员任务|(?:执行|运行)任务|タスクを?実行(?:して)?|/task)'
    r'\s*[:：]\s*\S',
    re.I,
)

_IMAGE_GEN_INLINE_MARKER_RE = re.compile(
    r'^\[开始生图\s*[:：]\s*(?P<prompt>[^\]\r\n]{1,4000})\]$',
    re.I,
)

_IMAGE_GEN_MULTILINE_PREFIX_RE = re.compile(
    r'^\[开始生图\][ \t]*\r?\n',
    re.I,
)

_IMAGE_GEN_PREFIX_PROMPT_RE = re.compile(
    r'^\[开始生图\](?P<prompt>[\s\S]{1,4000})$',
    re.I,
)

_IMAGE_GEN_MARKER_LIKE_RE = re.compile(
    r'^\[开始生图[^\]\r\n]{0,4000}\]$',
    re.I,
)

_IMAGE_GEN_MARKER_TRAILING_TEXT_LIKE_RE = re.compile(
    r'^\[开始生图\][ \t]+[^\r\n]{1,4000}$',
    re.I,
)

_IMAGE_GEN_EXPLICIT_COMMAND_RE = re.compile(
    r'^生成图片[：:][\s\u2005\u2009\u202f]*(?P<prompt>[\s\S]*)$'
)

_IMAGE_GEN_MENTION_SPACES = ' \t\r\n\u2005\u2009\u202f\u00a0'

_IMAGE_EDIT_FOLLOWUP_ACTION_RE = re.compile(
    r'^(?:@\S+[\s\u2005\u2009\u202f]*)?'
    r'(?:(?:请|麻烦|帮我|给我)\s*)?'
    r'(?:(?:再|重新)\s*)?'
    r'(?:(?:把|将).{0,30}|(?:裤子|长裤|短裤|外套|上衣|衣服|风衣|大衣|西装|夹克|衬衫|裙子|鞋子|鞋|帽子|口袋(?:盖)?|领子|衣领|袖子|袖口|纽扣|扣子|颜色|配色|材质|面料|图案|背景|头发|发型|脸|面部|主体|人物).{0,20})?'
    r'(?:改成|改为|换成|换为|换上|穿上|戴上|加上|添加|去掉|去除|移除|'
    r'删除|删掉|删去|擦掉|擦除|抹掉|抹除|抠掉|拿掉|裁掉|P掉|P走|P成|'
    r'P上|p掉|p走|p成|p上|变成|还原成|还原为|恢复成|恢复为|恢复原样|还原原样|替换成|替换|做成|风格化|重绘|重画|'
    r'重写|重新写|重做(?:一版)?|重新做|再来一版|再做一版)',
    re.I,
)

_IMAGE_EDIT_REFERENCE_REPEAT_RE = re.compile(
    r'^(?:'
    r'(?:再\s*)?(?:给我\s*)?画\s*一个'
    r'|(?:照|按|按照)\s*(?:这个|这张(?:图|图片|照片)?|这幅(?:图|图片)?)\s*'
    r'(?:再\s*)?(?:画|做|来)\s*一个'
    r'|(?:再\s*)?生成\s*(?:一\s*张\s*)?(?:类似|相似)(?:的)?(?:图|图片|照片)?'
    r'|(?:照|按|按照)\s*(?:这个|这张(?:图|图片|照片)?|这幅(?:图|图片)?)\s*'
    r'(?:再\s*)?生成\s*(?:一\s*张|一个)(?:\s*(?:类似|相似)(?:的)?(?:图|图片|照片)?)?'
    r'|(?:以|基于|根据)\s*(?:该|这个|这件|这条|这张|此)'
    r'[^，。！？!?\n]{0,20}(?:(?:为|作为)\s*)?(?:基础|参考)\s*[，,:： ]*'
    r'(?:生成|绘制|制作|画)\s*(?:一\s*张|一\s*幅|一个)?'
    r'(?:[^？?\n]{0,80}(?:图|图片|照片)|[^？?\n]{1,80}照)'
    r'|来\s*(?:个|一个)\s*类似的'
    r'|再\s*来\s*(?:一个|一版)'
    r')\s*[。！!~～]*$',
    re.I,
)

_IMAGE_EDIT_CORRECTION_EXPLICIT_RE = re.compile(
    r'(?:^|[，,:：。；;\s])'
    r'(?:应(?:该|当)?|正确(?:的)?|准确(?:的)?)'
    r'(?:写|改|换)?(?:成|为|是)\s*'
    r'[「『“\"]?[^，。！？!?\n]{1,80}',
    re.I,
)

_IMAGE_EDIT_CORRECTION_TARGET_FIRST_RE = re.compile(
    r'^(?P<target>[^，,:：。！？!?\n]{2,80})'
    r'[，,:： ]*'
    r'(?:这几个字|这些字|这个字|文字|标题|文案|名字|名称)'
    r'.{0,16}?'
    r'(?:改错了|写错了|弄错了|搞错了|错了|不对)'
    r'[。！! ]*$',
    re.I,
)

_IMAGE_EDIT_CORRECTION_TARGET_SUFFIX_RE = re.compile(
    r'^(?P<target>[^，,:：。！？!?\n]{2,80})'
    r'(?:才对|才正确|才是对的)'
    r'[。！! ]*$',
    re.I,
)

_IMAGE_EDIT_GENERIC_EXECUTION_RE = re.compile(
    r'(?:^|[，,:：。；;\s])'
    r'(?:那(?:你)?|就|请|麻烦|帮我|给我)?\s*'
    r'(?:修改|编辑|调整|改一下|修一下)\s*'
    r'(?:一下\s*)?(?:这个|这张|这幅)?\s*(?:图|图片|照片)'
    r'(?:吧|啊|呀|。|！|!|~|～|\s|@|＠|$)',
    re.I,
)

_AGENT_REACH_SOURCE_HEADING_RE = re.compile(
    r'(?im)^(?:来源|参考来源|资料来源|参考资料|sources?)\s*[:：]?\s*'
)

BOT_FILES_ROOT = Path('/home/wechat/.cache/bot-files')

_SAFE_FILE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}')

_SAFE_FILE_BATCH = re.compile(r'[A-Za-z0-9_-]{1,40}')

MAX_SEND_FILE_BYTES = 20 * 1024 * 1024
