"""Per-sender reply ordering and guarded sends.

Mixed into ``worker.GroupWorker``; state lives on the worker instance.
"""
from __future__ import annotations
from . import core, decision_client, memory_docs
from .worker_text import *  # noqa: F401,F403


class ReplyGuardMixin:
    def _send_fixed(self, text):
        final = (
            core.bot_prefix()
            + text
        )
        with self._v392_send_lock:
            send_seconds = (
                core.focus_and_send(
                    self.chat_name,
                    final,
                )
            )
            self._v39_note_bot_sent()
        core.save_message(
            self.chat_name,
            "bot",
            final,
        )
        core.log(
            f"group={self.chat_name!r} "
            "fixed reply sent; "
            f"send={send_seconds:.2f}s"
        )

    def _v335_reply_sender_key(self, event):
        return str(
            event.get("sender_username")
            or "__unknown__"
        )

    def _v335_reserve_sender(self, sender_key):
        with self._reply_sender_lock:
            self._reply_sender_jobs[sender_key] = (
                self._reply_sender_jobs.get(sender_key, 0)
                + 1
            )
            self._reply_blocked_senders.add(sender_key)

    def _v335_cancel_sender_reservation(self, sender_key):
        with self._reply_sender_lock:
            remaining = max(
                0,
                self._reply_sender_jobs.get(sender_key, 0)
                - 1,
            )
            if remaining:
                self._reply_sender_jobs[sender_key] = remaining
                return
            self._reply_sender_jobs.pop(sender_key, None)
            self._reply_blocked_senders.discard(sender_key)

    def _v335_reply_job_finished(self, sender_key):
        """Queue sender resume on the serial GroupWorker after reply completion."""
        with self._reply_sender_lock:
            remaining = max(
                0,
                self._reply_sender_jobs.get(sender_key, 0)
                - 1,
            )
            if remaining:
                self._reply_sender_jobs[sender_key] = remaining
                return
            self._reply_sender_jobs.pop(sender_key, None)

            # Keep the sender blocked until the control marker reaches the
            # serial GroupWorker. Events already ahead of that marker in q
            # must first join the sender's deferred list in original order.
            if sender_key not in self._reply_blocked_senders:
                return

        if self.stop_event.is_set():
            return
        self.q.put(
            {
                "_v335_control": "resume_sender",
                "sender_key": sender_key,
            }
        )

    def _v335_defer_if_blocked(self, event):
        sender_key = self._v335_reply_sender_key(event)
        with self._reply_sender_lock:
            if sender_key not in self._reply_blocked_senders:
                return False

        deferred = dict(event)
        if not isinstance(
            deferred.get("_v335_context_snapshot"),
            dict,
        ):
            try:
                deferred["_v335_context_snapshot"] = (
                    core.freeze_llm_context_bounded(
                        dict(self.group_cfg),
                        deferred.get("sender_username") or "other",
                        self.profiles,
                        current_text=deferred.get("text"),
                    )
                )
            except Exception as exc:
                # Deferral is still safer than violating sender order. The
                # eventual reply can fall back to a fresh context snapshot.
                core.log(
                    "[v3.35-reply-dispatch] "
                    f"group={self.chat_name!r} "
                    f"local_id={deferred.get('local_id')} "
                    "context_freeze_failed=yes "
                    f"error={type(exc).__name__}: {exc}"
                )

        with self._reply_sender_lock:
            # Re-check after context capture in case a reply completed while
            # the snapshot was being built. The sender remains blocked until
            # the serial resume marker removes it, so this is mostly defensive.
            if sender_key not in self._reply_blocked_senders:
                return False
            bucket = self._reply_deferred_events.setdefault(
                sender_key,
                [],
            )
            bucket.append(deferred)
            deferred_count = len(bucket)

        core.log(
            "[v3.35-reply-dispatch] "
            f"group={self.chat_name!r} "
            f"local_id={deferred.get('local_id')} "
            f"sender_deferred=yes pending={deferred_count}"
        )
        return True

    def _v335_resume_sender(self, sender_key):
        if not sender_key:
            return

        with self._reply_sender_lock:
            # A newer job may have become active before an older resume marker
            # was handled. In that case keep all deferred work blocked.
            if self._reply_sender_jobs.get(sender_key, 0) > 0:
                return
            self._reply_blocked_senders.discard(sender_key)
            deferred = self._reply_deferred_events.pop(
                sender_key,
                [],
            )

        if deferred:
            core.log(
                "[v3.35-reply-dispatch] "
                f"group={self.chat_name!r} sender_resume=yes "
                f"deferred={len(deferred)}"
            )

        for index, event in enumerate(deferred):
            if self.stop_event.is_set():
                return
            self._handle(event)
            with self._reply_sender_lock:
                blocked_again = (
                    sender_key in self._reply_blocked_senders
                )
                if blocked_again:
                    remaining = deferred[index + 1:]
                    if remaining:
                        existing = self._reply_deferred_events.get(
                            sender_key,
                            [],
                        )
                        self._reply_deferred_events[sender_key] = (
                            remaining + existing
                        )
                    return

    def _reply_job_current(self, generation):
        if self.stop_event.is_set():
            return False
        with self._reply_state_lock:
            return generation == self._reply_generation

    def _reply_send_guarded(
        self,
        generation,
        final,
        local_id=None,
    ):
        """Send only while this exact GroupWorker generation is still live."""
        return self._reply_send_many_guarded(
            generation,
            [final],
            local_id=local_id,
        )

    def _reply_send_many_guarded(
        self,
        generation,
        finals,
        local_id=None,
    ):
        """Send one logical reply as ordered chunks under the existing send lock."""
        finals = [str(item) for item in finals if str(item)]
        if not finals:
            return None
        with self._v392_send_lock:
            send_seconds = 0.0
            for index, final in enumerate(finals):
                with self._reply_state_lock:
                    stale = (
                        self.stop_event.is_set()
                        or generation != self._reply_generation
                    )
                if stale:
                    core.log(
                        "[v3.35-reply-dispatch] "
                        f"group={self.chat_name!r} local_id={local_id} "
                        "send_dropped=yes reason=stale-before-send "
                        f"chunks_sent={index}/{len(finals)}"
                    )
                    return None
                send_seconds += core.focus_and_send(
                    self.chat_name,
                    final,
                )
                self._v39_note_bot_sent()
            return send_seconds

    def _reply_send_fixed_guarded(
        self,
        generation,
        event,
        text,
    ):
        final = core.bot_prefix() + text
        send_seconds = self._reply_send_guarded(
            generation,
            final,
            local_id=event.get("local_id"),
        )
        if send_seconds is None:
            return False
        core.save_message(
            self.chat_name,
            "bot",
            final,
        )
        core.log(
            "[v3.35-reply-dispatch] "
            f"group={self.chat_name!r} local_id={event.get('local_id')} "
            f"fixed_reply_sent=yes send={send_seconds:.2f}s"
        )
        return True
