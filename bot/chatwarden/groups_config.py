"""Group configuration file: load, validate and rewrite ``groups.json``.

Split out of ``core``; ``core`` re-exports every name here. Anything that
still lives in ``core`` is reached as ``core.NAME`` so it stays late-bound.
"""
import hashlib
import json
import os
import threading

__all__ = [
    'load_groups_config',
    '_normalize_groups_config',
    'load_groups',
    'group_config_mtime',
    'auto_register_group_config',
    'update_group_personality_config',
    '_v3170_groups_raw_cached',
    '_v3170_live_settings_ignored',
    '_v3193_validate_private_contacts',
    '_v3170_rewrite_groups_config',
    'update_group_settings',
    'update_group_member_ids',
    'update_group_member_map',
    'update_group_member_name_map',
    'update_group_ui_title',
]


def load_groups_config():
    if not core.GROUPS_FILE.exists():
        raise RuntimeError(
            "groups.json missing; pure v3 requires explicit group config"
        )

    raw = json.loads(
        core.GROUPS_FILE.read_text(encoding="utf-8")
    )
    return _normalize_groups_config(raw)


def _normalize_groups_config(raw):
    if not isinstance(raw, dict):
        raise RuntimeError("groups.json root must be an object")
    if (
        "private_nonowner_enabled" in raw
        and not isinstance(raw["private_nonowner_enabled"], bool)
    ):
        raise RuntimeError(
            "groups.json private_nonowner_enabled must be a boolean"
        )
    _v3193_validate_private_contacts(raw.get("private_contacts"))

    raw_groups = raw.get("groups")
    if not isinstance(raw_groups, list):
        raise RuntimeError("groups.json groups must be a list")

    groups = []
    seen = set()
    seen_chat_usernames = {}

    for item in raw_groups:
        if not isinstance(item, dict):
            raise RuntimeError("each group config must be an object")

        name = str(item.get("name") or "").strip()
        if not name:
            raise RuntimeError("group name cannot be empty")
        if name in seen:
            raise RuntimeError(f"duplicate group name: {name!r}")
        seen.add(name)

        normalized = dict(item)
        normalized["name"] = name
        normalized["enabled"] = bool(item.get("enabled", True))
        normalized["pending"] = bool(item.get("pending", False))
        normalized["session_key"] = core.normalize_session_key(
            item.get("session_key"),
            name,
        )

        personality = str(
            item.get("personality") or ""
        ).strip()
        if len(personality) > 2000:
            raise RuntimeError(
                f"group {name!r} personality exceeds 2000 characters"
            )
        normalized["personality"] = personality

        default_personality = str(
            item.get("default_personality")
            if "default_personality" in item
            else personality
        ).strip()
        if len(default_personality) > 2000:
            raise RuntimeError(
                f"group {name!r} default_personality exceeds 2000 characters"
            )
        normalized["default_personality"] = default_personality

        name_prefix = str(
            item.get("name_prefix") or ""
        ).strip()
        if len(name_prefix) > 200:
            raise RuntimeError(
                f"group {name!r} name_prefix exceeds 200 characters"
            )
        if name_prefix:
            normalized["name_prefix"] = name_prefix

        # v3.170: owner "总开关 关" keeps history but suppresses every send.
        if "muted" in item:
            if not isinstance(item["muted"], bool):
                raise RuntimeError(f"group {name!r} muted must be a boolean")
            normalized["muted"] = item["muted"]

        for cooldown_key in (
            "proactive_any_bot_cooldown_sec",
            "proactive_send_cooldown_sec",
        ):
            if cooldown_key not in item:
                continue
            try:
                cooldown_value = float(item[cooldown_key])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"group {name!r} {cooldown_key} must be numeric"
                ) from exc
            if not 0.0 <= cooldown_value <= 86400.0:
                raise RuntimeError(
                    f"group {name!r} {cooldown_key} must be between 0 and 86400 seconds"
                )
            normalized[cooldown_key] = cooldown_value

        member_labels = item.get("member_labels") or {}
        if not isinstance(member_labels, dict):
            raise RuntimeError(
                f"group {name!r} member_labels must be an object"
            )
        normalized["member_labels"] = {
            str(k): str(v).strip()
            for k, v in member_labels.items()
            if str(v).strip()
        }

        verified_members = item.get("verified_members") or {}
        if not isinstance(verified_members, dict):
            raise RuntimeError(
                f"group {name!r} verified_members must be an object"
            )
        normalized["verified_members"] = {
            str(k): str(v).strip()
            for k, v in verified_members.items()
            if str(v).strip()
        }

        chat_username = str(
            item.get("chat_username")
            or item.get("db_username")
            or ""
        ).strip()
        if chat_username:
            if "@chatroom" not in chat_username:
                raise RuntimeError(
                    f"group {name!r} chat_username is not a chatroom"
                )
            previous_name = seen_chat_usernames.get(chat_username)
            if previous_name is not None:
                raise RuntimeError(
                    f"duplicate chat_username {chat_username!r}: "
                    f"{previous_name!r} and {name!r}"
                )
            seen_chat_usernames[chat_username] = name
            normalized["chat_username"] = chat_username

        groups.append(normalized)

    enabled = [
        item
        for item in groups
        if item["enabled"]
    ]
    if not enabled:
        raise RuntimeError("groups.json has no enabled groups")
    return enabled


