"""Strategy Lab orchestration helpers shared by HTTP and Temporal paths.

Preconditions:
    Callers use the named helpers with the documented run-state and job-service contracts.
Postconditions:
    This module owns persistence, reconciliation, progress, and purge helper implementations;
    ``investment_team.api.main`` re-exports the same objects.
Invariants:
    Run-state synchronization uses ``strategy_lab.run_state`` as the source of truth.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

import httpx
from fastapi import HTTPException

from investment_team.models import StrategyLabRecord
from investment_team.strategy_lab import run_state
from shared.concurrency import parallel_map

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from investment_team.api.main import (
        _compute_signal_brief_snapshot,
        _finalize_strategy_lab_cycle_record,
        _is_strategy_lab_run_externally_stopped,
        _strategy_lab_external_terminal_status,
    )


def _snapshot_prior_records(*, reverse: bool = False) -> list[StrategyLabRecord]:
    """Locked read of the strategy-lab store, parsed and sorted by created_at.

    Preconditions:
        None — safe to call against an empty store.
    Postconditions:
        Returns a freshly parsed list of StrategyLabRecord, sorted by
        ``created_at`` ascending (oldest-first) by default, or descending
        (newest-first) when ``reverse=True``. Never returns None.
    """
    # Import the store lazily and outside the lock so loading ``api.main``
    # never contends with callers that already hold ``run_state.lock``.
    from investment_team.api.main import _strategy_lab_records

    with run_state.lock:
        raw = list(_strategy_lab_records.values())
    records = [StrategyLabRecord.parse_persisted(r) for r in raw]
    records.sort(key=lambda r: r.created_at, reverse=reverse)
    return records


STRATEGY_LAB_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "completed_with_errors", "failed", "cancelled", "interrupted"}
)


def _persist_run_state(run_id: str, state: Dict[str, Any], *, create: bool = False) -> None:
    """Write the run state to the job service so it survives restarts.

    Preconditions:
        - ``run_id`` is a non-empty ``str``.

    Postconditions:
        - ``create=True``: creates the job via ``client.create_job(...)``,
          defaulting ``status`` to ``"running"`` when ``state`` omits it (a
          fresh run's initial persist always has a real status in practice --
          see ``_build_run_state`` -- so this default is a pure safety net).
        - ``create=False`` (default): updates the existing job via
          ``client.update_job(...)``. When ``state`` includes a ``status``
          key, that value is written. When it does NOT (a progress-only delta
          -- e.g. the Temporal batch workflow's per-cycle/per-batch persists
          via ``persist_run_state_activity``, which routinely omit ``status``
          -- see ``_STRATEGY_LAB_PROGRESS_FIELDS``), NO ``status`` kwarg is
          passed at all, so the job service's own update path
          (``backend/job_service/db.py``: ``status`` is only written to the
          ``UPDATE`` when actually supplied) leaves the persisted status
          untouched. This previously defaulted a status-less update to
          ``"running"`` unconditionally, which could clobber a
          ``cancelled``/``failed``/``completed`` status a concurrent
          restart/resume/cancel had already persisted with a routine
          progress-only write.
        - Every key in ``state`` other than ``run_id``/``status`` is persisted
          as a field.

    Raises:
        - Whatever ``create_job``/``update_job`` raises (transport errors,
          HTTP error statuses, a ``RuntimeError`` for unconfigured
          ``JOB_SERVICE_URL``, ...) propagates uncaught. A durable-write
          failure must not be silently absorbed here: run/resume/restart
          dispatch a Temporal workflow immediately after calling this, and
          that workflow's own resume-from-restart safety depends on this
          write having actually landed. ``persist_run_state_activity``
          (``strategy_lab/temporal/activities.py``) delegates to this
          verbatim, so propagating also lets that activity's already-
          configured Temporal retry policy (``_ACTIVITY_RETRY`` in
          ``strategy_lab/temporal/workflows.py``) retry a transient failure
          instead of it going unnoticed. A caller with a genuine best-effort/
          never-raises contract (``_fail_strategy_lab_run``, or restart's
          rollback-on-collision persist) must catch and log locally instead
          of relying on this helper to swallow the error.
    """
    client = run_state.get_lab_run_job_client()
    fields = {k: v for k, v in state.items() if k not in ("run_id", "status")}
    if create:
        client.create_job(run_id, status=state.get("status", "running"), **fields)
    elif "status" in state:
        client.update_job(run_id, status=state["status"], **fields)
    else:
        client.update_job(run_id, **fields)


_STRATEGY_LAB_PROGRESS_FIELDS: tuple[str, ...] = (
    "completed_cycles",
    "skipped_cycles",
    "errored_cycles",
    "errored_details",
    "tracker_merge_error_count",
    "completed_record_ids",
    "current_batch",
    "completed_batches",
    "contiguous_cycles",
    "current_cycle",
)


def _reconcile_run_progress(run_id: str) -> None:
    """Sync run_id's in-memory progress counters + terminal status from the job service.

    Shared by ``list_strategy_lab_runs``, ``get_strategy_lab_run_status``, and
    ``stream_strategy_lab_run``'s connect-time snapshot so all three read
    surfaces see live progress instead of stale dispatch-time/last-resume
    values. Re-reads ``run_state.active_runs`` itself (rather than accepting a
    caller-supplied snapshot) so it always mutates whatever dict object is
    currently installed for ``run_id`` -- a resume/restart that installs a new
    dict between a caller's initial read and this call can't have its state
    clobbered by a stale reference.

    Preconditions:
        - ``run_id`` may or may not be present in ``run_state.active_runs``.

    Postconditions:
        - No-op (no job-service call) when ``run_id`` is absent from
          ``run_state.active_runs``, or its in-memory ``status`` is already in
          ``STRATEGY_LAB_TERMINAL_STATUSES``.
        - Otherwise calls ``client.get_job(run_id)`` at most once. When a
          persisted record is returned, every key in
          ``_STRATEGY_LAB_PROGRESS_FIELDS`` present in the record's data (via
          the ``job.get("data", job)`` fallback used elsewhere in this file,
          with an explicit ``"data": None`` treated the same as a missing
          ``"data"`` key) is copied onto ``run_state.active_runs[run_id]``; a key
          absent from the persisted record is left untouched (a sparse/early
          persisted record can never erase a more-complete in-memory value).
          ``status``/
          ``error`` are copied onto ``run_state.active_runs[run_id]`` only when the
          persisted status is itself in ``STRATEGY_LAB_TERMINAL_STATUSES``
          (unchanged from prior behavior).
        - All mutation happens under ``_lock`` and is guarded by re-checking,
          immediately before writing, both that the entry still exists and
          that its status is still non-terminal (the run may have been
          deleted, replaced by a resume/restart, or independently completed —
          e.g. by the worker's own finishing write — between the initial
          check and the job-service round trip); a terminal transition in
          that window makes this call a no-op rather than overwriting the
          fresher authoritative state with the (possibly pre-completion)
          fetched data. The network call itself is never made while holding
          ``_lock``.

    Raises:
        - None. Job-service construction/lookup failures are caught and
          logged via ``logger.debug("Job service reconciliation failed for
          run %s", run_id, exc_info=True)``; the run's in-memory state is
          left unchanged in that case.
    """
    with run_state.lock:
        state = run_state.active_runs.get(run_id)
    if not state or state.get("status") in STRATEGY_LAB_TERMINAL_STATUSES:
        return
    try:
        client = run_state.get_lab_run_job_client()
        persisted = client.get_job(run_id)
    except Exception:
        logger.debug("Job service reconciliation failed for run %s", run_id, exc_info=True)
        return
    if not persisted:
        return
    data = persisted.get("data", persisted)
    if data is None:
        # "data" can be explicitly present but None (distinct from being
        # absent, which the .get default above already handles) -- treat
        # both the same instead of letting a bare None reach the `field in
        # data` loop below and raise TypeError, violating this function's
        # "Raises: None" contract.
        data = persisted
    with run_state.lock:
        current = run_state.active_runs.get(run_id)
        if current is None or current.get("status") in STRATEGY_LAB_TERMINAL_STATUSES:
            # Another thread (e.g. the worker's own completion write) may have
            # removed the entry or advanced it to terminal while the
            # job-service round trip above was in flight. Either way, this
            # call's (possibly stale, pre-completion) fetch must not clobber
            # the fresher authoritative state with older progress counters.
            return
        for field in _STRATEGY_LAB_PROGRESS_FIELDS:
            if field in data:
                current[field] = data[field]
        js_status = persisted.get("status", "")
        if js_status in STRATEGY_LAB_TERMINAL_STATUSES:
            current["status"] = js_status
            current["error"] = persisted.get("error") or data.get("error")


def _job_progress_percent(completed: int, total: int) -> int:
    """Compute a job's completion percentage, tolerating a non-positive total.

    Preconditions:
        - ``completed`` and ``total`` are integers (possibly 0 or negative,
          e.g. from malformed persisted state).

    Postconditions:
        - Returns ``0`` when ``total <= 0`` (never divides by a non-positive
          total, so this can never raise ``ZeroDivisionError``).
        - Otherwise returns ``int((completed / total) * 100)``, clamped to
          ``0..100`` -- ``completed`` exceeding ``total`` or being negative
          (both possible from malformed persisted state) can never produce
          an out-of-range percentage.
    """
    if total <= 0:
        return 0
    return max(0, min(100, int((completed / total) * 100)))


_PURGE_MAX_WORKERS = 16

_PURGE_TIMEOUT_S = 120.0


def _delete_jobs_concurrently(
    client: Any,
    job_ids: list[str],
    *,
    max_workers: int = _PURGE_MAX_WORKERS,
) -> int:
    """Delete the given job ids via ``client.delete_job`` concurrently.

    Preconditions:
        - ``client`` exposes a thread-safe ``delete_job(job_id: str) -> truthy``.
        - ``job_ids`` contains the already-filtered ids to delete (no further
          filtering happens here).

    Postconditions:
        - Returns the count of ids for which ``delete_job`` returned a truthy
          value. The count equals the number of jobs successfully deleted and is
          independent of completion order (each task contributes its own 0/1 and
          the results are summed — no shared mutable counter).
        - A per-item ``delete_job`` exception is logged and counted as not-deleted,
          so a single failure never aborts the batch.
        - When ``job_ids`` is empty, returns 0 without spawning any threads.
    """
    if not job_ids:
        return 0

    def _delete_one(jid: str) -> int:
        # Isolate per-item failures: one job's delete raising (e.g. a transient
        # network error) must not abort the remaining concurrent deletions.
        try:
            return 1 if client.delete_job(jid) else 0
        except Exception:
            logger.warning("delete_job failed for %s; counted as not deleted", jid, exc_info=True)
            return 0

    workers = min(max_workers, len(job_ids))
    return sum(
        parallel_map(
            job_ids, _delete_one, max_workers=workers, preserve_order=False, skip_none=False
        )
    )


def _delete_paper_sessions_for_lab_record(lab_record_id: str) -> int:
    """Remove paper trading jobs whose payload references this lab record.

    Preconditions:
        - ``lab_record_id`` is the lab record id to match against each job's
          ``data["lab_record_id"]``.
        - ``JobServiceClient`` for ``investment_paper_trading_sessions`` is
          importable and thread-safe for concurrent ``delete_job`` calls.

    Postconditions:
        - Only jobs with a truthy ``job_id`` whose ``data`` is a dict and whose
          ``data["lab_record_id"]`` equals ``lab_record_id`` are deleted.
        - Returns the number of those jobs for which ``delete_job`` returned a
          truthy value. The count equals the number of jobs successfully deleted
          and is independent of the order in which the concurrent deletes finish.

    Raises:
        - ``HTTPException`` 503: ``list_jobs`` failed with ``httpx.HTTPError``
          (transport/HTTP) or ``RuntimeError`` (e.g. unconfigured
          ``JOB_SERVICE_URL``). Callers must leave lab state intact so a retry
          can re-attempt cleanup.
    """
    from job_service_client import JobServiceClient

    try:
        client = JobServiceClient(team="investment_paper_trading_sessions")
        jobs = client.list_jobs() or []
    except (httpx.HTTPError, RuntimeError):
        # Narrow environmental failures only — same pair sibling list endpoints
        # use. Soft-failing here would orphan paper sessions after the lab
        # record is deleted; fail closed with 503 instead.
        logger.warning(
            "list_jobs failed for paper trading sessions; cleanup unavailable",
            exc_info=True,
        )
        raise HTTPException(
            status_code=503,
            detail="Paper-trading session cleanup is temporarily unavailable; retry later.",
        ) from None

    matching_ids: list[str] = []
    for job in jobs:
        jid = job.get("job_id")
        if not jid:
            continue
        payload = job.get("data")
        if not isinstance(payload, dict):
            continue
        if payload.get("lab_record_id") != lab_record_id:
            continue
        matching_ids.append(str(jid))

    return _delete_jobs_concurrently(client, matching_ids)


def _purge_strategy_lab_job_storage() -> dict[str, Optional[int]]:
    """Delete strategy lab jobs plus all paper-trading session jobs for this team.

    Preconditions:
        - ``JobServiceClient`` is importable and each per-team client is
          thread-safe for concurrent ``delete_job`` calls (the four teams are
          processed in parallel, and the deletes within each team are too).

    Postconditions:
        - ``deleted_lab_records`` counts ``investment_strategy_lab_records`` jobs
          with a truthy ``job_id`` that ``delete_job`` removed.
        - ``deleted_lab_strategies`` counts ``investment_strategies`` jobs whose
          id starts with ``strat-lab-`` that ``delete_job`` removed.
        - ``deleted_lab_backtests`` counts ``investment_backtests`` jobs whose id
          starts with ``bt-lab-`` that ``delete_job`` removed.
        - ``deleted_paper_trading_sessions`` counts
          ``investment_paper_trading_sessions`` jobs with a truthy ``job_id``
          that ``delete_job`` removed.
        - Each count equals the number of matching jobs the corresponding unit
          *reported* deleting within the shared deadline, and is independent
          of the order in which the concurrent units/deletes finish; the
          returned dict always has exactly these four keys.
        - A unit that does not finish within the shared deadline
          (``_PURGE_TIMEOUT_S``) is reported as ``None`` — not ``0`` — even
          though its background thread keeps running (see
          ``pool.shutdown(wait=False, ...)`` below) and may go on to delete
          some or all of its matching jobs asynchronously, after this
          function (and the endpoint calling it) has already returned.
          ``None`` means "unknown, still in flight"; only a non-``None`` int
          is a confirmed count. Callers must not treat ``None`` as ``0``.
    """
    from job_service_client import JobServiceClient

    def _purge_all(team: str) -> int:
        """Delete every truthy-id job for ``team`` (no id-prefix filter)."""
        client = JobServiceClient(team=team)
        ids = [str(jid) for job in (client.list_jobs() or []) if (jid := job.get("job_id"))]
        return _delete_jobs_concurrently(client, ids)

    def _purge_prefixed(team: str, prefix: str) -> int:
        """Delete jobs for ``team`` whose id starts with ``prefix``."""
        client = JobServiceClient(team=team)
        ids = [
            jid
            for job in (client.list_jobs() or [])
            if (jid := str(job.get("job_id") or "")).startswith(prefix)
        ]
        return _delete_jobs_concurrently(client, ids)

    units: dict[str, concurrent.futures.Future[int]] = {}
    # NB: not a `with` block — the context manager's exit calls shutdown(wait=True),
    # which would re-introduce the very unbounded join the deadline below avoids.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    try:
        units["deleted_lab_records"] = pool.submit(_purge_all, "investment_strategy_lab_records")
        units["deleted_lab_strategies"] = pool.submit(
            _purge_prefixed, "investment_strategies", "strat-lab-"
        )
        units["deleted_lab_backtests"] = pool.submit(
            _purge_prefixed, "investment_backtests", "bt-lab-"
        )
        units["deleted_paper_trading_sessions"] = pool.submit(
            _purge_all, "investment_paper_trading_sessions"
        )

        # Collect against a single shared deadline so the whole fan-out is bounded
        # (per-unit timeouts would let each unit reset the clock). A unit that
        # overruns is reported as None (unknown, not a confirmed 0 — its worker
        # thread is still running and may delete jobs after this call returns,
        # see the docstring); a unit that *raises* still propagates (preserving
        # the prior error contract).
        deadline = time.monotonic() + _PURGE_TIMEOUT_S
        results: dict[str, Optional[int]] = {}
        for key, future in units.items():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                results[key] = future.result(timeout=remaining)
            except concurrent.futures.TimeoutError:
                logger.warning(
                    "purge unit %s did not finish within %.0fs; reported as unknown (None)",
                    key,
                    _PURGE_TIMEOUT_S,
                )
                results[key] = None
        return results
    finally:
        # Never block on a straggler: in-flight HTTP deletes are themselves bounded
        # by the client's per-request timeout, so abandoning the worker thread leaks
        # nothing unbounded. cancel_futures drops any unit that hasn't started.
        pool.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "_persist_run_state",
    "_STRATEGY_LAB_PROGRESS_FIELDS",
    "_reconcile_run_progress",
    "STRATEGY_LAB_TERMINAL_STATUSES",
    "_job_progress_percent",
    "_snapshot_prior_records",
    "_delete_jobs_concurrently",
    "_delete_paper_sessions_for_lab_record",
    "_purge_strategy_lab_job_storage",
    "_PURGE_MAX_WORKERS",
    "_PURGE_TIMEOUT_S",
    "_compute_signal_brief_snapshot",
    "_is_strategy_lab_run_externally_stopped",
    "_strategy_lab_external_terminal_status",
    "_finalize_strategy_lab_cycle_record",
]
_LAZY_EXPORTS = frozenset(
    {
        "_compute_signal_brief_snapshot",
        "_is_strategy_lab_run_externally_stopped",
        "_strategy_lab_external_terminal_status",
        "_finalize_strategy_lab_cycle_record",
    }
)


def __getattr__(name: str):
    """Resolve remaining Temporal-hot re-exports from ``api.main`` lazily.

    Preconditions:
        ``name`` is a non-empty attribute name requested on this module.
    Postconditions:
        Returns the matching ``api.main`` object for a remaining lazy export,
        otherwise raises ``AttributeError`` without importing ``api.main``.
    """
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from investment_team.api import main as _api_main

    return getattr(_api_main, name)


def __dir__() -> list[str]:
    """Return module attributes including every public export.

    Preconditions:
        None.
    Postconditions:
        Returns a sorted list containing all names in ``__all__``.
    """
    return sorted(set(globals()) | set(__all__))
