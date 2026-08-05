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
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from job_service_client import JobServiceClient

logger = logging.getLogger(__name__)

lock = threading.Lock()

# The fencing generation a fresh/never-restarted run presents, and the value
# every "generation" read/default falls back to when the field is absent,
# None, or empty. Shared by this module, `api.main`, and
# `strategy_lab.temporal.workflows` so a future change to the default (e.g.
# to 0 or a sentinel) only needs updating in one place.
DEFAULT_FENCING_GENERATION = 1

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


def get_run_generation_strict(run_id: str, *, client: Optional["JobServiceClient"] = None) -> int:
    """Return run_id's current fencing generation, propagating durable-read failures.

    The sole authoritative read path for a run's fencing generation. Used by
    the two Temporal fencing checks (``persist_run_state_activity``/
    ``finalize_cycle_record_activity``) AND by ``resume_strategy_lab_run``
    (to carry the generation forward from the durable store rather than a
    possibly-stale ``active_runs`` snapshot — see its own call site for why).
    ``run_strategy_lab``/``restart_strategy_lab_run`` don't need it: the
    former always starts at the default, and the latter mints a fresh value
    via an atomic increment rather than reading one.

    Deliberately reads the DURABLE job store directly rather than going
    through ``get_run_state`` (which prefers the process-local ``active_runs``
    cache): the Temporal fencing checks run inside a worker, which may be a
    different process from the API server that handled a restart, and
    ``resume_strategy_lab_run`` has the identical cross-process staleness
    risk in a multi-replica API deployment. Trusting a cached, possibly stale
    in-memory generation here would silently defeat fencing in either case.

    Fails CLOSED: a durable-read failure raises (propagates to the caller)
    rather than defaulting to the most permissive generation, which could
    otherwise mask a genuinely higher current generation and accept a write
    that should have been fenced out.

    Preconditions:
        - ``run_id`` names a strategy-lab run (may not exist). ``client``,
          when provided, is used instead of ``get_lab_run_job_client()`` —
          lets a caller that already has its own consistently-mockable
          client-fetch alias (e.g. ``api.main``'s ``_get_lab_run_job_client``)
          supply that same instance, rather than this function reaching for
          a separate one a test wouldn't have patched.

    Postconditions:
        - Returns the run's durably persisted ``generation`` field, clamped
          up to at least ``DEFAULT_FENCING_GENERATION`` (so a persisted value
          below it, e.g. ``0`` or a negative number, is treated the same as
          a missing one rather than returned verbatim — the generation
          sequence is defined to start at ``DEFAULT_FENCING_GENERATION`` and
          only increase, so a sub-default persisted value already indicates
          the same kind of uninitialized/corrupt state a missing field
          would). Returns exactly ``DEFAULT_FENCING_GENERATION`` for a run
          with no ``generation`` field yet, a ``None``/empty value, or a run
          that does not exist (all "not a read failure" cases
          indistinguishable from a fresh/never-restarted run). Raises
          ``ValueError`` when the persisted ``generation`` field is present
          but unparseable as an int (e.g. a non-numeric string, list, or
          dict) — that's durable-record corruption, not a legitimate
          "missing field" case, and returning the permissive default ``1``
          for it would let a stale pre-restart activity (carrying token
          ``1``) pass ``check_fencing_token`` (which accepts
          ``provided_token >= current_token``), reopening the exact race
          this module's fencing exists to close. A ``bool`` or ``float``
          persisted value is treated the same as unparseable (raises
          ``ValueError``) rather than silently coerced via ``int(...)``,
          which would truncate a float or accept a bool as a seemingly
          valid generation -- a numeric *string* (e.g. ``"5"``) is still
          accepted, only the raw Python type is restricted. Also raises
          whatever the underlying job-service client raises on a transport
          failure — callers must let both propagate, not swallow them.
    """
    client = client or get_lab_run_job_client()
    job = client.get_job(run_id)
    if not job:
        return DEFAULT_FENCING_GENERATION
    # Same null/malformed-"data" coercion as normalize_persisted_job: a
    # "data" key present but None (or any other non-dict value) must fall
    # back to the top-level job dict, not crash with AttributeError.
    raw_data = job.get("data")
    data = raw_data if isinstance(raw_data, dict) else job
    raw_generation = data.get("generation", DEFAULT_FENCING_GENERATION)
    if raw_generation is None or raw_generation == "":
        return DEFAULT_FENCING_GENERATION
    if isinstance(raw_generation, bool) or isinstance(raw_generation, float):
        raise ValueError(f"Invalid persisted generation for run {run_id}: {raw_generation!r}")
    try:
        # A persisted value below DEFAULT_FENCING_GENERATION is treated as
        # uninitialized/corrupt, not returned verbatim: the generation
        # sequence starts at DEFAULT_FENCING_GENERATION and only increases,
        # so clamping here (rather than raising, unlike the unparseable case
        # below) prevents a stale pre-restart activity from passing
        # check_fencing_token against an implausibly low durable value.
        return max(DEFAULT_FENCING_GENERATION, int(raw_generation))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid persisted generation for run {run_id}: {raw_generation!r}"
        ) from exc


