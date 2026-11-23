"""v3.170 owner-only private settings commands.

Only the exact configured owner's one-to-one private chat may use ``/设置``.
Commands are parsed deterministically (never by a model) and every write goes
through the atomic groups.json writers in ``core``; the existing mtime hot
reload then propagates the change to GroupWorkers. Groups are addressed by the
index shown in ``/设置 状态`` and every pending confirmation is bound to the
exact group name + chat_username, so an index shift can never retarget it.
"""
import difflib
import hashlib
import re
import threading
import time

from . import llm_client, core, memory_docs


COMMAND = "/设置"
CONFIRM_TTL_SECONDS = 120.0
SELECTION_TTL_SECONDS = 300.0
SEARCH_LIMIT = 10
_SPACES = " \t\r\n    　"
_ON = {"开", "开启", "打开", "on", "1", "是"}
_OFF = {"关", "关闭", "off", "0", "否"}
_UNIT_SECONDS = {
    "": 1, "秒": 1, "s": 1,
    "分": 60, "分钟": 60, "min": 60, "m": 60,
    "小时": 3600, "时": 3600, "h": 3600,
}
_DURATION_RE = re.compile(r"^(\d{1,6})(秒|s|分钟|分|min|m|小时|时|h)?$", re.I)
_COOLDOWN_KEYS = {
    "主动冷却": "proactive_any_bot_cooldown_sec",
    "主动间隔": "proactive_send_cooldown_sec",
}
_DEFAULT_COOLDOWN = 2400.0
_WS = "[" + re.escape(_SPACES) + "]+"
_PERSONALITY_TEXT_RE = re.compile(
    r"^/设置" + _WS + r"\d{1,3}" + _WS + r"人格" + _WS + r"(.+)$", re.S,
)
_PROFILE_ADD_RE = re.compile(
    r"^/设置" + _WS + r"\d{1,3}" + _WS + r"成员" + _WS + r"#\d{1,2}" + _WS
    + r"画像" + _WS + r"加" + _WS + r"(.+)$", re.S,
)
_GROUP_MEMORY_ADD_RE = re.compile(
    r"^/设置" + _WS + r"\d{1,3}" + _WS + r"群记忆" + _WS + r"加" + _WS + r"(.+)$",
    re.S,
)
_MEMBER_REF_RE = re.compile(r"^#(\d{1,2})$")
# v3.193 per-contact private switch: "/设置 私聊 【名字】 开|关|默认".
_PRIVATE_NAME_RE = re.compile(
    r"^/设置" + _WS + r"私聊" + r"[" + re.escape(_SPACES) + r"]*【([^】]{1,40})】"
    + r"[" + re.escape(_SPACES) + r"]*(\S+)$", re.S,
)
_PRIVATE_ACTIONS = _ON | _OFF | {"默认", "忘记"}
PRIVATE_LIST_LIMIT = 10
_VALUE_ERRORS = {
    "sensitive": "这条内容疑似敏感信息，没有保存",
    "full": "已达上限，先删除再添加",
    "duplicate": "已经有这条了",
    "empty": "内容不能为空",
    "index": "编号无效，先重新查看列表",
    "missing": "这条已经不存在了，先重新查看列表",
}
# v3.175 global per-function Gateway model policy (gateway-owned).
MODEL_FUNCTIONS = ("聊天", "搜索", "研究", "主动", "识图", "语音", "判定", "记忆", "人设")
MODEL_LIST_TTL_SECONDS = 600.0
MODEL_LIST_LIMIT = 40
MODEL_IDENTITY = {"name": "全局模型", "chat_username": ""}
# v3.178 natural-language /设置 (translated by the main chat model).
NL_IDENTITY = {"name": "自然语言设置", "chat_username": ""}
NL_MAX_COMMANDS = 5
NL_MAX_TEXT = 1000
# Pending kinds that keep their own preview/confirmation even inside a
# confirmed natural-language batch (the owner has not seen that text yet).
_NL_MANUAL_KINDS = {"personality_set", "personality_default"}
_SWITCH_ITEMS = {"总开关", "主动发言", "欢迎"}
_FREE_ITEMS = {"人格", "成员", "群记忆"}
# v3.179: the translator names a member as 【name】 (typos allowed); the Bot
# resolves it by fuzzy match against that group's members.
_NL_MEMBER_RE = re.compile(r"^(/设置[" + re.escape(_SPACES) + r"]+(\d{1,3})[" + re.escape(_SPACES)
                           + r"]+成员[" + re.escape(_SPACES) + r"]*)【([^】]{1,40})】(.*)$", re.S)
NL_CONTEXT_TTL_SECONDS = 300.0
FUZZY_MIN_SCORE = 0.5
FUZZY_MARGIN = 0.1
_MODEL_REASONS = {
    "unknown_model": "目录里没有这个模型",
    "capability_check_failed": "功能测试没通过（例如没有联网搜索或没有真正看图）",
    "too_slow": "太慢",
    "timeout": "超时",
    "queue_timeout": "排队超时",
    "upstream_failed": "上游调用失败",
    "tool_policy_violation": "调用了不允许的工具",
    "tool_budget": "工具调用次数超限",
    "invalid_structured_output": "结构化输出格式不对",
    "internal_output": "输出里带了内部内容",
    "image_not_viewed": "没有真正读取图片",
    "audio_not_viewed": "没有真正读取音频",
    "quota_exhausted": "额度用尽或被限流",
    "auth_required": "需要重新登录",
    "busy": "网关繁忙",
}


# Channel label for a model id: the provider prefix before the first "/".
def _base_id(model_id):
    return str(model_id or "").partition("@")[0]


def remember_model_sources(items, clock=time.monotonic):
    """Hook for gateways that report richer channel metadata; unused by default."""
    del items, clock
    return False


def refresh_model_sources(loader, clock=time.monotonic):
    del loader, clock


def model_channel(model_id):
    """(rank, channel label) for one catalog id."""
    base = _base_id(model_id)
    if "/" not in base:
        return 0, "默认渠道"
    return 10, base.split("/", 1)[0]


def _model_note(model_id):
    del model_id
    return ""


_EFFORT_ORDER = {"": 0, "none": 1, "minimal": 2, "low": 3, "medium": 4, "high": 5, "xhigh": 6, "max": 7}


def _model_group_rank(item):
    """Channel first, then model name, then reasoning effort (@suffix)."""
    mid = str(item.get("id") or "")
    rank, channel = model_channel(mid)
    base, _, effort = mid.partition("@")
    return (rank, channel, base, _EFFORT_ORDER.get(effort, 8), effort)


def model_with_channel(model_id):
    return f"{model_id}（{model_channel(model_id)[1]}）"

HELP = (
    "私聊设置命令（仅主人可用）：\n"
    "/设置 状态 —— 总览和群编号\n"
    "/设置 <群号> —— 查看该群设置\n"
    "/设置 私聊 —— 私聊总览：默认开关、单独设置的人、最近私聊的人（带 #编号）\n"
    "/设置 私聊 开|关 —— 除主人外私聊的默认开关（没单独设置的人按它走）\n"
    "/设置 私聊 搜 <关键词> —— 搜索联系人，结果带 #编号（5分钟内有效）\n"
    "/设置 私聊 #编号 开|关|默认 —— 单独设置这个人；默认=取消单独设置、跟随默认开关\n"
    "/设置 私聊 【名字】 开|关|默认 —— 按名字单独设置（错别字也能认，不唯一时让你选）\n"
    "/设置 私聊 #编号 忘记 或 【名字】 忘记 —— 清除机器人对这个人的私聊记忆（摘要和存档），开关不变\n"
    "/设置 <群号> 总开关 开|关 —— 关=完全不处理：不记录、不回复（需确认）；新拉进的群默认关\n"
    "/设置 <群号> 主动发言 开|关\n"
    "/设置 <群号> 主动冷却 <时长>|默认 —— 任意机器人发言后多久可主动发言\n"
    "/设置 <群号> 主动间隔 <时长>|默认 —— 两次主动发言的最小间隔\n"
    "/设置 <群号> 欢迎 开|关 —— 入群欢迎\n"
    "/设置 <群号> 人格 —— 查看完整人格\n"
    "/设置 <群号> 人格 默认|随机|<人格要求> —— 预览后需确认\n"
    "/设置 <群号> 成员 <关键词> —— 搜索成员，结果带 #编号（5分钟内有效）\n"
    "/设置 <群号> 成员 #编号 —— 查看该成员\n"
    "/设置 <群号> 成员 #编号 称呼 <称呼>|清除\n"
    "/设置 <群号> 成员 #编号 认证 <标签>|取消 —— 需确认\n"
    "/设置 <群号> 成员 #编号 画像 —— 主人设定 + 跨群整理画像\n"
    "/设置 <群号> 成员 #编号 画像 加 <内容>|删 <编号>|重建|清空 —— 清空需确认\n"
    "/设置 <群号> 群记忆 —— 群内设定 + 整理后的群记忆\n"
    "/设置 <群号> 群记忆 加 <内容>|删 <编号>|重建 —— 删除需确认\n"
    "/设置 模型 —— 各功能当前使用的模型（全局）\n"
    "/设置 模型 列表 [关键词] —— 可用模型，按来源分组带编号（10分钟内有效）\n"
    "/设置 模型 <功能> <编号|模型名> —— 先实测验证，通过后需确认\n"
    "/设置 模型 <功能>、<功能>… <编号|模型名> —— 多个功能一起换，逐项实测，只换通过的\n"
    "/设置 模型 <功能>[、<功能>…]|全部 默认 —— 恢复默认模型（需确认）\n"
    "功能：聊天 搜索 研究 主动 识图 语音 判定 记忆 人设\n"
    "/设置 <自然语言> —— 直接描述，如“/设置 把小卫士群的主动发言关掉”“/设置 看看小明同学里graceless的画像”；"
    "群名、成员名有错别字也能认；查看类直接执行，修改类列出后回复“确认”执行\n"
    "确认 / 取消 —— 处理待确认操作（2分钟内有效）\n"
    "时长示例：2400、40分钟、1小时"
)


