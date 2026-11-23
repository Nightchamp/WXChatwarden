from __future__ import annotations

import os
import hashlib
import json
import queue
import sqlite3
import xml.etree.ElementTree as ET
import re
import threading
import time
from dataclasses import dataclass

from . import core, decision_client, memory_docs, slow_ack
from .emoticon_pipeline import EmoticonPipeline
from .image_pipeline import ImageResourceIncompleteError
from .reply_dispatch import ReplyDispatcher, ReplyJob
from .voice_pipeline import VoicePipeline
from .worker_text import *  # noqa: F401,F403
from .worker_proactive import ProactiveMixin
from .worker_quote import QuoteContextMixin
from .worker_followup import FollowupMixin
from .worker_media import MediaMixin
from .worker_commands import CommandsMixin
from .worker_jobs import JobsMixin
from .worker_reply import ReplyGuardMixin

class GroupWorker(
    ProactiveMixin,
    QuoteContextMixin,
    FollowupMixin,
    MediaMixin,
    CommandsMixin,
    JobsMixin,
    ReplyGuardMixin,
):

    def __init__(
        self,
        resolved,
        image_pipeline,
        profiles,
        bot_wxid,
        owner_wxid,
    ):
        self.resolved = resolved
        self.group_cfg = resolved[
            "group_cfg"
        ]
        self._refresh_newcomer_welcome_enabled()
        self.chat_name = resolved[
            "chat_name"
        ]
        self.image_pipeline = (
            image_pipeline
        )
        self.voice_pipeline = (
            VoicePipeline(image_pipeline.db)
            if image_pipeline is not None
            and getattr(image_pipeline, "db", None) is not None
            else None
        )
        # db-native-v3.12-group-emoticon-vision-p2
        self.emoticon_pipeline = EmoticonPipeline()
        self.profiles = profiles
        self.bot_wxid = bot_wxid
        decision_client.initialize()
        self.owner_wxid = (
            owner_wxid
        )
        self.image_max_age = float(
            os.environ.get(
                "CW_V3_IMAGE_PAIR_MAX_AGE",
                "120",
            )
        )
        self.pending_wait = float(
            os.environ.get(
                "CW_V3_NEXT_IMAGE_WAIT",
                "8",
            )
        )

        self.q = queue.Queue()
        self.stop_event = (
            threading.Event()
        )
        self.recent = {}
        # Short-lived cache for: quote an image/question, then send @bot
        # separately. It is deliberately sender-scoped and time-bounded.
        self.recent_text = {}
        # v3.23.1: quote image edit candidate waiting for a follow-up @bot.
        # sender-scoped and intentionally short-lived (15s).
        self.pending_image_edit = {}
        # v3.21 Type49 share cards: keep only supported ordinary link cards
        # (app types 4/5) and merged chat records (app type 19). Mini-program
        # cards are deliberately excluded.
        self.recent_app = {}
        self.adjacent_quote_max_age = max(
            1.0,
            float(
                os.environ.get(
                    "CW_V3_ADJACENT_QUOTE_MAX_AGE",
                    "15",
                )
            ),
        )
        self.app_context_max_chars = max(
            1000,
            int(
                os.environ.get(
                    "CW_V321_APP_CONTEXT_MAX_CHARS",
                    "12000",
                )
            ),
        )
        self.pending = {}

        core.log(
            "[v3.21-appmsg] "
            f"group={self.chat_name!r} "
            "links=type4+type5 "
            "merged_record=type19 "
            "miniprogram=unchanged "
            f"adjacent={self.adjacent_quote_max_age:.0f}s "
            f"max_chars={self.app_context_max_chars} "
            "record_media=text-only"
        )
        core.log(
            "[v3.22-imagegen] "
            f"group={self.chat_name!r} enabled=yes "
            "mode=text-to-image async=yes global_concurrency=1 "
            "backend=imagegen-gateway "
            "image_edit=single-reference-v3.23 proactive_trigger=no"
        )
        core.log(
            "[v3.23-image-edit] "
            f"group={self.chat_name!r} enabled=yes "
            "sources=adjacent+quoted-image+quoted-emoticon single_reference=yes "
            "explicit_mention_required=yes proactive_trigger=no "
            "ordinary_stickers=wake-no"
        )

        # v3.15 semantic follow-up lease: per-group worker + per sender wxid.
        # The lease has no fixed reply count. A successful bot reply refreshes
        # the three-minute window; non-addressed messages leave its anchor
        # unchanged so a later continuation can still be recognized.
        self.followup_ttl = max(
            1.0,
            float(
                os.environ.get(
                    "CW_V36_FOLLOWUP_TTL_SEC",
                    "180",
                )
            ),
        )
        self.followups = {}
        # v3.144: the semantic follow-up classifier must never block the
        # GroupWorker FIFO. One sender-scoped candidate per group is classified
        # on an independent daemon lane; another text from the same sender
        # invalidates the older result before it can produce a reply.
        self._followup_gate_queue = queue.Queue(maxsize=1)
        self._followup_gate_slot = threading.BoundedSemaphore(1)
        # v3.205: at most two voice transcriptions off-thread per group.
        self._v3205_voice_slots = threading.BoundedSemaphore(2)
        self._followup_gate_state_lock = threading.RLock()
        self._followup_gate_generation = {}
        # v3.96: remember the exact image local_id behind the most recent
        # Vision attempt for each sender. A short same-sender
        # correction such as "你看图了吗" can then reattach that image instead
        # of degrading to text-only context containing only "[图片]". The
        # anchor is established before provider work so a tiny-thumbnail
        # fail-closed notice can also be retried once the original arrives.
        self._v396_last_vision = {}
        # v3.35 bounded formal-reply dispatcher. Text remains one active call
        # per group to preserve the native short-session ordering; Vision
        # gets an independent lane so a slow image request cannot block a
        # different sender's text reply in the GroupWorker FIFO.
        self._reply_text_max_outstanding = max(
            1,
            int(
                os.environ.get(
                    "CW_V335_TEXT_MAX_OUTSTANDING_PER_GROUP",
                    "8",
                )
            ),
        )
        self._reply_vision_max_outstanding = max(
            1,
            int(
                os.environ.get(
                    "CW_V335_VISION_MAX_OUTSTANDING_PER_GROUP",
                    "4",
                )
            ),
        )
        self._reply_max_outstanding = (
            self._reply_text_max_outstanding
            + self._reply_vision_max_outstanding
        )
        self._reply_state_lock = threading.RLock()
        self._reply_generation = 0
        self._reply_sender_lock = threading.RLock()
        self._reply_sender_jobs = {}
        self._reply_blocked_senders = set()
        self._reply_deferred_events = {}
        # v3.29 hardening for the v3.25 async Reach lane. Keep a single
        # per-group dispatcher thread and a bounded number of in-flight jobs;
        # never create one daemon thread per request.
        self._reach_max_inflight = max(
            1,
            int(
                os.environ.get(
                    "CW_V325_REACH_MAX_INFLIGHT_PER_GROUP",
                    "2",
                )
            ),
        )
        self._reach_queue = queue.Queue(
            maxsize=self._reach_max_inflight,
        )
        self._reach_slots = threading.BoundedSemaphore(
            self._reach_max_inflight,
        )
        self._reach_state_lock = threading.RLock()
        self._reach_generation = 0

        core.log(
            "[v3.15-semantic-followup] "
            f"group={self.chat_name!r} "
            f"ttl={self.followup_ttl:.0f}s "
            "turn_limit=none semantic_gate=text-light+media-integrated"
        )
        core.log(
            "[v3.24-followup-gate] "
            f"group={self.chat_name!r} pure_text=yes "
            "route=llm-classifier async=yes session=no "
            "gateway_timeout=4s client_timeout=5s effort=low "
            f"context={core.FOLLOWUP_GATE_CONTEXT_MESSAGES}x"
            f"{core.FOLLOWUP_GATE_CONTEXT_CHARS} "
            "media_fallback=v3.15-integrated fail_closed=yes"
        )

        # v3.7 DB-native quote context. Only explicit @bot + refermsg uses it.
        self.quote_before = max(
            0,
            int(
                os.environ.get(
                    "CW_V37_QUOTE_BEFORE",
                    "5",
                )
            ),
        )
        self.quote_after = max(
            0,
            int(
                os.environ.get(
                    "CW_V37_QUOTE_AFTER",
                    "3",
                )
            ),
        )
        self.quote_max_chars = max(
            120,
            int(
                os.environ.get(
                    "CW_V37_QUOTE_MESSAGE_MAX_CHARS",
                    "500",
                )
            ),
        )

        core.log(
            "[v3.7-quote-context] "
            f"group={self.chat_name!r} "
            f"before={self.quote_before} "
            f"after={self.quote_after} "
            f"max_chars={self.quote_max_chars} "
            "history_images=text-only"
        )

        core.log(
            "[v3.8-quote-image] "
            f"group={self.chat_name!r} "
            "quoted_target=vision "
            "surrounding_history_images=text-only "
            "exact_quoted_media_over_recent=yes "
            "fallback=v3.7-text-only"
        )

        # db-native-v3.11.2-proactive-all-groups
        # Proactive live defaults to enabled for every GroupWorker, including
        # groups discovered later by hot reload. Keep both a global emergency
        # kill switch and a durable per-group switch.
        self._refresh_proactive_enabled()
        self.proactive_window = max(
            4,
            int(
                os.environ.get(
                    "CW_V39_PROACTIVE_WINDOW",
                    "8",
                )
            ),
        )
        self.proactive_activity_sec = max(
            30.0,
            float(
                os.environ.get(
                    "CW_V39_PROACTIVE_ACTIVITY_SEC",
                    "240",
                )
            ),
        )
        self.proactive_activity_min = max(
            2,
            int(
                os.environ.get(
                    "CW_V39_PROACTIVE_ACTIVITY_MIN",
                    "3",
                )
            ),
        )
        self.proactive_preferred_participants = max(
            1,
            int(
                os.environ.get(
                    "CW_V39_PROACTIVE_PREFERRED_PARTICIPANTS",
                    "2",
                )
            ),
        )
        default_proactive_cooldown = max(
            self.proactive_send_cooldown_default(),
            float(
                os.environ.get(
                    "CW_V39_PROACTIVE_SEND_COOLDOWN_SEC",
                    str(self.proactive_send_cooldown_default()),
                )
            ),
        )
        self.proactive_send_cooldown = max(
            0.0,
            float(
                self.group_cfg.get(
                    "proactive_send_cooldown_sec",
                    default_proactive_cooldown,
                )
            ),
        )
        self.proactive_any_bot_cooldown = max(
            0.0,
            float(
                self.group_cfg.get(
                    "proactive_any_bot_cooldown_sec",
                    default_proactive_cooldown,
                )
            ),
        )
        self.proactive_daily_cap = max(
            0,
            int(
                os.environ.get(
                    "CW_V392_PROACTIVE_DAILY_CAP",
                    "0",
                )
            ),
        )
        self.proactive_daily_cap_label = (
            str(self.proactive_daily_cap)
            if self.proactive_daily_cap > 0
            else "unlimited"
        )
        self.proactive_debounce = max(
            2.0,
            float(
                os.environ.get(
                    "CW_V39_PROACTIVE_DEBOUNCE_SEC",
                    "8",
                )
            ),
        )
        self.proactive_silent_rearm_messages = max(
            1,
            int(
                os.environ.get(
                    "CW_V39_PROACTIVE_SILENT_REARM_MESSAGES",
                    "6",
                )
            ),
        )
        self.proactive_silent_rearm_sec = max(
            15.0,
            float(
                os.environ.get(
                    "CW_V39_PROACTIVE_SILENT_REARM_SEC",
                    "90",
                )
            ),
        )

        self._v39_recent_humans = []
        # Visual messages are context for an already-eligible proactive
        # judgment, but never independently wake proactive speech. Store only
        # exact current-chat identities here and materialize lazily at fire.
        self._v39_recent_visuals = []
        self._v39_last_human_mono = None
        self._v39_burst_judged = False
        self._v39_humans_since_judgment = 0
        self._v39_last_judged_mono = 0.0
        self._v39_last_judgment_outcome = None
        self._v39_last_bot_sent_mono = 0.0
        self._v39_last_proactive_sent_mono = 0.0
        self._v39_shadow_timer = None
        self._v39_shadow_generation = 0
        self._v39_shadow_inflight = False
        self._v39_shadow_lock = threading.Lock()

        # v3.9.2 live send serialization + persistent per-group quota state.
        # Keep the old internal v39 names for patch compatibility only.
        self._v392_send_lock = threading.Lock()
        self._v392_state_db = str(
            os.environ.get(
                "CW_V392_STATE_DB",
                "/var/lib/chatwarden/bot.sqlite3",
            )
            or "/var/lib/chatwarden/bot.sqlite3"
        )
        self._v392_state_ready = True
        if self.proactive_shadow_enabled:
            try:
                self._v392_init_state_db()
            except Exception as exc:
                self._v392_state_ready = False
                self.proactive_shadow_enabled = False
                core.log(
                    "[v3.9.2-proactive-live] "
                    f"group={self.chat_name!r} "
                    "state_init_failed=yes "
                    f"error={type(exc).__name__}: {exc} "
                    "live_disabled=yes"
                )

        core.log(
            "[v3.9-proactive-live] "
            f"group={self.chat_name!r} "
            f"enabled={'yes' if self.proactive_shadow_enabled else 'no'} "
            f"window={self.proactive_window} "
            f"active={self.proactive_activity_min}/{self.proactive_activity_sec:.0f}s "
            f"preferred_participants={self.proactive_preferred_participants} "
            f"debounce={self.proactive_debounce:.0f}s "
            f"silent_rearm={self.proactive_silent_rearm_messages}msgs/"
            f"{self.proactive_silent_rearm_sec:.0f}s "
            f"proactive_cooldown={self.proactive_send_cooldown:.0f}s "
            f"any_bot_cooldown={self.proactive_any_bot_cooldown:.0f}s "
            "cooldown_source=split-per-group-policy "
            f"daily_cap={self.proactive_daily_cap_label} "
            "mode=live send=yes "
            "decision_lane=llm-proactive fallback=no "
            "session=no"
        )

        core.log(
            "[v3.9.1-proactive-style] "
            f"group={self.chat_name!r} "
            "banter=speak-allowed "
            "real-conflict=silent "
            "mode=live send=yes"
        )

        core.log(
            "[v3.9.2-proactive-live] "
            f"group={self.chat_name!r} "
            f"enabled={'yes' if self.proactive_shadow_enabled else 'no'} "
            f"proactive_cooldown={self.proactive_send_cooldown:.0f}s "
            f"any_bot_cooldown={self.proactive_any_bot_cooldown:.0f}s "
            f"daily_cap={self.proactive_daily_cap_label} "
            "quota=persistent-per-group "
            "followup_lease=not-opened"
        )

        core.log(
            "[v3.100-newcomer-welcome] "
            f"group={self.chat_name!r} "
            f"enabled={'yes' if self.welcome_enabled else 'no'} "
            "source=type10000 async=text-dispatch "
            "session=no fallback=fixed"
        )

        self._reply_dispatcher = ReplyDispatcher(
            name=(
                "wechat-v335-reply-"
                + self.chat_name
            ),
            max_outstanding=self._reply_max_outstanding,
            lane_limits={
                "text": 1,
                "vision": 2,
            },
            lane_max_outstanding={
                "text": self._reply_text_max_outstanding,
                "vision": self._reply_vision_max_outstanding,
            },
            logger=core.log,
        )
        # Owner background completion must reuse this exact GroupWorker's
        # guarded send lock and proactive cooldown accounting.
        from . import owner_jobs
        owner_jobs.register_group_worker(self)
        core.log(
            "[v3.35-reply-dispatch] "
            f"group={self.chat_name!r} enabled=yes "
            f"max_outstanding={self._reply_max_outstanding} "
            f"text_outstanding={self._reply_text_max_outstanding} "
            f"vision_outstanding={self._reply_vision_max_outstanding} "
            "text_concurrency=1 vision_concurrency=2 "
            "same_sender_serial=yes context_snapshot=yes late_send_guard=yes"
        )

        self._reach_thread = threading.Thread(
            target=self._reach_run,
            name=(
                "wechat-v325-reach-"
                + self.chat_name
            ),
            daemon=True,
        )
        self._reach_thread.start()
        core.log(
            "[v3.29-reach-hardening] "
            f"group={self.chat_name!r} "
            "dispatcher=single-thread "
            f"max_inflight={self._reach_max_inflight} "
            "late_send_guard=yes"
        )

        self._followup_gate_thread = threading.Thread(
            target=self._v3144_followup_gate_run,
            name=(
                "wechat-v3144-followup-gate-"
                + self.chat_name
            ),
            daemon=True,
        )
        self._followup_gate_thread.start()

        self.thread = threading.Thread(
            target=self._run,
            name=(
                "wechat-v3-"
                + self.chat_name
            ),
            daemon=True,
        )
        self.thread.start()

    def update(
        self,
        resolved=None,
        profiles=None,
    ):
        if resolved is not None:
            self.resolved = resolved
            self.group_cfg = resolved[
                "group_cfg"
            ]
            self._refresh_proactive_enabled()
            self._refresh_newcomer_welcome_enabled()
        if profiles is not None:
            self.profiles = profiles

    def put(self, event):
        self.q.put(event)

    def stop(self):
        reply_lock = getattr(
            self,
            "_reply_state_lock",
            None,
        )
        if reply_lock is not None:
            with reply_lock:
                self.stop_event.set()
                self._reply_generation += 1
        else:
            self.stop_event.set()

        reply_dispatcher = getattr(
            self,
            "_reply_dispatcher",
            None,
        )
        if reply_dispatcher is not None:
            reply_dispatcher.stop()

        gate_lock = getattr(self, "_followup_gate_state_lock", None)
        if gate_lock is not None:
            with gate_lock:
                self._followup_gate_generation.clear()
        gate_queue = getattr(self, "_followup_gate_queue", None)
        if gate_queue is not None:
            try:
                gate_queue.put_nowait(None)
            except queue.Full:
                pass

        with self._reach_state_lock:
            self.stop_event.set()
            self._reach_generation += 1

        # Drop queued-but-not-started Reach work immediately. An already
        # running provider call cannot be force-cancelled safely, but its
        # generation guard prevents any late send after this worker stops.
        while True:
            try:
                item = self._reach_queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                try:
                    self._reach_slots.release()
                except ValueError:
                    pass
            self._reach_queue.task_done()
        try:
            self._reach_queue.put_nowait(None)
        except queue.Full:
            pass
        self.q.put(None)

    def _legacy_sender(
        self,
        sender_wxid,
        display,
    ):
        if (
            sender_wxid
            and sender_wxid
            == self.owner_wxid
        ):
            return "me"

        if (
            not sender_wxid
            and display == os.environ.get("CW_OWNER_DISPLAY", "Owner")
        ):
            return "me"

        return (
            "other"
            if (
                sender_wxid
                or display
            )
            else "unknown"
        )

    _V315_NO_REPLY_MARKER = "[V3.15_FOLLOWUP_NOT_ADDRESSED]"

    def _expire(self):
        now = time.monotonic()
        self._followup_prune(now)
        self._v396_prune_last_vision(now)

        for sender, cached in list(
            self.recent_app.items()
        ):
            try:
                created = float(cached[1])
            except Exception:
                created = 0.0
            if (
                created > 0.0
                and now - created
                <= self.adjacent_quote_max_age
            ):
                continue
            self.recent_app.pop(
                sender,
                None,
            )

        for sender, ref in list(
            self.recent.items()
        ):
            if (
                now - ref.created
                <= self.image_max_age
            ):
                continue

            self.recent.pop(
                sender,
                None,
            )
            self._cleanup_image_ref(
                ref
            )

        for sender, pending in list(
            self.pending.items()
        ):
            if (
                now - pending.created
                <= self.pending_wait
            ):
                continue

            self.pending.pop(
                sender,
                None,
            )
            event = pending.event

            core.log(
                f"group={self.chat_name!r} "
                f"pending next-image trigger "
                f"expired local_id="
                f"{event['local_id']}; "
                "generation suppressed"
            )

            if event.get("_v3206_quoted_image"):
                core.log(
                    "[v3.206-pending-expired] "
                    f"group={self.chat_name!r} "
                    f"local_id={event.get('local_id')} "
                    "notice=suppressed reason=quoted-image"
                )
                continue

            self._send_fixed(
                "我还没收到你说的那张图。"
            )

    def _run_reply_job(
        self,
        work,
    ):
        generation = work.generation
        event = dict(work.event)
        image_path = work.image_path
        provider_prompt = work.provider_prompt
        sender = work.sender
        display = work.display
        legacy_sender = work.legacy_sender
        group_cfg = work.group_cfg
        profiles = work.profiles
        context_snapshot = work.context_snapshot
        queued_at = work.queued_at
        media_visual_source = getattr(
            work,
            "media_visual_source",
            "resolved" if image_path else "none",
        )
        media_route = str(
            getattr(work, "media_route", "host_fallback")
            or "host_fallback"
        )
        # Reach may run later on another lane; it must not reread future chat
        # rows or a subsequently changed personality when that happens.
        event['_research_context'] = context_snapshot
        event['_research_group_cfg'] = group_cfg
        event['_research_profiles'] = profiles

        if not self._reply_job_current(generation):
            core.log(
                "[v3.35-reply-dispatch] "
                f"group={self.chat_name!r} local_id={event.get('local_id')} "
                "dropped=yes reason=stale-before-provider"
            )
            return

        image_local_id = getattr(
            work,
            "image_local_id",
            None,
        )
        if image_path and image_local_id:
            self._v396_note_vision_source(
                event,
                image_local_id,
                reason="attempt",
            )

        if image_path:
            try:
                image_path = self._v396_upgrade_vision_path(
                    work,
                    image_path,
                )
            except ImageResourceIncompleteError as exc:
                core.log(
                    "[v3.96-vision-quality] "
                    f"group={self.chat_name!r} "
                    f"local_id={event.get('local_id')} "
                    "provider_suppressed=yes reason=tiny-thumbnail-original-pending "
                    f"error={type(exc).__name__}"
                )
                sent = self._reply_send_fixed_guarded(
                    generation,
                    event,
                    "这张图现在只拿到了低清缩略图，原图还没加载完整。"
                    "我先不瞎猜，等几秒再让我看一次。",
                )
                if sent:
                    self._followup_after_success(event)
                return
            except Exception as exc:
                core.log(
                    "[v3.96-vision-quality] "
                    f"group={self.chat_name!r} "
                    f"local_id={event.get('local_id')} "
                    "provider_suppressed=yes reason=quality-upgrade-error "
                    f"error={type(exc).__name__}: {str(exc)[:180]}"
                )
                sent = self._reply_send_fixed_guarded(
                    generation,
                    event,
                    "这张图的原图资源暂时没准备好，我现在不硬猜。"
                    "等几秒再让我看一次。",
                )
                if sent:
                    self._followup_after_success(event)
                return

        started = time.monotonic()
        db_age = (
            max(
                0.0,
                time.time() - event["timestamp"],
            )
            if event.get("timestamp")
            else 0.0
        )
        dispatch_wait = max(0.0, started - queued_at)

        core.log(
            f"group={self.chat_name!r} "
            f"trigger local_id={event['local_id']} "
            f"from={display!r} "
            f"db_age={db_age:.2f}s "
            f"image={'yes' if image_path else 'no'}; "
            "calling native backend=gateway "
            f"dispatch_wait={dispatch_wait:.2f}s"
        )

        try:
            provider_started = time.monotonic()
            owner_instruction = self._v343_current_instruction_text(event)
            owner_route, owner_context = False, []
            if not image_path and owner_instruction.strip():
                owner_route, owner_context = core.owner_task_route(
                    event, owner_instruction, self.chat_name,
                )
            if owner_route:
                # Admit the verified current-owner instruction durably, then
                # release the group reply lane. A separate Bot-owned monitor
                # provides progress/final replies through the normal UI lock.
                from . import owner_jobs
                try:
                    ack = owner_jobs.submit(
                        event, owner_instruction, self.chat_name,
                        private_chat=False,
                        context=owner_context,
                    )
                except core.llm_client.LLMExecutionError as exc:
                    core.log(
                        "[wechat-owner-async] submission_failed=%s" % exc.kind
                    )
                    ack = "管理员任务本次未成功提交，没有开始新的执行；请稍后重试。"
                if ack:
                    self._reply_send_fixed_guarded(generation, event, ack)
                return
            else:
                vision_ack = None
                if image_path:
                    vision_ack = slow_ack.SlowAck(
                        slow_ack.vision_delay(),
                        lambda: self._reply_send_fixed_guarded(
                            generation, event, slow_ack.vision_text(),
                        ),
                        log=core.log,
                        label=(
                            f"group={self.chat_name!r} "
                            f"local_id={event.get('local_id')} lane=vision"
                        ),
                    ).start()
                try:
                    reply = core.call_formal_reply(
                        group_cfg,
                        provider_prompt,
                        sender or legacy_sender,
                        profiles,
                        image_path=image_path,
                        raw_question=self._v380_provider_question(event),
                        context_snapshot=context_snapshot,
                        explicit=event.get('_v36_trigger_kind') != 'followup',
                        allow_imagegen_route=media_route == 'host_fallback',
                    )
                finally:
                    if vision_ack is not None:
                        vision_ack.finish()
            provider_seconds = time.monotonic() - provider_started
        except core.VisionGenerationError as exc:
            core.log(
                f"[v3.14-vision-retry] group={self.chat_name!r} "
                f"local_id={event.get('local_id')} "
                f"exhausted_notice=yes error={type(exc).__name__}"
            )
            try:
                self._reply_send_fixed_guarded(
                    generation,
                    event,
                    "这次识图链路失败了，你可以稍后再试一次。",
                )
            except Exception as send_exc:
                core.log(
                    f"[v3.14-vision-retry] group={self.chat_name!r} "
                    "exhausted_notice=failed "
                    f"error={type(send_exc).__name__}: {send_exc}"
                )
            return
        except Exception as exc:
            core.log(
                f"group={self.chat_name!r} "
                "reply worker error: "
                f"{type(exc).__name__}: {exc}"
            )
            return

        if not self._reply_job_current(generation):
            core.log(
                "[v3.35-reply-dispatch] "
                f"group={self.chat_name!r} local_id={event.get('local_id')} "
                "dropped=yes reason=stale-after-provider"
            )
            return

        if (
            event.get("_v36_trigger_kind") == "followup"
            and self._v315_is_no_reply(reply)
        ):
            core.log(
                "[v3.15-semantic-followup] "
                f"group={self.chat_name!r} "
                f"sender={sender!r} "
                f"local_id={event.get('local_id')} "
                "addressed_to_bot=no send=no lease_anchor=unchanged"
            )
            return

        if core.is_image_gen_marker(reply) and media_route == 'host_fallback':
            if core.image_gen_enabled():
                if self._v322_start_imagegen(
                    event,
                    reply_generation=generation,
                    routed_reply=reply,
                ):
                    self._v3163_media_route_shadow_observe(
                        event,
                        "imagegen",
                        visual_available=bool(image_path),
                        visual_source=media_visual_source,
                        explicit_imagegen_prefix=(
                            event.get("_v3153_explicit_imagegen_prompt") is not None
                        ),
                    )
                    return
                reply = (
                    "我识别到你想生图，但这条消息里没有拿到可用的图片描述。"
                    "请补充一下想生成什么。"
                )
            else:
                reply = "生图功能现在暂时关闭，其他功能正常。"
                core.log(
                    "[v3.22-imagegen] "
                    f"group={self.chat_name!r} local_id={event.get('local_id')} "
                    "marker_suppressed=yes reason=runtime-disabled"
                )

        if core.is_internal_image_gen_marker_like(reply):
            core.log(
                "[v3.74-imagegen-marker-guard] "
                f"group={self.chat_name!r} local_id={event.get('local_id')} "
                "invalid_marker_suppressed=yes route=imagegen-no"
            )
            self._reply_send_fixed_guarded(
                generation,
                event,
                "刚才生图路由没拿到有效描述。你再说一次想画什么，我继续。",
            )
            return

        self._v3163_media_route_shadow_observe(
            event,
            "vision" if image_path else "text",
            visual_available=bool(image_path),
            visual_source=media_visual_source,
            explicit_imagegen_prefix=(
                event.get("_v3153_explicit_imagegen_prompt") is not None
            ),
        )

        vision_reach_context = core.vision_agent_reach_context(reply)
        if vision_reach_context is not None:
            ack_final = core.bot_prefix() + core.agent_reach_ack_text()
            ack_send_seconds = self._reply_send_guarded(
                generation,
                ack_final,
                local_id=event.get("local_id"),
            )
            if ack_send_seconds is None:
                return

            reach_prompt = core.build_vision_agent_reach_prompt(
                provider_prompt,
                vision_reach_context,
            )
            core.log(
                f"[v3.37-vision-reach] group={self.chat_name!r} "
                f"local_id={event['local_id']} "
                f"gate=yes visual_context_chars={len(vision_reach_context)} "
                f"ack_sent=yes send={ack_send_seconds:.2f}s"
            )
            queued = self._start_agent_reach_async(
                event,
                reach_prompt,
                sender or legacy_sender,
            )
            if not queued:
                self._reply_send_fixed_guarded(
                    generation,
                    event,
                    "当前调查任务排队已满，稍后再试一下。",
                )
            return

        if core.is_vision_agent_reach_marker(reply):
            core.log(
                f"[v3.37-vision-reach] group={self.chat_name!r} "
                f"local_id={event.get('local_id')} "
                "gate=invalid handoff_suppressed=yes"
            )
            self._reply_send_fixed_guarded(
                generation,
                event,
                "我看出来这张图需要先识别再查，但这次没拿到足够可检索的线索。"
                "可以补充店名、地点，或者换张更清楚的图再试。",
            )
            return

        if core.is_agent_reach_marker(reply):
            ack_final = core.bot_prefix() + core.agent_reach_ack_text()
            ack_send_seconds = self._reply_send_guarded(
                generation,
                ack_final,
                local_id=event.get("local_id"),
            )
            if ack_send_seconds is None:
                return

            core.log(
                f"[v3.11-reach] group={self.chat_name!r} "
                f"local_id={event['local_id']} "
                f"gate=yes ack_sent=yes send={ack_send_seconds:.2f}s"
            )
            queued = self._start_agent_reach_async(
                event,
                provider_prompt,
                sender or legacy_sender,
            )
            if not queued:
                self._reply_send_fixed_guarded(
                    generation,
                    event,
                    "当前调查任务排队已满，稍后再试一下。",
                )
            return

        reply_chunks = core.split_formal_reply(reply)
        if not reply_chunks:
            core.log(
                "[v3.46-formal-reply] "
                f"group={self.chat_name!r} local_id={event.get('local_id')} "
                "send_suppressed=yes reason=empty-after-split"
            )
            return
        final_chunks = [
            core.bot_prefix() + chunk
            for chunk in reply_chunks
        ]
        send_seconds = self._reply_send_many_guarded(
            generation,
            final_chunks,
            local_id=event.get("local_id"),
        )
        if send_seconds is None:
            return

        final = core.bot_prefix() + reply
        self._followup_after_success(event)
        if image_path and image_local_id:
            self._v396_note_vision_source(
                event,
                image_local_id,
                reason="success",
                reply_text=final,
            )
        core.save_message(
            self.chat_name,
            "bot",
            final,
        )

        core.log(
            f"group={self.chat_name!r} "
            "AI reply sent; "
            f"chunks={len(reply_chunks)} chars={len(reply)} "
            f"provider={provider_seconds:.2f}s "
            f"send={send_seconds:.2f}s "
            f"total={time.monotonic() - started:.2f}s "
            f"dispatch_total={time.monotonic() - queued_at:.2f}s"
        )

    def _answer(
        self,
        event,
        image_ref=None,
    ):
        sender = event[
            "sender_username"
        ]
        display = (
            event["sender_display"]
            or sender
            or "未识别群成员"
        )
        legacy_sender = (
            self._legacy_sender(
                sender,
                display,
            )
        )

        image_path = (
            image_ref.path
            if image_ref
            else None
        )
        image_error = (
            image_ref.error
            if image_ref
            else None
        )
        image_edit_reference = bool(
            image_ref and image_ref.path
        )
        image_reference_source = (
            "adjacent"
            if image_edit_reference
            else None
        )

        if (
            image_error
            and not image_path
        ):
            if image_ref and image_ref.local_id:
                self._v396_note_vision_source(
                    event,
                    image_ref.local_id,
                    reason="decode-failed",
                )
            core.log(
                f"group={self.chat_name!r} "
                f"local_id="
                f"{event['local_id']} "
                "paired image unavailable; "
                "generation suppressed"
            )
            self._send_fixed(
                "这张图的本地原图暂时"
                "解析失败，我现在看不到"
                "图片内容。"
            )
            return

        # Build v3.7 text context first. On an explicit quoted-image
        # request this also records the exactly resolved historical target.
        provider_prompt = (
            self._v37_prompt_for_event(
                event
            )
        )
        provider_prompt = self._v315_followup_prompt(
            event,
            provider_prompt,
        )

        if image_path:
            quoted_target = event.pop(
                "_v38_quote_target_event",
                None,
            )
            if quoted_target is not None:
                core.log(
                    "[v3.8-quote-image] "
                    f"group={self.chat_name!r} "
                    f"local_id={event.get('local_id')} "
                    "quoted_target_vision=skipped "
                    "reason=current-image-precedence"
                )

            quoted_emoticon_target = event.pop(
                "_v312_quote_emoticon_target_event",
                None,
            )
            if quoted_emoticon_target is not None:
                core.log(
                    "[v3.12-emoticon-group] "
                    f"group={self.chat_name!r} "
                    f"local_id={event.get('local_id')} "
                    "quoted_emoticon_vision=skipped "
                    "reason=current-image-precedence"
                )
        else:
            pending_edit_event = event.pop(
                "_v323_pending_edit_event",
                None,
            )
            if isinstance(pending_edit_event, dict):
                pending_ref = self._v38_quote_image_ref(
                    pending_edit_event
                )
                if pending_ref is not None:
                    image_ref = pending_ref
                    image_path = pending_ref.path
                    image_edit_reference = bool(image_path)
                    image_reference_source = "pending-quoted"

            quoted_image_ref = (
                self._v38_quote_image_ref(
                    event
                )
            )
            if quoted_image_ref is not None:
                image_ref = quoted_image_ref
                image_path = (
                    quoted_image_ref.path
                )
                image_edit_reference = bool(image_path)
                image_reference_source = "quoted"

            if not image_path:
                quoted_emoticon_ref = (
                    self._v312_quote_emoticon_ref(
                        event
                    )
                )
                if quoted_emoticon_ref is not None:
                    image_ref = quoted_emoticon_ref
                    image_path = (
                        quoted_emoticon_ref.path
                    )
                    image_edit_reference = bool(image_path)
                    image_reference_source = "quoted-emoticon"

        current_instruction = self._v343_current_instruction_text(event)
        provider_question = self._v380_provider_question(event)
        media_visual_source = "none"
        if image_path:
            if int(event.get("local_type") or 0) == 3 or str(event.get("msg_type") or "") == "图片":
                media_visual_source = "current"
            elif image_reference_source in {
                "quoted", "quoted-emoticon", "adjacent", "pending-quoted"
            }:
                media_visual_source = image_reference_source
            else:
                media_visual_source = "resolved"
        if (
            image_path
            and image_reference_source in {"quoted", "quoted-emoticon"}
            and event.get("_v36_trigger_kind") == "explicit"
        ):
            residue = str(current_instruction or "")
            for token in (
                str(event.get("bot_display_name") or "").strip(),
                str(getattr(self, "bot_wxid", "") or "").strip(),
            ):
                if token:
                    residue = residue.replace(token, " ")
            residue = re.sub(
                r"[@＠\s\u2005\u2006\u2009\u202f\u2060]+",
                "",
                residue,
            )
            if not residue:
                provider_question = (
                    "这条消息只有明确@你并引用了一个视觉媒体，没有附加文字。"
                    "被引用的图片/表情本身就是当前用户要你看的对象：必须实际查看它，"
                    "再自然地描述、评价、接梗或回应这个视觉内容。不要把引用位置附近"
                    "的其他群聊话题当成当前被问对象。"
                )
                provider_prompt = provider_question
                core.log(
                    "[v3.142-quoted-media-bare-mention] "
                    f"group={self.chat_name!r} "
                    f"local_id={event.get('local_id')} "
                    f"source={image_reference_source} "
                    "quoted_media_is_current_object=yes"
                )
            else:
                provider_question = self._v313_contextualize_bare_mention(
                    event,
                    provider_question,
                )
        else:
            provider_question = self._v313_contextualize_bare_mention(
                event,
                provider_question,
            )
        if provider_question != str(event.get("text") or ""):
            quote_meta = self._v37_parse_quote_payload(
                event.get("raw_content")
            ) or {}
            core.log(
                "[v3.80-quoted-media-prompt] "
                f"group={self.chat_name!r} "
                f"local_id={event.get('local_id')} "
                f"refer_type={int(quote_meta.get('refer_type') or 0)} "
                "quoted_preview_removed=yes"
            )
        image_edit_trigger_allowed = (
            event.get("_v36_trigger_kind") == "explicit"
            or (
                event.get("_v36_trigger_kind") == "quote"
                and event.get("_v350_quote_gate_passed") is True
            )
        )
        if (
            image_edit_reference
            and image_path
            and image_edit_trigger_allowed
            and (
                event.get("_v3153_explicit_imagegen_prompt") is not None
                or core.image_edit_followup_intent(current_instruction)
            )
        ):
            if not core.image_gen_enabled():
                self._cleanup_image_ref(image_ref)
                self._send_fixed("生图功能现在暂时关闭，其他功能正常。")
                return
            self._v3163_media_route_shadow_observe(
                event,
                "image_edit",
                visual_available=True,
                visual_source=media_visual_source,
                explicit_imagegen_prefix=(
                    event.get("_v3153_explicit_imagegen_prompt") is not None
                ),
            )
            owned_ref = image_ref
            image_ref = None
            explicit_edit_prompt = event.get("_v3153_explicit_imagegen_prompt")
            edit_instruction = (
                explicit_edit_prompt
                if explicit_edit_prompt is not None
                else current_instruction
            )
            if (
                explicit_edit_prompt is None
                and core.image_edit_needs_recent_context(current_instruction)
            ):
                edit_instruction = core.image_edit_contextual_prompt(
                    current_instruction,
                    provider_prompt,
                )
            try:
                self._v323_start_image_edit(
                    event,
                    owned_ref,
                    reference_source=(
                        image_reference_source
                        or "adjacent"
                    ),
                    instruction_override=edit_instruction,
                )
            except Exception:
                self._cleanup_image_ref(owned_ref)
                raise
            return

        if (
            event.get("_v3153_explicit_imagegen_reference_expected") is True
            and not image_path
        ):
            self._cleanup_image_ref(image_ref)
            self._send_fixed(
                "这张引用图暂时解析失败，没法按“生成图片：”直接改图。"
                "你可以重新引用图片再试一次。"
            )
            return

        media_route = self._v3167_media_route_active_decide(
            event,
            visual_available=bool(image_path),
            visual_source=media_visual_source,
            explicit_imagegen_prefix=(
                event.get("_v3153_explicit_imagegen_prompt") is not None
            ),
        )
        if media_route == "imagegen":
            if not core.image_gen_enabled():
                self._cleanup_image_ref(image_ref)
                self._send_fixed("生图功能现在暂时关闭，其他功能正常。")
                return
            if image_ref is not None:
                self._cleanup_image_ref(image_ref)
                image_ref = None
                image_path = None
            prompt_override = str(current_instruction or provider_question or "").strip()
            prompt_override = self._v3192_imagegen_prompt_with_quoted_text(
                event,
                prompt_override,
            )
            if self._v322_start_imagegen(
                event,
                prompt_override=prompt_override,
            ):
                core.log(
                    "[v3.167-decision-media-active] "
                    f"group={self.chat_name!r} local_id={event.get('local_id')} "
                    "route=imagegen fallback=no"
                )
                return
            media_route = "host_fallback"
        elif media_route == "image_edit":
            if image_ref is not None and image_path:
                if not core.image_gen_enabled():
                    self._cleanup_image_ref(image_ref)
                    self._send_fixed("生图功能现在暂时关闭，其他功能正常。")
                    return
                owned_ref = image_ref
                image_ref = None
                edit_instruction = str(current_instruction or provider_question or "").strip()
                if core.image_edit_needs_recent_context(edit_instruction):
                    edit_instruction = core.image_edit_contextual_prompt(
                        edit_instruction,
                        provider_prompt,
                    )
                try:
                    self._v323_start_image_edit(
                        event,
                        owned_ref,
                        reference_source=(image_reference_source or media_visual_source or "resolved"),
                        instruction_override=edit_instruction,
                    )
                except Exception:
                    self._cleanup_image_ref(owned_ref)
                    raise
                core.log(
                    "[v3.167-decision-media-active] "
                    f"group={self.chat_name!r} local_id={event.get('local_id')} "
                    "route=image_edit fallback=no"
                )
                return
            media_route = "host_fallback"
        elif media_route == "vision" and not image_path:
            media_route = "host_fallback"
        elif media_route == "text" and image_path:
            media_route = "host_fallback"
        elif media_route not in {"vision", "text", "host_fallback", None}:
            media_route = "host_fallback"

        if media_route in {"vision", "text"}:
            core.log(
                "[v3.167-decision-media-active] "
                f"group={self.chat_name!r} local_id={event.get('local_id')} "
                f"route={media_route} fallback=no"
            )
        else:
            media_route = "host_fallback"

        # Synthetic smoke fixtures historically construct GroupWorker via
        # __new__ and call _answer() directly. Keep that narrow seam working
        # synchronously; every real GroupWorker created by __init__ owns the
        # v3.35 dispatcher and always takes the bounded async path below.
        if not hasattr(self, "_reply_dispatcher"):
            if not hasattr(self, "stop_event"):
                self.stop_event = threading.Event()
            if not hasattr(self, "_reply_state_lock"):
                self._reply_state_lock = threading.RLock()
            if not hasattr(self, "_reply_generation"):
                self._reply_generation = 0
            queued_at = time.monotonic()
            try:
                self._run_reply_job(
                    FormalReplyWork(
                        generation=self._reply_generation,
                        event=dict(event),
                        image_path=image_path,
                        provider_prompt=provider_prompt,
                        sender=sender,
                        display=display,
                        legacy_sender=legacy_sender,
                        group_cfg=dict(self.group_cfg),
                        profiles=self.profiles,
                        context_snapshot=None,
                        queued_at=queued_at,
                        image_local_id=(
                            image_ref.local_id
                            if image_ref
                            else None
                        ),
                        media_visual_source=media_visual_source,
                        media_route=media_route,
                    )
                )
            finally:
                if image_ref:
                    self._cleanup_image_ref(image_ref)
            return

        group_cfg_snapshot = dict(self.group_cfg)
        profiles_snapshot = (
            dict(self.profiles)
            if isinstance(self.profiles, dict)
            else self.profiles
        )
        context_snapshot = event.get(
            "_v335_context_snapshot"
        )
        if not isinstance(context_snapshot, dict):
            try:
                context_snapshot = core.freeze_llm_context_bounded(
                    group_cfg_snapshot,
                    sender or legacy_sender,
                    profiles_snapshot,
                    current_text=provider_question,
                    history_request_text=self._v380_provider_question(event),
                )
            except Exception:
                if image_ref:
                    self._cleanup_image_ref(image_ref)
                    image_ref = None
                raise
        event_snapshot = dict(event)
        event_snapshot.pop(
            "_v335_context_snapshot",
            None,
        )
        queued_at = time.monotonic()
        lane = "vision" if image_path else "text"
        sender_key = self._v335_reply_sender_key(event)
        owned_ref = image_ref

        with self._reply_state_lock:
            if self.stop_event.is_set():
                if owned_ref:
                    self._cleanup_image_ref(owned_ref)
                return
            generation = self._reply_generation

        self._v335_reserve_sender(sender_key)
        reply_work = FormalReplyWork(
            generation=generation,
            event=event_snapshot,
            image_path=image_path,
            provider_prompt=provider_prompt,
            sender=sender,
            display=display,
            legacy_sender=legacy_sender,
            group_cfg=group_cfg_snapshot,
            profiles=profiles_snapshot,
            context_snapshot=context_snapshot,
            queued_at=queued_at,
            image_local_id=(
                image_ref.local_id
                if image_ref
                else None
            ),
            media_visual_source=media_visual_source,
            media_route=media_route,
        )

        def run_reply_job(_dispatch_current):
            try:
                self._run_reply_job(reply_work)
            finally:
                self._v335_reply_job_finished(sender_key)

        def cleanup_reply_job():
            if owned_ref:
                self._cleanup_image_ref(owned_ref)

        submitted = self._reply_dispatcher.submit(
            ReplyJob(
                lane=lane,
                sender_key=sender_key,
                local_id=int(event.get("local_id") or 0),
                run=run_reply_job,
                cleanup=cleanup_reply_job,
                queued_at=queued_at,
            )
        )
        if not submitted:
            self._v335_cancel_sender_reservation(sender_key)
            cleanup_reply_job()
            core.log(
                "[v3.35-reply-dispatch] "
                f"group={self.chat_name!r} local_id={event.get('local_id')} "
                f"lane={lane} queued=no reason=max-outstanding-or-stopped"
            )
            if not self.stop_event.is_set():
                self._send_fixed(
                    "当前回复任务排队有点多，这条没有继续挤队列。你稍后再发一次。"
                )
            return

        route_age = (
            max(0.0, time.time() - event["timestamp"])
            if event.get("timestamp")
            else 0.0
        )
        core.log(
            "[v3.35-reply-dispatch] "
            f"group={self.chat_name!r} local_id={event.get('local_id')} "
            f"lane={lane} sender_scoped=yes queued=yes "
            f"route_age={route_age:.2f}s"
        )

    def _call_auxiliary_generation(self, task_kind, prompt, image_path=None):
        """Keep existing business parsers/senders, replace only generation."""
        protocols = {
            'welcome': '把最终欢迎语放在官方结构化结果 text 字段，不输出其它字段。',
            'personality': '把最终人格提示词放在官方结构化结果 personality 字段，不输出其它字段。',
            'personality_random': '官方结构化结果只能包含 personality 和 summary。',
            'proactive': '官方结构化结果只能包含 decision、draft、reason；SILENT 的 draft 必须为空。',
        }
        if task_kind not in protocols:
            raise ValueError('unsupported auxiliary generation task')
        started = time.monotonic()
        sem = core._PROACTIVE_SEM if task_kind == 'proactive' else None
        acquired = sem is None or sem.acquire(blocking=False)
        if not acquired:
            raise core.llm_client.LLMExecutionError('proactive_busy')
        try:
            try:
                call_kwargs = {
                    'explicit': task_kind in {'personality', 'personality_random'},
                }
                if image_path is not None:
                    call_kwargs['image_path'] = image_path
                result = core.call_llm_auxiliary(
                    task_kind,
                    str(prompt) + '\n\n输出协议以官方 schema 为准：' + protocols[task_kind],
                    **call_kwargs,
                )
            except core.llm_client.LLMExecutionError as exc:
                # Keep exactly one format regeneration, never retry auth,
                # transport, timeout or quota and never select another backend.
                if task_kind == 'proactive' and exc.kind == 'invalid_structured_output':
                    return '', {}, time.monotonic() - started, 0.0, 'gateway:proactive'
                raise
            data = result['structured']
            if task_kind == 'welcome':
                content = data['text']
            elif task_kind == 'personality':
                content = data['personality']
            else:
                content = json.dumps(data, ensure_ascii=False)
            usage = result.get('usage') or {}
            tool_count = result.get('tool_count', 0)
            if type(tool_count) is not int or tool_count < 0:
                tool_count = 0
            return (content, {'prompt_tokens': usage.get('input_tokens', 0),
                              'completion_tokens': usage.get('output_tokens', 0),
                              'tool_count': tool_count,
                              'web_used': result.get('web_used') is True,
                              'image_viewed': result.get('image_viewed') is True},
                    time.monotonic() - started, 0.0, 'gateway:' + task_kind)
        finally:
            if sem is not None:
                sem.release()

    # db-native-v3.13-true-mention-trigger
    def _v313_true_mention(
        self,
        event,
    ):
        bot_wxid = str(
            self.bot_wxid
            or ""
        ).strip()
        if not bot_wxid:
            return False

        mentions = event.get(
            "mentioned_wxids"
        )
        if not isinstance(
            mentions,
            (list, tuple, set),
        ):
            return False

        matched = any(
            str(item or "").strip()
            == bot_wxid
            for item in mentions
        )

        if matched:
            core.log(
                "[v3.13-true-mention] "
                f"group={self.chat_name!r} "
                f"local_id={event.get('local_id')} "
                "explicit=yes "
                "source=msgsource-atuserlist "
                "legacy_name_trigger=no"
            )
            return True

        # v3.84: WeChat can occasionally persist a visually addressed message
        # without <atuserlist> even though the current body contains the bot's
        # displayed group nickname.  Recover only from the bot display name
        # resolved for this exact chat/event snapshot; never hard-code one
        # global nickname.  Use only the current user's instruction so a bot
        # name that appears solely inside a quoted historical preview cannot
        # wake the bot.
        bot_display_name = str(
            event.get("bot_display_name")
            or ""
        ).strip()
        current_body = str(
            self._v343_current_instruction_text(
                event
            )
            or ""
        )
        display_matched = bool(
            bot_display_name
            and bot_display_name != bot_wxid
            and bot_display_name in current_body
        )
        if display_matched:
            core.log(
                "[v3.84-bot-nickname-trigger] "
                f"group={self.chat_name!r} "
                f"local_id={event.get('local_id')} "
                "explicit=yes source=current-body-group-nickname "
                "structured_mention=no"
            )
            return True

        return False

    def _handle_text(
        self,
        event,
    ):
        sender = event[
            "sender_username"
        ]
        text = event["text"]

        # Any newer same-sender text invalidates an older async semantic-gate
        # result. This lets an explicit @ overtake a slow unmentioned follow-up
        # without an older classifier result replying afterward.
        self._v3144_note_sender_activity(sender)

        explicit = self._v313_true_mention(event)
        if explicit:
            # v3.206: an explicit @ from the same sender supersedes an older
            # NEXT-image wait. Without this, a request re-sent with @ is
            # answered, and the stale wait expires seconds later with
            # "我还没收到你说的那张图".
            pending_waits = getattr(self, "pending", None)
            superseded = (
                pending_waits.pop(sender, None)
                if isinstance(pending_waits, dict)
                else None
            )
            if superseded is not None:
                core.log(
                    "[v3.206-pending-superseded] "
                    f"group={self.chat_name!r} "
                    f"old_local_id={superseded.event.get('local_id')} "
                    f"local_id={event.get('local_id')}"
                )
        # Media/edit routing must always use only the current instruction.
        # Type57 quoted previews are historical context and may contain words
        # such as "图片" / "表情" / "P图" that must never open a media wait
        # or image-edit route for an otherwise ordinary quoted-bot follow-up.
        intent_text = (
            self._v343_current_instruction_text(
                event
            )
        )
        media_intent_text = _v3161_structured_own_mention_body(
            event,
            getattr(self, "bot_wxid", ""),
            intent_text,
        )

        explicit_imagegen_prompt = core.image_gen_explicit_prompt(
            intent_text,
            event.get("bot_display_name"),
            getattr(self, "bot_wxid", ""),
        )
        explicit_imagegen = explicit_imagegen_prompt is not None
        if explicit_imagegen:
            event["_v3153_explicit_imagegen_prompt"] = explicit_imagegen_prompt
            if not explicit_imagegen_prompt:
                self._send_fixed("请在“生成图片：”后面写上你想生成或修改的内容。")
                return

            # ``生成图片：`` is itself a deterministic explicit trigger.  The
            # exact quote resolver intentionally refuses non-explicit events,
            # so establish that trigger state before asking it whether this
            # command quotes an existing image.  Previously this assignment
            # happened later in the ordinary explicit-message path, causing
            # ``@bot 生成图片：...`` + exact quoted image to be misclassified
            # as text-to-image and sent to 8652 without the reference.
            event["_v36_trigger_kind"] = "explicit"

            quote_meta, _ = self._v37_quote_metadata(event)
            refer_type = int((quote_meta or {}).get("refer_type") or 0)
            quoted_visual = refer_type in (3, 47)
            if refer_type == 49 and quote_meta:
                quoted_visual = EmoticonPipeline.is_emoticon({
                    "local_type": 49,
                    "raw_content": quote_meta.get("content") or "",
                })

            if quoted_visual:
                # The explicit prefix itself is a wake-up command. Reuse the
                # exact quote resolver below and never bind unrelated adjacent
                # media merely because it happens to be recent.
                if not explicit:
                    core.log(
                        "[v3.153-explicit-imagegen] "
                        f"group={self.chat_name!r} local_id={event.get('local_id')} "
                        "wake=yes quoted_reference=yes mention_required=no"
                    )
                explicit = True
                event["_v3153_explicit_imagegen_reference_expected"] = True
            else:
                if not core.image_gen_enabled():
                    self._send_fixed("生图功能现在暂时关闭，其他功能正常。")
                    return
                core.log(
                    "[v3.153-explicit-imagegen] "
                    f"group={self.chat_name!r} local_id={event.get('local_id')} "
                    "route=text-to-image mention_required=no llm_gate=no"
                )
                self._v3163_media_route_shadow_observe(
                    event,
                    "imagegen",
                    visual_available=False,
                    visual_source="none",
                    explicit_imagegen_prefix=True,
                )
                self._v322_start_imagegen(
                    event,
                    prompt_override=explicit_imagegen_prompt,
                )
                return

        if self._v3109_handle_personality_command(
            event,
            explicit=explicit,
            intent_text=intent_text,
        ):
            return

        if self._v3172_handle_profile_request(
            event,
            explicit=explicit,
            text=media_intent_text,
        ):
            return

        if not hasattr(self, "pending_image_edit"):
            self.pending_image_edit = {}

        # v3.23.1: support "quote image + edit instruction" followed by a
        # separate @bot. The first message is only a candidate; it never runs
        # image edit without the explicit mention confirmation.
        if not explicit:
            quote_meta, _ = self._v37_quote_metadata(event)
            if (
                quote_meta is not None
                and core.image_edit_intent(intent_text)
            ):
                self.pending_image_edit[sender] = {
                    "event": dict(event),
                    "created": time.monotonic(),
                }
                core.log(
                    "[v3.23.1-image-edit] "
                    f"group={self.chat_name!r} "
                    f"local_id={event.get('local_id')} "
                    "candidate=yes waiting_for_explicit_mention=yes ttl=15s"
                )

        if explicit:
            candidate = self.pending_image_edit.pop(sender, None)
            if isinstance(candidate, dict):
                age = time.monotonic() - float(candidate.get("created") or 0)
                if age <= 15.0 and isinstance(candidate.get("event"), dict):
                    event["_v323_pending_edit_event"] = candidate["event"]
                    core.log(
                        "[v3.23.1-image-edit] "
                        f"group={self.chat_name!r} "
                        f"local_id={event.get('local_id')} "
                        "candidate_consumed=yes"
                    )

        # Preserve the existing NEXT-image contract. While a next-image
        # trigger is pending, a non-explicit same-sender text remains an
        # adjacency boundary rather than becoming a follow-up trigger.
        if (
            not explicit
            and sender in self.pending
        ):
            old = self.recent.pop(
                sender,
                None,
            )
            self._cleanup_image_ref(
                old
            )
            self.pending.pop(
                sender,
                None,
            )
            self.recent_text.pop(sender, None)
            return

        if not explicit:
            self._remember_adjacent_text(event)

        if explicit:
            event[
                "_v36_trigger_kind"
            ] = "explicit"
            current_quote_meta, _ = (
                self._v37_quote_metadata(event)
            )
            if current_quote_meta is None:
                if self._v3118_match_adjacent_history_request(event):
                    self._answer(event)
                    return

                adjacent_app_context = (
                    self._match_adjacent_app_context(
                        event
                    )
                )
                if adjacent_app_context:
                    event[
                        "_v321_adjacent_app_context"
                    ] = adjacent_app_context

                adjacent_image = (
                    self._match_adjacent_quoted_image(
                        event
                    )
                )
                if adjacent_image is not None:
                    self._answer(
                        event,
                        image_ref=adjacent_image,
                    )
                    return

                # If the runtime already queued an immediately following
                # same-sender image in the same poll, consume it before this
                # explicit text trigger enters the LLM. This fixes the common
                # WeChat pattern "@bot question" -> image without delaying
                # ordinary mentions that have no queued image.
                forward_image = (
                    self._consume_queued_forward_image(
                        event
                    )
                )
                if forward_image is not None:
                    self._answer(
                        event,
                        image_ref=forward_image,
                    )
                    return
            else:
                self.recent_text.pop(sender, None)
                self.recent_app.pop(sender, None)
        elif self._event_quotes_bot(
            event
        ):
            event[
                "_v36_trigger_kind"
            ] = "quote"
            # This event is already a self-contained exact bot quote. Do not
            # leave it in the split-message adjacency cache where a later @
            # could accidentally treat it as an unrelated precursor.
            self.recent_text.pop(sender, None)
            core.log(
                "[v3.50-quoted-bot-gate] "
                f"group={self.chat_name!r} "
                f"sender={sender!r} "
                f"local_id={event.get('local_id')} "
                "quote/reply candidate=yes reply_policy=quoted-bot-default"
            )
            if not self._v350_gate_quoted_bot_reply(event):
                return
        else:
            state = self._followup_state(
                sender
            )
            if state is None:
                # Existing behavior for an ordinary same-sender text:
                # it terminates image adjacency and is otherwise ignored by
                # the normal reply path. v3.9 may observe it in Shadow only.
                old = self.recent.pop(
                    sender,
                    None,
                )
                self._cleanup_image_ref(
                    old
                )
                self.pending.pop(
                    sender,
                    None,
                )
                # Keep only a freshly received supported Type49 share card
                # adjacent for a following explicit @. Any other ordinary
                # text from the same sender breaks the card adjacency.
                _, app_detail = self._v321_app_detail(
                    event
                )
                if not app_detail:
                    self.recent_app.pop(
                        sender,
                        None,
                    )
                self._v39_note_ordinary_human(
                    event
                )
                return

            event[
                "_v36_trigger_kind"
            ] = "followup"

            core.log(
                "[v3.15-semantic-followup] "
                f"group={self.chat_name!r} "
                f"sender={sender!r} "
                "follow-up trigger "
                "semantic_gate=light-pending"
            )

        recent = self.recent.pop(
            sender,
            None,
        )

        quote_payload = self._v37_parse_quote_payload(
            event.get("raw_content")
        )
        exact_quoted_media = False
        if isinstance(quote_payload, dict):
            refer_type = int(quote_payload.get("refer_type") or 0)
            if refer_type in (3, 47):
                exact_quoted_media = True
            elif refer_type == 49:
                exact_quoted_media = EmoticonPipeline.is_emoticon({
                    "local_type": 49,
                    "raw_content": quote_payload.get("content") or "",
                })

        # v3.90: an exact quoted image/emoticon is a stronger media reference
        # than stale same-sender adjacency. A real incident quoted local_id
        # 19104 while the requester had just sent sticker 19105; the old order
        # fed 19105 to Vision and suppressed exact quote resolution. Discard
        # recent media here so _answer() can resolve the exact quoted target.
        if recent and exact_quoted_media:
            core.log(
                "[v3.90-quoted-media-precedence] "
                f"group={self.chat_name!r} "
                f"local_id={event.get('local_id')} "
                f"recent_local_id={recent.local_id} "
                "recent_suppressed=yes reason=exact-quoted-media"
            )
            self._cleanup_image_ref(
                recent
            )
            recent = None

        if recent:
            age = (
                time.monotonic()
                - recent.created
            )
            if (
                age
                <= self.image_max_age
            ):
                if _adjacent_media_reference_intent(media_intent_text):
                    core.log(
                        f"group="
                        f"{self.chat_name!r} "
                        "matched PREVIOUS "
                        "adjacent image "
                        f"local_id="
                        f"{recent.local_id}"
                    )
                    self._answer(
                        event,
                        image_ref=recent,
                    )
                    return
                core.log(
                    "[v3.123-adjacent-vision-gate] "
                    f"group={self.chat_name!r} "
                    f"local_id={event.get('local_id')} "
                    f"recent_local_id={recent.local_id} "
                    "recent_suppressed=yes reason=current-text-not-visual"
                )
                self._cleanup_image_ref(recent)
                recent = None

            if recent is not None:
                self._cleanup_image_ref(
                    recent
                )

        # v3.158: a proven explicit/quoted continuation of the sender's last
        # successful Vision turn must be resolved before the legacy NEXT-image
        # wait.  Otherwise phrases such as "这图里的角色是谁" match the broad
        # direct-image wording and incorrectly wait for a brand-new image even
        # though the exact previous Vision image is already anchored.
        if event.get("_v36_trigger_kind") in {"explicit", "quote"}:
            previous_vision_ref = self._v396_previous_vision_image_ref(
                event
            )
            if previous_vision_ref is not None:
                core.log(
                    "[v3.158-vision-continuation-priority] "
                    f"group={self.chat_name!r} "
                    f"local_id={event.get('local_id')} "
                    f"vision_local_id={previous_vision_ref.local_id} "
                    "bypass_next_image_wait=yes"
                )
                self._answer(
                    event,
                    image_ref=previous_vision_ref,
                )
                return

        sticker_make_target = _v3192_sticker_make_target(media_intent_text)
        if sticker_make_target:
            core.log(
                "[v3.192-sticker-make-target] "
                f"group={self.chat_name!r} "
                f"local_id={event.get('local_id')} "
                "bypass_next_emoticon_wait=yes"
            )
        if not sticker_make_target and re.search(
            r"(?:表情|贴纸|sticker|emoji)",
            media_intent_text,
            re.I,
        ):
            # Explicit quote of an existing Type47 target must be answered
            # immediately instead of waiting for another sticker.
            if explicit:
                quote_meta, _ = (
                    self._v37_quote_metadata(
                        event
                    )
                )
                if quote_meta and (
                    int(quote_meta.get("refer_type") or 0) == 47
                    or (
                        int(quote_meta.get("refer_type") or 0) == 49
                        and EmoticonPipeline.is_emoticon({
                            "local_type": 49,
                            "raw_content": quote_meta.get("content") or "",
                        })
                    )
                ):
                    core.log(
                        "[v3.12-emoticon-group] "
                        f"group={self.chat_name!r} "
                        f"local_id={event.get('local_id')} "
                        "quoted_target=emoticon-media "
                        "bypass_next_emoticon_wait=yes"
                    )
                    self._answer(event)
                    return

            self.pending[sender] = (
                PendingTrigger(
                    event=event,
                    created=time.monotonic(),
                )
            )
            core.log(
                "[v3.12-emoticon-group] "
                f"group={self.chat_name!r} "
                f"local_id={event.get('local_id')} "
                "waiting_for_next_emoticon=yes "
                f"from={event['sender_display'] or sender!r} "
                f"up_to={self.pending_wait:.1f}s"
            )
            return

        image_intent = _wants_next_image(
            media_intent_text
        )
        if (
            not image_intent
            and explicit
            and _v3168_explicit_deictic_image_question(media_intent_text)
        ):
            image_intent = True
            core.log(
                "[v3.168-deictic-next-image] "
                f"group={self.chat_name!r} "
                f"local_id={event.get('local_id')} "
                "explicit=yes wait_for_next_image=yes"
            )
        imagegen_create_intent = bool(
            _TEXT_TO_IMAGE_CREATE_INTENT.search(
                media_intent_text
            )
        )
        if image_intent and imagegen_create_intent:
            core.log(
                "[v3.22.1-imagegen-route] "
                f"group={self.chat_name!r} "
                f"local_id={event.get('local_id')} "
                "bypass_next_image_wait=yes "
                "reason=text-to-image-create-intent"
            )
        elif image_intent:
            # A verified quote of an existing image must not be mistaken for
            # the old "wait for NEXT image" adjacency intent. Explicit @
            # quotes use the v3.7 metadata reader; unmentioned exact bot
            # quotes reuse the metadata already proven by the quoted-bot gate.
            quote_meta = None
            if explicit:
                quote_meta, _ = (
                    self._v37_quote_metadata(
                        event
                    )
                )
            elif (
                event.get("_v36_trigger_kind") == "quote"
                and event.get("_v350_quote_gate_passed") is True
                and isinstance(
                    event.get("_v350_quoted_bot_meta"),
                    dict,
                )
            ):
                quote_meta = event.get(
                    "_v350_quoted_bot_meta"
                )

            if (
                quote_meta
                and int(
                    quote_meta.get(
                        "refer_type"
                    )
                    or 0
                ) in (3, 47)
            ):
                core.log(
                    "[v3.8-quote-image] "
                    f"group={self.chat_name!r} "
                    f"local_id={event.get('local_id')} "
                    "bypass_next_image_wait=yes"
                )
                self._answer(event)
                return

            # v3.206: a message that quotes an existing image refers to that
            # image, not a next one. If the wait expires, saying the image
            # never arrived is wrong, so the expiry stays silent.
            quoted = self._v37_parse_quote_payload(
                event.get("raw_content")
            )
            if (
                isinstance(quoted, dict)
                and int(quoted.get("refer_type") or 0) in (3, 47)
            ):
                event["_v3206_quoted_image"] = True

            self.pending[sender] = (
                PendingTrigger(
                    event=event,
                    created=(
                        time.monotonic()
                    ),
                )
            )
            core.log(
                f"group={self.chat_name!r} "
                "waiting for NEXT DB image "
                f"from="
                f"{event['sender_display'] or sender!r} "
                f"up_to={self.pending_wait:.1f}s"
            )
            return

        if event.get("_v36_trigger_kind") == "followup":
            self._v3144_submit_followup_gate(event)
            return

        previous_vision_ref = self._v396_previous_vision_image_ref(
            event
        )
        if previous_vision_ref is not None:
            self._answer(
                event,
                image_ref=previous_vision_ref,
            )
            return

        self._answer(event)


    def _handle(
        self,
        event,
    ):
        self._expire()

        # v3.177: owner master switch off (muted) means no processing at all:
        # no history, membership notice, title refresh, routing or media. The
        # runtime stops reading a muted group; this drops events that were
        # already queued when the switch flipped.
        if (getattr(self, "group_cfg", None) or {}).get("muted") is True:
            return

        current_ui_title = str(
            event.get("chat_display_name")
            or ""
        ).strip()
        if current_ui_title:
            core.update_group_ui_title(
                self.chat_name,
                current_ui_title,
            )

        sender = event[
            "sender_username"
        ]
        text = str(
            event["text"] or ""
        )

        if (
            sender
            and sender
            == self.bot_wxid
        ):
            return

        if (
            (not sender or sender == "unknown")
            and core.looks_like_our_message(
                self.chat_name,
                text,
            )
        ):
            return

        # v3.100: group membership notices are system events, not human chat
        # turns. Persist them as before, but keep them out of follow-up,
        # adjacency and proactive-human state; only verified join forms may
        # enqueue the independent newcomer welcome job.
        if int(event.get("local_type") or 0) == 10000:
            core.save_message(
                self.chat_name,
                (
                    f"dbwxid:{sender}"
                    if sender
                    else "dbwxid:unknown"
                ),
                text,
            )
            self._v3100_handle_system_message(event)
            return

        # v3.35: preserve the old same-sender causal ordering while allowing
        # other senders to keep flowing around a slow provider call. Deferral
        # happens before any sender-scoped routing state or message persistence
        # is mutated; the serial resume path will run this event exactly once.
        if self._v335_defer_if_blocked(event):
            return

        # Count human messages from other senders only after bot self-echo
        # filtering, so our own outgoing message never burns a lease.
        self._followup_note_intervening(
            sender
        )

        if int(event.get("local_type") or 0) == 34:
            self._handle_voice(event)
            return

        if event[
            "local_type"
        ] == 3 or event[
            "msg_type"
        ] == "图片":
            self.recent_app.pop(
                sender,
                None,
            )
            core.save_message(
                self.chat_name,
                (
                    f"dbwxid:{sender}"
                    if sender
                    else "dbwxid:unknown"
                ),
                "[图片]",
            )
            self._v39_note_visual_context(event)
            self._handle_image(
                event
            )
            return

        if int(
            event.get("local_type")
            or 0
        ) == 47:
            self.recent_app.pop(
                sender,
                None,
            )
            # Ordinary Type47 is context-only; _handle_emoticon decides
            # whether an existing explicit pending trigger / follow-up lease
            # is allowed to turn it into Vision.
            event = dict(event)
            event["text"] = "[表情]"
            core.save_message(
                self.chat_name,
                (
                    f"dbwxid:{sender}"
                    if sender
                    else "dbwxid:unknown"
                ),
                "[表情]",
            )
            self._v39_note_visual_context(event)
            self._handle_emoticon(
                event
            )
            return

        self._remember_adjacent_app(
            event
        )

        core.save_message(
            self.chat_name,
            (
                f"dbwxid:{sender}"
                if sender
                else "dbwxid:unknown"
            ),
            text,
        )
        self._handle_text(
            event
        )

    def _run(self):
        core.log(
            f"group-worker started "
            f"for={self.chat_name!r}"
        )

        while not (
            self.stop_event.is_set()
        ):
            try:
                event = self.q.get(
                    timeout=0.25
                )
            except queue.Empty:
                self._expire()
                continue

            if event is None:
                break

            try:
                if (
                    isinstance(event, dict)
                    and event.get("_v3144_control") == "followup_gate_result"
                ):
                    self._v3144_resume_followup_gate(event)
                    continue
                if (
                    isinstance(event, dict)
                    and event.get("_v3205_control") == "voice_result"
                ):
                    self._v3205_resume_voice(event)
                    continue
                if (
                    isinstance(event, dict)
                    and event.get("_v335_control") == "resume_sender"
                ):
                    self._v335_resume_sender(
                        str(event.get("sender_key") or "")
                    )
                    continue
                self._handle(
                    event
                )
            except core.VisionGenerationError as exc:
                core.log(
                    f"[v3.14-vision-retry] group={self.chat_name!r} "
                    f"exhausted_notice=yes error={type(exc).__name__}"
                )
                try:
                    self._send_fixed(
                        "这次识图链路失败了，"
                        "你可以稍后再试一次。"
                    )
                except Exception as send_exc:
                    core.log(
                        f"[v3.14-vision-retry] group={self.chat_name!r} "
                        "exhausted_notice=failed "
                        f"error={type(send_exc).__name__}: {send_exc}"
                    )
            except Exception as exc:
                core.log(
                    f"group="
                    f"{self.chat_name!r} "
                    "worker error: "
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

        for ref in (
            self.recent.values()
        ):
            self._cleanup_image_ref(
                ref
            )

        core.log(
            f"group-worker stopped "
            f"for={self.chat_name!r}"
        )