def load_groups():
    return load_groups_config()


def group_config_mtime():
    try:
        return core.GROUPS_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def auto_register_group_config(chat_username, display_name):
    """Atomically append one newly joined exact chatroom to groups.json."""
    chat_username = str(chat_username or "").strip()
    display_name = str(display_name or "").strip()
    if not chat_username or "@chatroom" not in chat_username:
        raise RuntimeError("auto-register requires an exact chatroom username")
    if not display_name:
        raise RuntimeError("auto-register requires a non-empty group title")

    with core._GROUPS_CONFIG_WRITE_LOCK:
        source_stat = core.GROUPS_FILE.stat()
        raw = json.loads(core.GROUPS_FILE.read_text(encoding="utf-8"))
        groups = raw.get("groups") if isinstance(raw, dict) else None
        if not isinstance(groups, list):
            raise RuntimeError("groups.json groups must be a list")

        for item in groups:
            if not isinstance(item, dict):
                continue
            existing_username = str(
                item.get("chat_username")
                or item.get("db_username")
                or ""
            ).strip()
            if existing_username == chat_username:
                return {
                    "created": False,
                    "group": dict(item),
                }

        used_names = {
            str(item.get("name") or "").strip()
            for item in groups
            if isinstance(item, dict)
        }
        logical_name = display_name
        if logical_name in used_names:
            suffix = hashlib.sha256(
                chat_username.encode("utf-8")
            ).hexdigest()[:8]
            logical_name = f"{display_name} [{suffix}]"
            counter = 2
            while logical_name in used_names:
                logical_name = f"{display_name} [{suffix}-{counter}]"
                counter += 1

        item = {
            "name": logical_name,
            "enabled": True,
            # A new contact row can appear before its message table is ready.
            # Keep the existing pending-group binding path responsible for
            # waiting without making the whole runtime unhealthy.
            "pending": True,
            "chat_username": chat_username,
            "session_key": core.session_key_for_chat(logical_name),
            "personality": core._AUTO_GROUP_DEFAULT_PERSONALITY,
            "default_personality": core._AUTO_GROUP_DEFAULT_PERSONALITY,
            "member_labels": {},
            "verified_members": {},
            "auto_registered": True,
            # v3.177: a newly joined group starts with the owner master switch
            # off (not ingested, never speaks) until the owner opens it.
            "muted": True,
        }
        groups.append(item)

        tmp = core.GROUPS_FILE.with_name(
            f".{core.GROUPS_FILE.name}.autoreg-{os.getpid()}-{threading.get_ident()}.tmp"
        )
        core._write_groups_file_atomic(raw, source_stat, tmp)

        return {
            "created": True,
            "group": dict(item),
        }


