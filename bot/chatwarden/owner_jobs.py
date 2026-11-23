"""Bot-owned async delivery outbox for source-authenticated owner jobs.

The gateway owns execution; this Bot process alone owns AT-SPI messaging.
Disk records survive Bot restarts. Sending is at-most-once under ambiguity:
if interrupted mid-send, do not replay or silently duplicate messages.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import weakref

from . import llm_client, core


OUTBOX = Path("/var/lib/chatwarden/owner-agent-outbox")
# v3.186: validated owner deliverables handed over by the Gateway.
HANDOFF = Path("/var/lib/chatwarden/owner-files")
_SAFE_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_MAX_FILE_BYTES = 20 * 1024 * 1024
IDENTIFIER = re.compile(r"owner-[a-f0-9]{32}\Z")
_TERMINAL = {"succeeded", "failed", "interrupted"}
_INTERVAL = 3.0
_LOCK = threading.RLock()
_STARTED = False
_OUTSTANDING: dict[str, dict] = {}
_GROUP_WORKERS = weakref.WeakValueDictionary()
# v3.189: exact-owner stop command; checked only after sender verification.
STOP_COMMAND = re.compile(
    r"^\s*(?:@\S+\s*)*(?:停止|终止|取消|中止|停掉|结束)\s*(?:管理员)?任务\s*"
    r"(?:#\s*([0-9a-fA-F]{6}))?\s*[。.!！]?\s*(?:@\S+\s*)*$"
)
_FIRST_NOTICE_S = 45
_NOTICE_EVERY_S = 180
_TOOL_LABELS = {
    "run_command": "执行命令", "read_file": "读文件", "write_file": "写文件",
    "search_web": "搜索网页", "read_url_content": "读取网页",
    "view_file": "查看文件", "finish": "整理结果",
}
_FAILURE_REASONS = {
    "stalled": "超过 10 分钟没有任何进展，判定为卡住，已终止",
    "safety_cap": "运行达到 60 分钟保底上限，已终止",
    "tool_budget": "工具调用次数达到上限，已终止",
}


def is_stop_command(text) -> bool:
    return isinstance(text, str) and bool(STOP_COMMAND.match(text))


def register_group_worker(worker):
    """Reuse existing per-group send lock and proactive cooldown accounting."""
    with _LOCK:
        _GROUP_WORKERS[worker.chat_name] = worker


def _file(rid: str) -> Path:
    if not IDENTIFIER.fullmatch(str(rid)):
        raise ValueError("invalid owner task id")
    return OUTBOX / (rid + ".json")


def _save(state: dict) -> None:
    OUTBOX.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(OUTBOX, 0o700)
    fd, temp = tempfile.mkstemp(prefix=".owner-", dir=OUTBOX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(state, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, _file(state["request_id"]))
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def _load() -> None:
    if not OUTBOX.is_dir():
        return
    for path in OUTBOX.glob("owner-*.json"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            rid = path.stem
            _file(rid)
            item = json.loads(path.read_text(encoding="utf-8"))
            if (item.get("request_id") == rid
                    and item.get("scope") in {"group", "private"}
                    and item.get("delivery") in {"pending", "sending", "sent", "uncertain"}):
                # A pre-crash send attempt may have reached WeChat; never
                # duplicate it automatically.
                if item["delivery"] == "sending":
                    item["delivery"] = "uncertain"
                    _save(item)
                if item["delivery"] == "pending":
                    _OUTSTANDING[rid] = item
        except (ValueError, OSError, TypeError):
            continue


def ensure_started() -> None:
    global _STARTED
    if os.environ.get("CW_IGNORE_LIVE_OWNER_SETTINGS") == "1":
        return
    with _LOCK:
        if _STARTED:
            return
        _load()
        thread = threading.Thread(
            target=_monitor, name="wechat-owner-agent-delivery", daemon=True
        )
        thread.start()
        _STARTED = True
        core.log("[wechat-owner-async] delivery_monitor=ready retained=%d" % len(_OUTSTANDING))


def submit(event, instruction, chat_name, *, private_chat=False, context=None):
    """Enqueue only AFTER the normal exact-wxid check; return fast ACK text."""
    owner = core.owner_wxid()
    if not llm_client.verified_owner_event(
        event, owner, private_chat=private_chat
    ):
        raise llm_client.LLMExecutionError("owner_not_verified")
    if is_stop_command(instruction):
        return _stop(instruction, chat_name, private_chat=private_chat)
    accepted = llm_client.submit_owner(
        event, instruction, owner, private_chat=private_chat, context=context,
    )
    rid = accepted["request_id"]
    with _LOCK:
        existing = _OUTSTANDING.get(rid)
        if existing is None and not _file(rid).exists():
            item = {
                "request_id": rid,
                "scope": "private" if private_chat else "group",
                "chat": str(chat_name),
                "delivery": "pending",
                "started_at": time.time(),
                "last_notice": 0,
                "last_notice_tool_count": 0,
                "ack_claimed": False,
            }
            _save(item)
            _OUTSTANDING[rid] = item
        else:
            item = existing
            if item is None:
                return None
        if item.get("ack_claimed"):
            return None
        item["ack_claimed"] = True
        _save(item)
    ensure_started()
    label = "只读查询" if accepted.get("effort") == "medium" else "深入处理"
    return (
        f"已接收管理员任务 #{rid[-6:]}（{label}），正在处理。"
        "任务完成后会自动回报；耗时较长时会发送阶段进度。"
    )


def _stop(instruction, chat_name, *, private_chat=False):
    """Stop one pending task of this exact chat; never starts a root task."""
    suffix = (STOP_COMMAND.match(instruction).group(1) or "").lower()
    scope = "private" if private_chat else "group"
    with _LOCK:
        candidates = sorted(
            rid for rid, item in _OUTSTANDING.items()
            if item.get("chat") == str(chat_name) and item.get("scope") == scope
            and item.get("delivery") == "pending"
            and (not suffix or rid.endswith(suffix))
        )
    label = f" #{suffix}" if suffix else ""
    if not candidates:
        return f"没有找到正在进行的管理员任务{label}。"
    if len(candidates) > 1:
        listed = "、".join("#" + rid[-6:] for rid in candidates)
        return f"当前有多个管理员任务在进行：{listed}。请回复「停止任务 #编号」。"
    rid = candidates[0]
    try:
        state = llm_client.owner_cancel(rid)
    except llm_client.LLMExecutionError as exc:
        core.log("[wechat-owner-async] cancel_failed=%s" % exc.kind)
        return f"停止管理员任务 #{rid[-6:]} 的请求没有发送成功，请稍后再试。"
    core.log("[wechat-owner-async] cancel_requested=yes state=%s" % state.get("state"))
    if state.get("state") == "succeeded":
        return f"管理员任务 #{rid[-6:]} 已经完成，结果马上发出。"
    return f"收到，正在停止管理员任务 #{rid[-6:]}，停下后会回报停止前的进展。"


def _claim(rid, *, terminal=False):
    with _LOCK:
        item = _OUTSTANDING.get(rid)
        if item is None or item.get("delivery") != "pending":
            return None
        if terminal:
            item["delivery"] = "sending"
            _save(item)
        return dict(item)


def _delivered(rid):
    with _LOCK:
        item = _OUTSTANDING.get(rid)
        if item is not None:
            item["delivery"] = "sent"
            _save(item)
            del _OUTSTANDING[rid]


def _deliver(item, message):
    """Exact normal Bot send guard; never send on API or model behalf."""
    chat = item["chat"]
    if not chat or len(chat) > 160:
        raise ValueError("invalid delivery chat")
    prefix = core.bot_prefix()
    chunks = core.split_formal_reply(str(message))
    if not chunks:
        raise ValueError("empty owner final")
    outgoing = [prefix + part for part in chunks]
    if item["scope"] == "private":
        result = core.private_focus_and_send_many(chat, outgoing)
    else:
        if core.group_muted(chat):
            raise RuntimeError("group muted")
        with _LOCK:
            worker = _GROUP_WORKERS.get(chat)
        if worker is None or worker.stop_event.is_set():
            raise RuntimeError("group worker unavailable")
        with worker._reply_state_lock:
            generation = worker._reply_generation
        result = worker._reply_send_many_guarded(
            generation, outgoing, local_id=None,
        )
        if result is None:
            raise RuntimeError("group worker changed during owner delivery")
    core.save_message(chat if item["scope"] == "group" else "private:" + chat, "bot",
                      prefix + str(message))
    return result


def _progress(item, state):
    # v3.189: bounded progress notices (first at 45s, then every 3 minutes);
    # no raw commands/prompts, only the tool kind.
    rid = item["request_id"]
    elapsed = max(0.0, time.time() - item["started_at"])
    milestone = _FIRST_NOTICE_S if elapsed >= _FIRST_NOTICE_S else 0
    if elapsed >= _NOTICE_EVERY_S:
        milestone = int(elapsed // _NOTICE_EVERY_S) * _NOTICE_EVERY_S
    if not milestone or item.get("last_notice", 0) >= milestone:
        return
    tool_count = int(state.get("tool_count") or 0)
    phase = str(state.get("phase") or "")
    # Never produce generic filler if the model has not advanced.
    if tool_count <= item.get("last_notice_tool_count", 0) and phase == "starting":
        return
    minutes = max(1, int(elapsed // 60))
    step = _TOOL_LABELS.get(str(state.get("last_tool") or ""), "")
    if phase == "stopping":
        status = "正在停止"
    elif phase == "wrapup":
        status = "正在收尾整理结果"
    else:
        status = "仍在处理中"
    msg = (
        f"管理员任务 #{rid[-6:]} {status}：已运行约 {minutes} 分钟，"
        f"调用 {tool_count} 次工具"
        + (f"，最近一步是{step}" if step else "")
        + "。不需要了可以回复「"
        + ("@我 " if item.get("scope") == "group" else "")
        + f"停止任务 #{rid[-6:]}」。"
    )
    try:
        _deliver(item, msg)
    except Exception as exc:
        core.log("[wechat-owner-async] progress_delivery_failed=%s" % type(exc).__name__)
        return
    with _LOCK:
        current = _OUTSTANDING.get(rid)
        if current is not None:
            current["last_notice"] = milestone
            current["last_notice_tool_count"] = tool_count
            _save(current)


def _human_size(size):
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _task_files(task):
    files = []
    for entry in (task.get("files") or [])[:3]:
        if not isinstance(entry, dict):
            continue
        name, size = entry.get("name"), entry.get("size")
        if (isinstance(name, str) and _SAFE_FILE.fullmatch(name)
                and type(size) is int and 0 < size <= _MAX_FILE_BYTES):
            files.append({"name": name, "size": size})
    return files


def _send_one_file(item, name):
    source = HANDOFF / item["request_id"] / name
    staged = core.stage_send_file(source, item["request_id"][-12:])
    try:
        if item["scope"] == "private":
            return core.private_focus_and_send_file(item["chat"], staged)
        if core.group_muted(item["chat"]):
            raise RuntimeError("group muted")
        return core.focus_and_send_file(item["chat"], staged)
    finally:
        core.unstage_send_file(staged)


def _deliver_files(item, files):
    """At-most-once attachment delivery after the final text was sent."""
    rid = item["request_id"]
    record = dict(item)
    # The final text is already delivered; never let this copy resurrect
    # the pre-send "pending" state (that would replay the text on restart).
    record["delivery"] = "sent"
    record["files"] = {}
    problems = []
    for entry in files:
        name = entry["name"]
        record["files"][name] = "sending"
        _save(record)  # durable before the only UI attempt for this file
        try:
            upload = _send_one_file(item, name)
            result = "upload_failed" if upload == "failed" else "sent"
        except core.FileSendUnconfirmedError:
            result = "uncertain"
        except Exception as exc:
            result = "failed"
            core.log("[wechat-owner-files] send_failed=%s" % type(exc).__name__)
        record["files"][name] = result
        _save(record)
        if result == "uncertain":
            problems.append(f"{name} 发送结果不确定，为避免重复未重发")
        elif result == "upload_failed":
            problems.append(f"{name} 已提交但微信显示上传失败")
        elif result == "failed":
            problems.append(f"{name} 未能发送")
    core.log("[wechat-owner-files] task=%s files=%d problems=%d" % (rid[-6:], len(files), len(problems)))
    if problems:
        try:
            _deliver(item, f"管理员任务 #{rid[-6:]} 的附件：" + "；".join(problems) + "。")
        except Exception as exc:
            core.log("[wechat-owner-files] notice_failed=%s" % type(exc).__name__)


def _failure_text(rid, task):
    error = str(task.get("error") or "执行失败")[:45]
    tools = task.get("tool_count")
    count = f"，已调用 {tools} 次工具" if type(tools) is int and tools > 0 else ""
    partial = task.get("partial")
    if (not isinstance(partial, str) or not partial.strip()
            or llm_client._INTERNAL_PROTOCOL_FIELD_RE.search(partial)):
        partial = ""
    detail = ("\n停止前的进展：\n" + partial.strip()[-1500:]) if partial else ""
    if error == "cancelled":
        return (
            f"管理员任务 #{rid[-6:]} 已按你的指令停止{count}。{detail}"
            "\n停止前的操作可能已经生效，未自动重试。"
        )
    reason = _FAILURE_REASONS.get(error, error)
    return (
        f"管理员任务 #{rid[-6:]} 未能完整完成（{reason}{count}）。{detail}"
        "\n已有操作可能产生效果，未自动重试；请先检查现状。"
    )


def _finish(item, task):
    rid = item["request_id"]
    state = task.get("state")
    files = _task_files(task) if state == "succeeded" else []
    if state == "succeeded":
        answer = task["text"]
        if task.get("stopped") is True:
            text = f"管理员任务 #{rid[-6:]} 已按你的指令停止，停止前的情况：\n" + answer
        else:
            text = f"管理员任务 #{rid[-6:]} 已完成：\n" + answer
        if files:
            text += "\n\n附件（随后发送）：" + "、".join(
                f"{f['name']}（{_human_size(f['size'])}）" for f in files
            )
    elif state == "interrupted":
        text = (
            f"管理员任务 #{rid[-6:]} 执行中断，之前的操作可能已部分生效。"
            "为避免重复修改，未自动重试；请先核对现状再决定是否继续。"
        )
    else:
        text = _failure_text(rid, task)
    if _claim(rid, terminal=True) is None:
        return
    try:
        _deliver(item, text)
    except Exception as exc:
        # WeChat send may have happened before an unconfirmed acknowledgement.
        # Keep 'sending'; on process restart becomes 'uncertain', never replay.
        core.log("[wechat-owner-async] final_delivery_uncertain=%s" % type(exc).__name__)
        return
    _delivered(rid)
    core.log(
        "[wechat-owner-async] task_completed=yes state=%s duration_ms=%s tools=%s"
        % (state, task.get("duration_ms"), task.get("tool_count"))
    )
    if files:
        _deliver_files(item, files)


def _monitor() -> None:
    while True:
        with _LOCK:
            jobs = [dict(v) for v in _OUTSTANDING.values()]
        for item in jobs:
            try:
                state = llm_client.owner_status(item["request_id"])
            except llm_client.LLMExecutionError as exc:
                if exc.kind not in {"owner_status_transport", "owner_status_http"}:
                    core.log("[wechat-owner-async] status_error=%s" % exc.kind)
                continue
            if state["state"] in _TERMINAL:
                _finish(item, state)
            else:
                _progress(item, state)
        time.sleep(_INTERVAL)