def is_command(text):
    value = str(text or "").strip(_SPACES)
    return value == COMMAND or value.startswith(COMMAND + " ") or any(
        value.startswith(COMMAND + ch) for ch in _SPACES
    )


def _tokens(text):
    value = str(text or "").strip(_SPACES)[len(COMMAND):]
    return [t for t in re.split("[" + re.escape(_SPACES) + "]+", value) if t]


def _switch(value):
    lowered = str(value or "").strip().lower()
    if lowered in _ON:
        return True
    if lowered in _OFF:
        return False
    return None


def parse_duration(value):
    match = _DURATION_RE.match(str(value or "").strip())
    if not match:
        return None
    seconds = int(match.group(1)) * _UNIT_SECONDS[(match.group(2) or "").lower()]
    if not 0 <= seconds <= 86400:
        return None
    return float(seconds)


def _fmt_duration(seconds):
    seconds = int(round(float(seconds)))
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600}小时"
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


def _yn(value):
    return "开" if value else "关"


def _flag(cfg, key):
    value = (cfg or {}).get(key, True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _is_group_token(token):
    return bool(re.fullmatch(r"\d{1,3}", str(token or "")))


_FUNCTION_SEP_RE = re.compile(r"[、，,/／+＋;；]|和|及")


def _model_functions(tokens):
    """Parse one or more function tokens ("聊天、识图、主动", "聊天 识图", "全部").

    Returns the functions in canonical order, or None when any piece is not a
    function (v3.182).
    """
    pieces = [p for t in tokens for p in _FUNCTION_SEP_RE.split(t) if p]
    if not pieces or any(p not in MODEL_FUNCTIONS and p != "全部" for p in pieces):
        return None
    if "全部" in pieces:
        return tuple(MODEL_FUNCTIONS)
    return tuple(f for f in MODEL_FUNCTIONS if f in pieces)


def _functions_label(functions):
    functions = tuple(functions)
    return "所有功能" if functions == tuple(MODEL_FUNCTIONS) else "、".join(functions)


def _is_structured(tokens):
    """True when the deterministic parser owns this command (v3.178)."""
    if not tokens:
        return True
    head, args = tokens[0], tokens[1:]
    if head in {"帮助", "help", "?", "确认", "取消", "状态"}:
        return True
    if head == "私聊":
        if not args:
            return True  # overview
        if len(args) == 1:
            return _switch(args[0]) is not None or bool(_MEMBER_REF_RE.match(args[0]))
        if args[0] in {"搜", "搜索"}:
            return len(args) == 2
        if _MEMBER_REF_RE.match(args[0]):
            return len(args) == 2 and args[1] in _PRIVATE_ACTIONS
        joined = " ".join(args)
        match = re.fullmatch(r"【[^】]{1,40}】 ?(\S+)", joined)
        return bool(match) and match.group(1) in _PRIVATE_ACTIONS
    if head == "模型":
        return (not args or (args[0] == "列表" and len(args) <= 2)
                or (len(args) >= 2 and _model_functions(args[:-1]) is not None))
    if not _is_group_token(head):
        return False
    if not args:
        return True
    item = args[0]
    if item in _SWITCH_ITEMS:
        return len(args) == 2 and _switch(args[1]) is not None
    if item in _COOLDOWN_KEYS:
        return len(args) == 2 and (args[1] == "默认" or parse_duration(args[1]) is not None)
    return item in _FREE_ITEMS


def _is_read_only(tokens):
    if not tokens:
        return True
    head, args = tokens[0], tokens[1:]
    if head in {"帮助", "help", "?", "状态"}:
        return True
    if head == "私聊":
        return not args or (len(args) == 1 and _switch(args[0]) is None) or args[0] in {"搜", "搜索"}
    if head == "模型":
        return not args or args[0] == "列表"
    if _is_group_token(head):
        if not args or (len(args) == 1 and args[0] in {"人格", "群记忆"}):
            return True
        if args[0] == "成员" and len(args) >= 2:
            # search, member view, or viewing that member's profile
            return len(args) == 2 or (len(args) == 3 and args[2] == "画像")
    return False


def _fold(text):
    return re.sub(r"[\s\W_]+", "", str(text or "").casefold())


def _name_score(query, name):
    """1.0 exact, 0.9 containment, else best fuzzy ratio (whole or window)."""
    q, n = _fold(query), _fold(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if q in n or n in q:
        return 0.9
    best = difflib.SequenceMatcher(None, q, n).ratio()
    for width in {len(q) - 1, len(q), len(q) + 1}:
        if 0 < width <= len(n):
            for i in range(len(n) - width + 1):
                best = max(best, difflib.SequenceMatcher(None, q, n[i:i + width]).ratio())
    return best


def fuzzy_members(candidates, query):
    """[(score, messages, wxid, info)] best first, above FUZZY_MIN_SCORE."""
    scored = []
    for wxid, info in candidates.items():
        names = [n for n in (info.get("label"), info.get("nickname"), info.get("contact")) if n]
        score = max((_name_score(query, n) for n in names), default=0.0)
        if score >= FUZZY_MIN_SCORE:
            scored.append((score, int(info.get("messages") or 0), wxid, info))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return scored


def _nl_shape(line):
    """(parse_text, group_token, member_ref) for one translated line.

    member_ref is ("name", text) for 【name】, ("ref", "#n") for a selection
    number, or None. parse_text substitutes "#1" so the deterministic parser
    can check the rest of the command.
    """
    match = _NL_MEMBER_RE.match(line)
    if match:
        return match.group(1) + "#1" + match.group(4), match.group(2), ("name", match.group(3).strip())
    tokens = _tokens(line) if is_command(line) else []
    if len(tokens) >= 3 and tokens[1] == "成员" and _MEMBER_REF_RE.match(tokens[2]):
        parse_text = re.sub(r"(成员[" + re.escape(_SPACES) + r"]+)#\d{1,2}", r"\g<1>#1", line, count=1)
        return parse_text, tokens[0], ("ref", tokens[2])
    return line, (tokens[0] if tokens else ""), None


def _nl_validate(lines):
    """Return (shapes, problem). Every line must be a deterministic command."""
    if len(lines) > NL_MAX_COMMANDS:
        return [], f"一次最多 {NL_MAX_COMMANDS} 条设置"
    shapes = []
    for line in lines:
        line = line.strip(_SPACES)
        parse_text, group_token, member = _nl_shape(line)
        tokens = _tokens(parse_text) if is_command(parse_text) else None
        if (not tokens or tokens[0] in {"确认", "取消"} or not _is_structured(tokens)
                or len(line) > 600
                or (member is None and tokens[0] != "私聊" and re.search(r"#\d", line))):
            return [], "翻译出的命令不符合格式，没有执行"
        shapes.append({"line": line, "parse": parse_text, "group": group_token, "member": member,
                       "read_only": _is_read_only(tokens)})
    return shapes, ""


class OwnerSettings:
    """Per-owner-private-chat command handler with one pending confirmation."""

    def __init__(self, send, group_worker_lookup=None, clock=time.monotonic):
        self._send = send
        self._lookup = group_worker_lookup
        self._clock = clock
        self._pending = None
        self._selection = None
        self._model_list = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ entry
    def handle(self, text):
        """Return True when ``text`` was consumed as an owner settings turn."""
        value = str(text or "").strip(_SPACES)
        if not is_command(value):
            if value in {"确认", "取消"} and self._live_pending() is not None:
                return self._resolve_pending(value == "确认")
            return False
        tokens = _tokens(value)
        try:
            if _is_structured(tokens):
                self._dispatch(tokens, value)
            else:
                self._natural_language(value)
        except Exception as exc:
            core.log(
                "[v3.170-owner-settings] "
                f"command={'/'.join(tokens[:2]) or 'help'} "
                f"failed=yes error={type(exc).__name__} changed=no"
            )
            self._reply("设置失败，配置没有改动")
        return True

    def _reply(self, text):
        self._send(str(text))

    # ------------------------------------------------------------ groups
    @staticmethod
    def _groups():
        return core.load_groups_config()

    def _group_at(self, token):
        if not re.fullmatch(r"\d{1,3}", str(token or "")):
            return None
        groups = self._groups()
        index = int(token)
        if not 1 <= index <= len(groups):
            return None
        return groups[index - 1]

    @staticmethod
    def _identity(cfg):
        return {
            "name": cfg["name"],
            "chat_username": str(cfg.get("chat_username") or ""),
        }

    # ------------------------------------------------------------ dispatch
    def _dispatch(self, tokens, raw_text):
        if not tokens or tokens[0] in {"帮助", "help", "?"}:
            self._reply(HELP)
            return
        head = tokens[0]
        if head in {"确认", "取消"}:
            if self._live_pending() is None:
                self._reply("当前没有待确认的操作")
                return
            self._resolve_pending(head == "确认")
            return
        if head == "状态":
            self._reply(self._status_text())
            return
        if head == "私聊":
            self._private(tokens[1:], raw_text)
            return
        if head == "模型":
            self._model(tokens[1:])
            return
        cfg = self._group_at(head)
        if cfg is None:
            self._reply("没有这个群号，先发 /设置 状态 查看编号")
            return
        if len(tokens) == 1:
            self._reply(self._group_text(head, cfg))
            return
        item, args = tokens[1], tokens[2:]
        if item == "总开关":
            self._mute_switch(cfg, args)
        elif item == "主动发言":
            self._bool_setting(cfg, args, "proactive_enabled", item, "主动发言")
        elif item == "欢迎":
            self._bool_setting(cfg, args, "welcome_enabled", item, "入群欢迎")
        elif item in _COOLDOWN_KEYS:
            self._cooldown_setting(cfg, args, item)
        elif item == "人格":
            self._personality(cfg, args, raw_text)
        elif item == "成员":
            self._member(cfg, args, raw_text)
        elif item == "群记忆":
            self._group_memory(cfg, args, raw_text)
        else:
            self._reply("不认识这个设置项，发 /设置 帮助 查看用法")

    # ------------------------------------------------------------ views
    def _status_text(self):
        overrides = core.private_contact_overrides()
        on = sum(1 for v in overrides.values() if v["enabled"])
        private_line = "私聊（除主人外）：" + _yn(core.private_nonowner_enabled())
        if overrides:
            private_line += (f"（默认），另有 {len(overrides)} 人单独设置"
                             f"（开 {on} / 关 {len(overrides) - on}），详见 /设置 私聊")
        lines = [
            private_line,
            "群列表：",
        ]
        for index, cfg in enumerate(self._groups(), 1):
            muted = cfg.get("muted") is True
            lines.append(
                f"{index}. {cfg['name']} —— "
                f"{'关闭' if muted else '正常'}"
                f" / 主动{_yn(_flag(cfg, 'proactive_enabled'))}"
                f" / 欢迎{_yn(_flag(cfg, 'welcome_enabled'))}"
            )
        pending = self._live_pending()
        if pending is not None:
            lines.append("待确认：" + pending["summary"])
        return "\n".join(lines)

    @staticmethod
    def _group_text(index, cfg):
        personality = str(cfg.get("personality") or "").strip()
        default = str(cfg.get("default_personality") or personality).strip()
        custom = personality != default

        def cooldown(key):
            if key in cfg:
                return _fmt_duration(cfg[key])
            return _fmt_duration(_DEFAULT_COOLDOWN) + "（默认）"

        preview = personality[:80] + ("…" if len(personality) > 80 else "")
        return "\n".join([
            f"{index}. {cfg['name']}",
            "总开关：" + ("关（不处理）" if cfg.get("muted") is True else "开"),
            "主动发言：" + _yn(_flag(cfg, "proactive_enabled")),
            "主动冷却：" + cooldown("proactive_any_bot_cooldown_sec"),
            "主动间隔：" + cooldown("proactive_send_cooldown_sec"),
            "入群欢迎：" + _yn(_flag(cfg, "welcome_enabled")),
            f"人格：{'自定义' if custom else '默认'}，{len(personality)}字",
            "人格开头：" + (preview or "（空）"),
        ])

    # ------------------------------------------------------------ writes
    def _write(self, identity, *, set_values=None, unset=()):
        core.update_group_settings(
            identity["name"],
            expected_chat_username=identity["chat_username"],
            set_values=set_values or {},
            unset=unset,
        )

    def _private_switch(self, args):
        value = _switch(args[0]) if len(args) == 1 else None
        if value is None:
            self._reply("用法：/设置 私聊 开|关")
            return
        core.update_runtime_setting("private_nonowner_enabled", value)
        core.log(
            "[v3.170-owner-settings] "
            f"action=private_nonowner value={_yn(value)} changed=yes"
        )
        overrides = core.private_contact_overrides()
        suffix = (f"（另有 {len(overrides)} 人单独设置，不受影响；发 /设置 私聊 查看）"
                  if overrides else "")
        self._reply("除主人以外的私聊回复默认已" + ("开启" if value else "关闭") + suffix)

    # ------------------------------------------------------------ private (v3.193)
    @staticmethod
    def _private_name(wxid, info):
        return (info.get("label") or info.get("contact")
                or "未命名联系人（" + OwnerSettings._member_tag(wxid) + "）")

    @staticmethod
    def _private_state(info):
        if info.get("override") is None:
            return _yn(core.private_nonowner_enabled()) + "（跟随默认）"
        return _yn(info["override"]) + "（单独设置）"

    def _private_line(self, wxid, info):
        return f"{self._private_name(wxid, info)} —— {self._private_state(info)}"

    def _private_select(self, wxids):
        with self._lock:
            self._private_selection = {"wxids": list(wxids), "created": self._clock()}

    def _private_selected(self, token):
        match = _MEMBER_REF_RE.match(str(token or ""))
        with self._lock:
            selection = getattr(self, "_private_selection", None)
        if (match is None or selection is None
                or self._clock() - selection["created"] > SELECTION_TTL_SECONDS):
            return None
        index = int(match.group(1)) - 1
        if not 0 <= index < len(selection["wxids"]):
            return None
        wxid = selection["wxids"][index]
        info = core.private_contact_candidates(include_contacts=True).get(wxid)
        return (wxid, info) if info is not None else None

    def _private(self, args, raw_text):
        if not args:
            self._reply(self._private_overview())
            return
        if len(args) == 1 and _switch(args[0]) is not None:
            self._private_switch(args)
            return
        named = _PRIVATE_NAME_RE.match(str(raw_text or "").strip(_SPACES))
        if named:
            action = named.group(2)
            if action not in _PRIVATE_ACTIONS:
                self._reply("用法：/设置 私聊 【名字】 开|关|默认")
                return
            hits = fuzzy_members(core.private_contact_candidates(include_contacts=True),
                                 named.group(1).strip())
            if not hits:
                self._reply(f"没找到像“{named.group(1).strip()}”的联系人")
                return
            if len(hits) > 1 and hits[0][0] - hits[1][0] < FUZZY_MARGIN:
                self._private_show_candidates(named.group(1).strip(), hits, action)
                return
            _score, _n, wxid, info = hits[0]
            self._private_apply(wxid, info, action)
            return
        if _MEMBER_REF_RE.match(args[0]):
            selected = self._private_selected(args[0])
            if selected is None:
                self._reply("编号无效或已过期（5分钟），先发 /设置 私聊 或搜索联系人")
                return
            wxid, info = selected
            if len(args) == 1:
                self._reply(self._private_line(wxid, info)
                            + f"\n用 /设置 私聊 {args[0]} 开|关|默认 修改")
                return
            if len(args) == 2 and args[1] in _PRIVATE_ACTIONS:
                self._private_apply(wxid, info, args[1])
                return
            self._reply("用法：/设置 私聊 #编号 开|关|默认")
            return
        if len(args) == 2 and args[0] in {"搜", "搜索"}:
            self._private_search(args[1])
            return
        self._reply("用法：/设置 私聊 [开|关|搜 <关键词>|#编号 开|关|默认|【名字】 开|关|默认]")

    def _private_overview(self):
        candidates = core.private_contact_candidates()
        lines = [
            "私聊开关（主人自己的私聊始终正常，不受影响）",
            "默认：" + _yn(core.private_nonowner_enabled()) + "（没单独设置的人按这个）",
        ]
        overridden = sorted(
            ((w, i) for w, i in candidates.items() if i["override"] is not None),
            key=lambda x: (not x[1]["override"], self._private_name(*x)),
        )
        recent = sorted(
            ((w, i) for w, i in candidates.items() if i["session"]),
            key=lambda x: -x[1]["last"],
        )[:PRIVATE_LIST_LIMIT]
        shown = []
        for wxid, _info in overridden + recent:
            if wxid not in shown:
                shown.append(wxid)
        self._private_select(shown)
        index = {w: n for n, w in enumerate(shown, 1)}
        if overridden:
            lines.append("单独设置：")
            lines.extend(f"#{index[w]} {self._private_line(w, i)}" for w, i in overridden)
        else:
            lines.append("单独设置：无")
        rest = [(w, i) for w, i in recent if i["override"] is None]
        if rest:
            lines.append("最近私聊过的人：")
            lines.extend(f"#{index[w]} {self._private_line(w, i)}" for w, i in rest)
        lines.append("用 /设置 私聊 #编号 开|关|默认 单独设置（5分钟内有效）；"
                     "找别人用 /设置 私聊 搜 <关键词>")
        return "\n".join(lines)

    def _private_search(self, keyword):
        keyword = keyword.strip()
        needle = keyword.casefold()
        hits = []
        for wxid, info in core.private_contact_candidates(include_contacts=True).items():
            names = [n for n in (info["label"], info["contact"]) if n]
            if not any(needle in n.casefold() for n in names):
                continue
            exact = any(needle == n.casefold() for n in names)
            hits.append((0 if exact else 1, 0 if info["session"] else 1, -info["last"],
                         self._private_name(wxid, info), wxid, info))
        hits.sort(key=lambda h: h[:4])
        if not hits:
            self._reply(f"联系人里没找到“{keyword}”。用法：/设置 私聊 搜 <关键词>，"
                        "或 /设置 私聊 【名字】 开|关|默认")
            return
        shown = hits[:SEARCH_LIMIT]
        self._private_select([h[4] for h in shown])
        lines = [f"联系人搜索“{keyword}”："]
        if len(hits) > SEARCH_LIMIT:
            lines[0] += f"（共{len(hits)}人，只列前{SEARCH_LIMIT}个，可换更精确的关键词）"
        lines.extend(f"#{n} {self._private_line(h[4], h[5])}" for n, h in enumerate(shown, 1))
        lines.append("用 /设置 私聊 #编号 开|关|默认 继续（5分钟内有效）")
        self._reply("\n".join(lines))

    def _private_show_candidates(self, query, hits, action):
        shown = hits[:5]
        self._private_select([h[2] for h in shown])
        lines = [f"有几位像“{query}”，你指的是哪位？"]
        lines.extend(f"#{n} {self._private_line(h[2], h[3])}" for n, h in enumerate(shown, 1))
        lines.append(f"回复如“/设置 私聊 #1 {action}”即可（5分钟内有效）")
        self._reply("\n".join(lines))

    def _private_apply(self, wxid, info, action):
        name = self._private_name(wxid, info)
        if action == "忘记":
            from . import private_thread
            removed = private_thread.forget(wxid)
            state = f"私聊记忆已清除（{removed} 条存档和摘要），开关不变"
        elif action == "默认":
            core.update_private_contact(wxid, None)
            state = "已取消单独设置，跟随默认（当前" + _yn(core.private_nonowner_enabled()) + "）"
        else:
            value = _switch(action)
            label = info.get("label") or info.get("contact") or ""
            core.update_private_contact(wxid, value, label)
            state = "私聊回复已单独" + ("开启" if value else "关闭")
        core.log(
            "[v3.193-owner-settings] "
            f"action=private_contact member={self._member_tag(wxid)} "
            f"value={action} changed=yes"
        )
        self._reply(f"{name}：{state}")

    def _bool_setting(self, cfg, args, key, item, label):
        value = _switch(args[0]) if len(args) == 1 else None
        if value is None:
            self._reply(f"用法：/设置 <群号> {item} 开|关")
            return
        self._write(self._identity(cfg), set_values={key: value})
        core.log(
            "[v3.170-owner-settings] "
            f"group={cfg['name']!r} action={key} value={_yn(value)} changed=yes"
        )
        self._reply(f"{cfg['name']}：{label}已{'开启' if value else '关闭'}")

    def _cooldown_setting(self, cfg, args, item):
        key = _COOLDOWN_KEYS[item]
        if len(args) != 1:
            self._reply(f"用法：/设置 <群号> {item} <时长>|默认")
            return
        identity = self._identity(cfg)
        if args[0] == "默认":
            self._write(identity, unset=(key,))
            shown = _fmt_duration(_DEFAULT_COOLDOWN) + "（默认）"
        else:
            seconds = parse_duration(args[0])
            if seconds is None:
                self._reply("时长无效：支持 0 到 24小时，例如 2400、40分钟、1小时")
                return
            self._write(identity, set_values={key: seconds})
            shown = _fmt_duration(seconds)
        core.log(
            "[v3.170-owner-settings] "
            f"group={cfg['name']!r} action={key} value={shown} changed=yes"
        )
        suffix = "" if _flag(cfg, "proactive_enabled") else "（当前主动发言是关闭的）"
        self._reply(f"{cfg['name']}：{item}已设为 {shown}{suffix}")

    def _mute_switch(self, cfg, args):
        value = _switch(args[0]) if len(args) == 1 else None
        if value is None:
            self._reply("用法：/设置 <群号> 总开关 开|关")
            return
        identity = self._identity(cfg)
        if value:
            self._write(identity, unset=("muted",))
            core.log(
                "[v3.170-owner-settings] "
                f"group={cfg['name']!r} action=muted value=关 changed=yes"
            )
            self._reply(f"{cfg['name']}：总开关已打开，从现在起正常处理（关闭期间的消息不会补）")
            return
        self._set_pending(
            identity,
            kind="mute",
            summary=f"关闭 {cfg['name']} 的总开关（完全不处理：不记录、不回复）",
        )

    # ------------------------------------------------------------ personality
    def _personality(self, cfg, args, raw_text):
        identity = self._identity(cfg)
        if not args:
            text = str(cfg.get("personality") or "").strip() or "（空）"
            self._reply(f"{cfg['name']} 当前人格：\n{text}")
            return
        if len(args) == 1 and args[0] == "默认":
            default = str(
                cfg.get("default_personality") or cfg.get("personality") or ""
            ).strip()
            self._set_pending(
                identity, kind="personality_default",
                summary=f"把 {cfg['name']} 恢复为默认人格",
                preview=default,
            )
            return
        worker = self._lookup(cfg["name"]) if self._lookup is not None else None
        if worker is None:
            self._reply("这个群当前没有在运行，暂时不能生成人格")
            return
        if len(args) == 1 and args[0] == "随机":
            self._reply("正在随机生成人格，稍等")
            try:
                personality, summary = worker._v3110_random_personality()
            except Exception as exc:
                core.log(
                    "[v3.170-owner-settings] "
                    f"group={cfg['name']!r} action=personality_random "
                    f"generator_failed=yes error={type(exc).__name__} changed=no"
                )
                self._reply("随机人格生成失败，原人格没有改动")
                return
            self._set_pending(
                identity, kind="personality_set",
                summary=f"把 {cfg['name']} 换成随机人格：{summary}",
                preview=personality, value=personality,
            )
            return
        raw = self._personality_request_text(raw_text)
        if not raw:
            self._reply("用法：/设置 <群号> 人格 默认|随机|<人格要求>")
            return
        self._reply("正在整理人格，稍等")
        try:
            optimized = worker._v3109_optimize_personality(raw)
        except Exception as exc:
            core.log(
                "[v3.170-owner-settings] "
                f"group={cfg['name']!r} action=personality_custom "
                f"optimizer_failed=yes error={type(exc).__name__} changed=no"
            )
            self._reply("人格整理失败，原人格没有改动")
            return
        self._set_pending(
            identity, kind="personality_set",
            summary=f"把 {cfg['name']} 换成新人格（{len(optimized)}字）",
            preview=optimized, value=optimized,
        )

    @staticmethod
    def _personality_request_text(raw_text):
        # Keep the owner's original text (including newlines) after
        # "/设置 <群号> 人格".
        match = _PERSONALITY_TEXT_RE.match(str(raw_text or ""))
        return match.group(1).strip() if match else ""

    # ------------------------------------------------------------ members
    @staticmethod
    def _member_name(info):
        return info["label"] or info["nickname"] or info["contact"] or "未命名成员"

    @staticmethod
    def _member_tag(wxid):
        return "id…" + hashlib.sha256(wxid.encode("utf-8")).hexdigest()[:6]

    def _member_line(self, wxid, info):
        name = self._member_name(info)
        extra = [
            f"{label}:{value}"
            for label, value in (("群昵称", info["nickname"]), ("好友名", info["contact"]))
            if value and value != name
        ]
        if info["label"]:
            extra.append("称呼:" + info["label"])
        if info["verified"]:
            extra.append("认证:" + info["verified"])
        if info["messages"]:
            extra.append(f"发言{info['messages']}")
        if not info["is_member"]:
            extra.append("不在当前成员名单")
        return f"{name}（{'；'.join(extra) or '无更多信息'}）{self._member_tag(wxid)}"

    def _member(self, cfg, args, raw_text):
        if not args:
            self._reply("用法：/设置 <群号> 成员 <关键词>，或 /设置 <群号> 成员 #编号 …")
            return
        if not _MEMBER_REF_RE.match(args[0]):
            self._member_search(cfg, " ".join(args))
            return
        selected = self._selected_member(cfg, args[0])
        if selected is None:
            self._reply("编号无效或已过期（5分钟），先重新搜索成员")
            return
        wxid, info = selected
        action, rest = (args[1], args[2:]) if len(args) > 1 else ("", [])
        if not action:
            self._reply(self._member_detail(cfg, wxid, info))
        elif action == "称呼":
            self._member_label(cfg, wxid, info, rest)
        elif action == "认证":
            self._member_verify(cfg, wxid, info, rest)
        elif action == "画像":
            self._member_profile(cfg, wxid, info, rest, raw_text)
        else:
            self._reply("不认识这个成员操作，发 /设置 帮助 查看用法")

    def _member_search(self, cfg, keyword):
        keyword = keyword.strip()
        needle = keyword.casefold()
        hits = []
        for wxid, info in core.group_member_candidates(cfg["name"], cfg).items():
            names = [n for n in (info["label"], info["nickname"], info["contact"]) if n]
            if not any(needle in n.casefold() for n in names):
                continue
            exact = any(needle == n.casefold() for n in names)
            hits.append((0 if exact else 1, -info["messages"], self._member_name(info), wxid, info))
        hits.sort(key=lambda h: h[:3])
        if not hits:
            self._reply(f"{cfg['name']} 里没找到“{keyword}”")
            return
        shown = hits[:SEARCH_LIMIT]
        with self._lock:
            self._selection = {
                "identity": self._identity(cfg),
                "wxids": [h[3] for h in shown],
                "created": self._clock(),
            }
        lines = [f"{cfg['name']} 成员搜索“{keyword}”："]
        if len(hits) > SEARCH_LIMIT:
            lines[0] += f"（共{len(hits)}人，只列前{SEARCH_LIMIT}个，可换更精确的关键词）"
        lines.extend(
            f"#{i} {self._member_line(h[3], h[4])}" for i, h in enumerate(shown, 1)
        )
        lines.append("用 /设置 <群号> 成员 #编号 … 继续操作（5分钟内有效）")
        self._reply("\n".join(lines))

    def _selected_member(self, cfg, token):
        match = _MEMBER_REF_RE.match(token)
        with self._lock:
            selection = self._selection
        if (
            match is None
            or selection is None
            or self._clock() - selection["created"] > SELECTION_TTL_SECONDS
            or selection["identity"] != self._identity(cfg)
        ):
            return None
        index = int(match.group(1)) - 1
        if not 0 <= index < len(selection["wxids"]):
            return None
        wxid = selection["wxids"][index]
        info = core.group_member_candidates(cfg["name"], cfg).get(wxid)
        return (wxid, info) if info is not None else None

    def _member_detail(self, cfg, wxid, info):
        memory = core.member_memory_snapshot(cfg["name"], wxid)
        return "\n".join([
            f"{cfg['name']} · {self._member_line(wxid, info)}",
            "称呼：" + (info["label"] or "无"),
            "认证：" + (info["verified"] or "否"),
            f"画像：主人设定 {len(memory['pinned'])} 条；整理画像"
            + self._doc_state("member", wxid)
            + f"（本群累计发言 {memory['message_count']} 条）",
        ])

    def _member_label(self, cfg, wxid, info, rest):
        if not rest:
            self._reply("用法：/设置 <群号> 成员 #编号 称呼 <称呼>|清除")
            return
        value = None if rest == ["清除"] else " ".join(rest)
        core.update_group_member_map(
            cfg["name"], expected_chat_username=self._identity(cfg)["chat_username"],
            map_key="member_labels", wxid=wxid, value=value,
        )
        core.log(
            "[v3.171-owner-settings] "
            f"group={cfg['name']!r} action=member_label "
            f"member={self._member_tag(wxid)} value={'clear' if value is None else 'set'} changed=yes"
        )
        name = self._member_name(info)
        self._reply(f"{cfg['name']}：{name} 的称呼已" + ("清除" if value is None else f"设为“{value}”"))

    def _member_verify(self, cfg, wxid, info, rest):
        if not rest:
            self._reply("用法：/设置 <群号> 成员 #编号 认证 <标签>|取消")
            return
        name = self._member_name(info)
        if rest == ["取消"]:
            if not info["verified"]:
                self._reply(f"{name} 目前不是认证成员")
                return
            self._set_pending(
                self._identity(cfg), kind="verify_clear", wxid=wxid,
                summary=f"取消 {cfg['name']} 中 {name}（{self._member_tag(wxid)}）的认证成员身份",
            )
            return
        value = " ".join(rest)
        if len(value) > 40:
            self._reply("认证标签最多 40 个字")
            return
        self._set_pending(
            self._identity(cfg), kind="verify_set", wxid=wxid, value=value,
            summary=(
                f"把 {cfg['name']} 中 {name}（{self._member_tag(wxid)}）设为认证成员，"
                f"标签“{value}”（人格里依赖 verified-member 的规则会对此人生效）"
            ),
        )

    @staticmethod
    def _doc_state(scope, subject):
        entry = memory_docs.get_doc(scope, subject)
        if not entry or not entry["doc"]:
            return "尚未生成"
        return "已生成（" + time.strftime("%m-%d %H:%M", time.localtime(entry["updated_at"])) + "）"

    def _rebuild_async(self, label, fn, render):
        self._reply(f"已开始重建{label}，完成后告诉你（约1–2分钟）")

        def done(result, exc):
            if exc is not None:
                kind = getattr(exc, "kind", type(exc).__name__)
                core.log(f"[v3.172-owner-settings] rebuild_failed=yes error={kind}")
                self._reply(f"{label}重建失败，原内容没有改动")
                return
            self._reply(f"{label}已重建：\n" + (render(result) or "（空）"))

        memory_docs.service().run_async(fn, done)

    def _member_profile(self, cfg, wxid, info, rest, raw_text):
        name = self._member_name(info)
        memory = core.member_memory_snapshot(cfg["name"], wxid)
        pinned = memory["pinned"]
        if not rest:
            lines = [f"{cfg['name']} · {name} 的画像"]
            lines.append("主人设定（本群固定）：" if pinned else "主人设定：无")
            lines.extend(f"{i}. {t}" for i, t in enumerate(pinned, 1))
            entry = memory_docs.get_doc("member", wxid)
            rendered = memory_docs.render_member_for_owner(entry["doc"]) if entry else ""
            if rendered:
                lines.append(f"整理画像（跨群，{self._doc_state('member', wxid)}）：")
                lines.append(rendered)
            else:
                lines.append("整理画像：尚未生成（新增发言满30条后自动生成，或发 画像 重建）")
            self._reply("\n".join(lines))
            return
        action = rest[0]
        try:
            if action == "加":
                match = _PROFILE_ADD_RE.match(raw_text.strip(_SPACES))
                text = match.group(1).strip() if match else ""
                core.member_memory_edit(cfg["name"], wxid, "pin_add", text=text)
                done = f"已为 {name} 添加主人设定"
            elif action == "删" and len(rest) == 2 and rest[1].isdigit():
                n = int(rest[1]) - 1
                if not 0 <= n < len(pinned):
                    raise ValueError("index")
                core.member_memory_edit(cfg["name"], wxid, "delete", kind="pinned", index=n)
                done = f"已删除 {name} 的主人设定第 {rest[1]} 条"
            elif action == "重建" and len(rest) == 1:
                self._rebuild_async(
                    f"{name} 的画像",
                    lambda: memory_docs.rebuild_member(wxid, explicit=True, reason="owner"),
                    lambda result: memory_docs.render_member_for_owner(result[0]),
                )
                return
            elif action == "清空" and len(rest) == 1:
                self._set_pending(
                    self._identity(cfg), kind="profile_clear", wxid=wxid,
                    summary=(
                        f"清空 {name} 的画像：{cfg['name']} 里的主人设定和自动记录，"
                        "以及跨群整理画像"
                    ),
                )
                return
            else:
                self._reply("用法：/设置 <群号> 成员 #编号 画像 [加 <内容>|删 <编号>|重建|清空]")
                return
        except ValueError as exc:
            self._reply(_VALUE_ERRORS.get(str(exc), "内容无效，没有保存"))
            return
        core.log(
            "[v3.171-owner-settings] "
            f"group={cfg['name']!r} action=profile_{'add' if action == '加' else 'delete'} "
            f"member={self._member_tag(wxid)} changed=yes"
        )
        self._reply(done)

    # ------------------------------------------------------------ group memory
    @staticmethod
    def _group_memory_items(chat):
        memories = core.group_memory_snapshot(chat)
        manual = [m for m in memories if m.get("manual")]
        automatic = [m for m in reversed(memories) if not m.get("manual")]
        return manual, automatic

    def _group_memory(self, cfg, args, raw_text):
        manual, _automatic = self._group_memory_items(cfg["name"])
        items = manual
        if not args:
            lines = [f"{cfg['name']} 群记忆"]
            lines.append("群内设定（固定）：" if manual else "群内设定：无")
            lines.extend(f"{i}. {m['anchor']}" for i, m in enumerate(manual, 1))
            entry = memory_docs.get_doc("group", cfg["name"])
            rendered = memory_docs.render_group(entry["doc"]) if entry else ""
            if rendered:
                lines.append(f"整理群记忆（{self._doc_state('group', cfg['name'])}）：")
                lines.append(rendered)
            else:
                lines.append("整理群记忆：尚未生成（每天自动整理一次，或发 群记忆 重建）")
            self._reply("\n".join(lines))
            return
        if args == ["重建"]:
            chat = cfg["name"]
            self._rebuild_async(
                f"{chat} 的群记忆",
                lambda: memory_docs.rebuild_group(chat, explicit=True, reason="owner"),
                memory_docs.render_group,
            )
            return
        try:
            if args[0] == "加":
                match = _GROUP_MEMORY_ADD_RE.match(raw_text.strip(_SPACES))
                text = match.group(1).strip() if match else ""
                core.group_memory_edit(cfg["name"], "add", text=text)
                core.log(
                    "[v3.171-owner-settings] "
                    f"group={cfg['name']!r} action=group_memory_add changed=yes"
                )
                self._reply(f"{cfg['name']}：已添加群内设定")
                return
            if args[0] == "删" and len(args) == 2 and args[1].isdigit():
                n = int(args[1]) - 1
                if not 0 <= n < len(items):
                    raise ValueError("index")
                item = items[n]
                self._set_pending(
                    self._identity(cfg), kind="group_memory_delete",
                    memory_id=item["id"],
                    summary=f"删除 {cfg['name']} 的群记忆第 {args[1]} 条",
                    preview=item["anchor"],
                )
                return
        except ValueError as exc:
            self._reply(_VALUE_ERRORS.get(str(exc), "内容无效，没有保存"))
            return
        self._reply("用法：/设置 <群号> 群记忆 [加 <内容>|删 <编号>|重建]")

    # ------------------------------------------------------------ models
    def _model(self, args):
        try:
            if not args:
                self._reply(self._model_policy_text())
            elif args[0] == "列表" and len(args) <= 2:
                self._reply(self._model_list_text(args[1] if len(args) == 2 else ""))
            elif len(args) >= 2 and args[-1] == "默认" and _model_functions(args[:-1]):
                functions = _model_functions(args[:-1])
                self._set_pending(
                    MODEL_IDENTITY, kind="model_default", value=functions,
                    summary=_functions_label(functions) + " 恢复默认模型",
                )
            elif len(args) >= 2 and _model_functions(args[:-1]):
                model = self._model_choice(args[-1])
                if model is None:
                    self._reply("编号无效或已过期，先发 /设置 模型 列表")
                    return
                functions = _model_functions(args[:-1])
                if len(functions) == 1:
                    self._probe_async(functions[0], model)
                else:
                    self._probe_batch_async(functions, model)
            else:
                self._reply("用法：/设置 模型 [列表 [关键词]] | <功能>[、<功能>…] <编号|模型名>"
                            " | <功能>[、<功能>…]|全部 默认\n"
                            "功能：" + " ".join(MODEL_FUNCTIONS))
        except llm_client.LLMExecutionError as exc:
            core.log(f"[v3.175-owner-model] failed=yes error={exc.kind} changed=no")
            self._reply("模型网关暂时不可用，没有改动")

    @staticmethod
    def _model_policy_text():
        policy = llm_client.admin_model_policy()
        refresh_model_sources(lambda: llm_client.admin_models(""))
        lines = ["各功能模型（全局，默认=网关默认模型）："]
        for name in MODEL_FUNCTIONS:
            value = str(policy.get(name) or "default")
            lines.append(f"{name}：" + ("默认" if value == "default" else model_with_channel(value)))
        lines.append("切换：/设置 模型 列表 [关键词]，再 /设置 模型 <功能> <编号>")
        return "\n".join(lines)

    def _model_list_text(self, query):
        # Filter locally so a keyword can also match the channel (provider prefix).
        needle = str(query or "").casefold()
        catalog = llm_client.admin_models("")
        remember_model_sources(catalog)
        items = [
            i for i in catalog
            if not needle or needle in i["id"].casefold()
            or needle in str(i.get("label") or "").casefold()
            or needle in model_channel(i["id"])[1].casefold()
        ]
        items.sort(key=_model_group_rank)
        if not items:
            return "没有匹配的模型，换个关键词试试（也可按渠道前缀搜）"
        shown = items[:MODEL_LIST_LIMIT]
        with self._lock:
            self._model_list = {"ids": [i["id"] for i in shown], "created": self._clock()}
        lines = [f"可用模型（共 {len(items)} 个" + (f"，关键词：{query}" if query else "") + "）："]
        channel = None
        for n, item in enumerate(shown, 1):
            current = model_channel(item["id"])[1]
            if current != channel:
                channel = current
                lines.append(f"【{channel}】")
            lines.append(f"{n}. {item['id']}{_model_note(item['id'])}")
        if len(items) > len(shown):
            lines.append(f"……只显示前 {len(shown)} 个，加关键词缩小范围，如 /设置 模型 列表 gmail")
        lines.append("@后缀=推理强度；用法：/设置 模型 <功能> <编号>（10分钟内有效）")
        return "\n".join(lines)

    def _model_choice(self, token):
        if not re.fullmatch(r"\d{1,3}", token):
            return token if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@/-]{0,127}", token) else None
        with self._lock:
            listing = self._model_list
            if listing is None or self._clock() - listing["created"] > MODEL_LIST_TTL_SECONDS:
                return None
            ids = listing["ids"]
        index = int(token)
        return ids[index - 1] if 1 <= index <= len(ids) else None

    def _probe_async(self, function, model):
        self._reply(f"正在实测 {model} 能否胜任「{function}」，通常需要 10–60 秒（研究最长约3分钟）……")

        def run():
            try:
                result = llm_client.admin_set_model(function, model, dry_run=True)
            except llm_client.LLMExecutionError as exc:
                core.log(f"[v3.175-owner-model] function={function} probe_failed=yes error={exc.kind}")
                self._reply("模型网关暂时不可用，没有改动")
                return
            except Exception as exc:
                core.log(f"[v3.175-owner-model] function={function} probe_failed=yes "
                         f"error={type(exc).__name__}")
                self._reply("验证出错，没有改动")
                return
            probe = result.get("probe") if isinstance(result.get("probe"), dict) else {}
            core.log(f"[v3.175-owner-model] function={function} model={model!r} "
                     f"probe_ok={'yes' if probe.get('ok') else 'no'} reason={probe.get('reason', '')}")
            if not probe.get("ok"):
                reason = _MODEL_REASONS.get(str(probe.get("reason")), str(probe.get("reason") or "未知原因"))
                if probe.get("reason") == "too_slow":
                    reason += f"（用时 {probe.get('seconds')} 秒，上限 {probe.get('limit')} 秒）"
                self._reply(f"{model} 没通过「{function}」验证：{reason}。没有改动")
                return
            self._set_pending(
                MODEL_IDENTITY, kind="model_set", value=(function, model),
                summary=(f"「{function}」改用 {model_with_channel(model)}"
                         f"（实测通过，用时 {probe.get('seconds')} 秒）"),
            )

        threading.Thread(target=run, daemon=True, name="owner-model-probe").start()

    def _probe_batch_async(self, functions, model):
        """Probe one model for several functions in turn; only passes are offered (v3.182)."""
        label = _functions_label(functions)
        self._reply(f"正在逐项实测 {model} 能否胜任「{label}」，每项通常 10–60 秒"
                    f"（研究最长约3分钟），共 {len(functions)} 项……")

        def run():
            passed, failed = [], []
            for function in functions:
                try:
                    result = llm_client.admin_set_model(function, model, dry_run=True)
                except llm_client.LLMExecutionError as exc:
                    core.log(f"[v3.182-owner-model] function={function} probe_failed=yes error={exc.kind}")
                    failed.append((function, "模型网关暂时不可用"))
                    continue
                except Exception as exc:
                    core.log(f"[v3.182-owner-model] function={function} probe_failed=yes "
                             f"error={type(exc).__name__}")
                    failed.append((function, "验证出错"))
                    continue
                probe = result.get("probe") if isinstance(result.get("probe"), dict) else {}
                core.log(f"[v3.182-owner-model] function={function} model={model!r} "
                         f"probe_ok={'yes' if probe.get('ok') else 'no'} reason={probe.get('reason', '')}")
                if probe.get("ok"):
                    passed.append((function, probe.get("seconds")))
                    continue
                reason = _MODEL_REASONS.get(str(probe.get("reason")), str(probe.get("reason") or "未知原因"))
                if probe.get("reason") == "too_slow":
                    reason += f"（用时 {probe.get('seconds')} 秒，上限 {probe.get('limit')} 秒）"
                failed.append((function, reason))
                if probe.get("reason") == "unknown_model":
                    break  # same answer for every function
            failed_text = "；".join(f"{f}：{r}" for f, r in failed)
            if not passed:
                self._reply(f"{model} 没通过「{label}」验证（{failed_text}）。没有改动")
                return
            ok = tuple(f for f, _ in passed)
            summary = (f"「{_functions_label(ok)}」改用 {model_with_channel(model)}（实测通过："
                       + "，".join(f"{f} {sec} 秒" for f, sec in passed) + "）")
            if failed:
                summary += f"；未通过、保持不变：{failed_text}"
            self._set_pending(MODEL_IDENTITY, kind="model_set", value=(ok, model), summary=summary)

        threading.Thread(target=run, daemon=True, name="owner-model-probe").start()

    # ------------------------------------------------------------ natural language
    def _nl_context_text(self):
        """Recent request and live member candidates, for follow-ups like 第2个."""
        lines = []
        now = self._clock()
        with self._lock:
            last = getattr(self, "_nl_last", None)
            selection = self._selection
        if last and now - last["created"] <= NL_CONTEXT_TTL_SECONDS:
            lines.append("主人上一条设置要求：" + last["request"])
        if selection and now - selection["created"] <= SELECTION_TTL_SECONDS:
            index = next((i for i, cfg in enumerate(self._groups(), 1)
                          if self._identity(cfg) == selection["identity"]), None)
            cfg = self._group_at(str(index)) if index else None
            if cfg is not None:
                candidates = core.group_member_candidates(cfg["name"], cfg)
                lines.append(f"最近的成员候选（{index} 群 {cfg['name']}，可用 #编号 指代）：")
                for n, wxid in enumerate(selection["wxids"], 1):
                    info = candidates.get(wxid)
                    if info is not None:
                        lines.append(f"#{n} {self._member_name(info)}")
        with self._lock:
            private_selection = getattr(self, "_private_selection", None)
        if private_selection and now - private_selection["created"] <= SELECTION_TTL_SECONDS:
            candidates = core.private_contact_candidates(include_contacts=True)
            rows = [(n, w) for n, w in enumerate(private_selection["wxids"], 1) if w in candidates]
            if rows:
                lines.append("最近列出的私聊联系人（可用“/设置 私聊 #编号 开|关|默认”指代）：")
                lines.extend(f"#{n} {self._private_name(w, candidates[w])}" for n, w in rows)
        return "\n".join(lines)

    def _natural_language(self, raw_text):
        request = str(raw_text or "").strip(_SPACES)[len(COMMAND):].strip(_SPACES)
        if len(request) > NL_MAX_TEXT:
            self._reply(f"描述太长了（超过{NL_MAX_TEXT}字），请拆开说")
            return
        self._reply("正在理解……")
        context = self._nl_context_text()
        prompt = (
            "你是一个微信机器人的设置助手。把主人用自然语言提出的要求，翻译成下面“命令语法”里的 /设置 命令，"
            "程序会直接执行，主人不需要再手动输入命令。\n"
            "规则：\n"
            f"1. commands 字段：每行一条完整的 /设置 命令，最多 {NL_MAX_COMMANDS} 条，只能使用语法里存在的写法；"
            "群一律用“当前状态”里的群编号，群名有错别字或简称时按最接近的群理解。\n"
            "2. 涉及具体成员时，用“【成员名】”代替编号，例如“/设置 2 成员 【张三】 画像”、"
            "“/设置 2 成员 【张三】 称呼 老张”；成员名照主人说的写，有错别字也没关系，程序会模糊匹配。"
            "如果下方“最近的成员候选”里已有对应的人（例如主人说“第2个”“刚才那个”），可以直接用 #编号。\n"
            "3. 查看某成员的画像用“/设置 <群号> 成员 【名】 画像”；只看成员信息用“/设置 <群号> 成员 【名】”。\n"
            "3b. 单独开关某个人的私聊回复用“/设置 私聊 【名字】 开|关|默认”（不需要群号，默认=跟随总的私聊默认开关）；"
            "改所有人的默认用“/设置 私聊 开|关”。\n"
            "4. 不要输出“确认”“取消”。切换模型只能用主人明确给出的完整模型名；不确定时输出“/设置 模型 列表 <关键词>”。\n"
            "5. 要求含糊、做不到或语法里没有对应命令时，commands 留空，在 reply 里简短说明或追问。\n"
            "6. reply：一句简短的中文，说明要做什么（会显示给主人）。\n\n"
            "命令语法：\n" + HELP + "\n\n当前状态：\n" + self._status_text()
            + ("\n\n" + context if context else "")
            + "\n\n主人的要求：\n" + request
        )
        with self._lock:
            self._nl_last = {"request": request, "created": self._clock()}
        try:
            result = llm_client.execute_auxiliary("settings_intent", prompt, explicit=True)
        except llm_client.LLMExecutionError as exc:
            core.log(f"[v3.178-owner-nl] translated=no error={exc.kind} changed=no")
            self._reply("暂时没能理解（模型不可用），请稍后再试或用标准命令（/设置 帮助）")
            return
        data = result.get("structured") or {}
        reply = str(data.get("reply") or "").strip()
        lines = [x.strip() for x in str(data.get("commands") or "").splitlines() if x.strip()]
        shapes, problem = _nl_validate(lines)
        items = []
        if not problem:
            items, problem = self._nl_resolve(shapes)
        read_only = bool(items) and all(item["read_only"] for item in items)
        core.log(
            "[v3.179-owner-nl] "
            f"translated=yes commands={len(lines)} valid={'no' if problem else 'yes'} "
            f"members={sum(1 for i in items if i['wxid'])} read_only={'yes' if read_only else 'no'}"
        )
        if problem is None:
            return  # ambiguity: candidates were shown, waiting for the owner
        if problem:
            self._reply((reply + "\n" if reply else "") + problem
                        + "。可以换个说法，或用标准命令（/设置 帮助）")
            return
        if not items:
            self._reply(reply or "没理解要改什么，可以说得具体些，或发 /设置 帮助")
            return
        if read_only:
            self._run_batch(items)
            return
        listing = "\n".join(f"{n}. {item['display']}" for n, item in enumerate(items, 1))
        self._set_pending(
            NL_IDENTITY, kind="nl_batch", value=items,
            summary=(reply + "\n" if reply else "") + listing,
        )

    def _nl_resolve(self, shapes):
        """Bind every member reference to an exact wxid.

        Returns (items, problem). problem is "" on success, a message on
        failure, or None when ambiguous candidates were shown to the owner.
        """
        items = []
        for shape in shapes:
            item = {"line": shape["line"], "display": shape["line"], "wxid": None,
                    "identity": None, "read_only": shape["read_only"]}
            ptokens = _tokens(shape["parse"])
            if ptokens and ptokens[0] == "私聊" and len(ptokens) >= 3:
                target = self._nl_private_target(shape["line"], ptokens)
                if target is None:
                    return [], None
                if isinstance(target, str):
                    return [], target
                wxid, info, action = target
                item["private_wxid"] = wxid
                item["line"] = f"/设置 私聊 #1 {action}"
                item["display"] = f"/设置 私聊 【{self._private_name(wxid, info)}】 {action}"
                items.append(item)
                continue
            member = shape["member"]
            if member is not None:
                cfg = self._group_at(shape["group"])
                if cfg is None:
                    return [], "翻译出的群编号不存在"
                if member[0] == "ref":
                    selected = self._selected_member(cfg, member[1])
                    if selected is None:
                        return [], "成员编号已失效，请直接说成员名字"
                    wxid, info = selected
                else:
                    hits = fuzzy_members(core.group_member_candidates(cfg["name"], cfg), member[1])
                    if not hits:
                        return [], f"{cfg['name']} 里没找到像“{member[1]}”的成员"
                    unique = len(hits) == 1 or hits[0][0] - hits[1][0] >= FUZZY_MARGIN
                    if not unique:
                        self._nl_show_candidates(cfg, member[1], hits)
                        return [], None
                    _score, _messages, wxid, info = hits[0]
                item["wxid"] = wxid
                item["identity"] = self._identity(cfg)
                item["line"] = shape["parse"]  # "#1" bound to this wxid at run time
                name = self._member_name(info)
                item["display"] = re.sub(r"(成员[" + re.escape(_SPACES) + r"]*)(【[^】]*】|#\d+)",
                                         lambda m: m.group(1) + "【" + name + "】",
                                         shape["line"], count=1)
            items.append(item)
        return items, ""

    def _nl_show_candidates(self, cfg, query, hits):
        shown = hits[:5]
        with self._lock:
            self._selection = {"identity": self._identity(cfg),
                               "wxids": [h[2] for h in shown], "created": self._clock()}
        lines = [f"{cfg['name']} 里有几位像“{query}”，你指的是哪位？"]
        lines.extend(f"#{n} {self._member_line(h[2], h[3])}" for n, h in enumerate(shown, 1))
        lines.append("直接回复如“/设置 第1个”即可")
        self._reply("\n".join(lines))

    def _nl_private_target(self, line, tokens):
        """Bind one translated private per-contact line to an exact wxid (v3.193).

        Returns (wxid, info, action), a problem string, or None after showing
        ambiguous candidates to the owner.
        """
        action = tokens[-1]
        if action not in _PRIVATE_ACTIONS:
            return "翻译出的命令不符合格式"
        if _MEMBER_REF_RE.match(tokens[1]) and len(tokens) == 3:
            selected = self._private_selected(tokens[1])
            if selected is None:
                return "联系人编号已失效，请直接说名字"
            return selected[0], selected[1], action
        named = _PRIVATE_NAME_RE.match(line.strip(_SPACES))
        if not named:
            return "翻译出的命令不符合格式"
        query = named.group(1).strip()
        hits = fuzzy_members(core.private_contact_candidates(include_contacts=True), query)
        if not hits:
            return f"联系人里没找到像“{query}”的人"
        if len(hits) > 1 and hits[0][0] - hits[1][0] < FUZZY_MARGIN:
            self._private_show_candidates(query, hits, action)
            return None
        return hits[0][2], hits[0][3], action

    def _run_batch(self, items):
        """Run validated items; confirmations inside count as given."""
        self._nl_auto = True
        try:
            for n, item in enumerate(items, 1):
                self._nl_stop = False
                if item.get("wxid"):
                    # Bind "#1" to the exact member resolved when translating.
                    with self._lock:
                        self._selection = {"identity": item["identity"],
                                           "wxids": [item["wxid"]], "created": self._clock()}
                if item.get("private_wxid"):
                    self._private_select([item["private_wxid"]])
                try:
                    self._dispatch(_tokens(item["line"]), item["line"])
                except Exception as exc:
                    core.log(f"[v3.179-owner-nl] step={n} failed=yes error={type(exc).__name__}")
                    self._reply(f"第 {n} 条执行失败：{item['display']}")
                rest = items[n:]
                if self._nl_stop and rest:
                    self._reply("这一步需要你先看预览并确认，后面这些还没执行，确认后再说一次即可：\n"
                                + "\n".join(i["display"] for i in rest))
                    break
        finally:
            self._nl_auto = False
            self._nl_stop = False

    # ------------------------------------------------------------ pending
    def _live_pending(self):
        with self._lock:
            pending = self._pending
            if pending is None:
                return None
            if self._clock() - pending["created"] > CONFIRM_TTL_SECONDS:
                self._pending = None
                return None
            return pending

    def _set_pending(self, identity, *, kind, summary, preview=None, value=None,
                     wxid=None, memory_id=None):
        with self._lock:
            replaced = self._pending is not None
            self._pending = {
                "identity": identity,
                "kind": kind,
                "summary": summary,
                "value": value,
                "wxid": wxid,
                "memory_id": memory_id,
                "created": self._clock(),
            }
        core.log(
            "[v3.170-owner-settings] "
            f"group={identity['name']!r} action={kind} pending=yes"
        )
        if getattr(self, "_nl_auto", False):
            if kind not in _NL_MANUAL_KINDS:
                # The owner already confirmed this exact command in the batch.
                self._resolve_pending(True)
                return
            self._nl_stop = True
        lines = []
        if replaced:
            lines.append("（之前的待确认操作已作废）")
        lines.append("待确认：" + summary)
        if preview is not None:
            lines.append("预览：\n" + (str(preview).strip() or "（空）"))
        lines.append("2分钟内回复“确认”执行，回复“取消”放弃")
        self._reply("\n".join(lines))

    def _resolve_pending(self, confirmed):
        with self._lock:
            pending = self._pending
            self._pending = None
        if pending is None or self._clock() - pending["created"] > CONFIRM_TTL_SECONDS:
            self._reply("待确认操作已过期，请重新发起")
            return True
        identity = pending["identity"]
        if not confirmed:
            core.log(
                "[v3.170-owner-settings] "
                f"group={identity['name']!r} action={pending['kind']} "
                "cancelled=yes changed=no"
            )
            self._reply("已取消，配置没有改动")
            return True
        try:
            if pending["kind"] == "mute":
                self._write(identity, set_values={"muted": True})
                done = f"{identity['name']}：总开关已关闭，该群消息不再处理"
            elif pending["kind"] == "personality_default":
                self._check_identity(identity)
                core.update_group_personality_config(
                    identity["name"], restore_default=True,
                )
                done = f"{identity['name']}：已恢复默认人格"
            elif pending["kind"] == "personality_set":
                self._check_identity(identity)
                core.update_group_personality_config(
                    identity["name"], pending["value"],
                )
                done = f"{identity['name']}：人格已更新"
            elif pending["kind"] in {"verify_set", "verify_clear"}:
                core.update_group_member_map(
                    identity["name"],
                    expected_chat_username=identity["chat_username"],
                    map_key="verified_members",
                    wxid=pending["wxid"],
                    value=pending["value"] if pending["kind"] == "verify_set" else None,
                )
                done = f"{identity['name']}：认证成员已" + (
                    "设置" if pending["kind"] == "verify_set" else "取消"
                )
            elif pending["kind"] == "profile_clear":
                self._check_identity(identity)
                core.member_memory_edit(identity["name"], pending["wxid"], "clear")
                memory_docs.delete_doc("member", pending["wxid"])
                done = f"{identity['name']}：该成员画像已清空"
            elif pending["kind"] == "nl_batch":
                self._run_batch(pending["value"])
                return True
            elif pending["kind"] == "model_set":
                functions, model = pending["value"]
                if isinstance(functions, str):
                    functions = (functions,)
                committed, refused = [], []
                for function in functions:
                    result = llm_client.admin_set_model(function, model, dry_run=False)
                    if result.get("committed") is True:
                        committed.append(function)
                    else:
                        probe = result.get("probe") or {}
                        refused.append(f"{function}：" + _MODEL_REASONS.get(
                            str(probe.get("reason")), str(probe.get("reason") or "未知原因")))
                if not committed:
                    self._reply(f"{model} 复核没通过（" + "；".join(refused) + "）。没有改动")
                    return True
                done = (f"「{_functions_label(committed)}」已改用 {model_with_channel(model)}，新对话立即生效"
                        + (f"；复核没通过、保持不变：{'；'.join(refused)}" if refused else ""))
            elif pending["kind"] == "model_default":
                functions = pending["value"]
                if isinstance(functions, str):
                    functions = MODEL_FUNCTIONS if functions == "全部" else (functions,)
                for name in functions:
                    llm_client.admin_set_model(name, "default", dry_run=False)
                label = _functions_label(functions)
                done = (label if label == "所有功能" else f"「{label}」") + "已恢复默认模型"
            elif pending["kind"] == "group_memory_delete":
                self._check_identity(identity)
                core.group_memory_edit(
                    identity["name"], "delete", memory_id=pending["memory_id"],
                )
                done = f"{identity['name']}：群记忆已删除"
            else:
                raise RuntimeError("unknown pending kind")
        except Exception as exc:
            core.log(
                "[v3.170-owner-settings] "
                f"group={identity['name']!r} action={pending['kind']} "
                f"confirm_failed=yes error={type(exc).__name__} changed=no"
            )
            self._reply("执行失败，配置没有改动")
            return True
        core.log(
            "[v3.170-owner-settings] "
            f"group={identity['name']!r} action={pending['kind']} "
            "confirmed=yes changed=yes"
        )
        self._reply(done)
        return True

    def _check_identity(self, identity):
        for cfg in self._groups():
            if cfg["name"] == identity["name"]:
                if str(cfg.get("chat_username") or "") != identity["chat_username"]:
                    raise RuntimeError("group identity changed")
                return
        raise RuntimeError("group not found")
