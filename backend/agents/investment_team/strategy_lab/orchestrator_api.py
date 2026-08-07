"""Strategy Lab orchestration API helpers (run/cycle helpers).

Target home for Strategy Lab helpers that previously lived in
``investment_team.api.main``. This module owns run-state I/O helpers and keeps
only the still-coupled Temporal helpers as lazy re-exports.

See ``ORCHESTRATOR_API_BOUNDARIES.md`` for the full helper inventory, call
graph, module ownership, and shared-state access plan.

Preconditions:
    Callers import named helpers from this module (prefer lazy import inside
    Temporal activities so ``api.main`` is not loaded at worker import time).
Postconditions:
    Run-state I/O helpers are defined directly in this module. Deferred names
    resolve to the same callable currently defined on ``investment_team.api.main``
    via lazy attribute lookup. Behavior is unchanged from importing those
    symbols directly from ``api.main``.
Invariants:
    ``__all__`` is the complete public helper surface for this module; adding
    a name requires updating the boundaries note.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pydantic import ValidationError

from investment_team.strategy_lab.run_state import active_runs as _active_runs
from investment_team.strategy_lab.run_state import (
    get_lab_run_job_client as _get_lab_run_job_client,
)
from investment_team.strategy_lab.run_state import lock as _lock

if TYPE_CHECKING:
    from investment_team.api.main import (
        StrategyLabCycleProgress,
        StrategyLabRunStatusResponse,
        _compute_signal_brief_snapshot,
        _finalize_strategy_lab_cycle_record,
        _is_strategy_lab_run_externally_stopped,
        _snapshot_prior_records,
        _strategy_lab_external_terminal_status,
    )

logger = logging.getLogger(__name__)

STRATEGY_LAB_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "completed_with_errors", "failed", "cancelled", "interrupted"}
)


def _api_main_attr(name: str, default: Any) -> Any:
    """Return an already-imported ``api.main`` attribute override when present.

    Preconditions:
        ``name`` is an attribute name and ``default`` is the run-state-backed
        implementation this module normally owns.
    Postconditions:
        Returns ``default`` when ``api.main`` has not been imported or has not
        replaced the alias. Returns the ``api.main`` attribute otherwise. Never
        imports ``api.main``.
    """
    api_main = sys.modules.get("investment_team.api.main")
    if api_main is None:
        return default
    return getattr(api_main, name, default)


def _active_run_store() -> Dict[str, Dict[str, Any]]:
    """Return the active-run mapping, honoring ``api.main`` test monkeypatches.

    Preconditions:
        None.
    Postconditions:
        Returns the run-state mapping by default, or the current
        ``api.main._active_runs`` alias when an already-imported test/module has
        intentionally replaced it. Never imports ``api.main``.
    """
    return _api_main_attr("_active_runs", _active_runs)


def _lab_run_job_client_factory() -> Any:
    """Return the lab-run job-client factory, honoring ``api.main`` monkeypatches.

    Preconditions:
        None.
    Postconditions:
        Returns ``run_state.get_lab_run_job_client`` by default, or the current
        ``api.main._get_lab_run_job_client`` alias when an already-imported
        test/module has intentionally replaced it. Never imports ``api.main``.
    """
    return _api_main_attr("_get_lab_run_job_client", _get_lab_run_job_client)


def _run_state_to_response(state: Dict[str, Any]) -> "StrategyLabRunStatusResponse":
    """Convert an ``_active_runs`` entry to a Pydantic response.

    Preconditions:
        ``state`` is an ``_active_runs`` entry (or a persisted job dict of the
        same shape). Every field, including ``run_id``, ``status``,
        ``started_at``, and ``total_cycles``, is read with a default, so a
        partially-populated merged/resume/snapshot dict (e.g. a job-service
        entry with unexpected gaps) is safe -- this defends the same
        currently-enforced-by-construction invariant (every writer of
        ``_active_runs`` sets ``run_id``) that ``status`` already defended
        against, rather than assuming it can never be violated.
    Postconditions:
        Returns a ``StrategyLabRunStatusResponse`` mirroring ``state`` field for
        field, defaulting each absent field to its response default
        (``"unknown"`` status, ``""`` started_at, ``0`` numeric fields/empty
        lists — including ``tracker_merge_error_count`` (``0`` when absent)) —
        and mapping a present ``current_cycle`` dict to a
        ``StrategyLabCycleProgress`` (``None`` when absent). ``batch_size`` is
        the one field that deliberately does NOT fall back to the model's
        structural default of ``1``: an absent ``batch_size`` means this is a
        legacy single-batch record predating multi-batch support, so it falls
        back to ``total_cycles`` (the whole run was one batch) rather than to
        ``1`` (which would misreport it as ``total_cycles`` batches of size 1).
        Pure: ``state`` is not mutated. A ``current_cycle`` that is not a
        dict, or is a dict whose fields fail ``StrategyLabCycleProgress``
        validation, degrades to ``None`` instead of raising -- ``state`` can
        be job-service data reconciled with no shape check (see
        ``_reconcile_run_progress``), so it is not assumed well-formed.
    """
    from investment_team.api.main import StrategyLabCycleProgress, StrategyLabRunStatusResponse

    cc = state.get("current_cycle")
    current_cycle: Optional[StrategyLabCycleProgress] = None
    if isinstance(cc, dict):
        try:
            current_cycle = StrategyLabCycleProgress(**cc)
        except ValidationError:
            current_cycle = None
    return StrategyLabRunStatusResponse(
        run_id=state.get("run_id", ""),
        status=state.get("status", "unknown"),
        started_at=state.get("started_at", ""),
        total_cycles=state.get("total_cycles", 0),
        completed_cycles=state.get("completed_cycles", 0),
        skipped_cycles=state.get("skipped_cycles", 0),
        errored_cycles=state.get("errored_cycles", 0),
        errored_details=state.get("errored_details", []),
        tracker_merge_error_count=state.get("tracker_merge_error_count", 0),
        current_cycle=current_cycle,
        completed_record_ids=state.get("completed_record_ids", []),
        error=state.get("error"),
        batch_size=state.get("batch_size", state.get("total_cycles", 1)),
        batch_count=state.get("batch_count", 1),
        completed_batches=state.get("completed_batches", 0),
        current_batch=state.get("current_batch"),
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
    override = _api_main_attr("_persist_run_state", _persist_run_state)
    if override is not _persist_run_state:
        override(run_id, state, create=create)
        return
    client = _lab_run_job_client_factory()()
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
    values. Re-reads ``_active_runs`` itself (rather than accepting a
    caller-supplied snapshot) so it always mutates whatever dict object is
    currently installed for ``run_id`` -- a resume/restart that installs a new
    dict between a caller's initial read and this call can't have its state
    clobbered by a stale reference.

    Preconditions:
        - ``run_id`` may or may not be present in ``_active_runs``.

    Postconditions:
        - No-op (no job-service call) when ``run_id`` is absent from
          ``_active_runs``, or its in-memory ``status`` is already in
          ``STRATEGY_LAB_TERMINAL_STATUSES``.
        - Otherwise calls ``client.get_job(run_id)`` at most once. When a
          persisted record is returned, every key in
          ``_STRATEGY_LAB_PROGRESS_FIELDS`` present in the record's data (via
          the ``job.get("data", job)`` fallback used elsewhere in this file,
          with an explicit ``"data": None`` treated the same as a missing
          ``"data"`` key) is copied onto ``_active_runs[run_id]``; a key
          absent from the persisted record is left untouched (a sparse/early
          persisted record can never erase a more-complete in-memory value).
          ``status``/
          ``error`` are copied onto ``_active_runs[run_id]`` only when the
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
    with _lock:
        state = _active_run_store().get(run_id)
    if not state or state.get("status") in STRATEGY_LAB_TERMINAL_STATUSES:
        return
    try:
        client = _lab_run_job_client_factory()()
        persisted = client.get_job(run_id)
    except Exception:
        _api_main_attr("logger", logger).debug(
            "Job service reconciliation failed for run %s", run_id, exc_info=True
        )
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
    with _lock:
        current = _active_run_store().get(run_id)
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


def _build_run_state(
    run_id: str,
    *,
    started_at: str,
    total_cycles: int,
    batch_size: int,
    batch_count: int,
    request_payload: Dict[str, Any],
    completed_cycles: int = 0,
    contiguous_cycles: Optional[int] = None,
    skipped_cycles: int = 0,
    errored_cycles: int = 0,
    errored_details: Optional[List[Any]] = None,
    tracker_merge_error_count: int = 0,
    completed_record_ids: Optional[List[Any]] = None,
    completed_batches: int = 0,
) -> Dict[str, Any]:
    """Build a strategy-lab run-state dict, shared by run/resume/restart.

    Defaults match the fresh-run (initial) case; resume/restart override the
    fields that carry forward or reset.

    Preconditions:
        - ``request_payload`` is the serialized ``RunStrategyLabRequest`` for this run.

    Postconditions:
        - Returns a new dict with ``status == "running"``. The ``contiguous_cycles``
          key is present iff ``contiguous_cycles`` is not ``None`` (the initial run
          omits it; resume sets the offset; restart resets it to ``0``). Mutable
          defaults (``errored_details``, ``completed_record_ids``) become fresh lists
          when not supplied. Does not mutate its arguments.
    """
    state: Dict[str, Any] = {
        "run_id": run_id,
        "status": "running",
        "started_at": started_at,
        "total_cycles": total_cycles,
        "completed_cycles": completed_cycles,
        "skipped_cycles": skipped_cycles,
        "errored_cycles": errored_cycles,
        "errored_details": errored_details if errored_details is not None else [],
        "tracker_merge_error_count": tracker_merge_error_count,
        "current_cycle": None,
        "completed_record_ids": (completed_record_ids if completed_record_ids is not None else []),
        "error": None,
        "request_payload": request_payload,
        "batch_size": batch_size,
        "batch_count": batch_count,
        "completed_batches": completed_batches,
        "current_batch": None,
    }
    if contiguous_cycles is not None:
        state["contiguous_cycles"] = contiguous_cycles
    return state


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


__all__ = [
    "STRATEGY_LAB_TERMINAL_STATUSES",
    "_STRATEGY_LAB_PROGRESS_FIELDS",
    "_persist_run_state",
    "_reconcile_run_progress",
    "_run_state_to_response",
    "_build_run_state",
    "_job_progress_percent",
    "_snapshot_prior_records",
    "_compute_signal_brief_snapshot",
    "_is_strategy_lab_run_externally_stopped",
    "_strategy_lab_external_terminal_status",
    "_finalize_strategy_lab_cycle_record",
]

_DEFERRED_EXPORTS = frozenset(
    {
        "_snapshot_prior_records",
        "_compute_signal_brief_snapshot",
        "_is_strategy_lab_run_externally_stopped",
        "_strategy_lab_external_terminal_status",
        "_finalize_strategy_lab_cycle_record",
    }
)


def __getattr__(name: str):
    """Resolve a re-exported helper from ``api.main`` on first attribute access.

    Preconditions:
        ``name`` is a non-empty attribute name requested on this module.
    Postconditions:
        Returns ``getattr(investment_team.api.main, name)`` when ``name`` is
        in the deferred export set; otherwise raises ``AttributeError``. Loads
        ``api.main`` only when a deferred export is first requested.
    """
    if name not in _DEFERRED_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from investment_team.api import main as _api_main

    return getattr(_api_main, name)


def __dir__() -> list[str]:
    """Return module attributes including the façade re-exports.

    Preconditions:
        None.
    Postconditions:
        Returns a sorted list that includes every name in ``__all__`` plus
        the module's ordinary attributes.
    """
    return sorted(set(globals()) | _DEFERRED_EXPORTS)