def update_group_personality_config(
    group_name,
    personality=None,
    *,
    restore_default=False,
):
    """Atomically update one group's personality while preserving file owner/mode."""
    name = str(group_name or "").strip()
    if not name:
        raise RuntimeError("group name cannot be empty")
    if restore_default and personality is not None:
        raise RuntimeError("restore_default cannot include personality text")

    with core._GROUPS_CONFIG_WRITE_LOCK:
        source_stat = core.GROUPS_FILE.stat()
        raw = json.loads(core.GROUPS_FILE.read_text(encoding="utf-8"))
        groups = raw.get("groups") if isinstance(raw, dict) else None
        if not isinstance(groups, list):
            raise RuntimeError("groups.json groups must be a list")

        target = None
        for item in groups:
            if isinstance(item, dict) and str(item.get("name") or "").strip() == name:
                target = item
                break
        if target is None:
            raise RuntimeError(f"group not found: {name!r}")

        current = str(target.get("personality") or "").strip()
        if "default_personality" in target:
            default_personality = str(
                target.get("default_personality") or ""
            ).strip()
        else:
            default_personality = current
            target["default_personality"] = default_personality

        if len(default_personality) > 2000:
            raise RuntimeError(
                f"group {name!r} default_personality exceeds 2000 characters"
            )

        if restore_default:
            final_personality = default_personality
        else:
            final_personality = str(personality or "").strip()
            if not final_personality:
                raise RuntimeError("personality cannot be empty")
            if len(final_personality) > 2000:
                raise RuntimeError("personality exceeds 2000 characters")

        target["personality"] = final_personality
        tmp = core.GROUPS_FILE.with_name(
            f".{core.GROUPS_FILE.name}.personality-{os.getpid()}.tmp"
        )
        core._write_groups_file_atomic(raw, source_stat, tmp)

        return final_personality


def _v3170_groups_raw_cached():
    """Cheap mtime-cached raw groups.json; keeps the last good copy on error."""
    try:
        mtime = core.GROUPS_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    with core._V3170_SETTINGS_CACHE_LOCK:
        if core._V3170_SETTINGS_CACHE["mtime"] == mtime:
            return core._V3170_SETTINGS_CACHE["raw"]
        last_good = core._V3170_SETTINGS_CACHE["raw"]
    try:
        raw = json.loads(core.GROUPS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return last_good
    if not isinstance(raw, dict):
        return last_good
    with core._V3170_SETTINGS_CACHE_LOCK:
        core._V3170_SETTINGS_CACHE["mtime"] = mtime
        core._V3170_SETTINGS_CACHE["raw"] = raw
    return raw


def _v3170_live_settings_ignored():
    """Offline regressions must not inherit live owner runtime settings."""
    return os.environ.get("CW_IGNORE_LIVE_OWNER_SETTINGS") == "1"


def _v3193_validate_private_contacts(value):
    if value is None:
        return
    if not isinstance(value, dict):
        raise RuntimeError("groups.json private_contacts must be an object")
    for wxid, entry in value.items():
        if not isinstance(wxid, str) or not wxid.strip() or wxid != wxid.strip():
            raise RuntimeError("groups.json private_contacts keys must be exact wxids")
        if not isinstance(entry, dict) or not isinstance(entry.get("enabled"), bool):
            raise RuntimeError(
                "groups.json private_contacts entries need a boolean enabled"
            )
        name = entry.get("name", "")
        if not isinstance(name, str) or len(name) > core._V3193_PRIVATE_NAME_MAX:
            raise RuntimeError("groups.json private_contacts name must be a short string")


def _v3170_rewrite_groups_config(mutator, tag):
    """Validate-then-atomically-replace groups.json, preserving owner/mode."""
    with core._GROUPS_CONFIG_WRITE_LOCK:
        source_stat = core.GROUPS_FILE.stat()
        raw = json.loads(core.GROUPS_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("groups"), list):
            raise RuntimeError("groups.json groups must be a list")
        result = mutator(raw)
        _normalize_groups_config(json.loads(json.dumps(raw)))
        tmp = core.GROUPS_FILE.with_name(
            f".{core.GROUPS_FILE.name}.{tag}-{os.getpid()}.tmp"
        )
        core._write_groups_file_atomic(raw, source_stat, tmp)
        return result


def update_group_settings(
    group_name,
    *,
    expected_chat_username,
    set_values=None,
    unset=(),
):
    """Owner settings write for one exact group (name + chat_username)."""
    name = str(group_name or "").strip()
    expected = str(expected_chat_username or "").strip()
    set_values = dict(set_values or {})
    unset = tuple(unset or ())
    for key, value in set_values.items():
        kind = core._V3170_GROUP_SETTING_KEYS.get(key)
        if kind is bool and not isinstance(value, bool):
            raise RuntimeError(f"{key} must be a boolean")
        if kind is float and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= float(value) <= 86400.0
        ):
            raise RuntimeError(f"{key} must be 0..86400 seconds")
        if kind is None:
            raise RuntimeError(f"unsupported group setting {key!r}")
    for key in unset:
        if key not in core._V3170_GROUP_SETTING_KEYS:
            raise RuntimeError(f"unsupported group setting {key!r}")

    def mutate(raw):
        target = None
        for item in raw["groups"]:
            if isinstance(item, dict) and str(item.get("name") or "").strip() == name:
                target = item
                break
        if target is None:
            raise RuntimeError(f"group not found: {name!r}")
        actual = str(
            target.get("chat_username") or target.get("db_username") or ""
        ).strip()
        if actual != expected:
            raise RuntimeError("group identity changed")
        for key, value in set_values.items():
            if key == "muted" and value is False:
                target.pop("muted", None)
                continue
            target[key] = float(value) if core._V3170_GROUP_SETTING_KEYS[key] is float else value
        for key in unset:
            target.pop(key, None)

    return _v3170_rewrite_groups_config(mutate, "settings")


