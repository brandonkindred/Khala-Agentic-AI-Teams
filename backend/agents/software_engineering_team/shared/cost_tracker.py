"""Per-job LLM cost accumulation for the Software Engineering team.

Registered once as an :mod:`llm_service` call observer (see
:func:`register_cost_observer`): every LLM call attributed to an SE job adds its
estimated ``cost_usd`` to a process-wide running total for that ``job_id`` and
(throttled) flushes the total to the job store via ``update_job(cost_usd=...)``
so the figure is durable and visible in ``GET /run-team/{job_id}`` and the
metrics endpoint.

Reporting only — this module never blocks or halts a job; there is no budget cap.

Invariants:
    - The accumulated cost for a job is monotonically non-decreasing until
      :func:`reset` is called for that job.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from software_engineering_team.shared.env_config import env_float

logger = logging.getLogger(__name__)


@dataclass
class _CostState:
    cost_usd: float = 0.0
    flushed_cost: float = 0.0
    last_flushed_at: float = 0.0


_states: dict[str, _CostState] = {}
_lock = threading.Lock()


def _flush_interval_s() -> float:
    """Min seconds between job-store flushes per job (env ``SE_COST_FLUSH_INTERVAL_S``).

    Postconditions: returns a non-negative float; garbage env → default ``2.0``.
    """
    return env_float("SE_COST_FLUSH_INTERVAL_S", 2.0, 0.0)


def add_cost(job_id: str, cost_usd: float) -> float:
    """Add ``cost_usd`` to ``job_id``'s running total; throttled-flush to the store.

    Preconditions:
        - ``job_id`` is a non-empty string.
        - ``cost_usd >= 0``.
    Postconditions:
        - Returns the new cumulative total (``>=`` the previous total).
        - The total is flushed to the job store when it has grown and at least
          ``SE_COST_FLUSH_INTERVAL_S`` seconds have passed since the last flush.
          A flush failure is swallowed (logged at DEBUG) — accounting never
          breaks the LLM call path.
    """
    if not job_id:
        raise ValueError("job_id must be a non-empty string")
    if cost_usd < 0:
        raise ValueError(f"cost_usd must be non-negative, got {cost_usd}")

    # Read time and the (env-derived) flush interval *outside* the lock so the
    # critical section is a few field updates only — this runs on every LLM call
    # across all SE worker threads, so a shorter hold reduces contention.
    now = time.time()
    interval = _flush_interval_s()
    flush_total: float | None = None
    with _lock:
        state = _states.setdefault(job_id, _CostState())
        state.cost_usd += cost_usd
        total = state.cost_usd
        if total > state.flushed_cost and (now - state.last_flushed_at) >= interval:
            state.flushed_cost = total
            state.last_flushed_at = now
            flush_total = total
    if flush_total is not None:
        _flush_to_job_store(job_id, flush_total)
    return total


def get_cost(job_id: str) -> float:
    """Return the current cumulative cost for ``job_id`` (``0.0`` if untracked)."""
    with _lock:
        state = _states.get(job_id)
        return state.cost_usd if state else 0.0


def flush(job_id: str) -> None:
    """Force a job-store flush of the current total for ``job_id`` (no-op if untracked)."""
    with _lock:
        state = _states.get(job_id)
        if state is None:
            return
        total = state.cost_usd
        state.flushed_cost = total
        state.last_flushed_at = time.time()
    _flush_to_job_store(job_id, total)


def reset(job_id: str) -> None:
    """Drop all accumulated state for ``job_id`` (e.g. on job retry/reset)."""
    with _lock:
        _states.pop(job_id, None)


def _flush_to_job_store(job_id: str, total: float) -> None:
    try:
        from software_engineering_team.shared.job_store import update_job

        update_job(job_id, cost_usd=round(total, 6))
    except Exception:
        logger.debug("failed to flush job cost for %s", job_id, exc_info=True)


# ---------------------------------------------------------------------------
# llm_service observer wiring
# ---------------------------------------------------------------------------

_registered = False
_register_lock = threading.Lock()


# The SE team family whose LLM cost this observer accumulates. An exact-match
# set (not a ``startswith`` prefix) so an unrelated team that merely shares the
# prefix — e.g. a hypothetical "software_engineering_tools" — is never captured.
# Both ids occur in practice: attribution sets ``software_engineering`` while the
# job store's ``JobServiceClient`` is constructed with ``software_engineering_team``.
_SE_TEAMS = frozenset({"software_engineering", "software_engineering_team"})


def _cost_observer(record: object) -> None:
    """Accumulate cost for SE-attributed LLM call records.

    Only records with a ``job_id`` whose ``team`` is in :data:`_SE_TEAMS`
    contribute, so this stays a no-op for other teams sharing the process.
    """
    job_id = getattr(record, "job_id", None)
    team = getattr(record, "team", "") or ""
    if not job_id or team not in _SE_TEAMS:
        return
    cost = getattr(record, "cost_usd", 0.0) or 0.0
    if cost <= 0:
        return
    try:
        add_cost(job_id, cost)
    except Exception:
        logger.debug("cost observer failed for job %s", job_id, exc_info=True)


def register_cost_observer() -> None:
    """Register the SE cost observer with :mod:`llm_service` (idempotent).

    Postconditions: after the first call, every subsequent SE-attributed LLM
        call accrues to its job's cumulative cost. Safe to call from app startup
        more than once.
    """
    global _registered
    with _register_lock:
        if _registered:
            return
        try:
            from llm_service import register_call_observer

            register_call_observer(_cost_observer)
            _registered = True
        except Exception:
            logger.warning("could not register SE cost observer", exc_info=True)
