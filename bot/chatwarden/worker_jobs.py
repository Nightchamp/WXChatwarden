"""Background jobs started from a chat turn: image generation, image editing and research.

Mixed into ``worker.GroupWorker``; state lives on the worker instance.
"""
from __future__ import annotations
import os
import queue
import threading
import time
from . import core, decision_client, memory_docs
from .worker_text import *  # noqa: F401,F403


class JobsMixin:
    def _v3192_imagegen_prompt_with_quoted_text(
        self,
        event,
        prompt,
    ):
        """Append an exact quoted plain-text message to a text-to-image prompt.

        _v343_current_instruction_text deliberately drops the quote preview, so
        "把这两段文字转成表情包" alone would reach 8652 without the text it
        refers to. Only a Type1 refermsg is carried, as bounded quoted data;
        media/share quotes keep their existing reference routes.
        """
        value = str(prompt or "").strip()
        if not value:
            return value
        quote = self._v37_parse_quote_payload(event.get("raw_content"))
        if not isinstance(quote, dict):
            return value
        if int(quote.get("refer_type") or 0) != 1:
            return value
        quoted = str(quote.get("content") or "").replace("\x00", "").strip()
        if not quoted or quoted in value:
            return value
        if len(quoted) > 800:
            quoted = quoted[:800].rstrip()
        core.log(
            "[v3.192-imagegen-quoted-text] "
            f"group={getattr(self, 'chat_name', '')!r} "
            f"local_id={event.get('local_id')} "
            f"quoted_chars={len(quoted)}"
        )
        return f"{value}\n\n被引用的文字内容：\n{quoted}"

    # db-native-v3.22-text-to-image
    def _v322_imagegen_job(
        self,
        event,
        prompt,
        aspect_ratio,
        reference_image_path=None,
        reference_source=None,
    ):
        started = time.monotonic()
        generated = None
        image_sent = False
        local_id = event.get("local_id")
        try:
            generated = core.call_image_gen(
                prompt,
                aspect_ratio=aspect_ratio,
                chat_name=self.chat_name,
                reference_image_path=reference_image_path,
            )
            if self.stop_event.is_set() or not core.image_gen_enabled():
                core.log(
                    "[v3.22-imagegen] "
                    f"group={self.chat_name!r} local_id={local_id} "
                    "send_suppressed=yes reason=worker-stopping-or-runtime-disabled"
                )
                return
            with self._v392_send_lock:
                send_seconds = core.focus_and_send_image(
                    self.chat_name,
                    generated["path"],
                )
                image_sent = True
                self._v39_note_bot_sent()

            # Match the existing v3.6 rule: only a confirmed final delivery
            # opens/refreshes the same-sender follow-up lease. The immediate
            # text ACK alone does not mutate it.
            self._followup_after_success(event)
            saved_request = (
                core.image_gen_prompt(event.get("text"))
                if reference_image_path
                else prompt
            )
            core.save_message(
                self.chat_name,
                "bot",
                (
                    core.bot_prefix()
                    + (
                        "[已编辑图片] 原始请求："
                        if reference_image_path
                        else "[已生成图片] 原始请求："
                    )
                    + saved_request[:500]
                ),
            )
            core.log(
                ("[v3.23-image-edit] " if reference_image_path else "[v3.22-imagegen] ")
                +
                f"group={self.chat_name!r} local_id={local_id} "
                "completed=yes image_sent=yes "
                f"aspect={aspect_ratio} "
                f"reference_source={reference_source or 'none'} "
                f"provider={generated.get('provider') or 'unknown'} "
                f"model={generated.get('model') or 'unknown'} "
                f"generate={generated.get('elapsed', 0.0):.2f}s "
                f"send={send_seconds:.2f}s "
                f"total={time.monotonic() - started:.2f}s"
            )
        except Exception as exc:
            if image_sent:
                # The image is already proven sent. Never follow it with a
                # misleading failure message because a bookkeeping step failed.
                core.log(
                    "[v3.22-imagegen] "
                    f"group={self.chat_name!r} local_id={local_id} "
                    f"post_send_error={type(exc).__name__}: {exc}"
                )
                return

            if isinstance(exc, core.ImageSendUnconfirmedError):
                message = (
                    "图片已经触发发送，但微信没有返回明确确认。"
                    "我先不自动重发，避免重复，你看一下群里有没有出现图片。"
                )
            elif isinstance(exc, core.ImageGenerationRejectedError):
                message = "这次图片请求被上游拒绝了。可以换个描述或减少敏感/冲突要求后再试一次。"
            elif isinstance(exc, core.ImageGenerationQuotaError):
                message = "当前生图额度暂时不可用，稍后再试一次。"
            else:
                message = "这张图刚才没画成功，或者发送链路出了问题。你可以再试一次。"
            try:
                with self._v392_send_lock:
                    core.focus_and_send(
                        self.chat_name,
                        core.bot_prefix() + message,
                    )
                    self._v39_note_bot_sent()
            except Exception as send_exc:
                core.log(
                    "[v3.22-imagegen] "
                    f"group={self.chat_name!r} local_id={local_id} "
                    f"failure_notice_send_error={type(send_exc).__name__}: {send_exc}"
                )
            core.log(
                "[v3.22-imagegen] "
                f"group={self.chat_name!r} local_id={local_id} "
                f"failed={type(exc).__name__}: {exc}"
            )
        finally:
            if generated and generated.get("path"):
                try:
                    os.unlink(generated["path"])
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    core.log(
                        "[v3.22-imagegen] "
                        f"group={self.chat_name!r} local_id={local_id} "
                        f"cleanup_error={type(exc).__name__}: {exc}"
                    )
            if reference_image_path:
                try:
                    self.image_pipeline.cleanup(reference_image_path)
                except Exception as exc:
                    core.log(
                        "[v3.23-image-edit] "
                        f"group={self.chat_name!r} local_id={local_id} "
                        f"reference_cleanup_error={type(exc).__name__}: {exc}"
                    )

    def _v322_start_imagegen(
        self,
        event,
        reply_generation=None,
        routed_reply=None,
        prompt_override=None,
    ):
        prompt = (
            core.image_gen_prompt(prompt_override)
            if prompt_override is not None
            else core.image_gen_routed_prompt(
                routed_reply,
                event.get("text"),
            )
        )
        if not prompt:
            core.log(
                "[v3.29-imagegen-marker-guard] "
                f"group={self.chat_name!r} local_id={event.get('local_id')} "
                "marker_suppressed=yes reason=empty-prompt"
            )
            return False
        aspect_ratio = core.image_gen_aspect_ratio(prompt)

        ack_final = core.bot_prefix() + core.image_gen_ack_text()
        if reply_generation is not None:
            ack_send_seconds = self._reply_send_guarded(
                reply_generation,
                ack_final,
                local_id=event.get("local_id"),
            )
            if ack_send_seconds is None:
                return False
        else:
            with self._v392_send_lock:
                ack_send_seconds = core.focus_and_send(
                    self.chat_name,
                    ack_final,
                )
                # The bot visibly spoke, so proactive chatter should stay quiet
                # while the dedicated image job is in flight.
                self._v39_note_bot_sent()

        event_snapshot = dict(event)
        thread = threading.Thread(
            target=self._v322_imagegen_job,
            args=(event_snapshot, prompt, aspect_ratio),
            name=(
                "wechat-imagegen-"
                + str(self.chat_name)[:24]
                + "-"
                + str(event.get("local_id") or "0")
            ),
            daemon=True,
        )
        thread.start()
        core.log(
            "[v3.22-imagegen] "
            f"group={self.chat_name!r} local_id={event.get('local_id')} "
            f"gate=yes ack_sent=yes aspect={aspect_ratio} "
            f"prompt_chars={len(prompt)} send={ack_send_seconds:.2f}s "
            "async=yes vision=no"
        )
        return True

    def _v323_start_image_edit(
        self,
        event,
        image_ref,
        reference_source="adjacent",
        instruction_override=None,
    ):
        if not image_ref or not image_ref.path:
            raise RuntimeError("image edit reference is unavailable")
        current_instruction = str(
            instruction_override
            if instruction_override is not None
            else self._v343_current_instruction_text(event)
            or ""
        )
        bot_display_name = str(
            event.get("bot_display_name")
            or ""
        ).strip()
        if bot_display_name:
            current_instruction = current_instruction.replace(
                "@" + bot_display_name,
                " ",
            ).replace(
                "＠" + bot_display_name,
                " ",
            )
        prompt = core.image_edit_prompt(current_instruction)
        if not prompt:
            raise RuntimeError("image edit request has no prompt")
        aspect_ratio = core.image_gen_aspect_ratio(prompt)

        ack_final = core.bot_prefix() + core.image_edit_ack_text()
        with self._v392_send_lock:
            ack_send_seconds = core.focus_and_send(
                self.chat_name,
                ack_final,
            )
            self._v39_note_bot_sent()

        thread = threading.Thread(
            target=self._v322_imagegen_job,
            args=(
                dict(event),
                prompt,
                aspect_ratio,
                image_ref.path,
                reference_source,
            ),
            name=(
                "wechat-image-edit-"
                + str(self.chat_name)[:24]
                + "-"
                + str(event.get("local_id") or "0")
            ),
            daemon=True,
        )
        thread.start()
        core.log(
            "[v3.23-image-edit] "
            f"group={self.chat_name!r} local_id={event.get('local_id')} "
            f"route=yes source={reference_source} ack_sent=yes "
            f"aspect={aspect_ratio} prompt_chars={len(prompt)} "
            f"send={ack_send_seconds:.2f}s async=yes vision=no"
        )

    def _reach_job_current(self, generation):
        if self.stop_event.is_set():
            return False
        with self._reach_state_lock:
            return generation == self._reach_generation

    def _reach_run(self):
        while True:
            job = self._reach_queue.get()
            if job is None:
                self._reach_queue.task_done()
                return

            generation, snapshot, provider_prompt, sender_identity = job
            started = time.monotonic()
            try:
                if not self._reach_job_current(generation):
                    core.log(
                        f"[v3.29-reach-hardening] group={self.chat_name!r} "
                        f"local_id={snapshot.get('local_id')} "
                        "dropped=yes reason=stale-before-run"
                    )
                    continue

                reply = core.call_agent_reach(
                    snapshot.get('_research_group_cfg', self.group_cfg),
                    provider_prompt,
                    sender_identity,
                    snapshot.get('_research_profiles', self.profiles),
                    **({'context_snapshot': snapshot['_research_context']}
                       if isinstance(snapshot.get('_research_context'), dict) else {}),
                )
                if not self._reach_job_current(generation):
                    core.log(
                        f"[v3.29-reach-hardening] group={self.chat_name!r} "
                        f"local_id={snapshot.get('local_id')} "
                        "dropped=yes reason=stale-after-provider"
                    )
                    continue

                chunks = core.split_agent_reach_reply(reply)
                if not chunks:
                    raise RuntimeError("Agent Reach reply became empty")

                send_seconds = 0.0
                with self._v392_send_lock:
                    # Serialize the final validity check with stop()/identity
                    # replacement. Once stop() returns, this old worker can no
                    # longer begin or continue a Reach send.
                    with self._reach_state_lock:
                        if (
                            self.stop_event.is_set()
                            or generation != self._reach_generation
                        ):
                            core.log(
                                f"[v3.29-reach-hardening] group={self.chat_name!r} "
                                f"local_id={snapshot.get('local_id')} "
                                "dropped=yes reason=stale-before-send"
                            )
                            continue
                        for chunk in chunks:
                            send_seconds += core.focus_and_send(
                                self.chat_name,
                                core.bot_prefix() + chunk,
                            )
                            self._v39_note_bot_sent()

                self._followup_after_success(snapshot)
                core.save_message(
                    self.chat_name,
                    "bot",
                    core.bot_prefix() + reply,
                )
                core.log(
                    f"[v3.25-reach-async] group={self.chat_name!r} "
                    f"local_id={snapshot.get('local_id')} "
                    "reply_sent=yes "
                    f"reach={time.monotonic()-started:.2f}s "
                    f"send={send_seconds:.2f}s"
                )
            except Exception as exc:
                core.log(
                    f"[v3.25-reach-async] group={self.chat_name!r} "
                    f"local_id={snapshot.get('local_id')} "
                    f"failed={type(exc).__name__}: {exc}"
                )
                if self._reach_job_current(generation):
                    try:
                        with self._v392_send_lock:
                            with self._reach_state_lock:
                                if (
                                    not self.stop_event.is_set()
                                    and generation == self._reach_generation
                                ):
                                    core.focus_and_send(
                                        self.chat_name,
                                        core.bot_prefix()
                                        + "刚才调查链路出了点问题，没能查完。你可以再试一次。",
                                    )
                                    self._v39_note_bot_sent()
                    except Exception:
                        pass
            finally:
                try:
                    self._reach_slots.release()
                except ValueError:
                    pass
                self._reach_queue.task_done()

    def _start_agent_reach_async(
        self,
        event,
        provider_prompt,
        sender_identity,
    ):
        """Queue bounded Agent Reach work outside the serial event loop."""
        if self.stop_event.is_set():
            return False
        if not self._reach_slots.acquire(blocking=False):
            core.log(
                f"[v3.29-reach-hardening] group={self.chat_name!r} "
                f"local_id={event.get('local_id')} "
                "queued=no reason=max-inflight"
            )
            return False

        with self._reach_state_lock:
            generation = self._reach_generation
        try:
            self._reach_queue.put_nowait(
                (
                    generation,
                    dict(event),
                    provider_prompt,
                    sender_identity,
                )
            )
        except queue.Full:
            self._reach_slots.release()
            core.log(
                f"[v3.29-reach-hardening] group={self.chat_name!r} "
                f"local_id={event.get('local_id')} "
                "queued=no reason=queue-full"
            )
            return False
        core.log(
            f"[v3.25-reach-async] group={self.chat_name!r} "
            f"local_id={event.get('local_id')} queued=yes "
            f"pending={self._reach_queue.qsize()} "
            f"max_inflight={self._reach_max_inflight}"
        )
        return True
