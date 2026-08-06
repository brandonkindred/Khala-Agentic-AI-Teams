"""Strategy Lab in-memory run-state store, shared by ``api.main`` (FastAPI
routes) and ``strategy_lab.temporal.start_workflow`` (the Temporal dispatch
path).

Extracted from ``investment_team.api.main`` so both callers depend on one
public store instead of the Temporal path reaching into ``api.main``'s
private module state.

Note: ``lock`` is the same mutex ``api.main`` uses to guard several other
pieces of unrelated module-level state (profiles/backtests/strategy-lab-record
dicts, live paper-trading polling, etc.) — it predates this extraction and was
never exclusive to the run registry. Splitting it into per-concern locks is a
larger change and out of scope here; this module only relocates where the
single shared lock is constructed.

``async_lock`` is a companion ``asyncio.Lock`` for async callers (notably the
strategy-lab SSE stream) that need to read ``active_runs`` without blocking the
event loop on the threading ``lock``. Sync writers continue to use ``lock``;
async readers use ``async_lock``. Individual ``dict.get`` / pointer reads are
atomic under the GIL, so the companion lock's job is serializing concurrent
async readers without stalling other coroutines — not replacing ``lock`` for
multi-key sync mutations.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, Optional

lock = threading.Lock()
async_lock = asyncio.Lock()

# In-memory state for active strategy lab runs (keyed by run_id).
active_runs: Dict[str, Dict[str, Any]] = {}

# Per-run_id transition lock, serializing the run/resume/restart
# check-then-write critical section in api.main so two concurrent
# transitions for the SAME run_id can't both pass a stale check before
# either writes state (see #4028). Grows monotonically (never evicted) —
# see acquire_run_transition_lock's Invariants for why that's acceptable.
_run_transition_locks: Dict[str, threading.Lock] = {}


def acquire_run_transition_lock(run_id: str) -> Optional[threading.Lock]:
    """Try to acquire run_id's run/resume/restart transition lock, non-blocking.

    Serializes same-run_id run/resume/restart transitions in ``api.main`` so
    two concurrent requests for the same run_id can't both pass a
    check-then-act window (e.g. both pass ``_ensure_no_active_run()`` before
    either writes ``active_runs[run_id]``) and race to mutate/dispatch
    against the same deterministic Temporal workflow id (#4028).

    Preconditions:
        - Caller does not already hold ``lock`` (the shared module lock) or
          this run_id's transition lock.

    Postconditions:
        - Returns the acquired ``threading.Lock`` (now held by the caller —
          the caller MUST release it, e.g. via ``try/finally:
          run_lock.release()``) when no other transition for this run_id is
          currently in flight.
        - Returns ``None`` (holds nothing) when another transition for this
          run_id is already in flight. Never blocks.

    Invariants:
        - Exactly one ``threading.Lock`` instance ever exists per distinct
          run_id — registry insertion is itself serialized via ``lock``, so
          concurrent first-callers for the same run_id always contend for
          the SAME lock object, never two different ones.
        - ``_run_transition_locks`` only grows (entries are never evicted).
          Acceptable here: run_ids are minted by a human-triggered action
          (not attacker/request-volume-controlled), the existing global
          ``_ensure_no_active_run()`` already caps the system to one
          *active* run at a time, each entry costs one ``threading.Lock``
          (tens of bytes), and the registry — like ``active_runs`` — is
          in-memory only and resets on process restart.
    """
    with lock:
        run_lock = _run_transition_locks.setdefault(run_id, threading.Lock())
    return run_lock if run_lock.acquire(blocking=False) else None


def get_lab_run_job_client():
    """Return a JobServiceClient scoped to strategy lab runs."""
    from job_service_client import JobServiceClient

    return JobServiceClient(team="investment_strategy_lab_runs")


def normalize_persisted_job(
    job: Dict[str, Any], *, fallback_status: str, run_id: Optional[str] = None
) -> Dict[str, Any]:
    """Normalize a job-service record into an ``active_runs``-shaped state dict.

    Centralizes the ``run_id``/``status`` fallback logic repeated across every
    call site that merges a persisted job-service record into the in-memory
    run-state shape, so a future change to that shape only needs updating here.

    Preconditions:
        ``job`` is a job-service record; when its ``"data"`` key holds a
        dict, that dict holds the run-state fields, otherwise ``job`` itself
        is treated as the state dict -- this covers ``"data"`` being absent,
        ``None`` (a key present but null, e.g. a not-yet-populated persisted
        record), or any other non-dict value.

    Postconditions:
        Returns ``job["data"]`` when that's a dict, else ``job`` itself --
        the same dict object either way, not a copy -- with ``run_id`` set to
        ``run_id`` when given, else derived from ``job.get("job_id") or
        job.get("run_id", "")``; and ``status`` defaulted (via
        ``setdefault``, so an existing ``status`` in the returned dict wins)
        to ``job.get("status", fallback_status)``. Never raises ``TypeError``
        from a null/malformed ``"data"`` value.
    """
    raw_data = job.get("data")
    data = raw_data if isinstance(raw_data, dict) else job
    data["run_id"] = run_id if run_id is not None else (job.get("job_id") or job.get("run_id", ""))
    data.setdefault("status", job.get("status", fallback_status))
    return data


def load_run_from_job_service(run_id: str) -> Optional[Dict[str, Any]]:
    """Try to load a run state from the job service (fallback when not in ``active_runs``).

    Preconditions:
        - ``run_id`` may or may not name a persisted job.

    Postconditions:
        - Returns the normalized state dict (via ``normalize_persisted_job``)
          when the job service holds a record for ``run_id``.
        - Returns ``None`` when the job service reports no record — the job
          service's ``GET /jobs/{team}/{job_id}`` always responds 200 with
          ``job: null`` for a missing job (see ``backend/job_service/main.py``),
          never an error, so this is the only "not found" outcome and it
          never involves an exception.

    Raises:
        - Whatever ``get_lab_run_job_client().get_job(run_id)`` raises
          (transport errors, HTTP error statuses, a ``RuntimeError`` for
          unconfigured ``JOB_SERVICE_URL``, etc.) propagates uncaught. These
          are genuine failures, not "run not found" — swallowing them here
          previously made a transient job-service outage indistinguishable
          from a nonexistent run, which corrupted resume behavior downstream
          (``rehydrate_active_run_offset``/``get_resume_seed_counters`` would
          silently default to 0/empty instead of the real persisted offset).
    """
    client = get_lab_run_job_client()
    job = client.get_job(run_id)
    if job:
        return normalize_persisted_job(job, fallback_status="completed", run_id=run_id)
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

    Raises:
        - Whatever ``load_run_from_job_service`` raises when ``run_id`` is
          absent from ``active_runs`` and the job-service lookup itself fails
          (as opposed to genuinely finding no record) — see its docstring.
          Not caught here so callers can fail closed rather than silently
          treating a job-service outage as "no state."
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

    Raises:
        - Whatever ``get_run_state`` raises when the job-service lookup
          itself fails (as opposed to genuinely finding no record) — not
          caught here, so a resume dispatched during a job-service outage
          fails closed instead of silently restarting from offset 0.
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


def get_resume_seed_counters(run_id: str) -> Dict[str, Any]:
    """Return the skip/error/tracker-merge counters a resumed run should seed with.

    Sibling to ``rehydrate_active_run_offset``: called from the Temporal batch
    workflow's input builder so a resumed run's ``StrategyLabBatchWorkflow``
    continues accumulating these counters instead of restarting them at zero —
    mirroring the same carry-forward ``resume_strategy_lab_run`` already
    performs on ``_active_runs``/persisted state before dispatch.

    Preconditions:
        - ``run_id`` names a strategy-lab run (may not exist).

    Postconditions:
        - Returns a dict with keys ``skipped_cycles`` (int), ``errored_cycles``
          (int), ``errored_details`` (a fresh list, not aliasing any stored
          list), ``tracker_merge_error_count`` (int), and
          ``completed_record_ids`` (a fresh list, not aliasing any stored
          list) — carrying forward the pre-resume completed record ids so the
          durable ``completed_record_ids`` field isn't truncated to only
          post-resume records the next time the batch workflow persists it
          (``update_job`` replaces the field's value wholesale, it does not
          append). Read from ``get_run_state(run_id)``. A fresh/unknown run
          (no persisted state) or a missing/malformed individual field
          defaults to ``0``/``0``/``[]``/``0``/``[]`` respectively.

    Raises:
        - Whatever ``get_run_state`` raises when the job-service lookup
          itself fails (as opposed to genuinely finding no record) — not
          caught here, so a resume dispatched during a job-service outage
          fails closed instead of silently seeding zero/empty counters.
    """
    state = get_run_state(run_id) or {}

    def _int(key: str) -> int:
        try:
            return max(0, int(state.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _fresh_list(key: str) -> list:
        value = state.get(key)
        return list(value) if isinstance(value, list) else []

    return {
        "skipped_cycles": _int("skipped_cycles"),
        "errored_cycles": _int("errored_cycles"),
        "errored_details": _fresh_list("errored_details"),
        "tracker_merge_error_count": _int("tracker_merge_error_count"),
        "completed_record_ids": _fresh_list("completed_record_ids"),
    }


__all__ = [
    "lock",
    "async_lock",
    "active_runs",
    "acquire_run_transition_lock",
    "get_lab_run_job_client",
    "load_run_from_job_service",
    "get_run_state",
    "rehydrate_active_run_offset",
    "get_resume_seed_counters",
]
