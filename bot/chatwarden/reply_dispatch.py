from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable


CurrentGuard = Callable[[], bool]
RunReplyJob = Callable[[CurrentGuard], None]
CleanupReplyJob = Callable[[], None]


def _noop_cleanup():
    return None


@dataclass(frozen=True)
class ReplyJob:
    """One immutable provider/send job owned by a ReplyDispatcher."""

    lane: str
    sender_key: str
    local_id: int
    run: RunReplyJob
    cleanup: CleanupReplyJob = _noop_cleanup
    queued_at: float = field(default_factory=time.monotonic)


class ReplyDispatcher:
    """Bounded per-group scheduler with lane and sender isolation.

    The interface deliberately knows nothing about WeChat, Hermes, or Vision.
    It guarantees only these scheduling invariants:

    - total outstanding work is bounded;
    - each lane has an independent concurrency cap;
    - one sender may have at most one active job across all lanes;
    - a blocked sender does not head-of-line block a runnable different sender;
    - stop() drops queued work and invalidates the current guard of active work.
    """

    def __init__(
        self,
        name,
        max_outstanding=8,
        lane_limits=None,
        lane_max_outstanding=None,
        logger=None,
    ):
        self.name = str(name or "reply-dispatch")
        self.max_outstanding = max(1, int(max_outstanding))
        raw_limits = lane_limits or {"text": 1, "vision": 1}
        self.lane_limits = {
            str(lane): max(1, int(limit))
            for lane, limit in raw_limits.items()
        }
        if not self.lane_limits:
            raise ValueError("ReplyDispatcher requires at least one lane")
        raw_lane_max = lane_max_outstanding or {}
        self.lane_max_outstanding = {
            lane: max(
                self.lane_limits[lane],
                int(raw_lane_max.get(lane, self.max_outstanding)),
            )
            for lane in self.lane_limits
        }

        self._logger = logger
        self._condition = threading.Condition(threading.RLock())
        self._pending = []
        self._active_senders = set()
        self._active_lanes = {
            lane: 0
            for lane in self.lane_limits
        }
        self._stopped = False
        self._generation = 0

        worker_count = sum(self.lane_limits.values())
        self._threads = []
        for index in range(worker_count):
            thread = threading.Thread(
                target=self._run,
                name=f"{self.name}-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def _log(self, message):
        if self._logger is None:
            return
        try:
            self._logger(message)
        except Exception:
            # Scheduling must never fail because observability failed.
            pass

    def _outstanding_locked(self):
        return len(self._pending) + sum(self._active_lanes.values())

    def _lane_outstanding_locked(self, lane):
        return (
            self._active_lanes[lane]
            + sum(
                1
                for job in self._pending
                if job.lane == lane
            )
        )

    def submit(self, job):
        if not isinstance(job, ReplyJob):
            raise TypeError("job must be ReplyJob")
        if job.lane not in self.lane_limits:
            raise ValueError(f"unsupported reply lane: {job.lane!r}")
        if not str(job.sender_key or "").strip():
            raise ValueError("ReplyJob sender_key must be non-empty")

        with self._condition:
            if self._stopped:
                return False
            if self._outstanding_locked() >= self.max_outstanding:
                return False
            if (
                self._lane_outstanding_locked(job.lane)
                >= self.lane_max_outstanding[job.lane]
            ):
                return False
            self._pending.append(job)
            self._condition.notify_all()
            return True

    def stop(self):
        dropped = []
        with self._condition:
            if self._stopped:
                return
            self._stopped = True
            self._generation += 1
            dropped = self._pending
            self._pending = []
            self._condition.notify_all()

        for job in dropped:
            self._cleanup(job)

    def _take_runnable_locked(self):
        for index, job in enumerate(self._pending):
            if job.sender_key in self._active_senders:
                continue
            if self._active_lanes[job.lane] >= self.lane_limits[job.lane]:
                continue

            self._pending.pop(index)
            self._active_senders.add(job.sender_key)
            self._active_lanes[job.lane] += 1
            return job, self._generation
        return None

    def _is_current(self, generation):
        with self._condition:
            return (
                not self._stopped
                and generation == self._generation
            )

    def _cleanup(self, job):
        try:
            job.cleanup()
        except Exception as exc:
            self._log(
                f"[v3.35-reply-dispatch] name={self.name!r} "
                f"local_id={job.local_id} cleanup_error="
                f"{type(exc).__name__}: {exc}"
            )

    def _run(self):
        while True:
            with self._condition:
                selected = None
                while selected is None:
                    if self._stopped and not self._pending:
                        return
                    selected = self._take_runnable_locked()
                    if selected is None:
                        self._condition.wait(timeout=0.25)

                job, generation = selected

            wait_seconds = max(0.0, time.monotonic() - job.queued_at)
            self._log(
                f"[v3.35-reply-dispatch] name={self.name!r} "
                f"local_id={job.local_id} lane={job.lane} "
                f"job_wait={wait_seconds:.2f}s started=yes"
            )
            try:
                job.run(lambda: self._is_current(generation))
            except Exception as exc:
                self._log(
                    f"[v3.35-reply-dispatch] name={self.name!r} "
                    f"local_id={job.local_id} lane={job.lane} "
                    f"uncaught={type(exc).__name__}: {exc}"
                )
            finally:
                self._cleanup(job)
                with self._condition:
                    self._active_senders.discard(job.sender_key)
                    self._active_lanes[job.lane] -= 1
                    self._condition.notify_all()

