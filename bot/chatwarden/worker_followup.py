"""Follow-up windows after a reply and the light gate that decides whether to continue.

Mixed into ``worker.GroupWorker``; state lives on the worker instance.
"""
from __future__ import annotations
import queue
import time
from . import core, decision_client, memory_docs, prefilter
from .worker_text import *  # noqa: F401,F403


class FollowupMixin:
    def _v3144_note_sender_activity(self, sender):
        """Invalidate any older async follow-up decision for this sender."""
        lock = getattr(self, "_followup_gate_state_lock", None)
        generations = getattr(self, "_followup_gate_generation", None)
        if lock is None or generations is None:
            return 0
        key = str(sender or "")
        with lock:
            generation = int(generations.get(key, 0)) + 1
            generations[key] = generation
            return generation

    def _v3144_submit_followup_gate(self, event):
        """Classify a pure-text follow-up off the GroupWorker thread."""
        sender = str(event.get("sender_username") or "").strip()
        gate_queue = getattr(self, "_followup_gate_queue", None)
        gate_slot = getattr(self, "_followup_gate_slot", None)
        gate_lock = getattr(self, "_followup_gate_state_lock", None)
        generations = getattr(self, "_followup_gate_generation", None)
        if not sender or gate_queue is None or gate_slot is None or gate_lock is None or generations is None:
            self._v39_note_ordinary_human(event)
            return False

        if not gate_slot.acquire(blocking=False):
            core.log(
                "[v3.144-followup-async-gate] "
                f"group={self.chat_name!r} sender={sender!r} "
                f"local_id={event.get('local_id')} "
                "queued=no reason=gate-busy fail_closed=yes send=no"
            )
            self._v39_note_ordinary_human(event)
            return False

        with gate_lock:
            generation = int(generations.get(sender, 0))
        item = {
            "event": dict(event),
            "sender_key": sender,
            "generation": generation,
        }
        try:
            gate_queue.put_nowait(item)
        except queue.Full:
            gate_slot.release()
            core.log(
                "[v3.144-followup-async-gate] "
                f"group={self.chat_name!r} sender={sender!r} "
                f"local_id={event.get('local_id')} "
                "queued=no reason=queue-full fail_closed=yes send=no"
            )
            self._v39_note_ordinary_human(event)
            return False

        core.log(
            "[v3.144-followup-async-gate] "
            f"group={self.chat_name!r} sender={sender!r} "
            f"local_id={event.get('local_id')} queued=yes "
            "group_worker_blocked=no deadline=4s fail_closed=yes"
        )
        return True

    def _v3144_followup_gate_run(self):
        while not self.stop_event.is_set():
            try:
                item = self._followup_gate_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                self._followup_gate_queue.task_done()
                break
            sender = str(item.get("sender_key") or "")
            generation = int(item.get("generation") or 0)
            event = item.get("event")
            addressed = False
            stale = True
            shadow_capture = None
            decision_mode = decision_client.mode('followup_v2')
            try:
                with self._followup_gate_state_lock:
                    stale = self._followup_gate_generation.get(sender) != generation
                rule = None
                prefilter_mode = prefilter.mode()
                if prefilter_mode != 'off' and isinstance(event, dict):
                    try:
                        rule = prefilter.followup_rule(
                            event, bot_wxid=getattr(self, 'bot_wxid', ''),
                        )
                    except Exception:
                        rule = None
                if (not stale and isinstance(event, dict) and not self.stop_event.is_set()
                        and rule and prefilter_mode == 'active'):
                    addressed = False
                    core.log(
                        "[v3.205-prefilter] kind=followup "
                        f"group={self.chat_name!r} local_id={event.get('local_id')} "
                        f"rule=no reason={rule} model_call=skipped"
                    )
                    with self._followup_gate_state_lock:
                        stale = self._followup_gate_generation.get(sender) != generation
                elif not stale and isinstance(event, dict) and not self.stop_event.is_set():
                    if decision_mode == 'active':
                        try:
                            snapshot = core.freeze_followup_snapshot(
                                self.group_cfg,
                                str(event.get("text") or ""),
                                sender,
                                bot_identity=self.bot_wxid,
                                bot_display_name=str(event.get('bot_display_name') or ''),
                            )
                            action = decision_client.decide_followup(
                                snapshot,
                                {
                                    'local_id': event.get('local_id'),
                                    'generation': generation,
                                    'stale': False,
                                },
                            )
                        except Exception as exc:
                            action = 'host_fallback'
                            core.log(
                                '[v3.167-decision-active] kind=followup_v2 '
                                f'error={type(exc).__name__} fallback=legacy'
                            )
                        if action in {'reply', 'silent'}:
                            addressed = action == 'reply'
                        else:
                            addressed = core.call_followup_gate(
                                self.group_cfg,
                                str(event.get("text") or ""),
                                sender,
                                self.profiles,
                            )
                    else:
                        gate_kwargs = {}
                        if decision_mode == 'shadow':
                            shadow_capture = {'bot_identity': self.bot_wxid,
                                              'bot_display_name': str(event.get('bot_display_name') or '')}
                            gate_kwargs['shadow_capture'] = shadow_capture
                        addressed = core.call_followup_gate(
                            self.group_cfg,
                            str(event.get("text") or ""),
                            sender,
                            self.profiles,
                            **gate_kwargs,
                        )
                    with self._followup_gate_state_lock:
                        stale = self._followup_gate_generation.get(sender) != generation
                    if rule:
                        core.log(
                            "[v3.205-prefilter] kind=followup "
                            f"group={self.chat_name!r} local_id={event.get('local_id')} "
                            f"rule=no reason={rule} model={'yes' if addressed else 'no'} "
                            f"agree={'no' if addressed else 'yes'} mode=shadow"
                        )
                if not stale and not self.stop_event.is_set():
                    self.q.put({
                        "_v3144_control": "followup_gate_result",
                        "event": event,
                        "sender_key": sender,
                        "generation": generation,
                        "addressed": bool(addressed),
                    })
            finally:
                try:
                    self._followup_gate_slot.release()
                except ValueError:
                    pass
                self._followup_gate_queue.task_done()

            # Legacy control is published and its capacity slot is released.
            # Only a non-blocking enqueue is allowed here: no HTTP/DB work.
            if (decision_mode == 'shadow' and shadow_capture
                    and shadow_capture.get('snapshot') is not None
                    and not self.stop_event.is_set()):
                try:
                    decision_client.submit(shadow_capture['snapshot'], addressed, {
                        'local_id': event.get('local_id'), 'generation': generation, 'stale': stale,
                    })
                except Exception:
                    core.log('[v3.162-decision-shadow] enqueue_failed=yes behavior=legacy')

    def _v3144_resume_followup_gate(self, control):
        event = control.get("event")
        sender = str(control.get("sender_key") or "")
        generation = int(control.get("generation") or 0)
        if not isinstance(event, dict) or not sender:
            return
        with self._followup_gate_state_lock:
            if self._followup_gate_generation.get(sender) != generation:
                core.log(
                    "[v3.144-followup-async-gate] "
                    f"group={self.chat_name!r} sender={sender!r} "
                    f"local_id={event.get('local_id')} result_dropped=yes reason=stale"
                )
                return
            self._followup_gate_generation.pop(sender, None)

        if control.get("addressed") is not True:
            core.log(
                "[v3.24-followup-gate] "
                f"group={self.chat_name!r} sender={sender!r} "
                f"local_id={event.get('local_id')} "
                "addressed_to_bot=no send=no full_reply_skipped=yes "
                "lease_anchor=unchanged async=yes"
            )
            self._v39_note_ordinary_human(event)
            return

        event["_v324_followup_gate_passed"] = True
        core.log(
            "[v3.24-followup-gate] "
            f"group={self.chat_name!r} sender={sender!r} "
            f"local_id={event.get('local_id')} "
            "addressed_to_bot=yes full_reply=yes async=yes"
        )
        previous_vision_ref = self._v396_previous_vision_image_ref(event)
        if previous_vision_ref is not None:
            self._answer(event, image_ref=previous_vision_ref)
            return
        self._answer(event)

    def _followup_drop(
        self,
        sender,
        reason,
    ):
        state = self.followups.pop(
            sender,
            None,
        )
        if state is not None:
            core.log(
                "[v3.6-followup] "
                f"group={self.chat_name!r} "
                f"sender={sender!r} "
                f"lease closed reason={reason}"
            )

    def _followup_prune(
        self,
        now=None,
    ):
        if now is None:
            now = time.monotonic()

        for sender, state in list(
            self.followups.items()
        ):
            if (
                now - state["last_success"]
                <= self.followup_ttl
            ):
                continue
            self._followup_drop(
                sender,
                "ttl",
            )

    def _followup_state(
        self,
        sender,
    ):
        if not sender:
            return None

        now = time.monotonic()
        self._followup_prune(now)
        state = self.followups.get(sender)
        if not state:
            return None

        return state

    def _followup_note_intervening(
        self,
        sender,
    ):
        # v3.15 keeps the sender-scoped lease for the full TTL even when the
        # group talks in between. The semantic gate, not a message counter,
        # decides whether a later message is actually addressed to the bot.
        del sender
        self._followup_prune()

    def _v315_followup_prompt(
        self,
        event,
        base_prompt,
    ):
        if event.get("_v36_trigger_kind") != "followup":
            return base_prompt
        if event.get("_v324_followup_gate_passed") is True:
            # Pure-text v3.24 follow-ups were already classified on the
            # isolated light gate. Do not make the full answer model repeat
            # the expensive addressed/not-addressed decision.
            return base_prompt

        return (
            str(base_prompt).rstrip()
            + "\n\n"
            + "【未@机器人的续聊识别】\n"
            + "当前消息来自机器人上次成功回复后3分钟识别窗口内的同一位发言人，"
            + "但这不代表消息一定是在对机器人说。请先根据当前消息、最近群聊上下文、"
            + "发言对象和上下文衔接判断：发言人此刻是否明显在继续和机器人对话。\n"
            + "以下情况视为继续：追问或回答机器人、补充/纠正上一轮、要求机器人继续、"
            + "对机器人上一句作出自然回应，或语义上明显承接机器人。\n"
            + "以下情况不视为继续：明显在对其他群友说、群内广播、自言自语、与机器人无关"
            + "的新话题，或无法可靠判断发言对象。中途出现无关消息不会自动结束3分钟窗口；"
            + "只判断当前这条消息是否在和机器人说。\n"
            + "若明显在继续和机器人对话，正常回答当前消息。否则不要回答、不要解释判定，"
            + "只输出这一行精确标记："
            + self._V315_NO_REPLY_MARKER
        )

    def _v315_is_no_reply(
        self,
        reply,
    ):
        value = str(reply or "").strip()
        return value in {
            self._V315_NO_REPLY_MARKER,
            "`" + self._V315_NO_REPLY_MARKER + "`",
        }

    def _followup_after_success(
        self,
        event,
    ):
        sender = event.get(
            "sender_username"
        )
        if not sender:
            return

        kind = event.get(
            "_v36_trigger_kind",
            "",
        )
        now = time.monotonic()

        if kind in ("explicit", "quote"):
            self.followups[sender] = {
                "last_success": now,
            }
            core.log(
                "[v3.15-semantic-followup] "
                f"group={self.chat_name!r} "
                f"sender={sender!r} "
                f"lease opened reason={kind} "
                f"ttl={self.followup_ttl:.0f}s "
                "turn_limit=none"
            )
            return

        if kind != "followup":
            return

        state = self.followups.get(sender)
        if not state:
            return

        state["last_success"] = now

        core.log(
            "[v3.15-semantic-followup] "
            f"group={self.chat_name!r} "
            f"sender={sender!r} "
            f"lease refreshed ttl={self.followup_ttl:.0f}s "
            "turn_limit=none"
        )