def get_lab_run_job_client() -> "JobServiceClient":
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
        from a null/malformed ``"data"`` value (falls back to ``job`` itself
        instead of trying to use a non-dict).
    """
    raw_data = job.get("data")
    data = raw_data if isinstance(raw_data, dict) else job
    data["run_id"] = run_id if run_id is not None else (job.get("job_id") or job.get("run_id", ""))
    data.setdefault("status", job.get("status", fallback_status))
    return data


def _load_run_from_job_service_strict(run_id: str) -> Optional[Dict[str, Any]]:
    """Load a run's durable state, propagating job-service read failures.

    Shared read logic for ``load_run_from_job_service`` (lenient) and
    ``get_run_state_strict`` (fail-closed). The job service's ``GET
    /jobs/{team}/{job_id}`` route always returns ``200`` with a null job for
    a missing id (see ``job_service/main.py``'s ``get_job``) rather than a
    404, so the only way this function raises is a genuine transport/read
    failure -- a missing job is a normal, non-raising ``None`` return.

    Preconditions:
        - ``run_id`` names a strategy-lab run (may not exist).
    Postconditions:
        - Returns the persisted state via ``normalize_persisted_job`` -- the
          same dict object as ``job["data"]`` (or ``job`` itself), not a
          copy; see its own docstring -- or ``None`` when the job genuinely
          does not exist. Raises whatever the underlying job-service client
          raises on a transport failure.
    """
    client = get_lab_run_job_client()
    job = client.get_job(run_id)
    if not job:
        return None
    return normalize_persisted_job(job, fallback_status="completed", run_id=run_id)


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
          Now equivalent to ``_load_run_from_job_service_strict``; kept as a
          separate public name since it predates that helper and is the
          name every existing caller/test already uses.
    """
    return _load_run_from_job_service_strict(run_id)


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
          treating a job-service outage as "no state." Now equivalent to
          ``get_run_state_strict``; kept as a separate public name since it
          predates that helper and is the name every existing caller/test
          already uses.
    """
    with lock:
        state = active_runs.get(run_id)
    return state if state is not None else load_run_from_job_service(run_id)


def get_run_state_strict(run_id: str) -> Optional[Dict[str, Any]]:
    """Like ``get_run_state``, but propagates durable-read failures instead of
    swallowing them.

    Used by dispatch-time callers (``rehydrate_active_run_offset``,
    ``get_resume_seed_counters``) that run synchronously inside
    ``_dispatch_strategy_lab_run``'s broad exception boundary (``api/main.py``),
    which already maps ANY exception raised during dispatch to a 503 + failed
    run. Letting a transient job-service outage propagate here turns it into
    that retryable 503 instead of ``get_run_state``'s lenient ``None`` --
    which, for these two callers specifically, would otherwise be
    indistinguishable from "no prior state" and cause a resumed run to
    silently replay already-completed cycles from ``start_cycle_offset=0``
    (Temporal would see a successful activity/call, not a failure to retry).

    Preconditions:
        - ``run_id`` names a strategy-lab run (may not exist).
    Postconditions:
        - Returns the in-memory state when present, otherwise the persisted
          state from the job store, or ``None`` when neither exists (not a
          read failure). Raises whatever the underlying job-service client
          raises on a durable-read failure. Does not mutate ``active_runs``.
          Delegates to ``get_run_state`` (now equivalent, since it also
          fails closed) rather than duplicating its body, so a test/caller
          that patches either ``get_run_state`` or ``load_run_from_job_service``
          observes the same behavior through this name too.
    """
    return get_run_state(run_id)


def rehydrate_active_run_offset(run_id: str) -> int:
    """Ensure ``active_runs[run_id]`` exists and return the resume cycle offset.

    Called synchronously from ``build_strategy_lab_batch_input`` during
    dispatch (inside ``_dispatch_strategy_lab_run``'s exception boundary) so
    the strategy-lab worker behaves identically whether it runs in the
    dispatching process or a fresh one after a restart/retry: it rehydrates
    the in-memory run entry from the durable job store (so ``_update_run``
    can persist progress) and derives the offset from the persisted
    contiguous-cycle count (so a retry resumes instead of replaying
    completed cycles).

    Preconditions:
        - ``run_id`` names a strategy-lab run whose state was persisted via
          ``_persist_run_state`` before dispatch.

    Postconditions:
        - ``active_runs[run_id]`` is populated when durable state exists.
        - Returns the number of contiguous completed cycles to pass as
          ``start_cycle_offset`` (``0`` for a fresh or restarted run, or when
          no durable state is found). Uses ``get_run_state_strict`` (now
          equivalent to ``get_run_state`` itself, which also fails closed --
          see its docstring) deliberately: a transient job-service outage
          must raise here rather than silently defaulting to ``0``, which
          would otherwise cause a resumed run to replay already-completed
          cycles with no error Temporal could retry on. Raises whatever the
          underlying job-service client raises on a durable-read failure;
          the caller's caller (``_dispatch_strategy_lab_run``) maps that to
          a 503 + failed run. Also raises ``ValueError`` when durable state
          exists but its ``contiguous_cycles`` field is present and
          unparseable as an int (e.g. a non-numeric string) — for the
          identical replay-risk reason the durable-read failure above
          raises rather than defaulting: a silent ``0`` here is
          indistinguishable from a genuinely fresh run and would replay
          already-completed cycles.
    """
    state = get_run_state_strict(run_id)
    if state is not None:
        # setdefault: never clobber a live in-memory entry with the durable copy.
        with lock:
            active_runs.setdefault(run_id, state)
    if not state:
        return 0
    try:
        return max(0, int(state.get("contiguous_cycles", 0) or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid persisted contiguous_cycles for run {run_id}: "
            f"{state.get('contiguous_cycles')!r}"
        ) from exc


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
          append). Read from ``get_run_state_strict(run_id)`` -- like
          ``rehydrate_active_run_offset``, deliberately (now equivalent to
          ``get_run_state`` itself, which also fails closed -- see its
          docstring), so a transient job-service outage raises (mapped
          by ``_dispatch_strategy_lab_run`` to a 503 + failed run) rather than
          being indistinguishable from a fresh/unknown run and silently
          resetting these counters to zero. A fresh/unknown run (no
          persisted state) or a missing/malformed individual field defaults
          to ``0``/``0``/``[]``/``0``/``[]`` respectively.
    """
    state = get_run_state_strict(run_id) or {}

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
    "active_runs",
    "acquire_run_transition_lock",
    "get_run_generation_strict",
    "get_lab_run_job_client",
    "load_run_from_job_service",
    "get_run_state",
    "get_run_state_strict",
    "rehydrate_active_run_offset",
    "get_resume_seed_counters",
]