def update_group_member_ids(chat_name, ids):
    """Exact current membership (chatroom_member) for owner member search."""
    chat_name = str(chat_name or "").strip()
    if not chat_name:
        return
    clean = frozenset(str(i).strip() for i in (ids or ()) if str(i).strip())
    with core._NAMES_LOCK:
        if clean:
            core._GROUP_MEMBER_IDS[chat_name] = clean
        else:
            core._GROUP_MEMBER_IDS.pop(chat_name, None)


def update_group_member_map(
    group_name,
    *,
    expected_chat_username,
    map_key,
    wxid,
    value,
):
    """Set/remove one member_labels / verified_members entry (exact wxid)."""
    if map_key not in core._V3171_MEMBER_MAPS:
        raise RuntimeError(f"unsupported member map {map_key!r}")
    name = str(group_name or "").strip()
    expected = str(expected_chat_username or "").strip()
    wxid = str(wxid or "").strip()
    if not wxid or wxid.startswith("dbwxid:"):
        raise RuntimeError("member id must be an exact raw wxid")
    if value is not None:
        value = " ".join(str(value).split()).strip()
        if not value or len(value) > 40:
            raise RuntimeError("member value must be 1..40 characters")

    def mutate(raw):
        target = None
        for item in raw["groups"]:
            if isinstance(item, dict) and str(item.get("name") or "").strip() == name:
                target = item
                break
        if target is None:
            raise RuntimeError(f"group not found: {name!r}")
        actual = str(
            target.get("chat_username") or target.get("db_username") or ""
        ).strip()
        if actual != expected:
            raise RuntimeError("group identity changed")
        mapping = target.get(map_key)
        if not isinstance(mapping, dict):
            mapping = {}
        mapping.pop("dbwxid:" + wxid, None)
        if value is None:
            mapping.pop(wxid, None)
        else:
            mapping[wxid] = value
        target[map_key] = mapping

    return _v3170_rewrite_groups_config(mutate, "member")


def update_group_member_name_map(chat_name, names):
    chat_name = str(chat_name or "").strip()
    if not chat_name or not isinstance(names, dict):
        return

    clean = {
        str(k).strip(): str(v).strip()
        for k, v in names.items()
        if str(k).strip() and str(v).strip()
    }
    with core._NAMES_LOCK:
        if clean:
            core._GROUP_MEMBER_NAMES[chat_name] = clean
        else:
            core._GROUP_MEMBER_NAMES.pop(chat_name, None)


def update_group_ui_title(chat_name, ui_title):
    """Bind one stable logical group name to its current WeChat window title."""
    logical = str(chat_name or "").strip()
    current = str(ui_title or "").strip()
    if not logical:
        return
    if not current:
        current = logical
    with core._GROUP_UI_TITLES_LOCK:
        previous = core._GROUP_UI_TITLES.get(logical, logical)
        if previous != current:
            core._GROUP_UI_PREVIOUS_TITLES[logical] = previous
            # Keep an already identity-proven exact X11 window binding across
            # a group-title rename. WeChat can leave a detached chat window on
            # the old title even after DB metadata has the new title. The
            # exact window id is stronger identity evidence than the mutable
            # display title; resolve_group_send_title() re-validates the live
            # title before every use and fails closed if the window vanished.
        core._GROUP_UI_TITLES[logical] = current
    if previous != current:
        core.log(
            "[v3.124-group-ui-title] "
            f"group={logical!r} previous_ui_title={previous!r} "
            f"ui_title={current!r} changed=yes"
        )


from . import core  # noqa: E402  (bottom import: core re-exports this module)
