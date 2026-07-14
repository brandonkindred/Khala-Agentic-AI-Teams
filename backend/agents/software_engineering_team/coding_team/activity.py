"""Shared bridge from agent (step, detail, fraction) progress reports to the job record.

One implementation for every reporting site (quality-gate code review, Tech Lead
review, the /review-pr flow) so the ``current_activity`` schema, percent
formatting, write coalescing, and error handling cannot drift between hand-rolled
copies.

Two cost controls matter here because every write is a synchronous job-service
HTTP PATCH on the reporting worker's thread:

- Coalescing: a report is written when the step changes, when the fraction is
  terminal (>= 1.0), or when ``min_interval_s`` has elapsed since the last write.
  Other same-step reports are dropped — the next write carries the newest
  fraction, so a polling UI loses nothing observable.
- Failure cooldown: after a failed write, reports are skipped for
  ``failure_cooldown_s`` so a down job store cannot serialize its client's
  retry sleeps into the worker thread once per report. ``clear()`` is always
  attempted regardless of cooldown.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_MIN_INTERVAL_S = 2.0
DEFAULT_FAILURE_COOLDOWN_S = 30.0


class ActivityBridge:
    """Forward an agent's progress reports into the job record, safely.

    Each written report persists a human ``status_text`` line plus a structured
    ``current_activity`` entry (agent/step/detail/fraction[/task_id/task_title]).

    Preconditions:
        - ``update_fn`` accepts arbitrary keyword fields and persists them to the
          job record.
        - ``fraction`` values are in [0.0, 1.0] and non-decreasing per the
          review-progress contract; callers violating that get clamped output,
          not an error.
    Postconditions:
        - ``__call__`` and ``clear`` never raise: observability must not break
          the run. A failed report write opens the cooldown window; a failed
          clear is logged and swallowed.
    Invariants:
        - Same-step, non-terminal reports produce at most one write per
          ``min_interval_s``.
        - No report writes happen inside the failure cooldown window.
    """

    def __init__(
        self,
        update_fn: Callable[..., None],
        *,
        agent: str,
        label: str,
        task_id: Optional[str] = None,
        task_title: Optional[str] = None,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        failure_cooldown_s: float = DEFAULT_FAILURE_COOLDOWN_S,
    ) -> None:
        assert callable(update_fn), "update_fn must be callable"
        self._update_fn = update_fn
        self._agent = agent
        self._label = label
        self._task_id = task_id
        self._task_title = task_title
        self._min_interval_s = max(0.0, min_interval_s)
        self._failure_cooldown_s = max(0.0, failure_cooldown_s)
        self._lock = threading.Lock()
        self._last_step: Optional[str] = None
        # -inf so the first report always writes and no cooldown is active.
        self._last_write_at = float("-inf")
        self._skip_until = float("-inf")

    def _status_text(self, detail: str, fraction: float) -> str:
        pct = round(min(max(fraction, 0.0), 1.0) * 100)
        text = f"{self._label} ({pct}%)"
        if self._task_title:
            text += f": {self._task_title}"
        if detail:
            text += f" — {detail}"
        return text

    def __call__(self, step: str, detail: str, fraction: float) -> None:
        now = time.monotonic()
        with self._lock:
            if now < self._skip_until:
                return
            same_step = step == self._last_step
            if same_step and fraction < 1.0 and (now - self._last_write_at) < self._min_interval_s:
                return
            activity: dict[str, Any] = {
                "agent": self._agent,
                "step": step,
                "detail": detail,
                "fraction": fraction,
            }
            if self._task_id is not None:
                activity["task_id"] = self._task_id
            if self._task_title is not None:
                activity["task_title"] = self._task_title
            try:
                self._update_fn(
                    status_text=self._status_text(detail, fraction),
                    current_activity=activity,
                )
                self._last_step = step
                self._last_write_at = now
            except Exception as e:  # noqa: BLE001 — observability must not break execution
                self._skip_until = now + self._failure_cooldown_s
                logger.warning(
                    "activity report failed (ignored; cooling down %.0fs): %s",
                    self._failure_cooldown_s,
                    e,
                )

    def clear(self) -> None:
        """Clear ``current_activity`` so a stale sub-progress bar never outlives its step.

        Always attempted, even during a failure cooldown — a missed clear is worse
        than a missed report because a frozen bar masquerades as progress.
        """
        try:
            self._update_fn(current_activity=None)
        except Exception as e:  # noqa: BLE001 — observability must not break execution
            logger.warning("activity clear failed (ignored): %s", e)
