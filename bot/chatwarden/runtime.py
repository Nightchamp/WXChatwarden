from __future__ import annotations

import os
import sqlite3
import time

from . import core
from .db_reader import MessageDb
from .image_pipeline import (
    ImagePipeline,
)
from .state import StateStore
from .worker import GroupWorker
from .private_worker import PrivateWorker
from .wal_watcher import WalChangeWatcher, _normalise_rel_key


POLL_SECONDS = float(
    os.environ.get(
        "CW_DB_POLL_SECONDS",
        "0.5",
    )
)

WAL_INGEST_MODE = str(
    os.environ.get(
        "CW_V330_WAL_INGEST_MODE",
        "primary",
    )
).strip().lower()
WAL_FALLBACK_SECONDS = max(
    0.5,
    float(
        os.environ.get(
            "CW_V330_WAL_FALLBACK_SECONDS",
            "2.0",
        )
    ),
)
WAL_SANITY_SECONDS = max(
    10.0,
    float(
        os.environ.get(
            "CW_V330_WAL_SANITY_SECONDS",
            "60.0",
        )
    ),
)
WAL_WAIT_SLICE_SECONDS = max(
    0.05,
    min(
        0.5,
        float(
            os.environ.get(
                "CW_V330_WAL_WAIT_SLICE_SECONDS",
                "0.5",
            )
        ),
    ),
)
PENDING_GROUP_PROBE_SECONDS = max(
    1.0,
    float(
        os.environ.get(
            "CW_V338_PENDING_GROUP_PROBE_SECONDS",
            "5.0",
        )
    ),
)
UI_RELOGIN_SETTLE_SECONDS = max(
    0.0,
    float(
        os.environ.get(
            "CW_UI_RELOGIN_SETTLE_SECONDS",
            "2.0",
        )
    ),
)


