"""Generic checkpoint and human-in-the-loop helpers.

These helpers let any team persist partial progress onto its job record
and pause for external input without reinventing the pattern the blogging
team hand-rolled in ``blogging/shared/blog_job_store.py``.

Checkpoints are stored under the ``checkpoints`` key on the job record:

    {
        "checkpoints": {
            "phase_name": {"payload": ..., "completed_at": "..."},
        },
        "waiting_for": {"title_selection": {...}},
    }

Consumers pair these with ``workflow.wait_condition`` inside a Temporal
workflow (query ``load_checkpoint`` + signal handler calling
``submit_input``) or, in thread-mode fallback, with a simple polling loop.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _manager(team: str) -> Any:
    from job_service_client import JobServiceClient

    return JobServiceClient(team=team)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_checkpoint(team: str, job_id: str, phase: str, payload: Any = None) -> None:
    """Record that ``phase`` completed for ``job_id`` with optional payload.

    Replayed workflows can call :func:`load_checkpoint` to short-circuit
    already-completed phases.
    """
    mgr = _manager(team)
    job = mgr.get_job(job_id) or {}
    checkpoints = dict(job.get("checkpoints") or {})
    checkpoints[phase] = {"payload": payload, "completed_at": _now()}
    mgr.update_job(job_id, checkpoints=checkpoints, last_phase=phase)


def load_checkpoint(team: str, job_id: str, phase: str) -> Optional[dict[str, Any]]:
    """Return the stored checkpoint dict for ``phase``, or None."""
    mgr = _manager(team)
    job = mgr.get_job(job_id) or {}
    return (job.get("checkpoints") or {}).get(phase)


def wait_for_input(
    team: str,
    job_id: str,
    key: str,
    *,
    prompt: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    poll_interval: float = 1.0,
) -> Any:
    """Block (thread-mode) until an external caller submits input for ``key``.

    Inside a Temporal workflow, prefer ``workflow.wait_condition`` with a
    signal handler that calls :func:`submit_input`; use this helper only in
    the thread-mode fallback.
    """
    mgr = _manager(team)
    job = mgr.get_job(job_id) or {}
    waiting = dict(job.get("waiting_for") or {})
    waiting[key] = {"prompt": prompt, "since": _now()}
    mgr.update_job(job_id, waiting_for=waiting, status="waiting")

    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    while True:
        job = mgr.get_job(job_id) or {}
        submitted = (job.get("inputs") or {}).get(key)
        if submitted is not None:
            remaining = dict(job.get("waiting_for") or {})
            remaining.pop(key, None)
            mgr.update_job(job_id, waiting_for=remaining, status="running")
            return submitted
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"wait_for_input timed out: job={job_id} key={key}")
        time.sleep(poll_interval)


def submit_input(team: str, job_id: str, key: str, value: Any) -> None:
    """Record user-supplied input under ``key`` so a paused job can resume."""
    mgr = _manager(team)
    job = mgr.get_job(job_id) or {}
    inputs = dict(job.get("inputs") or {})
    inputs[key] = value
    waiting = dict(job.get("waiting_for") or {})
    waiting.pop(key, None)
    mgr.update_job(job_id, inputs=inputs, waiting_for=waiting)
