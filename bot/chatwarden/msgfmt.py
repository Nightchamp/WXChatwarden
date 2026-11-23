"""Plain-data helpers for message rows.

Everything here works on rows that are already readable: it turns a stored
message type, body and sender id into the text the rest of the bot uses.
There is no key handling and no cipher code in this module.
"""
from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET

try:  # optional: only needed when a store keeps bodies zstd-compressed
    import zstandard as _zstd
except Exception:  # pragma: no cover - exercised only without the package
    _zstd = None

_ZSTD_COMPRESSED = 4
_XML_MAX_CHARS = 20000
_XML_FORBIDDEN = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")

_TYPE_LABELS = {
    1: "文本",
    3: "图片",
    34: "语音",
    42: "名片",
    43: "视频",
    47: "表情",
    48: "位置",
    49: "链接/文件",
    50: "通话",
    10000: "系统",
    10002: "撤回",
}
_PLACEHOLDERS = {
    3: "[图片]",
    43: "[视频]",
    34: "[语音]",
    47: "[表情]",
}
_CALL_STATUS = {
    "Canceled": "已取消",
    "Line busy": "对方忙线",
    "Call not answered": "未接听",
    "Call wasn't answered": "未接听",
}
_MINI_PROGRAM_TYPES = {33, 36, 44}


def split_type(value):
    """Return ``(base_type, sub_type)`` for a stored message type."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0, 0
    if number > 0xFFFFFFFF:
        return number & 0xFFFFFFFF, number >> 32
    return number, 0


def format_msg_type(value):
    base, _ = split_type(value)
    return _TYPE_LABELS.get(base, f"type={value}")


def decompress_content(content, compression):
    """Return a message body as text; ``None`` when it cannot be read."""
    if isinstance(content, bytes):
        if compression == _ZSTD_COMPRESSED:
            if _zstd is None:
                return None
            try:
                raw = _zstd.ZstdDecompressor().decompress(content)
            except Exception:
                return None
            return raw.decode("utf-8", errors="replace")
        return content.decode("utf-8", errors="replace")
    return content


def load_name2id_maps(conn):
    """Map the per-shard numeric sender id to its username."""
    mapping = {}
    try:
        rows = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
    except sqlite3.Error:
        return mapping
    for rowid, username in rows:
        if username:
            mapping[rowid] = username
    return mapping


def _squash(text):
    return _SPACES.sub(" ", text).strip() if text else ""


def _xml_root(content):
    if (
        not content
        or len(content) > _XML_MAX_CHARS
        or _XML_FORBIDDEN.search(content)
    ):
        return None
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


def _to_int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _app_text(content, local_type):
    if not content or "<appmsg" not in content:
        return None
    root = _xml_root(content)
    if root is None:
        return None
    app = root.find(".//appmsg")
    if app is None:
        return None
    _, sub_type = split_type(local_type)
    title = _squash(app.findtext("title") or "")
    kind = _to_int((app.findtext("type") or "").strip(), _to_int(sub_type))

    if kind == 57:
        quoted = ""
        quoted_from = ""
        ref = app.find(".//refermsg")
        if ref is not None:
            quoted_from = (ref.findtext("displayname") or "").strip()
            quoted = _squash(_TAGS.sub("", ref.findtext("content") or ""))
        if len(quoted) > 160:
            quoted = quoted[:160] + "..."
        text = title or "[引用消息]"
        if quoted:
            lead = f"回复 {quoted_from}: " if quoted_from else "回复: "
            text += f"\n  ↳ {lead}{quoted}"
        return text
    if kind == 6:
        return f"[文件] {title}" if title else "[文件]"
    if kind == 5:
        return f"[链接] {title}" if title else "[链接]"
    if kind in _MINI_PROGRAM_TYPES:
        return f"[小程序] {title}" if title else "[小程序]"
    return f"[链接/文件] {title}" if title else "[链接/文件]"


def _call_text(content):
    if not content or "<voip" not in content:
        return None
    root = _xml_root(content)
    if root is None:
        return "[通话]"
    status = _squash(root.findtext(".//msg") or "")
    if not status:
        return "[通话]"
    if status.startswith("Duration:"):
        length = status.split(":", 1)[1].strip()
        return f"[通话] 通话时长 {length}" if length else "[通话]"
    return f"[通话] {_CALL_STATUS.get(status, status)}"


def format_message_text(local_type, content, is_group):
    """Return ``(sender_prefix, text)`` for one message body.

    Group bodies may carry the sender as a ``username:\\n`` prefix; it is
    split off and returned separately.
    """
    sender = ""
    if content is None:
        text = ""
    elif isinstance(content, bytes):
        text = "(二进制内容)"
    else:
        text = content
        if is_group and ":\n" in content:
            sender, text = content.split(":\n", 1)

    base, _ = split_type(local_type)
    if base in _PLACEHOLDERS:
        return sender, _PLACEHOLDERS[base]
    if base == 1:
        return sender, text
    if base == 50:
        return sender, _call_text(text) or "[通话]"
    if base == 49:
        return sender, _app_text(text, local_type) or "[链接/文件]"
    if base == 10000:
        if content and "revokemsg" in content:
            root = _xml_root(content)
            if root is not None:
                revoked = root.findtext(".//content") or ""
                if revoked:
                    return "", f"[撤回] {revoked}"
        label = format_msg_type(local_type)
        return sender, f"[{label}] {text}" if text else f"[{label}]"
    if base == 10002:
        return "", f"[撤回] {text}" if text else "[撤回]"
    label = format_msg_type(local_type)
    return sender, f"[{label}] {text}" if text else f"[{label}]"


def load_contact_names(contact_db_path):
    """Return ``{username: display_name}`` from a readable contact store.

    A contact shows its remark, then its nickname, then its username. A group
    with no name of its own is shown as its first members plus a head count.
    """
    names = {}
    conn = sqlite3.connect(contact_db_path)
    try:
        for username, nick, remark in conn.execute(
            "SELECT username, nick_name, remark FROM contact"
        ).fetchall():
            names[username] = remark or nick or username

        for username in [
            key for key, shown in names.items()
            if "@chatroom" in key and shown == key
        ]:
            try:
                room = conn.execute(
                    "SELECT id FROM contact WHERE username = ?", (username,)
                ).fetchone()
                if not room:
                    continue
                member_ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT member_id FROM chatroom_member "
                        "WHERE room_id = ?",
                        (room[0],),
                    ).fetchall()
                ]
                if not member_ids:
                    continue
                marks = ",".join("?" * len(member_ids))
                members = conn.execute(
                    "SELECT username, nick_name, remark FROM contact "
                    f"WHERE id IN ({marks})",
                    member_ids,
                ).fetchall()
                shown = [
                    (m_remark or m_nick or m_user)[:10]
                    for m_user, m_nick, m_remark in members[:5]
                ]
                names[username] = (
                    "、".join(shown) + f" ({len(member_ids)}人)"
                )
            except sqlite3.Error:
                continue
    finally:
        conn.close()
    return names