class Runtime:
    def __init__(self):
        core.require_pure_contract()

        self.db = MessageDb()
        self.state = StateStore()
        self.images = ImagePipeline(
            self.db
        )
        self.images.gc(force=True)

        self.groups = (
            core.load_groups()
        )
        self.groups_mtime = (
            core.groups_mtime()
        )
        self.profiles = (
            core.load_profiles()
        )
        self.profiles_mtime = (
            core.profiles_mtime()
        )

        core.migrate_history(
            self.groups
        )
        self._v317_backfill_raw_wechat_history()

        self.entries = {}
        core.set_group_exact_route_provider(
            self._v3128_group_exact_route,
        )
        self.pause_logged = False
        self._v370_ui_paused = False

        # v3.30 WeChatWatcher-style ingest: use WAL filesystem activity as the
        # wake signal, while keeping the existing DB-native parser. A cheap
        # stat fallback catches missed notifications; a much lower-rate sanity
        # scan remains the final fail-safe. No GUI/archive/JSONL/mirror is added.
        self._v330_wal_mode = WAL_INGEST_MODE
        self._v330_wal_watcher = WalChangeWatcher(
            self.db.db_dir,
            self.db.all_keys,
            settle_seconds=float(
                os.environ.get(
                    "CW_V330_WAL_SETTLE_SECONDS",
                    "0.08",
                )
            ),
        )
        self._v330_wal_enabled = (
            self._v330_wal_mode in {"primary", "on", "1", "true"}
            and self._v330_wal_watcher.available
        )
        now_mono = time.monotonic()
        self._v330_next_stat_scan = now_mono
        self._v330_next_sanity_scan = now_mono + WAL_SANITY_SECONDS
        core.log(
            "[v3.30-wal-ingest] "
            f"requested={self._v330_wal_mode!r} "
            f"enabled={'yes' if self._v330_wal_enabled else 'no'} "
            f"watcher={'inotify' if self._v330_wal_watcher.available else 'unavailable'} "
            f"watched_dirs={len(self._v330_wal_watcher.watched_dirs)} "
            f"settle={self._v330_wal_watcher.settle_seconds:.2f}s "
            f"stat_fallback={WAL_FALLBACK_SECONDS:.1f}s "
            f"sanity={WAL_SANITY_SECONDS:.0f}s "
            "archive=no gui=no jsonl=no mirror=no"
            + (
                ""
                if not self._v330_wal_watcher.error
                else f" watcher_error={self._v330_wal_watcher.error}"
            )
        )

        # db-native-v3.10-private-media-quote
        self._v310_private_enabled = (
            os.environ.get(
                "CW_V310_PRIVATE_ENABLED",
                "1",
            ).strip().lower()
            not in {"0", "false", "no", "off"}
        )
        self._v310_private_poll_seconds = float(
            os.environ.get(
                "CW_V310_PRIVATE_POLL_SECONDS",
                "0.5",
            )
        )
        self._v310_private_last_poll = 0.0
        self._v310_private_state = {}
        self._v310_private_workers = {}
        self._v310_private_empty_edge_grace_seconds = max(
            2.0,
            float(
                os.environ.get(
                    "CW_V359_PRIVATE_EMPTY_EDGE_GRACE_SECONDS",
                    "30.0",
                )
            ),
        )
        self._v310_private_empty_edges = {}

        core.log(
            "[v3.10-private] "
            f"enabled={'yes' if self._v310_private_enabled else 'no'} "
            "scope=all-wxid-1to1 text=reply no-at=yes "
            "images=vision quotes=exact-target+quoted-image-vision proactive=no "
            "send=exact-title-fail-closed"
        )

    def _v317_backfill_raw_wechat_history(self):
        if core.v317_raw_wechat_backfill_done():
            return

        snapshot = self.db.snapshot()
        group_stats = {}
        batch_limit = 500

        for group_cfg in self.groups:
            try:
                resolved = self.db.resolve_group(
                    group_cfg,
                    snapshot,
                )
            except RuntimeError as exc:
                if not bool(group_cfg.get("pending", False)):
                    raise
                core.log(
                    "[v3.38-pending-group] "
                    f"group={group_cfg['name']!r} "
                    "raw_backfill=skipped state=waiting "
                    f"reason={type(exc).__name__}"
                )
                continue
            chat = str(resolved["chat_name"])
            target_max = self.db.max_local_id(
                resolved,
                snapshot,
            )
            cursor = 0
            events = []

            while cursor < target_max:
                batch = self.db.events_after(
                    resolved,
                    cursor,
                    snapshot,
                    limit=batch_limit,
                )
                if not batch:
                    raise RuntimeError(
                        "v3.17 raw history backfill stalled "
                        f"for {chat!r} at local_id={cursor} "
                        f"target={target_max}"
                    )
                events.extend(batch)
                next_cursor = max(
                    int(event.get("local_id") or 0)
                    for event in batch
                )
                if next_cursor <= cursor:
                    raise RuntimeError(
                        "v3.17 raw history backfill made no progress "
                        f"for {chat!r} at local_id={cursor}"
                    )
                cursor = next_cursor

            stats = core.v317_replace_group_memory_from_events(
                chat,
                events,
            )
            group_stats[chat] = stats
            core.log(
                "[v3.17-user-memory] raw_backfill_group=yes "
                f"group={chat!r} scanned={stats['scanned']} "
                f"remembered={stats['remembered']} users={stats['users']} "
                f"max_local_id={target_max}"
            )

        core.v317_finish_raw_wechat_backfill(group_stats)
        core.log(
            "[v3.17-user-memory] raw_backfill_v2=yes "
            f"groups={len(group_stats)} "
            f"scanned={sum(item['scanned'] for item in group_stats.values())} "
            f"remembered={sum(item['remembered'] for item in group_stats.values())} "
            f"users_sum={sum(item['users'] for item in group_stats.values())} "
            "source=raw-wechat-msg-tables"
        )

    def _self_wxid(self):
        return core.bot_wxid(
            self.db.account_wxid
        )

    def _v3128_group_exact_route(
        self,
        logical_name,
        current_title,
    ):
        entry = self.entries.get(
            str(logical_name or "").strip()
        )
        if not entry:
            return None
        resolved = entry.get("resolved") or {}
        chat_username = str(
            resolved.get("chat_username") or ""
        ).strip()
        if not chat_username:
            return None
        route = self.db.v3128_group_ui_route(
            chat_username,
            current_title,
        )
        return route

    def _v3170_group_worker(self, chat_name):
        entry = self.entries.get(str(chat_name or "").strip()) or {}
        return entry.get("worker")

    def _v3177_muted_gate(self, chat_name, entry, snapshot):
        """Return True when this poll must skip the group entirely.

        v3.177: owner master switch off => the group is not read, persisted,
        routed or dispatched at all. Turning it back on rebases the cursor to
        the latest message (no replay or history backfill of the muted gap).
        """
        resolved = entry["resolved"]
        cursor = entry["cursor"]
        if core.group_muted(chat_name):
            if not entry.get("muted"):
                entry["muted"] = True
                core.log(
                    "[v3.177-muted] "
                    f"group={chat_name!r} ingest=off cursor={cursor}"
                )
            return True
        if not entry.get("muted"):
            return False
        try:
            latest = int(self.db.max_local_id(resolved, snapshot) or 0)
        except Exception as exc:
            core.log(
                "[v3.177-muted] "
                f"group={chat_name!r} rebase_error={type(exc).__name__}"
            )
            return True
        entry["muted"] = False
        entry["cursor"] = max(int(cursor), latest)
        self.state.set_checkpoint(
            chat_name,
            resolved["chat_username"],
            resolved["table_name"],
            entry["cursor"],
        )
        core.log(
            "[v3.177-muted] "
            f"group={chat_name!r} ingest=on rebase_from={cursor} "
            f"to={entry['cursor']} replay=no"
        )
        return True

    def _owner_wxid(self):
        return core.owner_wxid(
            "wxid_example0284"
        )

    def _v3127_auto_register_groups(
        self,
        snapshot,
        reason,
    ):
        joined = self.db.current_joined_chatrooms(
            snapshot.get("names", {})
        )
        configured = {
            str(cfg.get("chat_username") or "").strip(): cfg
            for cfg in self.groups
            if str(cfg.get("chat_username") or "").strip()
        }

        if not self.state.auto_group_registry_initialized():
            baseline = dict(joined)
            for chat_username, cfg in configured.items():
                baseline.setdefault(
                    chat_username,
                    str(cfg.get("name") or "").strip(),
                )
            self.state.remember_chatrooms(baseline)
            self.state.mark_auto_group_registry_initialized()
            core.log(
                "[v3.127-auto-group-register] "
                f"state=baseline reason={reason} "
                f"joined={len(joined)} configured={len(configured)} "
                f"remembered={len(baseline)} auto_registered=0"
            )
            return 0

        seen = self.state.seen_chatrooms()

        # Configured rooms are never registration candidates, even if a state
        # DB was restored from before that config entry existed.
        configured_seen = {
            chat_username: str(cfg.get("name") or "").strip()
            for chat_username, cfg in configured.items()
            if chat_username not in seen
        }
        if configured_seen:
            self.state.remember_chatrooms(configured_seen)
            seen.update(configured_seen)

        created = 0
        for chat_username in sorted(joined):
            if chat_username in seen or chat_username in configured:
                continue

            display_name = str(joined.get(chat_username) or "").strip()
            if not display_name:
                core.log(
                    "[v3.127-auto-group-register] "
                    f"chatroom={chat_username!r} state=waiting-title "
                    f"reason={reason}"
                )
                continue

            try:
                result = core.auto_register_group_config(
                    chat_username,
                    display_name,
                )
            except Exception as exc:
                core.log(
                    "[v3.127-auto-group-register] "
                    f"chatroom={chat_username!r} title={display_name!r} "
                    f"state=register-error reason={reason} "
                    f"error={type(exc).__name__}: {exc}"
                )
                continue

            group_cfg = result.get("group") or {}
            self.state.remember_chatrooms(
                {chat_username: display_name}
            )
            seen[chat_username] = display_name
            if bool(result.get("created")):
                created += 1
                core.log(
                    "[v3.127-auto-group-register] "
                    f"chatroom={chat_username!r} title={display_name!r} "
                    f"logical_name={str(group_cfg.get('name') or '')!r} "
                    f"state=registered reason={reason} pending=yes"
                )
            else:
                core.log(
                    "[v3.127-auto-group-register] "
                    f"chatroom={chat_username!r} title={display_name!r} "
                    "state=already-configured"
                )

        return created

    _v330_norm_rel_key = staticmethod(_normalise_rel_key)

    def _v330_group_touched(
        self,
        resolved,
        snapshot,
        changed_rel_keys,
    ):
        if changed_rel_keys is None:
            return True
        changed = {
            self._v330_norm_rel_key(value)
            for value in changed_rel_keys
        }
        table_name = resolved["table_name"]
        return any(
            self._v330_norm_rel_key(db.get("rel_key")) in changed
            and table_name in db.get("tables", ())
            for db in snapshot["dbs"]
        )

    def _v330_wait_for_work(self, group_changed=False):
        """Return ``(changed_rel_keys, wake_reason)`` for the next scan."""
        if group_changed or not self._v330_wal_enabled:
            return None, "config" if group_changed else "legacy-poll"

        while True:
            now = time.monotonic()
            waiting_pending = getattr(
                self,
                "_v338_waiting_pending",
                set(),
            )
            if waiting_pending:
                next_pending_probe = float(
                    getattr(self, "_v338_next_pending_probe", 0.0)
                    or 0.0
                )
                if now >= next_pending_probe:
                    self._v338_next_pending_probe = (
                        now + PENDING_GROUP_PROBE_SECONDS
                    )
                    return None, "pending-probe"

            if now >= self._v330_next_stat_scan:
                self._v330_next_stat_scan = now + WAL_FALLBACK_SECONDS
                changed = self._v330_wal_watcher.scan_for_changes()
                if changed:
                    core.log(
                        "[v3.30-wal-ingest] watcher_miss=yes "
                        "recovered_by=stat changed="
                        + ",".join(sorted(changed))
                    )
                    return changed, "stat-fallback"

            now = time.monotonic()
            if now >= self._v330_next_sanity_scan:
                self._v330_next_sanity_scan = now + WAL_SANITY_SECONDS
                return None, "sanity"

            timeout = min(
                WAL_WAIT_SLICE_SECONDS,
                max(0.0, self._v330_next_stat_scan - now),
                max(0.0, self._v330_next_sanity_scan - now),
            )
            if self._v330_wal_watcher.available:
                changed = self._v330_wal_watcher.wait(timeout)
            else:
                # A watcher that fails after startup degrades to the cheap
                # stat fallback instead of spinning on repeated recovery scans.
                if timeout:
                    time.sleep(timeout)
                changed = set()
            if self._v330_wal_watcher.consume_force_full_scan():
                self._v330_next_stat_scan = (
                    time.monotonic() + WAL_FALLBACK_SECONDS
                )
                self._v330_next_sanity_scan = (
                    time.monotonic() + WAL_SANITY_SECONDS
                )
                return None, "watcher-recovery"
            if changed:
                self._v330_next_stat_scan = (
                    time.monotonic() + WAL_FALLBACK_SECONDS
                )
                return changed, "wal"

            # Do not hide pause/config changes behind the fallback interval.
            if core.pause_file().exists():
                return set(), "control"
            if (
                core.groups_mtime() != self.groups_mtime
                or core.profiles_mtime() != self.profiles_mtime
            ):
                return set(), "control"

    def _v370_ui_login_gate(self):
        """Pause ingest while WeChat is logged out; rebaseline on recovery."""
        if core.message_sink() is not None:
            # No client UI is involved when a non-UI sink handles sending.
            return True
        if not core.wechat_ui_main_file().exists():
            if not self._v370_ui_paused:
                core.log(
                    "[v3.70-ui-login] main=no ingest_paused=yes "
                    "checkpoint=held"
                )
            self._v370_ui_paused = True
            return False

        if not self._v370_ui_paused:
            return True

        if UI_RELOGIN_SETTLE_SECONDS:
            time.sleep(UI_RELOGIN_SETTLE_SECONDS)

        try:
            snapshot = self.db.snapshot(
                force_names=True,
                force_message_refresh=True,
            )
            self.sync_groups(
                snapshot,
                reason="ui-main-resume",
                rebaseline_existing=True,
            )
            try:
                self._v310_baseline_private(
                    snapshot,
                    reason="ui-main-resume",
                )
            except Exception as exc:
                core.log(
                    "[v3.70-ui-login] private_rebaseline_error="
                    f"{type(exc).__name__}: {exc}"
                )

            if self._v330_wal_enabled:
                self._v330_wal_watcher.wait(0.0)
                self._v330_wal_watcher.consume_force_full_scan()
                now = time.monotonic()
                self._v330_next_stat_scan = now + WAL_FALLBACK_SECONDS
                self._v330_next_sanity_scan = now + WAL_SANITY_SECONDS
        except Exception as exc:
            core.log(
                "[v3.70-ui-login] resume_rebaseline_error="
                f"{type(exc).__name__}: {exc}; ingest_paused=yes"
            )
            return False

        self._v370_ui_paused = False
        core.log(
            "[v3.70-ui-login] main=yes history_backfill=yes "
            "rebaseline=yes replay=no "
            "ingest_resumed=yes"
        )
        return True

    def _v395_backfill_group_history(
        self,
        entry,
        resolved,
        snapshot,
        target_cursor,
    ):
        """Persist checkpoint gap as history without dispatching any event."""
        start_cursor = int(entry.get("cursor") or 0)
        target_cursor = int(target_cursor or 0)
        result = {
            "from_local_id": start_cursor,
            "to_local_id": target_cursor,
            "scanned": 0,
            "inserted": 0,
            "duplicates": 0,
            "bot_skipped": 0,
        }
        if target_cursor <= start_cursor:
            return result

        cursor = start_cursor
        batch_limit = 500
        while cursor < target_cursor:
            batch = self.db.events_after(
                resolved,
                cursor,
                snapshot,
                limit=batch_limit,
            )
            eligible = [
                event
                for event in batch
                if cursor < int(event.get("local_id") or 0) <= target_cursor
            ]
            if not eligible:
                raise RuntimeError(
                    "history-only backfill made no progress "
                    f"for {resolved['chat_name']!r} at local_id={cursor} "
                    f"target={target_cursor}"
                )

            stats = core.backfill_group_history_events(
                resolved["chat_name"],
                resolved["chat_username"],
                eligible,
            )
            for key in ("scanned", "inserted", "duplicates", "bot_skipped"):
                result[key] += int(stats.get(key) or 0)

            next_cursor = max(int(event["local_id"]) for event in eligible)
            if next_cursor <= cursor:
                raise RuntimeError(
                    "history-only backfill cursor did not advance "
                    f"for {resolved['chat_name']!r} at local_id={cursor}"
                )
            cursor = next_cursor

        core.log(
            "[v3.95-history-only-backfill] "
            f"group={resolved['chat_name']!r} "
            f"from={start_cursor} to={target_cursor} "
            f"scanned={result['scanned']} inserted={result['inserted']} "
            f"duplicates={result['duplicates']} bot_skipped={result['bot_skipped']} "
            "route=no replay=no vision=no reach=no imagegen=no proactive=no"
        )
        return result

    def _v395_filter_history_only_events(self, resolved, events):
        events = list(events or [])
        if not events:
            return events
        seen = core.history_backfill_seen_local_ids(
            resolved["chat_username"],
            [event.get("local_id") for event in events],
        )
        if not seen:
            return events
        filtered = [
            event
            for event in events
            if int(event.get("local_id") or 0) not in seen
        ]
        core.log(
            "[v3.95-history-only-backfill] "
            f"group={resolved['chat_name']!r} "
            f"live_dispatch_suppressed={len(events) - len(filtered)} "
            "reason=already-history-only"
        )
        return filtered

    def _new_entry(
        self,
        group_cfg,
        snapshot,
        reason,
        include_latest=False,
        cursor_override=None,
        persist_checkpoint=True,
        emit_ready=True,
    ):
        resolved = (
            self.db.resolve_group(
                group_cfg,
                snapshot,
            )
        )
        max_local_id = int(
            self.db.max_local_id(
                resolved,
                snapshot,
            )
        )
        if cursor_override is None:
            cursor = (
                max(0, max_local_id - 1)
                if include_latest and max_local_id > 0
                else max_local_id
            )
        else:
            cursor = int(cursor_override)

        worker = GroupWorker(
            resolved,
            self.images,
            self.profiles,
            self._self_wxid(),
            self._owner_wxid(),
        )

        core.update_group_ui_title(
            resolved["chat_name"],
            str(
                snapshot.get("names", {}).get(
                    resolved["chat_username"],
                    resolved["chat_name"],
                )
                or resolved["chat_name"]
            ).strip(),
        )

        if persist_checkpoint:
            self.state.set_checkpoint(
                resolved["chat_name"],
                resolved[
                    "chat_username"
                ],
                resolved[
                    "table_name"
                ],
                cursor,
            )

        if emit_ready:
            core.log(
                f"group={resolved['chat_name']!r} "
                f"ready reason={reason}; "
                f"chatroom="
                f"{resolved['chat_username']!r}; "
                f"table="
                f"{resolved['table_name']!r}; "
                f"checkpoint_local_id={cursor}"
            )

        return {
            "resolved": resolved,
            "cursor": cursor,
            "worker": worker,
        }

    def _v3139_startup_entry(self, group_cfg, snapshot):
        """Restore a persisted checkpoint before startup baseline can skip a gap.

        Cold-start used to construct every entry at the current max local_id,
        which meant messages arriving while bot.py was down were neither
        replayed nor persisted as history.  Reuse the v3.95 history-only path
        when the persisted checkpoint still belongs to the exact same chatroom
        and message table.  New/rebound groups keep the normal baseline policy.
        """
        chat_name = group_cfg["name"]
        if core.group_muted(chat_name):
            # v3.177: a muted group is not processed, so its gap is neither
            # replayed nor back-filled; unmuting starts from the latest message.
            entry = self._new_entry(
                group_cfg,
                snapshot,
                reason="startup-muted-baseline",
            )
            entry["muted"] = True
            return entry
        checkpoint = self.state.get_checkpoint(chat_name)
        if checkpoint is None:
            return self._new_entry(
                group_cfg,
                snapshot,
                reason="startup-baseline",
                include_latest=bool(group_cfg.get("pending", False)),
            )

        resolved = self.db.resolve_group(group_cfg, snapshot)
        same_identity = (
            checkpoint.get("chat_username") == resolved.get("chat_username")
            and checkpoint.get("table_name") == resolved.get("table_name")
        )
        target = int(self.db.max_local_id(resolved, snapshot) or 0)
        previous = int(checkpoint.get("last_local_id") or 0)
        if not same_identity or previous < 0 or previous > target:
            core.log(
                "[v3.139-cold-start-backfill] "
                f"group={chat_name!r} recovered=no "
                f"reason={'identity-change' if not same_identity else 'checkpoint-ahead'} "
                f"checkpoint={previous} target={target}"
            )
            return self._new_entry(
                group_cfg,
                snapshot,
                reason="startup-baseline",
            )

        entry = self._new_entry(
            group_cfg,
            snapshot,
            reason="startup-recovery",
            cursor_override=previous,
            persist_checkpoint=False,
            emit_ready=False,
        )
        if target > previous:
            self._v395_backfill_group_history(
                entry,
                resolved,
                snapshot,
                target,
            )
        entry["cursor"] = target
        entry["resolved"] = resolved
        self.state.set_checkpoint(
            chat_name,
            resolved["chat_username"],
            resolved["table_name"],
            target,
        )
        core.log(
            "[v3.139-cold-start-backfill] "
            f"group={chat_name!r} recovered=yes from={previous} to={target} "
            "route=no replay=no"
        )
        core.log(
            f"group={chat_name!r} ready reason=startup-recovered; "
            f"chatroom={resolved['chat_username']!r}; "
            f"table={resolved['table_name']!r}; checkpoint_local_id={target}"
        )
        return entry

    def _v3139_log_startup_readiness(self):
        """Emit one deterministic post-sync line for every configured group."""
        ready = 0
        waiting = getattr(self, "_v338_waiting_pending", set())
        for cfg in self.groups:
            chat_name = cfg["name"]
            entry = self.entries.get(chat_name)
            if entry is None:
                core.log(
                    "[v3.139-startup-ready] "
                    f"group={chat_name!r} status="
                    f"{'waiting-pending' if chat_name in waiting else 'missing'}"
                )
                continue
            ready += 1
            resolved = entry["resolved"]
            core.log(
                "[v3.139-startup-ready] "
                f"group={chat_name!r} status=ready "
                f"chatroom={resolved['chat_username']!r} "
                f"table={resolved['table_name']!r} "
                f"checkpoint_local_id={int(entry['cursor'])}"
            )
        core.log(
            "[v3.139-startup-ready] "
            f"summary ready={ready} configured={len(self.groups)} "
            f"waiting_pending={len(waiting)} complete={'yes' if ready == len(self.groups) else 'no'}"
        )

    def sync_groups(
        self,
        snapshot,
        reason="config",
        rebaseline_existing=False,
    ):
        waiting_pending = getattr(
            self,
            "_v338_waiting_pending",
            None,
        )
        if waiting_pending is None:
            waiting_pending = set()
            self._v338_waiting_pending = waiting_pending

        wanted = {
            cfg["name"]: cfg
            for cfg in self.groups
        }
        waiting_pending.intersection_update(wanted)

        for chat_name in list(
            self.entries
        ):
            if chat_name in wanted:
                continue

            entry = self.entries.pop(
                chat_name
            )
            entry["worker"].stop()
            core.clear_group_ui_title(chat_name)
            self.state.delete_group(
                chat_name
            )

            core.log(
                f"group={chat_name!r} "
                "removed from v3 runtime"
            )

        for chat_name, cfg in (
            wanted.items()
        ):
            if chat_name not in (
                self.entries
            ):
                if bool(cfg.get("pending", False)):
                    try:
                        self.db.resolve_group(
                            cfg,
                            snapshot,
                        )
                    except RuntimeError as exc:
                        waiting_pending.add(chat_name)
                        core.log(
                            "[v3.38-pending-group] "
                            f"group={chat_name!r} state=waiting "
                            "runtime_healthy=yes "
                            f"reason={type(exc).__name__}"
                        )
                        continue

                if reason == "startup-baseline":
                    self.entries[chat_name] = self._v3139_startup_entry(
                        cfg,
                        snapshot,
                    )
                else:
                    self.entries[
                        chat_name
                    ] = self._new_entry(
                        cfg,
                        snapshot,
                        reason=reason,
                        include_latest=(
                            bool(cfg.get("pending", False))
                            and self.state.get_checkpoint(chat_name) is None
                        ),
                    )
                waiting_pending.discard(chat_name)
                continue

            entry = self.entries[
                chat_name
            ]

            resolved = (
                self.db.resolve_group(
                    cfg,
                    snapshot,
                )
            )

            identity_changed = (
                resolved[
                    "chat_username"
                ]
                != entry[
                    "resolved"
                ][
                    "chat_username"
                ]
                or resolved[
                    "table_name"
                ]
                != entry[
                    "resolved"
                ][
                    "table_name"
                ]
            )

            if identity_changed:
                entry[
                    "worker"
                ].stop()
                self.entries[
                    chat_name
                ] = self._new_entry(
                    cfg,
                    snapshot,
                    reason=(
                        "identity-change"
                    ),
                )
                continue

            entry["resolved"] = (
                resolved
            )
            entry["worker"].update(
                resolved=resolved,
                profiles=self.profiles,
            )

            if rebaseline_existing:
                cursor = (
                    self.db.max_local_id(
                        resolved,
                        snapshot,
                    )
                )
                self._v395_backfill_group_history(
                    entry,
                    resolved,
                    snapshot,
                    cursor,
                )
                entry["cursor"] = (
                    cursor
                )
                self.state.set_checkpoint(
                    chat_name,
                    resolved[
                        "chat_username"
                    ],
                    resolved[
                        "table_name"
                    ],
                    cursor,
                )
                core.log(
                    f"group={chat_name!r} "
                    "re-baselined safely; "
                    f"checkpoint_local_id="
                    f"{cursor}"
                )

    def sync_pending_groups(
        self,
        snapshot,
        reason="pending-discovery",
    ):
        waiting_pending = getattr(
            self,
            "_v338_waiting_pending",
            set(),
        )
        if not waiting_pending:
            return 0

        pending_by_name = {
            cfg["name"]: cfg
            for cfg in self.groups
            if bool(cfg.get("pending", False))
        }
        activated = 0
        refresh_after = getattr(
            self,
            "_v338_table_refresh_after",
            None,
        )
        if refresh_after is None:
            refresh_after = {}
            self._v338_table_refresh_after = refresh_after
        name_refresh_after = getattr(
            self,
            "_v338_name_refresh_after",
            None,
        )
        if name_refresh_after is None:
            name_refresh_after = {}
            self._v338_name_refresh_after = name_refresh_after
        name_refresh_interval = getattr(
            self,
            "_v338_name_refresh_interval",
            None,
        )
        if name_refresh_interval is None:
            name_refresh_interval = {}
            self._v338_name_refresh_interval = name_refresh_interval

        for chat_name in list(waiting_pending):
            cfg = pending_by_name.get(chat_name)
            if cfg is None or chat_name in self.entries:
                waiting_pending.discard(chat_name)
                continue

            candidate_snapshot = snapshot
            try:
                self.db.resolve_group(
                    cfg,
                    candidate_snapshot,
                )
            except RuntimeError as exc:
                error_text = str(exc)
                table_missing = (
                    "message table " in error_text
                    and "not found in keyed shards" in error_text
                )
                now = time.monotonic()
                if table_missing:
                    if now < float(refresh_after.get(chat_name, 0.0)):
                        continue
                    refresh_after[chat_name] = now + 5.0
                    try:
                        candidate_snapshot = self.db.snapshot(
                            force_message_refresh=True,
                        )
                        self.db.resolve_group(
                            cfg,
                            candidate_snapshot,
                        )
                    except RuntimeError:
                        continue
                else:
                    if now < float(name_refresh_after.get(chat_name, 0.0)):
                        continue

                    interval = float(
                        name_refresh_interval.get(chat_name, 5.0)
                    )
                    name_refresh_after[chat_name] = now + interval
                    name_refresh_interval[chat_name] = min(
                        60.0,
                        max(5.0, interval * 2.0),
                    )
                    try:
                        candidate_snapshot = self.db.snapshot(
                            force_names=True,
                        )
                        self.db.resolve_group(
                            cfg,
                            candidate_snapshot,
                        )
                    except RuntimeError as refreshed_exc:
                        refreshed_text = str(refreshed_exc)
                        refreshed_table_missing = (
                            "message table " in refreshed_text
                            and "not found in keyed shards" in refreshed_text
                        )
                        if (
                            not refreshed_table_missing
                            or now < float(refresh_after.get(chat_name, 0.0))
                        ):
                            continue
                        refresh_after[chat_name] = now + 5.0
                        try:
                            candidate_snapshot = self.db.snapshot(
                                force_names=True,
                                force_message_refresh=True,
                            )
                            self.db.resolve_group(
                                cfg,
                                candidate_snapshot,
                            )
                        except RuntimeError:
                            continue

            entry = self._new_entry(
                cfg,
                candidate_snapshot,
                reason=reason,
                include_latest=True,
            )
            self.entries[chat_name] = entry
            waiting_pending.discard(chat_name)
            refresh_after.pop(chat_name, None)
            name_refresh_after.pop(chat_name, None)
            name_refresh_interval.pop(chat_name, None)
            activated += 1
            core.log(
                "[v3.38-pending-group] "
                f"group={chat_name!r} state=bound "
                f"chatroom={entry['resolved']['chat_username']!r} "
                f"checkpoint_local_id={entry['cursor']} "
                "latest_message_replay=yes older_history_replay=no"
            )

        return activated


    # db-native-v3.10-private-media-quote
    def _v310_baseline_private(
        self,
        snapshot,
        reason,
    ):
        if not self._v310_private_enabled:
            return

        sessions = (
            self.db.v310_private_sessions(
                snapshot
            )
        )
        try:
            # v3.193: owner private-switch views list recent private peers.
            core.note_private_sessions(sessions)
        except Exception:
            pass
        self._v310_private_state = {
            username: {
                "signature": item[
                    "signature"
                ],
                "last_timestamp": item[
                    "last_timestamp"
                ],
            }
            for username, item
            in sessions.items()
        }
        self._v310_private_empty_edges.clear()

        core.log(
            "[v3.10-private] "
            f"baseline reason={reason} "
            f"sessions={len(self._v310_private_state)} "
            "replay=no"
        )

    def _v310_worker_for(
        self,
        resolved,
    ):
        username = resolved[
            "chat_username"
        ]
        worker = self._v310_private_workers.get(
            username
        )
        if worker is None:
            worker = PrivateWorker(
                resolved,
                self.images,
                self.profiles,
                self._self_wxid(),
            )
            worker.group_worker_lookup = self._v3170_group_worker
            self._v310_private_workers[
                username
            ] = worker
        else:
            worker.update(
                resolved=resolved,
                profiles=self.profiles,
            )
        return worker

    def _v310_poll_private(
        self,
        snapshot,
    ):
        if not self._v310_private_enabled:
            return 0

        now = time.monotonic()
        if (
            now - self._v310_private_last_poll
            < self._v310_private_poll_seconds
        ):
            return 0
        self._v310_private_last_poll = now

        sessions = (
            self.db.v310_private_sessions(
                snapshot
            )
        )
        try:
            # v3.193: owner private-switch views list recent private peers.
            core.note_private_sessions(sessions)
        except Exception:
            pass
        total = 0
        wall_now = time.time()
        message_snapshot_refreshed = False

        for username, item in sessions.items():
            previous = (
                self._v310_private_state.get(
                    username
                )
            )

            if previous is None:
                # A session created after bot startup may be a brand-new DM.
                # Only treat it as live if it is very recent; old sessions
                # discovered after a names/cache refresh are baselined silently.
                if (
                    wall_now
                    - item["last_timestamp"]
                    > 30.0
                ):
                    self._v310_private_state[
                        username
                    ] = {
                        "signature": item[
                            "signature"
                        ],
                        "last_timestamp": item[
                            "last_timestamp"
                        ],
                    }
                    self._v310_private_empty_edges.pop(
                        username,
                        None,
                    )
                    continue
                previous_timestamp = (
                    item["last_timestamp"]
                    - 1
                )
                same_second = False
            else:
                if (
                    item["signature"]
                    == previous["signature"]
                ):
                    continue

                previous_timestamp = int(
                    previous[
                        "last_timestamp"
                    ]
                )
                same_second = (
                    item["last_timestamp"]
                    == previous_timestamp
                )

            if (
                getattr(self.db, "direct_query_enabled", False)
                and not message_snapshot_refreshed
            ):
                # Private helpers still use the mature plaintext historical
                # lookup path. Refresh it only when a DM actually changed.
                snapshot = self.db.snapshot(
                    force_names=False,
                    force_message_refresh=True,
                )
                message_snapshot_refreshed = True

            try:
                resolved = (
                    self.db.v310_resolve_private(
                        username,
                        snapshot,
                    )
                )

                if same_second:
                    events = (
                        self.db.v310_private_latest_at(
                            resolved,
                            item[
                                "last_timestamp"
                            ],
                            snapshot,
                        )
                    )
                else:
                    events = (
                        self.db.v310_private_events_since(
                            resolved,
                            previous_timestamp,
                            snapshot,
                        )
                    )
            except Exception as exc:
                core.log(
                    "[v3.10-private] "
                    f"peer={username!r} "
                    "read_error="
                    f"{type(exc).__name__}: {exc}"
                )
                # Baseline this SessionTable edge to avoid a hot error loop.
                self._v310_private_state[
                    username
                ] = {
                    "signature": item[
                        "signature"
                    ],
                    "last_timestamp": item[
                        "last_timestamp"
                    ],
                }
                self._v310_private_empty_edges.pop(
                    username,
                    None,
                )
                continue

            edge_timestamp = int(
                item["last_timestamp"]
                or 0
            )
            edge_visible = any(
                int(event.get("timestamp") or 0)
                >= edge_timestamp
                for event in events
            )
            if not edge_visible:
                signature = item["signature"]
                edge = self._v310_private_empty_edges.get(
                    username
                )
                if (
                    isinstance(edge, dict)
                    and edge.get("signature") == signature
                ):
                    first_seen = float(
                        edge.get("first_seen")
                        or now
                    )
                    attempts = int(
                        edge.get("attempts")
                        or 0
                    ) + 1
                else:
                    first_seen = now
                    attempts = 1

                age = max(
                    0.0,
                    now - first_seen,
                )
                if (
                    age
                    < self._v310_private_empty_edge_grace_seconds
                ):
                    self._v310_private_empty_edges[
                        username
                    ] = {
                        "signature": signature,
                        "first_seen": first_seen,
                        "attempts": attempts,
                    }
                    if attempts == 1:
                        core.log(
                            "[v3.59-private-edge] "
                            f"peer={username!r} "
                            "session_changed=yes message_edge=not-visible "
                            "checkpoint=deferred"
                        )
                    continue

                self._v310_private_state[
                    username
                ] = {
                    "signature": signature,
                    "last_timestamp": item[
                        "last_timestamp"
                    ],
                }
                self._v310_private_empty_edges.pop(
                    username,
                    None,
                )
                core.log(
                    "[v3.59-private-edge] "
                    f"peer={username!r} "
                    "session_changed=yes message_edge=not-visible "
                    f"checkpoint=baselined age={age:.2f}s"
                )
                continue

            self._v310_private_empty_edges.pop(
                username,
                None,
            )
            self._v310_private_state[
                username
            ] = {
                "signature": item[
                    "signature"
                ],
                "last_timestamp": item[
                    "last_timestamp"
                ],
            }

            if not resolved["send_safe"]:
                core.log(
                    "[v3.10-private] "
                    f"peer={username!r} "
                    f"chat={resolved['chat_name']!r} "
                    "reply_suppressed=yes reason="
                    f"{resolved['send_safe_reason']}"
                )
                continue

            incoming = []
            for event in events:
                if (
                    str(
                        event.get(
                            "sender_username"
                        )
                        or ""
                    )
                    != username
                ):
                    continue
                local_type = int(
                    event.get("local_type")
                    or 0
                )
                text = str(
                    event.get("text")
                    or ""
                ).strip()
                raw = str(
                    event.get("raw_content")
                    or ""
                )
                is_image = bool(
                    event.get("is_image")
                )
                is_quote = (
                    "<refermsg>" in raw
                    and "<svrid>" in raw
                )
                is_text = (
                    local_type == 1
                    and bool(text)
                )
                # db-native-v3.12-private-emoticon-vision-p1
                is_emoticon = (
                    local_type == 47
                )
                if not (
                    is_text
                    or is_image
                    or is_quote
                    or is_emoticon
                ):
                    continue
                incoming.append(event)

            if not incoming:
                continue

            worker = self._v310_worker_for(
                resolved
            )
            for event in incoming:
                worker.put(event)
                total += 1

            core.log(
                "[v3.10-private] "
                f"chat={resolved['chat_name']!r} "
                f"peer={username!r} "
                f"new_incoming={len(incoming)} "
                f"worker_q={worker.q.qsize()}"
            )

        return total

    def reload_configs(
        self,
    ):
        group_changed = False

        new_group_mtime = (
            core.groups_mtime()
        )
        if (
            new_group_mtime
            != self.groups_mtime
        ):
            self.groups = (
                core.load_groups()
            )
            self.groups_mtime = (
                new_group_mtime
            )
            group_changed = True

            core.log(
                "groups.json reloaded: "
                + ", ".join(
                    repr(
                        cfg["name"]
                    )
                    for cfg
                    in self.groups
                )
            )

        new_profiles_mtime = (
            core.profiles_mtime()
        )
        if (
            new_profiles_mtime
            != self.profiles_mtime
        ):
            self.profiles = (
                core.load_profiles()
            )
            self.profiles_mtime = (
                new_profiles_mtime
            )

            for entry in (
                self.entries.values()
            ):
                entry[
                    "worker"
                ].update(
                    profiles=(
                        self.profiles
                    )
                )

            core.log(
                "sender/profile config "
                "reloaded"
            )

        return group_changed

    def _v3191_startup_retry(self, exc, stage):
        core.log(
            f"[v3.191-snapshot-retry] stage={stage} "
            f"error={type(exc).__name__}: {str(exc)[:120]} "
            "discard_snapshot=yes full_reload=yes retry=1"
        )
        self.db.v3191_discard_snapshots()
        return self.db.snapshot(
            force_names=True,
            force_message_refresh=True,
        )

    def _v3191_startup_snapshot(self):
        try:
            return self.db.snapshot(force_names=True)
        except sqlite3.DatabaseError as exc:
            initial = self._v3191_startup_retry(exc, "snapshot")
            core.log("[v3.191-snapshot-retry] stage=snapshot retry=ok")
            return initial

    def _v3191_startup_sync(self, initial):
        try:
            self.sync_groups(initial, reason="startup-baseline")
            return initial
        except sqlite3.DatabaseError as exc:
            initial = self._v3191_startup_retry(exc, "startup-baseline")
            self.sync_groups(initial, reason="startup-baseline")
            core.log("[v3.191-snapshot-retry] stage=startup-baseline retry=ok")
            return initial

    def run(self):
        core.log(
            "DB-native v3.5 session-diet starting; "
            + (
                "ingest=wal-inotify; "
                f"stat_fallback={WAL_FALLBACK_SECONDS:.1f}s; "
                f"sanity={WAL_SANITY_SECONDS:.0f}s; "
                if self._v330_wal_enabled
                else f"ingest=legacy-poll; poll={POLL_SECONDS:.2f}s; "
            )
            + "checkpoint=monotonic-local-id; "
            "images=message-resource-v2-wxgf; "
            "vision_format=jpeg-fastpath; "
            "wechat_ui=send-only; core=pure-v3.5; legacy=none; "
            f"session_diet=rotating-{core.session_diet_contract()['session_turns']}turn-local6x500; "
            "screenshot_fallback=disabled"
        )

        core.log(
            "groups="
            + ", ".join(
                repr(cfg["name"])
                for cfg in self.groups
            )
            + f"; trigger="
            f"{core.trigger()!r}"
        )

        while core.message_sink() is None and not (
            core.bus_file().exists()
        ):
            core.log(
                "waiting for D-Bus "
                "send channel"
            )
            time.sleep(2)

        initial = self._v3191_startup_snapshot()
        auto_registered = self._v3127_auto_register_groups(
            initial,
            reason="startup",
        )
        if auto_registered:
            self.reload_configs()
            initial = self.db.snapshot(
                force_names=True,
                force_message_refresh=True,
            )
        self._v330_next_stat_scan = (
            time.monotonic() + WAL_FALLBACK_SECONDS
        )
        self._v330_next_sanity_scan = (
            time.monotonic() + WAL_SANITY_SECONDS
        )
        initial = self._v3191_startup_sync(initial)
        self._v3139_log_startup_readiness()
        try:
            self._v310_baseline_private(
                initial,
                reason="startup",
            )
        except Exception as exc:
            core.log(
                "[v3.10-private] startup_baseline_error="
                f"{type(exc).__name__}: {exc}; private_disabled_for_process=yes"
            )
            self._v310_private_enabled = False

        # Reattach Bot-owned delivery to previously accepted owner tasks.
        # Never re-execute root commands after Bot/Gateway restart.
        from . import owner_jobs
        owner_jobs.ensure_started()

        core.log(
            "all groups ready; "
            f"shared_snapshot_dbs="
            f"{len(initial['dbs'])}; "
            "AT-SPI ingest disabled; "
            "DB-native image decode "
            "enabled"
        )
        try:
            from . import memory_docs
            memory_docs.start_service()
        except Exception as exc:
            core.log(
                "[v3.172-memory-doc] scheduler_start_failed="
                f"{type(exc).__name__}"
            )

        while True:
            if not self._v370_ui_login_gate():
                time.sleep(0.5)
                continue

            if core.pause_file().exists():
                if not self.pause_logged:
                    core.log(
                        "paused by "
                        + str(
                            core.pause_file()
                        )
                    )
                    self.pause_logged = True
                time.sleep(0.5)
                continue

            if self.pause_logged:
                snapshot = (
                    self.db.snapshot(
                        force_names=True,
                        force_message_refresh=True,
                    )
                )
                core.log(
                    "resumed; history-backfilling then re-baselining "
                    "all group checkpoints without replay"
                )
                self.sync_groups(
                    snapshot,
                    reason="resume",
                    rebaseline_existing=True,
                )
                try:
                    self._v310_baseline_private(
                        snapshot,
                        reason="resume",
                    )
                except Exception as exc:
                    core.log(
                        "[v3.10-private] resume_baseline_error="
                        f"{type(exc).__name__}: {exc}"
                    )
                if self._v330_wal_enabled:
                    # Drop WAL notifications accumulated while paused. The
                    # resume snapshot just re-baselined all cursors.
                    self._v330_wal_watcher.wait(0.0)
                    self._v330_wal_watcher.consume_force_full_scan()
                    self._v330_next_stat_scan = (
                        time.monotonic() + WAL_FALLBACK_SECONDS
                    )
                    self._v330_next_sanity_scan = (
                        time.monotonic() + WAL_SANITY_SECONDS
                    )
                self.pause_logged = False

            group_changed = (
                self.reload_configs()
            )

            changed_rel_keys, wake_reason = (
                self._v330_wait_for_work(
                    group_changed=group_changed,
                )
            )
            if wake_reason == "control":
                continue

            poll_started = (
                time.monotonic()
            )

            try:
                snapshot = (
                    self.db.snapshot(
                        force_names=(
                            group_changed
                            or wake_reason == "pending-probe"
                        ),
                        changed_rel_keys=(
                            changed_rel_keys
                        ),
                    )
                )
            except Exception as exc:
                core.log(
                    "DB snapshot error: "
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )
                time.sleep(
                    POLL_SECONDS
                )
                continue

            if self._v330_wal_enabled:
                self._v330_wal_watcher.update_rel_keys(
                    self.db.all_keys
                )

            auto_group_probe = (
                changed_rel_keys is None
                or any(
                    self._v330_norm_rel_key(value).startswith(
                        "contact/contact.db"
                    )
                    for value in changed_rel_keys
                )
            )
            auto_registered = (
                self._v3127_auto_register_groups(
                    snapshot,
                    reason=wake_reason,
                )
                if auto_group_probe
                else 0
            )
            if auto_registered:
                group_changed = (
                    self.reload_configs()
                    or group_changed
                )
                snapshot = self.db.snapshot(
                    force_names=True,
                    force_message_refresh=True,
                )
                if self._v330_wal_enabled:
                    self._v330_wal_watcher.update_rel_keys(
                        self.db.all_keys
                    )

            pending_activated = self.sync_pending_groups(
                snapshot,
                reason="pending-discovery",
            )
            if pending_activated:
                snapshot = self.db.snapshot(
                    force_names=True,
                    force_message_refresh=True,
                )
                if self._v330_wal_enabled:
                    self._v330_wal_watcher.update_rel_keys(
                        self.db.all_keys
                    )

            if group_changed:
                try:
                    self.sync_groups(
                        snapshot,
                        reason=(
                            "groups-hot-reload"
                        ),
                    )
                except Exception as exc:
                    core.log(
                        "group hot-reload "
                        "resolution error: "
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )

            try:
                self._v310_poll_private(
                    snapshot
                )
            except Exception as exc:
                core.log(
                    "[v3.10-private] poll_error="
                    f"{type(exc).__name__}: {exc}"
                )

            # v3.124: group display titles may change without changing the
            # stable chatroom identity or groups.json logical name. Refresh
            # the send-only UI title from the current contact-name snapshot;
            # history/session/memory keys remain on the stable logical name.
            current_names = snapshot.get("names", {})
            for logical_name, current_entry in list(self.entries.items()):
                current_resolved = current_entry["resolved"]
                current_title = str(
                    current_names.get(
                        current_resolved["chat_username"],
                        "",
                    )
                    or ""
                ).strip()
                if not current_title:
                    continue
                core.update_group_ui_title(
                    logical_name,
                    current_title,
                )

            total_new = 0

            for chat_name, entry in (
                list(
                    self.entries.items()
                )
            ):
                resolved = entry[
                    "resolved"
                ]
                cursor = entry[
                    "cursor"
                ]

                if self._v3177_muted_gate(chat_name, entry, snapshot):
                    continue

                if not self._v330_group_touched(
                    resolved,
                    snapshot,
                    changed_rel_keys,
                ):
                    continue

                try:
                    events = (
                        self.db.events_after(
                            resolved,
                            cursor,
                            snapshot,
                        )
                    )
                except Exception as exc:
                    core.log(
                        f"group={chat_name!r} "
                        "DB read error: "
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )
                    continue

                if not events:
                    continue

                dispatch_events = self._v395_filter_history_only_events(
                    resolved,
                    events,
                )
                for event in dispatch_events:
                    entry[
                        "worker"
                    ].put(event)

                new_cursor = max(
                    event["local_id"]
                    for event in events
                )

                # Monotonic only. A DB snapshot can never
                # move the runtime cursor backwards.
                entry["cursor"] = max(
                    cursor,
                    new_cursor,
                )

                self.state.set_checkpoint(
                    chat_name,
                    resolved[
                        "chat_username"
                    ],
                    resolved[
                        "table_name"
                    ],
                    entry["cursor"],
                )

                total_new += len(
                    dispatch_events
                )

                core.log(
                    f"group={chat_name!r} "
                    f"new={len(dispatch_events)} "
                    f"checkpoint="
                    f"{entry['cursor']} "
                    f"worker_q="
                    f"{entry['worker'].q.qsize()}"
                )

            poll_elapsed = (
                time.monotonic()
                - poll_started
            )

            if total_new:
                core.log(
                    "poll complete; "
                    f"new={total_new}; "
                    f"wake={wake_reason}; "
                    f"dbs="
                    f"{len(snapshot['dbs'])}; "
                    f"refreshed="
                    f"{snapshot.get('refreshed_dbs', len(snapshot['dbs']))}; "
                    f"reused="
                    f"{snapshot.get('reused_dbs', 0)}; "
                    f"refresh="
                    f"{snapshot['refresh_seconds']:.2f}s; "
                    f"scan={poll_elapsed:.2f}s"
                )
                if (
                    self._v330_wal_enabled
                    and wake_reason in {
                        "stat-fallback",
                        "sanity",
                        "watcher-recovery",
                    }
                ):
                    core.log(
                        "[v3.30-wal-ingest] "
                        f"fallback_caught=yes reason={wake_reason} "
                        f"new={total_new} watcher_miss_possible=yes"
                    )

            self.images.gc()

            if not self._v330_wal_enabled:
                time.sleep(
                    max(
                        0.05,
                        POLL_SECONDS
                        - poll_elapsed,
                    )
                )


def main():
    runtime = Runtime()
    runtime.run()
