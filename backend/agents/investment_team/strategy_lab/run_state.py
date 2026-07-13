"""Strategy Lab in-memory run-state store, shared by ``api.main`` (thread-mode
worker + FastAPI routes) and ``strategy_lab.temporal.start_workflow`` (the
Temporal dispatch path).

Extracted from ``investment_team.api.main`` so both callers depend on one
public store instead of the Temporal path reaching into ``api.main``'s
private module state.

Note: ``lock`` is the same mutex ``api.main`` uses to guard several other
pieces of unrelated module-level state (profiles/backtests/strategy-lab-record
dicts, live paper-trading polling, etc.) — it predates this extraction and was
never exclusive to the run registry. Splitting it into per-concern locks is a
larger change and out of scope here; this module only relocates where the
single shared lock is constructed.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

lock = threading.Lock()

# In-memory state for active strategy lab runs (keyed by run_id).
active_runs: Dict[str, Dict[str, Any]] = {}


def get_lab_run_job_client():
    """Return a JobServiceClient scoped to strategy lab runs."""
    from job_service_client import JobServiceClient

    return JobServiceClient(team="investment_strategy_lab_runs")


def load_run_from_job_service(run_id: str) -> Optional[Dict[str, Any]]:
    """Try to load a run state from the job service (fallback when not in ``active_runs``)."""
    try:
        client = get_lab_run_job_client()
        job = client.get_job(run_id)
        if job:
            data = job.get("data", job)
            data["run_id"] = run_id
            data.setdefault("status", job.get("status", "completed"))
            return data
    except Exception:
        pass
    return None


def get_run_state(run_id: str) -> Optional[Dict[str, Any]]:
    """Return a strategy-lab run's state from ``active_runs``, else the job store.

    Centralizes the "live in-memory entry, else durable fallback" read shared by
    the resume/restart endpoints and the Temporal-activity helpers.

    Preconditions:
        - ``run_id`` names a strategy-lab run (may not exist).

    Postconditions:
        - Returns the in-memory state when present, otherwise the persisted state
          from the job store, or ``None`` when neither exists. Does not mutate
          ``active_runs``.
    """
    with lock:
        state = active_runs.get(run_id)
    return state if state is not None else load_run_from_job_service(run_id)


def rehydrate_active_run_offset(run_id: str) -> int:
    """Ensure ``active_runs[run_id]`` exists and return the resume cycle offset.

    Called from the Temporal activity so the strategy-lab worker behaves
    identically whether it runs in the dispatching process or a fresh one after
    a restart/retry: it rehydrates the in-memory run entry from the durable job
    store (so ``_update_run`` can persist progress) and derives the offset from
    the persisted contiguous-cycle count (so a retry resumes instead of
    replaying completed cycles).

    Preconditions:
        - ``run_id`` names a strategy-lab run whose state was persisted via
          ``_persist_run_state`` before dispatch.

    Postconditions:
        - ``active_runs[run_id]`` is populated when durable state exists.
        - Returns the number of contiguous completed cycles to pass as
          ``start_cycle_offset`` (``0`` for a fresh or restarted run, or when no
          durable state is found).
    """
    state = get_run_state(run_id)
    if state is not None:
        # setdefault: never clobber a live in-memory entry with the durable copy.
        with lock:
            active_runs.setdefault(run_id, state)
    if not state:
        return 0
    try:
        return max(0, int(state.get("contiguous_cycles", 0) or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "lock",
    "active_runs",
    "get_lab_run_job_client",
    "load_run_from_job_service",
    "get_run_state",
    "rehydrate_active_run_offset",
]
