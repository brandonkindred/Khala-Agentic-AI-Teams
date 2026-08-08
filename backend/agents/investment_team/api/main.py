"""FastAPI endpoints for the Investment Team."""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Dict, List, Literal, Optional, get_args

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from investment_team.agents import (
    FinancialAdvisorAgent,
    InvestmentCommitteeAgent,
    PolicyGuardianAgent,
)
from investment_team.exceptions import (
    InvestmentBacktestError,
    LookaheadViolationError,
    MarketDataUnavailableError,
    MissingStrategyCodeError,
    StrategyExecutionError,
)
from investment_team.market_data_cache.postgres import SCHEMA as MD_CACHE_SCHEMA
from investment_team.market_lab_data import (
    FreeTierMarketDataProvider,
    MarketLabContext,
    StrategyLabDataRequest,
)
from investment_team.models import (
    IPS,
    WINNING_THRESHOLD,
    AdvisorSession,
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    IncomeProfile,
    InvestmentCommitteeMemo,
    InvestmentProfile,
    LiquidityNeeds,
    NetWorth,
    PaperTradingSession,
    PaperTradingStatus,
    PaperTradingVerdict,
    PortfolioConstraints,
    PortfolioProposal,
    PromotionDecision,
    RiskTolerance,
    SavingsRate,
    StrategyLabRecord,
    StrategySpec,
    TaxProfile,
    TradeRecord,
    UserGoal,
    UserPreferences,
    ValidationReport,
    WorkflowMode,
    get_fee_defaults,
)
from investment_team.orchestrator import InvestmentTeamOrchestrator, QueueItem, WorkflowState
from investment_team.shared.job_store import (
    JOB_STATUS_CANCELLED as _BT_JOB_STATUS_CANCELLED,
)
from investment_team.shared.job_store import (
    JOB_STATUS_COMPLETED as _BT_JOB_STATUS_COMPLETED,
)
from investment_team.shared.job_store import (
    JOB_STATUS_FAILED as _BT_JOB_STATUS_FAILED,
)
from investment_team.shared.job_store import (
    JOB_STATUS_PENDING as _BT_JOB_STATUS_PENDING,
)
from investment_team.shared.job_store import (
    JOB_STATUS_RUNNING as _BT_JOB_STATUS_RUNNING,
)
from investment_team.shared.job_store import (
    cancel_job as _bt_cancel_job,
)
from investment_team.shared.job_store import (
    create_job as _bt_create_job,
)
from investment_team.shared.job_store import (
    delete_job as _bt_delete_job,
)
from investment_team.shared.job_store import (
    get_job as _bt_get_job,
)
from investment_team.shared.job_store import (
    is_job_cancelled as _bt_is_job_cancelled,
)
from investment_team.shared.job_store import (
    list_jobs as _bt_list_jobs,
)
from investment_team.shared.job_store import (
    mark_all_running_jobs_failed as _bt_mark_all_running_jobs_failed,
)
from investment_team.shared.job_store import (
    update_job as _bt_update_job,
)
from investment_team.signal_intelligence_agent import SignalIntelligenceExpert
from investment_team.signal_intelligence_models import SignalIntelligenceBriefV1
from investment_team.strategy_lab import orchestrator_api as _strategy_lab_orchestrator_api
from investment_team.strategy_lab.config import (
    MAX_BATCH_COUNT as _MAX_BATCH_COUNT,
)
from investment_team.strategy_lab.config import (
    MAX_PAPER_TRADING_LOOKBACK_DAYS as _MAX_PAPER_TRADING_LOOKBACK_DAYS,
)
from investment_team.strategy_lab.config import (
    MAX_PARALLEL as _MAX_PARALLEL,
)
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.run_state import (
    DEFAULT_FENCING_GENERATION,
)
from investment_team.strategy_lab.run_state import (
    active_runs as _active_runs,
)
from investment_team.strategy_lab.run_state import (
    async_lock as _async_lock,
)
from investment_team.strategy_lab.run_state import (
    get_lab_run_job_client as _get_lab_run_job_client,
)
from investment_team.strategy_lab.run_state import (
    get_run_generation_strict as _get_run_generation_strict,
)
from investment_team.strategy_lab.run_state import (
    get_run_state as _get_run_state,
)
from investment_team.strategy_lab.run_state import (
    load_run_from_job_service as _load_run_from_job_service,
)
from investment_team.strategy_lab.run_state import (
    lock as _lock,
)
from investment_team.strategy_lab.run_state import (
    normalize_persisted_job as _normalize_persisted_job,
)
from investment_team.strategy_lab.spec_dsl import (
    DEFAULT_SIZING_PAYLOAD,
    EntryRule,
    ExitRule,
    SizingRule,
)
from investment_team.strategy_lab_context import (
    PROMPT_ASSET_CLASSES,
    normalize_allowed_asset_classes,
    normalize_asset_class,
)
from job_service_client import (
    RESTARTABLE_STATUSES,
    RESUMABLE_STATUSES,
    JobNotFoundError,
    JobStateError,
    validate_job_for_action,
)
from shared.app import create_team_app
from shared.env_config import env_bool, env_float
from shared.sse import sse_job_stream_async, sse_line

logger = logging.getLogger(__name__)

STRATEGY_LAB_TERMINAL_STATUSES = _strategy_lab_orchestrator_api.STRATEGY_LAB_TERMINAL_STATUSES
_STRATEGY_LAB_PROGRESS_FIELDS = _strategy_lab_orchestrator_api._STRATEGY_LAB_PROGRESS_FIELDS
_PURGE_MAX_WORKERS = _strategy_lab_orchestrator_api._PURGE_MAX_WORKERS
_PURGE_TIMEOUT_S = _strategy_lab_orchestrator_api._PURGE_TIMEOUT_S
_build_run_state = _strategy_lab_orchestrator_api._build_run_state
_delete_jobs_concurrently = _strategy_lab_orchestrator_api._delete_jobs_concurrently
_delete_paper_sessions_for_lab_record = (
    _strategy_lab_orchestrator_api._delete_paper_sessions_for_lab_record
)
_job_progress_percent = _strategy_lab_orchestrator_api._job_progress_percent
_persist_run_state = _strategy_lab_orchestrator_api._persist_run_state
_purge_strategy_lab_job_storage = _strategy_lab_orchestrator_api._purge_strategy_lab_job_storage
_reconcile_run_progress = _strategy_lab_orchestrator_api._reconcile_run_progress
_run_state_to_response = _strategy_lab_orchestrator_api._run_state_to_response
_fail_strategy_lab_run = _strategy_lab_orchestrator_api._fail_strategy_lab_run
_dispatch_strategy_lab_run = _strategy_lab_orchestrator_api._dispatch_strategy_lab_run
_no_active_run_locked = _strategy_lab_orchestrator_api._no_active_run_locked
_ensure_no_active_run = _strategy_lab_orchestrator_api._ensure_no_active_run
_require_run_transition_lock = _strategy_lab_orchestrator_api._require_run_transition_lock


def _startup() -> None:
    """Start the Temporal worker backstop and recover orphaned paper-trading
    sessions (both best-effort).

    The team_service entrypoint normally starts the worker via
    ``TEAM_TEMPORAL_WORKER_MODULE`` before uvicorn accepts requests; this
    backstop covers running the app standalone (``uvicorn ...:app``) and a
    wrapper start that silently failed.

    Preconditions:
        - None (safe to call once at app startup; idempotent per team).

    Postconditions:
        - Starts the worker thread when Temporal is enabled; a no-op when
          ``TEMPORAL_ADDRESS`` is unset. Marks any paper-trading session left
          in an active status by a previous process as ``failed`` (see
          ``_recover_orphaned_paper_trading_sessions``). Never raises —
          failures are logged so they cannot abort app boot (this runs as
          ``create_team_app``'s ``on_startup`` hook, inside its custom
          ``lifespan=``; the deprecated ``@app.on_event("startup")``
          decorator is never invoked once a custom lifespan is set, so
          startup work belongs here, not on a separately decorated function).
    """
    try:
        from investment_team.temporal.worker import (
            start_investment_temporal_worker_thread,
        )

        start_investment_temporal_worker_thread()
    except Exception:
        logger.warning(
            "investment_team Temporal worker start (lifespan backstop) failed",
            exc_info=True,
        )
    _recover_orphaned_paper_trading_sessions()


def _run_investment_service_shutdown() -> (
    None
):  # pragma: no cover - process-lifecycle shutdown hook driven by uvicorn; the meaningful exercise needs a live server. The body is a defensive try/except around the event-bus reaper teardown and the backtest-job failure sweep.
    """Stop the per-job event-bus reaper and fail any still-running backtest jobs.

    ``run_backtest``'s Temporal-unavailable fallback runs backtests on a
    ``daemon=True`` thread, which is killed abruptly on process shutdown
    without updating the job's status — leaving it stuck at RUNNING forever
    after a restart. This sweep closes that gap: any job the store still
    considers pending/running when the process exits could not have
    finished, so it is marked FAILED here instead.

    Postconditions:
        - The event-bus reaper thread is stopped (idempotent; a missing or
          already-stopped reaper is a no-op). All jobs still pending/running
          in the backtest job store are marked FAILED (best-effort; a store
          error is logged and swallowed). Never raises — teardown failures
          are logged at ``warning`` (visible at standard operational log
          levels, not just under ``debug``) and swallowed so they cannot
          abort process shutdown.
    """
    try:
        from investment_team.api.job_event_bus import shutdown as _shutdown_event_bus

        _shutdown_event_bus()
    except Exception:
        logger.warning("Investment event-bus reaper shutdown skipped", exc_info=True)

    try:
        _bt_mark_all_running_jobs_failed("server shutdown")
    except Exception:
        logger.warning("Investment backtest job failure sweep skipped", exc_info=True)


# Standard team wiring: init_otel + Postgres-schema lifespan + OTel instrument.
# The Postgres schema registers the market-data cache snapshot index DDL on
# startup (no-op when POSTGRES_HOST is unset); the on_startup hook is the
# Temporal-worker lifespan backstop (a second start path alongside the
# team_service entrypoint); the on_shutdown hook stops the event-bus reaper
# thread before the pool is closed.
app = create_team_app(
    service_name="investment-team",
    team_key="investment",
    title="Investment Team API",
    description="Investment profile management, portfolio proposals, strategy validation, and promotion gates.",
    version="1.0.0",
    postgres_schema=MD_CACHE_SCHEMA,
    on_startup=_startup,
    on_shutdown=_run_investment_service_shutdown,
)

_workflow_state = WorkflowState()


# ---------------------------------------------------------------------------
# Persistent storage backed by JobServiceClient (survives server restarts)
# ---------------------------------------------------------------------------
_MISSING: Any = object()  # sentinel for _PersistentDict.pop: distinguishes "no
# default passed" from a caller explicitly passing default=None.


class _PersistentDict:
    """Dict-like wrapper around JobServiceClient for restart-safe entity storage.

    Usage:
        store = _PersistentDict('profiles')
        store['key'] = some_model_instance  # persists via JobServiceClient
        value = store.get('key', default)   # returns stored data dict, not the original object

    Invariants:
        - Keys are strings.
        - Pydantic ``BaseModel`` values are persisted via
          ``model_dump(mode="json")``; other values -- including objects that
          merely happen to expose a non-Pydantic attribute named
          ``model_dump`` -- are wrapped as ``{"value": value}`` before
          persistence.
        - Reads (``__getitem__``, ``get``, ``pop``, ``values``) return the
          persisted data dict, not a reconstructed model instance.
        - Storage is namespaced under JobServiceClient team
          ``investment_{entity_type}``.
    """

    def __init__(self, entity_type: str) -> None:
        """Bind a namespaced, process-wide-cached JobServiceClient to this store.

        Preconditions:
            - ``entity_type`` is a ``str`` used as the store namespace suffix.
        Postconditions:
            - ``self._client`` is the process-wide cached client for team
              ``investment_{entity_type}`` (see
              ``job_service_client.get_job_service_client`` -- one client per
              team for the life of the process, so distinct
              ``_PersistentDict`` instances constructed for the same
              ``entity_type`` share a single underlying client instead of
              each opening their own).
            - ``self._entity_type`` equals ``entity_type``.
        """
        from job_service_client import get_job_service_client

        self._client = get_job_service_client(f"investment_{entity_type}")
        self._entity_type = entity_type

    def __setitem__(self, key: str, value: Any) -> None:
        """Persist ``value`` under ``key`` (create or overwrite), atomically.

        Preconditions:
            - ``key`` is a ``str``.
        Postconditions:
            - Pydantic ``BaseModel`` values are stored via
              ``model_dump(mode="json")``; other values -- including objects
              that merely happen to expose a non-Pydantic attribute named
              ``model_dump`` -- are stored as ``{"value": value}``.
            - Always calls ``create_job(key, status="stored", data=data)`` --
              no read-before-write. The job-service DB layer implements
              ``create_job`` as ``INSERT ... ON CONFLICT (team, job_id) DO
              UPDATE`` (see ``backend/job_service/db.py``), so this atomically
              creates the job if absent or overwrites it in place if present,
              in one statement. Concurrent writers for the same key can no
              longer race on a stale "does it exist yet" read -- and unlike a
              per-process ``threading.Lock``, this holds across every worker
              process/replica, not just within one.
        """
        data = value.model_dump(mode="json") if isinstance(value, BaseModel) else {"value": value}
        self._client.create_job(key, status="stored", data=data)

    def __getitem__(self, key: str) -> Any:
        """Return the persisted data dict for ``key``.

        Preconditions:
            - ``key`` is a ``str``.
        Postconditions:
            - Returns the job's ``data`` payload (or the job mapping if ``data``
              is absent).
            - Raises ``KeyError`` when no job exists for ``key``.
        """
        job = self._client.get_job(key)
        if job is None:
            raise KeyError(key)
        return job.get("data", job)

    def get(self, key: str, default: Any = None) -> Any:
        """Return the persisted data dict for ``key``, or ``default`` if missing.

        Preconditions:
            - ``key`` is a ``str``.
        Postconditions:
            - When a job exists: returns its ``data`` payload (or the job
              mapping if ``data`` is absent).
            - When no job exists: returns ``default``.
        """
        job = self._client.get_job(key)
        if job is None:
            return default
        return job.get("data", job)

    def __contains__(self, key: str) -> bool:
        """Return whether a job exists for ``key``.

        Preconditions:
            - ``key`` is a ``str``.
        Postconditions:
            - Returns ``True`` iff ``get_job(key)`` is not ``None``.
        """
        return self._client.get_job(key) is not None

    def __delitem__(self, key: str) -> None:
        """Delete the job for ``key``.

        Preconditions:
            - ``key`` is a ``str``.
        Postconditions:
            - Delegates deletion to ``JobServiceClient.delete_job`` (missing-key
              behavior is defined by that client).
        """
        self._client.delete_job(key)

    def pop(self, key: str, default: Any = _MISSING) -> Any:
        """Remove ``key`` and return its persisted data dict.

        Preconditions:
            - ``key`` is a ``str``.
            - At most one ``default`` value is accepted -- Python's own
              parameter binding rejects a second positional/keyword argument
              with ``TypeError``, matching ``dict.pop``'s contract.
            - If the job is missing and ``default`` was not passed, raises
              ``KeyError``.
        Postconditions:
            - When present: deletes the job and returns its ``data`` payload
              (or the job mapping if ``data`` is absent) -- but ONLY when
              ``delete_job(key)`` itself reports that this call actually
              removed a row. If a concurrent ``pop()`` for the same key
              already deleted it between this call's ``get_job`` read and its
              own ``delete_job`` call, ``delete_job`` reports no row removed
              and this call is treated exactly like a missing key (default
              returned, or ``KeyError`` raised) rather than handing back data
              for a job it did not itself remove. The job-service DB layer
              has no atomic get-and-delete primitive (a plain ``DELETE``, no
              ``RETURNING``), so the returned data is whatever ``get_job``
              read moments before the confirmed delete -- not part of the
              same atomic operation. A third caller overwriting the job in
              that narrow window is a known, accepted residual gap; the
              exactly-one-caller-claims-the-deletion guarantee above is not.
            - When missing and ``default`` was passed (including explicitly
              ``None``): returns ``default`` without deleting.
        """
        job = self._client.get_job(key)
        if job is None:
            if default is not _MISSING:
                return default
            raise KeyError(key)
        if not self._client.delete_job(key):
            if default is not _MISSING:
                return default
            raise KeyError(key)
        return job.get("data", job)

    def values(self) -> List[Any]:
        """Return persisted data dicts for all jobs in this store.

        Preconditions:
            - None.
        Postconditions:
            - Returns a list of each job's ``data`` payload (or the job mapping
              if ``data`` is absent); empty list when there are no jobs.
        """
        jobs = self._client.list_jobs() or []
        return [j.get("data", j) for j in jobs]


_profiles: _PersistentDict = _PersistentDict("profiles")
_proposals: _PersistentDict = _PersistentDict("proposals")
_strategies: _PersistentDict = _PersistentDict("strategies")
_validations: _PersistentDict = _PersistentDict("validations")
_backtests: _PersistentDict = _PersistentDict("backtests")
_strategy_lab_records: _PersistentDict = _PersistentDict("strategy_lab_records")
_paper_trading_sessions: _PersistentDict = _PersistentDict("paper_trading_sessions")
_advisor_sessions: _PersistentDict = _PersistentDict("advisor_sessions")


def _snapshot_prior_records(*, reverse: bool = False) -> list[StrategyLabRecord]:
    """Locked read of the strategy-lab store, parsed and sorted by created_at.

    Preconditions:
        None — safe to call against an empty store.
    Postconditions:
        Returns a freshly parsed list of StrategyLabRecord, sorted by
        ``created_at`` ascending (oldest-first) by default, or descending
        (newest-first) when ``reverse=True``. Never returns None.
    """
    with _lock:
        raw = list(_strategy_lab_records.values())
    records = [StrategyLabRecord.parse_persisted(r) for r in raw]
    records.sort(key=lambda r: r.created_at, reverse=reverse)
    return records


@lru_cache(maxsize=1)
def _get_advisor_agent() -> FinancialAdvisorAgent:
    """Process-wide singleton. Call ``_get_advisor_agent.cache_clear()`` to reset."""
    return FinancialAdvisorAgent()


@lru_cache(maxsize=1)
def _get_policy_guardian() -> PolicyGuardianAgent:
    """Process-wide singleton. Call ``_get_policy_guardian.cache_clear()`` to reset."""
    return PolicyGuardianAgent()


@lru_cache(maxsize=1)
def _get_orchestrator() -> InvestmentTeamOrchestrator:
    """Process-wide singleton. Call ``_get_orchestrator.cache_clear()`` to reset."""
    return InvestmentTeamOrchestrator()


@lru_cache(maxsize=1)
def _get_committee_agent() -> InvestmentCommitteeAgent:
    """Process-wide singleton. Call ``_get_committee_agent.cache_clear()`` to reset."""
    return InvestmentCommitteeAgent()


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Strategy Lab run tracking models
# ---------------------------------------------------------------------------

# restart_strategy_lab_run's restartable-status gate: "completed_with_errors"
# is a terminal outcome of the same workflow as "completed" and must be
# restartable too, but it's lab-specific and doesn't belong in the shared
# job_service_client.RESTARTABLE_STATUSES constant.
STRATEGY_LAB_RESTARTABLE_STATUSES: frozenset[str] = RESTARTABLE_STATUSES | {"completed_with_errors"}

# restart_strategy_lab_run's fencing-generation mint amount: a run created
# before generation fencing shipped has no persisted "generation" field, and
# the job service's atomic increment treats an absent field as 0 -- a plain
# +1 would land on 1, which is also what a pre-upgrade caller that omits
# generation entirely is treated as presenting (check_fencing_token accepts
# equal tokens), so that first post-upgrade restart must jump straight to 2
# instead. See restart_strategy_lab_run's own inline comment for the full
# reasoning.
GENERATION_INCREMENT_NORMAL = 1
GENERATION_INCREMENT_LEGACY_BOOTSTRAP = 2

# Passed as `_persist_run_state`'s `exclude_fields` by every write that must
# never regress the durable fencing high-water mark from a possibly-stale
# in-memory snapshot (run/resume/restart/rollback state writes) -- the
# generation field is only ever advanced via `apply_and_get`'s atomic
# increment, never via one of these ordinary state persists.
_GENERATION_EXCLUDE_FIELDS = frozenset({"generation"})


class UnsafeDurableGenerationError(ValueError):
    """Durable ``generation`` cannot be advanced safely via job-service increment.

    Raised when the persisted value is a representation ``get_run_generation_strict``
    would accept as a positive fencing token (e.g. the numeric string ``\"5\"``) but
    job-service ``apply`` would coerce to ``0`` before adding the delta — so a plain
    increment would *regress* the conceptual generation and reopen fencing.
    """


def _legacy_generation_bootstrap_increment(durable_data: Dict[str, Any]) -> int:
    """Return the fencing-generation increment ``restart_strategy_lab_run``
    should atomically apply for this run's durable record.

    A run created before generation fencing shipped has no "generation"
    field in its persisted record at all, and the job service's atomic
    increment treats an absent field as 0 -- so a plain +1 would mint
    generation 1 for such a run's first restart. That's exactly the
    generation ``persist_run_state_activity``/``finalize_cycle_record_activity``
    fall back to for a caller that omits ``generation`` entirely (an activity
    scheduled before the field existed), and ``check_fencing_token`` accepts
    equal tokens -- so that stale activity would pass fencing again. Jumping
    straight to ``GENERATION_INCREMENT_LEGACY_BOOTSTRAP`` in that one case
    makes the minted value strictly exceed the legacy default; a run that
    already has an explicit positive integer "generation" field (every run
    created after this change, and any legacy run past its first
    post-upgrade restart) increments by the ordinary
    ``GENERATION_INCREMENT_NORMAL`` instead.

    The same +2 bootstrap applies when the key is present but still
    uninitialized in the sense ``get_run_generation_strict`` already uses for
    soft defaults: ``None``, ``""``, an int below ``DEFAULT_FENCING_GENERATION``,
    or a non-int that cannot be parsed as an int (job-service increment
    coerces those to ``0``, so +2 lands on generation 2). Bools and floats are
    rejected instead (``get_run_generation_strict`` raises on them). A
    non-int that *parses* as a positive int (e.g. ``\"5\"``) also fails closed:
    increment would zero the field first and mint ``2``, regressing the
    conceptual token and letting in-flight activities that present ``5``
    pass ``check_fencing_token``.

    Preconditions:
        - ``durable_data`` is the run's durable job record (its ``"data"``
          column merged in) -- or ``{}`` when the job does not exist.
          Callers MUST pass the DURABLE record here, not `_get_run_state`'s
          process-local ``active_runs`` snapshot: a resume of this same
          legacy run can already have populated that in-memory entry with a
          ``generation=1`` default (``resume_strategy_lab_run`` deliberately
          excludes "generation" from ITS durable write, so the durable
          record stays legacy even after that). Passing the in-memory
          snapshot instead would see "generation" present, mint only +1,
          and land on durable generation 1 -- exactly what a still-in-flight
          legacy activity (which omits ``generation`` entirely, defaulting
          to 1) presents, defeating fencing.
        - Callers MUST NOT substitute ``{}`` for a failed durable read.
          Blindly treating a read failure as legacy and minting +2 can
          regress a durable non-native positive token (e.g. ``\"5\"``) via
          job-service zeroing. Restart fails closed on bootstrap-read
          failure instead.

    Postconditions:
        - Returns ``GENERATION_INCREMENT_LEGACY_BOOTSTRAP`` when
          ``durable_data`` has no usable positive native-int "generation"
          (absent, ``None``/empty, unparseable non-int, or native int
          ``< DEFAULT_FENCING_GENERATION``).
        - Returns ``GENERATION_INCREMENT_NORMAL`` for a native ``int``
          ``>= DEFAULT_FENCING_GENERATION``.
        - Raises ``UnsafeDurableGenerationError`` when the durable value is a
          bool/float, or a non-int that parses to an int
          ``>= DEFAULT_FENCING_GENERATION`` (increment would regress the
          token). Callers must translate that into a fail-closed response
          rather than minting.
    """
    if "generation" not in durable_data:
        return GENERATION_INCREMENT_LEGACY_BOOTSTRAP
    raw = durable_data["generation"]
    if raw is None or raw == "":
        return GENERATION_INCREMENT_LEGACY_BOOTSTRAP
    # bool is an int subclass; float is accepted by job-service increment but
    # rejected by get_run_generation_strict — fail closed rather than minting
    # a float/bool-derived token that fencing reads cannot round-trip.
    if isinstance(raw, bool) or isinstance(raw, float):
        raise UnsafeDurableGenerationError(
            f"durable generation {raw!r} is not a native int fencing token"
        )
    if isinstance(raw, int):
        if raw < DEFAULT_FENCING_GENERATION:
            return GENERATION_INCREMENT_LEGACY_BOOTSTRAP
        return GENERATION_INCREMENT_NORMAL
    # Non-int: unparseable garbage zeros under job-service increment → +2 is
    # safe. A parseable positive value (e.g. "5") must NOT use increment: the
    # service would zero it first and regress the conceptual generation.
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return GENERATION_INCREMENT_LEGACY_BOOTSTRAP
    if parsed < DEFAULT_FENCING_GENERATION:
        return GENERATION_INCREMENT_LEGACY_BOOTSTRAP
    raise UnsafeDurableGenerationError(
        f"durable generation {raw!r} parses to {parsed} but is not a native int; "
        "job-service increment would zero it and regress the fencing token"
    )


class StrategyLabRunStartResponse(BaseModel):
    """Returned immediately when a strategy lab batch is started."""

    run_id: str
    status: str = "running"
    total_cycles: int
    message: str = "Strategy lab batch started."


class StrategyLabCycleProgress(BaseModel):
    """Progress snapshot for the currently-executing cycle."""

    cycle_index: int
    phase: str
    strategy: Optional[Dict[str, Any]] = None
    metrics: Optional[Dict[str, Any]] = None


class StrategyLabRunStatusResponse(BaseModel):
    """Full snapshot of a strategy lab run (for polling or initial SSE snapshot)."""

    run_id: str
    status: str
    started_at: str
    total_cycles: int
    completed_cycles: int = 0
    skipped_cycles: int = 0
    # Non-fatal per-cycle failures: run keeps going, but these are surfaced
    # to the UI so users can see that something went wrong during generation.
    errored_cycles: int = 0
    errored_details: List[Dict[str, Any]] = Field(default_factory=list)
    # Uncapped count of cycle_errored events tagged reason="tracker_merge_failed"
    # (a cycle that already published cycle_complete, then separately
    # cycle_errored for the same cycle_index when its post-completion
    # convergence-tracker merge failed). Unlike errored_details (capped at
    # _ERRORED_DETAILS_MAX), this counter never evicts, so a client
    # reconciling a double-counted cycle_index can always recover the exact
    # count instead of approximating it from a possibly-truncated array.
    tracker_merge_error_count: int = 0
    current_cycle: Optional[StrategyLabCycleProgress] = None
    completed_record_ids: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    # Multi-batch progress (batch_count > 1 runs N batches sequentially; each batch
    # generates ``batch_size`` strategies; ``total_cycles == batch_size * batch_count``).
    batch_size: int = 1
    batch_count: int = 1
    completed_batches: int = 0
    current_batch: Optional[int] = None
    # Read-only fencing generation (see run_state.get_run_generation /
    # _build_run_state) -- exposed so a caller can observe that a restart
    # superseded a prior incarnation; never accepted as client input anywhere,
    # so surfacing it carries none of the write-path fencing risk.
    generation: int = 1


class ActiveRunsResponse(BaseModel):
    """List of all tracked strategy lab runs (active and recently completed)."""

    runs: List[StrategyLabRunStatusResponse] = Field(default_factory=list)


class StrategyLabConfigResponse(BaseModel):
    """Operator-tunable limits the UI needs to render its run form."""

    batch_count_min: int
    batch_count_max: int
    # The ideation-valid asset categories the design agent can generate
    # strategies for. Served so the UI's category selector is sourced from the
    # backend (single source of truth) rather than a hand-maintained copy.
    asset_categories: List[str] = Field(default_factory=list)


class CreateProfileRequest(BaseModel):
    """Inputs to create an Investment Policy Statement (IPS) for a user.

    Rejects unknown fields (``model_config extra="forbid"``) so a
    stale/misspelled client payload fails fast with a 422 instead of
    silently dropping the field.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., description="Unique user identifier")
    risk_tolerance: RiskTolerance = Field(
        ..., description="One of: " + ", ".join(m.value for m in RiskTolerance)
    )
    max_drawdown_tolerance_pct: float = Field(..., ge=0, le=100)
    time_horizon_years: int = Field(..., ge=1)
    annual_gross_income: float = Field(..., ge=0)
    income_stability: Literal["stable", "variable", "seasonal", "commission"] = Field(
        default="stable"
    )
    total_net_worth: float = Field(..., ge=0)
    investable_assets: float = Field(..., ge=0)
    monthly_savings: float = Field(default=0.0)
    annual_savings: float = Field(default=0.0)
    tax_country: str = Field(default="US")
    tax_state: str = Field(default="")
    account_types: List[str] = Field(default_factory=list)
    emergency_fund_months: int = Field(default=6)
    excluded_asset_classes: List[str] = Field(default_factory=list)
    excluded_industries: List[str] = Field(default_factory=list)
    esg_preference: Literal["none", "light", "moderate", "strict"] = Field(default="none")
    crypto_allowed: bool = Field(default=True)
    options_allowed: bool = Field(default=True)
    leverage_allowed: bool = Field(default=False)
    goals: List[Dict[str, Any]] = Field(default_factory=list)
    max_single_position_pct: float = Field(default=10.0, ge=0, le=100)
    max_asset_class_pct: Dict[str, float] = Field(default_factory=dict)
    live_trading_enabled: bool = Field(default=False)
    human_approval_required_for_live: bool = Field(default=True)
    speculative_sleeve_cap_pct: float = Field(default=10.0, ge=0, le=100)
    rebalance_frequency: Literal["monthly", "quarterly", "semi-annual", "annual"] = Field(
        default="quarterly"
    )
    default_mode: WorkflowMode = Field(
        default=WorkflowMode.MONITOR_ONLY,
        description="One of: " + ", ".join(m.value for m in WorkflowMode),
    )
    notes: List[str] = Field(default_factory=list)


class CreateProfileResponse(BaseModel):
    """Returned by ``POST /profiles`` — the newly created IPS for the user."""

    user_id: str
    ips: IPS
    message: str = "Investment Policy Statement created successfully."


class GetProfileResponse(BaseModel):
    user_id: str
    ips: Optional[IPS] = None
    found: bool = True


class CreateProposalRequest(BaseModel):
    """Inputs to create a portfolio proposal for a user with an existing IPS.

    Rejects unknown fields (``model_config extra="forbid"``) so a
    stale/misspelled client payload fails fast with a 422 instead of
    silently dropping the field.
    """

    model_config = ConfigDict(extra="forbid")

    prepared_by: str = Field(..., description="Agent or user ID who prepared this proposal")
    user_id: str = Field(..., description="User ID whose IPS this is for")
    objective: str = Field(..., description="Investment objective")
    positions: List[Dict[str, Any]] = Field(..., description="List of portfolio positions")
    expected_return_pct: Optional[float] = None
    expected_volatility_pct: Optional[float] = None
    expected_max_drawdown_pct: Optional[float] = None
    assumptions: List[str] = Field(default_factory=list)


class CreateProposalResponse(BaseModel):
    """Returned by ``POST /proposals/create`` — the newly created proposal."""

    proposal_id: str
    proposal: PortfolioProposal
    message: str = "Portfolio proposal created successfully."


class GetProposalResponse(BaseModel):
    """Returned by ``GET /proposals/{proposal_id}``.

    ``found=False``/``proposal=None`` when no proposal exists for the given
    id — a normal, expected outcome, not an error.
    """

    proposal_id: str
    proposal: Optional[PortfolioProposal] = None
    found: bool = True


class ValidateProposalRequest(BaseModel):
    """Inputs to validate an existing proposal against a user's IPS.

    Rejects unknown fields (``model_config extra="forbid"``) so a
    stale/misspelled client payload fails fast with a 422 instead of
    silently dropping the field.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., description="User ID to get IPS for validation")


class ValidateProposalResponse(BaseModel):
    """Returned by ``POST /proposals/{proposal_id}/validate``."""

    proposal_id: str
    valid: bool
    violations: List[str] = Field(default_factory=list)


class CreateStrategyRequest(BaseModel):
    # Reject stale-client payloads (e.g. legacy ``sizing_rules: [...]``) at
    # the HTTP boundary with 422 rather than silently dropping them.
    model_config = ConfigDict(extra="forbid")

    authored_by: str = Field(..., description="Agent or user ID who authored the strategy")
    asset_class: str = Field(..., description="Primary asset class")
    hypothesis: str = Field(..., description="Investment hypothesis")
    signal_definition: str = Field(..., description="Signal definition")
    timeframe: Literal["1m", "5m", "15m", "1h", "1d"] = Field(
        ..., description="Bar timeframe the strategy was designed against"
    )
    entry_rules: List[EntryRule] = Field(default_factory=list)
    exit_rules: List[ExitRule] = Field(default_factory=list)
    sizing: Optional[SizingRule] = Field(default=None)
    risk_limits: Dict[str, Any] = Field(default_factory=dict)
    speculative: bool = Field(default=False)


class CreateStrategyResponse(BaseModel):
    """Returned by ``POST /strategies`` — the newly created strategy."""

    strategy_id: str
    strategy: StrategySpec
    message: str = "Strategy created successfully."


class ValidateStrategyRequest(BaseModel):
    """Inputs to run validation checks against an existing strategy."""

    backtest_period: str = Field(default="2020-01-01 to 2024-12-31")
    scenario_set: List[str] = Field(default_factory=lambda: ["baseline", "stress", "monte_carlo"])
    checks: List[Dict[str, Any]] = Field(default_factory=list)


class ValidateStrategyResponse(BaseModel):
    """Returned by ``POST /strategies/{strategy_id}/validate``."""

    strategy_id: str
    validation: ValidationReport
    passed: bool
    failures: List[str] = Field(default_factory=list)


class RunBacktestRequest(BaseModel):
    """Inputs to submit a backtest job for an existing strategy.

    Rejects unknown fields (``model_config extra="forbid"``) so a
    stale/misspelled client payload fails fast with a 422 instead of
    silently dropping the field.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(..., description="Strategy ID to back test")
    submitted_by: str = Field(..., description="Agent or user ID submitting the back test")
    start_date: str = Field(..., description="Backtest start date, ISO format")
    end_date: str = Field(..., description="Backtest end date, ISO format")
    initial_capital: float = Field(default=100000.0, gt=0)
    benchmark_symbol: str = Field(default="SPY")
    rebalance_frequency: str = Field(default="monthly")
    transaction_cost_bps: float = Field(default=5.0, ge=0)
    slippage_bps: float = Field(default=2.0, ge=0)
    notes: List[str] = Field(default_factory=list)


class RunBacktestResponse(BaseModel):
    """Full backtest result, returned once a backtest job completes."""

    backtest: BacktestRecord
    message: str = "Backtest completed and recorded successfully."


class ListBacktestsResponse(BaseModel):
    """Response body for ``GET /backtests``.

    ``count`` is always ``len(items)`` — enforced by a model_validator so a
    caller can never construct a payload whose count disagrees with its list.
    """

    items: List[BacktestRecord] = Field(default_factory=list)
    count: int = 0

    @model_validator(mode="after")
    def _derive_count_from_items(self) -> "ListBacktestsResponse":
        """Recompute ``count`` from ``items`` so it can never drift apart.

        Postconditions: ``count == len(items)`` regardless of what was passed
        in for ``count``.
        """
        self.count = len(self.items)
        return self


class PromotionDecisionRequest(BaseModel):
    """Inputs to run the promotion-gate decision for a validated strategy.

    Rejects unknown fields (``model_config extra="forbid"``) so a
    stale/misspelled client payload fails fast with a 422 instead of
    silently dropping the field.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(..., description="Strategy ID to promote")
    user_id: str = Field(..., description="User ID for IPS lookup")
    proposer_agent_id: str = Field(..., description="ID of agent who proposed the strategy")
    approver_agent_id: str = Field(..., description="ID of independent approver agent")
    approver_role: str = Field(default="approver")
    approver_version: str = Field(default="1.0")
    risk_veto: bool = Field(default=False)
    human_live_approval: bool = Field(default=False)


class PromotionDecisionResponse(BaseModel):
    """Returned by ``POST /promotions/decide`` — the computed promotion decision."""

    strategy_id: str
    decision: PromotionDecision


class WorkflowStatusResponse(BaseModel):
    """Trading-mode + audit/queue snapshot for the current workflow state.

    Not a ``WorkflowStatus``-equivalent enum: investment_team has no
    run-lifecycle status comparable to branding_team's or
    market_research_team's ``WorkflowStatus``. See
    ``backend/shared/hitl/README.md`` ("Non-shared: team WorkflowStatus")
    for the full cross-team decision record.
    """

    mode: str
    audit_log: List[str] = Field(default_factory=list)
    queue_counts: Dict[str, int] = Field(default_factory=dict)


class QueueItemResponse(BaseModel):
    """A single queued item, as returned by ``GET /workflow/queues``."""

    queue: str
    payload_id: str
    priority: str = "normal"


class QueuesResponse(BaseModel):
    """Returned by ``GET /workflow/queues`` — all queues, keyed by queue name."""

    queues: Dict[str, List[QueueItemResponse]] = Field(default_factory=dict)


class CreateMemoRequest(BaseModel):
    """Inputs to create an Investment Committee memo recording a recommendation.

    Rejects unknown fields (``model_config extra="forbid"``) so a
    stale/misspelled client payload fails fast with a 422 instead of
    silently dropping the field.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str
    recommendation: str
    rationale: str
    dissenting_views: List[str] = Field(default_factory=list)


class CreateMemoResponse(BaseModel):
    """Returned by ``POST /memos`` — the newly created committee memo."""

    memo: InvestmentCommitteeMemo


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok", "timestamp": _now()}


@app.post("/profiles", response_model=CreateProfileResponse)
def create_profile(request: CreateProfileRequest) -> CreateProfileResponse:
    """Create an Investment Policy Statement (IPS) for a user.

    Preconditions:
        - ``request.risk_tolerance``/``request.default_mode`` are already
          guaranteed to be valid ``RiskTolerance``/``WorkflowMode`` members —
          FastAPI/Pydantic rejects any other value with a 422 before this
          handler runs, since both fields are typed as the enum itself.
        - No profile may already exist for ``request.user_id`` — this endpoint
          creates; it does not upsert.

    Postconditions:
        - On success: a new IPS is persisted under ``_profiles[request.user_id]``
          and returned; no prior profile is overwritten.
        - Raises ``HTTPException`` 422 if constructing the nested ``UserGoal``,
          ``InvestmentProfile``, or ``IPS`` models fails Pydantic validation.
        - Raises ``HTTPException`` 409 if a profile already exists for
          ``request.user_id``.
    """
    risk_tol = request.risk_tolerance
    workflow_mode = request.default_mode

    try:
        goals = [
            UserGoal(
                name=g.get("name", ""),
                target_amount=g.get("target_amount", 0),
                target_date=g.get("target_date", ""),
                priority=g.get("priority", "medium"),
            )
            for g in request.goals
        ]

        profile = InvestmentProfile(
            user_id=request.user_id,
            created_at=_now(),
            risk_tolerance=risk_tol,
            max_drawdown_tolerance_pct=request.max_drawdown_tolerance_pct,
            time_horizon_years=request.time_horizon_years,
            liquidity_needs=LiquidityNeeds(emergency_fund_months=request.emergency_fund_months),
            income=IncomeProfile(
                annual_gross=request.annual_gross_income, stability=request.income_stability
            ),
            net_worth=NetWorth(
                total=request.total_net_worth, investable_assets=request.investable_assets
            ),
            savings_rate=SavingsRate(
                monthly=request.monthly_savings, annual=request.annual_savings
            ),
            tax_profile=TaxProfile(
                country=request.tax_country,
                state=request.tax_state,
                account_types=request.account_types,
            ),
            preferences=UserPreferences(
                excluded_asset_classes=request.excluded_asset_classes,
                excluded_industries=request.excluded_industries,
                esg_preference=request.esg_preference,
                crypto_allowed=request.crypto_allowed,
                options_allowed=request.options_allowed,
                leverage_allowed=request.leverage_allowed,
            ),
            goals=goals,
            constraints=PortfolioConstraints(
                max_single_position_pct=request.max_single_position_pct,
                max_asset_class_pct=request.max_asset_class_pct,
            ),
        )

        ips = IPS(
            profile=profile,
            live_trading_enabled=request.live_trading_enabled,
            human_approval_required_for_live=request.human_approval_required_for_live,
            speculative_sleeve_cap_pct=request.speculative_sleeve_cap_pct,
            rebalance_frequency=request.rebalance_frequency,
            default_mode=workflow_mode,
            notes=request.notes,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False, include_context=False)
        ) from exc

    with _lock:
        if request.user_id in _profiles:
            raise HTTPException(
                status_code=409,
                detail=f"Profile already exists for user {request.user_id}",
            )
        _profiles[request.user_id] = ips

    return CreateProfileResponse(user_id=request.user_id, ips=ips)


@app.get("/profiles/{user_id}", response_model=GetProfileResponse)
def get_profile(user_id: str) -> GetProfileResponse:
    """Get the Investment Policy Statement for a user.

    Postconditions:
        Returns ``found=True`` with the stored ``ips`` when ``user_id`` has
        a profile; otherwise returns ``found=False`` with ``ips=None``
        (never raises 404 — missing is a normal, expected outcome here).
    """
    with _lock:
        ips = _profiles.get(user_id)
    if not ips:
        return GetProfileResponse(user_id=user_id, ips=None, found=False)
    return GetProfileResponse(user_id=user_id, ips=ips, found=True)


@app.post("/proposals/create", response_model=CreateProposalResponse)
def create_proposal(request: CreateProposalRequest) -> CreateProposalResponse:
    """Create a new portfolio proposal (runs as a Temporal workflow).

    Preconditions:
        ``request.user_id`` must already have an IPS created via
        ``create_profile``.
    Postconditions:
        Persists a new ``PortfolioProposal`` under a freshly generated
        ``proposal_id`` and returns it.

    Raises:
        - ``HTTPException(404)`` if no IPS exists for ``request.user_id``.
        - ``HTTPException(502)`` if the advisory workflow returns a result
          that is not a dict, or whose ``proposal`` value is not a dict.
    """
    with _lock:
        ips = _profiles.get(request.user_id)

    if not ips:
        raise HTTPException(status_code=404, detail=f"No IPS found for user {request.user_id}")

    proposal_id = f"prop-{uuid.uuid4().hex[:8]}"
    result = _execute_advisory(
        "create_proposal",
        {"proposal_id": proposal_id, "request": request.model_dump(mode="json")},
        key=proposal_id,
    )
    if not isinstance(result, dict) or not isinstance(result.get("proposal"), dict):
        logger.error("Invalid advisory response for proposal %s: %r", proposal_id, result)
        raise HTTPException(
            status_code=502,
            detail="Advisory execution returned unexpected response structure",
        )
    return CreateProposalResponse(
        proposal_id=proposal_id, proposal=PortfolioProposal.model_validate(result["proposal"])
    )


@app.get("/proposals/{proposal_id}", response_model=GetProposalResponse)
def get_proposal(proposal_id: str) -> GetProposalResponse:
    """Get a portfolio proposal by ID.

    Postconditions:
        Returns ``found=True`` with the stored ``proposal`` when
        ``proposal_id`` exists; otherwise returns ``found=False`` with
        ``proposal=None`` (never raises 404 — missing is a normal, expected
        outcome here).
    """
    with _lock:
        proposal = _proposals.get(proposal_id)
    if not proposal:
        return GetProposalResponse(proposal_id=proposal_id, proposal=None, found=False)
    return GetProposalResponse(proposal_id=proposal_id, proposal=proposal, found=True)


@app.post("/proposals/{proposal_id}/validate", response_model=ValidateProposalResponse)
def validate_proposal(
    proposal_id: str, request: ValidateProposalRequest
) -> ValidateProposalResponse:
    """Validate a portfolio proposal against the user's IPS.

    Preconditions:
        ``proposal_id`` must identify a proposal created via
        ``create_proposal``; ``request.user_id`` must have an IPS created
        via ``create_profile``.
    Postconditions:
        Raises 404 if the proposal or the user's IPS is missing. Otherwise
        returns whether the proposal passed IPS validation and any
        violations found; does not mutate the stored proposal.

    Raises:
        - ``HTTPException(502)`` if the advisory workflow returns a result
          that is not a dict, whose ``valid`` value is not a bool, or whose
          ``violations`` value is not a list.
    """
    with _lock:
        proposal = _proposals.get(proposal_id)
        ips = _profiles.get(request.user_id)

    if not proposal:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")
    if not ips:
        raise HTTPException(status_code=404, detail=f"No IPS found for user {request.user_id}")

    result = _execute_advisory(
        "validate_proposal",
        {"proposal_id": proposal_id, "user_id": request.user_id},
        key=proposal_id,
    )
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("valid"), bool)
        or not isinstance(result.get("violations"), list)
    ):
        logger.error(
            "Invalid advisory response for proposal %s validation: %r", proposal_id, result
        )
        raise HTTPException(
            status_code=502,
            detail="Advisory execution returned unexpected response structure",
        )
    return ValidateProposalResponse(
        proposal_id=proposal_id,
        valid=result["valid"],
        violations=result["violations"],
    )


@app.post("/strategies", response_model=CreateStrategyResponse)
def create_strategy(request: CreateStrategyRequest) -> CreateStrategyResponse:
    """Create a new investment strategy specification.

    Preconditions:
        ``request`` rejects unknown fields (``model_config extra="forbid"``)
        so stale/legacy client payloads fail fast rather than silently
        dropping fields.
    Postconditions:
        Raises 422 if the constructed ``StrategySpec`` fails its own field
        validation (e.g. an invalid ``asset_class``). Otherwise persists the
        strategy under a freshly generated ``strategy_id`` and returns it.
    """
    strategy_id = f"strat-{uuid.uuid4().hex[:8]}"

    # ``StrategySpec`` enforces its field contracts (e.g. the asset_class
    # vocabulary) at construction. A bad value here is a client error, not a
    # server fault, so translate the Pydantic ValidationError into a 422
    # instead of letting it surface as an unhandled 500.
    try:
        strategy = StrategySpec(
            strategy_id=strategy_id,
            authored_by=request.authored_by,
            asset_class=request.asset_class,
            hypothesis=request.hypothesis,
            signal_definition=request.signal_definition,
            timeframe=request.timeframe,
            entry_rules=request.entry_rules,
            exit_rules=request.exit_rules,
            sizing=request.sizing if request.sizing is not None else DEFAULT_SIZING_PAYLOAD,
            risk_limits=request.risk_limits,
            speculative=request.speculative,
        )
    except ValidationError as exc:
        # ``include_context=False`` drops the raw exception object Pydantic
        # stashes under ``ctx`` (a ValueError isn't JSON-serializable), which
        # would otherwise make FastAPI fail to encode the 422 body and 500.
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False, include_context=False)
        ) from exc

    # Persist through the Temporal workflow (Temporal-only). The strategy was
    # already constructed/validated above, so return that instance verbatim.
    _execute_advisory(
        "create_strategy",
        {"strategy_id": strategy_id, "strategy": strategy.model_dump(mode="json")},
        key=strategy_id,
    )
    return CreateStrategyResponse(strategy_id=strategy_id, strategy=strategy)


@app.post("/strategies/{strategy_id}/validate", response_model=ValidateStrategyResponse)
def validate_strategy(
    strategy_id: str, request: ValidateStrategyRequest
) -> ValidateStrategyResponse:
    """Run validation checks on a strategy.

    Preconditions:
        ``strategy_id`` must identify a strategy created via
        ``create_strategy``.
    Postconditions:
        Raises 404 if the strategy is missing. Otherwise returns the
        ``ValidationReport`` produced by the validation checks, whether the
        strategy passed, and any failures found.

    Raises:
        - ``HTTPException(502)`` if the advisory workflow returns a result
          that is not a dict, is missing ``validation``, ``passed``, or
          ``failures``, or whose ``validation``/``passed``/``failures``
          values are not a dict/bool/list respectively.
    """
    with _lock:
        strategy = _strategies.get(strategy_id)

    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

    result = _execute_advisory(
        "validate_strategy",
        {"strategy_id": strategy_id, "request": request.model_dump(mode="json")},
        key=strategy_id,
    )
    required_keys = ("validation", "passed", "failures")
    if (
        not isinstance(result, dict)
        or any(key not in result for key in required_keys)
        or not isinstance(result["validation"], dict)
        or not isinstance(result["passed"], bool)
        or not isinstance(result["failures"], list)
    ):
        logger.error(
            "Invalid advisory response for strategy %s validation: %r", strategy_id, result
        )
        raise HTTPException(
            status_code=502,
            detail="Advisory execution returned unexpected response structure",
        )
    return ValidateStrategyResponse(
        strategy_id=strategy_id,
        validation=ValidationReport.model_validate(result["validation"]),
        passed=result["passed"],
        failures=result["failures"],
    )


class BacktestJobSubmission(BaseModel):
    """Immediate response returned when a backtest job is submitted for background execution."""

    job_id: str
    status: str = _BT_JOB_STATUS_PENDING


class BacktestJobStatus(BaseModel):
    """Full status and result payload for a single backtest job."""

    job_id: str
    status: str
    strategy_id: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BacktestJobListItem(BaseModel):
    """Summary of a single backtest job as returned in job-listing responses."""

    job_id: str
    status: str
    strategy_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BacktestJobListResponse(BaseModel):
    """Response wrapper containing the list of backtest jobs for the jobs-listing endpoint."""

    jobs: List[BacktestJobListItem]


class CancelBacktestJobResponse(BaseModel):
    """Response returned when a backtest job cancellation succeeds."""

    job_id: str
    status: str
    success: bool


class DeleteBacktestJobResponse(BaseModel):
    """Response returned when a backtest job is deleted."""

    job_id: str
    deleted: bool


def _run_backtest_background(
    job_id: str,
    strategy: StrategySpec,
    config: BacktestConfig,
    submitted_by: str,
    notes: List[str],
) -> str:
    """Background worker: run a real-data backtest and persist the completed record.

    Long-running (market data + sandbox execution), so this runs off the request
    thread (or via Temporal dispatch) to avoid proxy timeouts.

    Preconditions:
        - ``job_id`` must already exist in the backtest job store (created by
          ``run_backtest`` / ``_bt_create_job``), typically with status PENDING
        - ``strategy`` must be a valid ``StrategySpec`` suitable for
          ``_run_real_data_backtest``
        - ``config`` must be a valid ``BacktestConfig``
        - ``submitted_by`` and ``notes`` are recorded on the resulting
          ``BacktestRecord`` as-is

    Postconditions:
        - On the success path: job status becomes RUNNING then COMPLETED with a
          serialized ``RunBacktestResponse``; a ``BacktestRecord`` is stored under
          ``_backtests[backtest_id]``, where ``backtest_id`` is derived
          deterministically from ``job_id``. A second invocation for the same
          ``job_id`` (e.g. a Temporal activity retry that lands after a worker
          crash left the job at RUNNING) therefore overwrites the same record
          instead of orphaning a duplicate. Returns ``_BT_JOB_STATUS_COMPLETED``.
        - On ``InvestmentBacktestError`` or other exceptions: job status becomes
          FAILED with an error string, unless a cancel check already returned.
          Returns ``_BT_JOB_STATUS_FAILED`` after persisting FAILED.
        - If ``_bt_is_job_cancelled(job_id)`` is true at a check point, return
          ``_BT_JOB_STATUS_CANCELLED`` without writing COMPLETED or FAILED so the
          cancelled status visible at that check is preserved. Every
          status-changing ``_bt_update_job`` call (RUNNING, COMPLETED, FAILED)
          is immediately preceded by its own cancellation check — including a
          re-check taken right before the COMPLETED write, after the backtest
          record is built and stored — so no application-level work sits
          between a check and the update it guards.
    """
    try:
        if _bt_is_job_cancelled(job_id):
            return _BT_JOB_STATUS_CANCELLED
        _bt_update_job(job_id, status=_BT_JOB_STATUS_RUNNING)
        result, trades = _run_real_data_backtest(strategy, config)
        if _bt_is_job_cancelled(job_id):
            return _BT_JOB_STATUS_CANCELLED
        # Deterministic (not random) so a retry of the same job_id — e.g. a
        # Temporal activity retry after a worker crash left the job RUNNING —
        # overwrites the same record instead of minting a duplicate.
        backtest_id = f"bt-{hashlib.sha256(job_id.encode()).hexdigest()[:8]}"
        now = _now()
        record = BacktestRecord(
            backtest_id=backtest_id,
            strategy_id=strategy.strategy_id,
            strategy=strategy,
            config=config,
            submitted_by=submitted_by,
            submitted_at=now,
            completed_at=now,
            result=result,
            notes=notes,
            trades=trades,
        )
        with _lock:
            _backtests[backtest_id] = record
        if _bt_is_job_cancelled(job_id):
            return _BT_JOB_STATUS_CANCELLED
        _bt_update_job(
            job_id,
            status=_BT_JOB_STATUS_COMPLETED,
            result=RunBacktestResponse(backtest=record).model_dump(mode="json"),
            backtest_id=backtest_id,
        )
        return _BT_JOB_STATUS_COMPLETED
    except InvestmentBacktestError as exc:
        logger.error("Backtest job %s failed with domain error: %s", job_id, exc)
        if _bt_is_job_cancelled(job_id):
            return _BT_JOB_STATUS_CANCELLED
        _bt_update_job(job_id, status=_BT_JOB_STATUS_FAILED, error=str(exc))
        return _BT_JOB_STATUS_FAILED
    except Exception as exc:
        logger.exception("Backtest job %s failed", job_id)
        if _bt_is_job_cancelled(job_id):
            return _BT_JOB_STATUS_CANCELLED
        _bt_update_job(job_id, status=_BT_JOB_STATUS_FAILED, error=str(exc))
        return _BT_JOB_STATUS_FAILED


@app.post("/backtests", response_model=BacktestJobSubmission)
def run_backtest(request: RunBacktestRequest) -> BacktestJobSubmission:
    """Submit a backtest job against real historical market data.

    Preconditions:
        ``request.strategy_id`` must identify a strategy created via
        ``create_strategy``.
    Postconditions:
        Raises 404 (synchronously) if the strategy is missing. Otherwise
        creates a job and returns ``{job_id, status}`` immediately without
        waiting for the backtest to run; poll
        `GET /backtests/status/{job_id}` for the outcome. Strategies with
        generated ``strategy_code`` run in a sandbox (the normal Strategy
        Lab path); strategies without ``strategy_code`` are not rejected
        synchronously here — the job is accepted and
        ``_run_real_data_backtest`` raises ``MissingStrategyCodeError``
        inside ``_run_backtest_background`` once the job runs, which the
        job store surfaces as a FAILED status (poll the job to see the
        error), not a live HTTP 422 response from this endpoint.
    """
    with _lock:
        strategy = _strategies.get(request.strategy_id)

    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {request.strategy_id} not found")

    strategy = StrategySpec.parse_persisted(strategy)

    config = BacktestConfig(
        start_date=request.start_date,
        end_date=request.end_date,
        initial_capital=request.initial_capital,
        benchmark_symbol=request.benchmark_symbol,
        rebalance_frequency=request.rebalance_frequency,
        transaction_cost_bps=request.transaction_cost_bps,
        slippage_bps=request.slippage_bps,
    )

    job_id = str(uuid.uuid4())
    _bt_create_job(job_id, strategy_id=strategy.strategy_id)

    # When Temporal is enabled, dispatch the backtest as a durable workflow; on
    # any dispatch failure (or when Temporal is unavailable) fall back to the
    # in-process daemon thread so the job still runs and behavior is unchanged.
    if _dispatch_backtest_run(job_id, strategy, config, request.submitted_by, request.notes):
        return BacktestJobSubmission(job_id=job_id, status=_BT_JOB_STATUS_PENDING)

    thread = threading.Thread(
        target=_run_backtest_background,
        args=(job_id, strategy, config, request.submitted_by, request.notes),
        daemon=True,
    )
    thread.start()
    return BacktestJobSubmission(job_id=job_id, status=_BT_JOB_STATUS_PENDING)


@app.get("/backtests/status/{job_id}", response_model=BacktestJobStatus)
def get_backtest_job_status(job_id: str) -> BacktestJobStatus:
    """Return the current status of a backtest job.

    Preconditions:
        ``job_id`` identifies a job previously created by ``run_backtest``.
    Postconditions:
        Returns the job's status/result/error snapshot; raises 404 if no job
        with that ID exists.
    """
    data = _bt_get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return BacktestJobStatus(
        job_id=data.get("job_id", job_id),
        status=data.get("status", _BT_JOB_STATUS_PENDING),
        strategy_id=data.get("strategy_id"),
        result=data.get("result"),
        error=data.get("error"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


@app.get("/backtests/jobs", response_model=BacktestJobListResponse)
def list_backtest_jobs(running_only: bool = False) -> BacktestJobListResponse:
    """List backtest jobs, optionally filtered to only pending/running ones.

    Postconditions:
        Returns every job's summary when ``running_only`` is False; only jobs
        with status pending or running otherwise.
    """
    statuses = [_BT_JOB_STATUS_PENDING, _BT_JOB_STATUS_RUNNING] if running_only else None
    items = [
        BacktestJobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", _BT_JOB_STATUS_PENDING),
            strategy_id=j.get("strategy_id"),
            created_at=j.get("created_at"),
            updated_at=j.get("updated_at"),
        )
        for j in _bt_list_jobs(statuses=statuses)
    ]
    return BacktestJobListResponse(jobs=items)


@app.post("/backtests/jobs/{job_id}/cancel", response_model=CancelBacktestJobResponse)
def cancel_backtest_job(job_id: str) -> CancelBacktestJobResponse:
    """Cancel a pending or running backtest job.

    Preconditions:
        ``job_id`` identifies a job previously created by ``run_backtest``.
    Postconditions:
        Raises 404 if no job with that ID exists. Raises 409 if the job
        exists but is no longer pending/running (already completed, failed,
        or cancelled) and so cannot be cancelled. Otherwise cancels the job
        and returns ``{job_id, status: "cancelled", success: True}``.
    """
    data = _bt_get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if _bt_cancel_job(job_id):
        return CancelBacktestJobResponse(
            job_id=job_id, status=_BT_JOB_STATUS_CANCELLED, success=True
        )
    raise HTTPException(status_code=409, detail=f"Cannot cancel job in status {data.get('status')}")


@app.delete("/backtests/jobs/{job_id}", response_model=DeleteBacktestJobResponse)
def delete_backtest_job(job_id: str) -> DeleteBacktestJobResponse:
    """Delete a backtest job record.

    Preconditions:
        ``job_id`` identifies a job previously created by ``run_backtest``.
    Postconditions:
        Atomically deletes the job and returns ``{job_id, deleted: True}``.
        Raises 404 if no job with that ID exists at the moment of deletion,
        including when a concurrent request deleted it first — there is no
        separate existence check, so there is no window in which a race can
        turn a legitimate delete into a misleading response.
    """
    if not _bt_delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return DeleteBacktestJobResponse(job_id=job_id, deleted=True)


@app.get("/backtests", response_model=ListBacktestsResponse)
def list_backtests(strategy_id: Optional[str] = None) -> ListBacktestsResponse:
    """List recorded backtests, optionally filtered by strategy ID.

    Postconditions:
        Returns a ``_lock``-protected snapshot of ``_backtests``, with each
        stored record rehydrated via ``BacktestRecord.parse_persisted``. When
        ``strategy_id`` is given, only records whose ``strategy_id`` matches
        are kept — an unknown ``strategy_id`` yields an empty list, not a
        404, since no existence check is performed. Results are sorted
        newest-first by ``completed_at``. ``count`` is always
        ``len(items)`` after filtering, so it can never drift from the
        returned list.
    """
    with _lock:
        raw = list(_backtests.values())

    items = [BacktestRecord.parse_persisted(r) for r in raw]

    if strategy_id:
        items = [item for item in items if item.strategy_id == strategy_id]

    items.sort(key=lambda item: item.completed_at, reverse=True)
    return ListBacktestsResponse(items=items)


@app.post("/promotions/decide", response_model=PromotionDecisionResponse)
def promotion_decision(request: PromotionDecisionRequest) -> PromotionDecisionResponse:
    """Run promotion gate decision for a strategy.

    Postconditions:
        The decision is computed by ``promotion_decision_activity``, which may
        run in a different Temporal worker process. This route — always the API
        process that also serves ``/workflow/status``/``/workflow/queues`` —
        applies the activity's returned audit-log/escalation delta to the local
        ``_workflow_state`` so those reads stay consistent regardless of which
        process ran the activity. The activity result is fully validated
        before any of that shared state is mutated, so a malformed result
        never leaves ``_workflow_state`` partially updated.

    Raises:
        - ``HTTPException(404)`` if ``strategy_id`` or the user's IPS is not found.
        - ``HTTPException(400)`` if the strategy has no validation report.
        - ``HTTPException(502)`` if the advisory result is not a dict, is missing
          a ``decision`` field, has a non-list/non-string ``audit_log_appended``,
          or has a malformed ``escalation_enqueued`` payload (non-dict, missing
          ``queue``/``payload_id``/``priority``, or unknown queue name).
        - ``HTTPException(500)`` if ``decision`` fails ``PromotionDecision``
          validation.
    """
    with _lock:
        strategy = _strategies.get(request.strategy_id)
        validation = _validations.get(request.strategy_id)
        ips = _profiles.get(request.user_id)

    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {request.strategy_id} not found")
    if not validation:
        raise HTTPException(
            status_code=400, detail=f"Strategy {request.strategy_id} has no validation report"
        )
    if not ips:
        raise HTTPException(status_code=404, detail=f"No IPS found for user {request.user_id}")

    result = _execute_advisory(
        "promotion_decision",
        {
            "strategy_id": request.strategy_id,
            "user_id": request.user_id,
            "proposer_agent_id": request.proposer_agent_id,
            "approver_agent_id": request.approver_agent_id,
            "approver_role": request.approver_role,
            "approver_version": request.approver_version,
            "risk_veto": request.risk_veto,
            "human_live_approval": request.human_live_approval,
        },
        key=request.strategy_id,
    )

    # Validate the entire result up front — decision, audit-log shape, and
    # escalation queue item — before mutating any shared _workflow_state, so
    # a malformed activity result never leaves the audit log or queues
    # partially updated.
    if not isinstance(result, dict):
        logger.error(
            "Invalid advisory response for strategy %s promotion: %r", request.strategy_id, result
        )
        raise HTTPException(
            status_code=502,
            detail="Advisory execution returned unexpected response structure",
        )
    if "decision" not in result or result["decision"] is None:
        raise HTTPException(
            status_code=502,
            detail="Promotion decision result is missing required 'decision' field",
        )
    decision_data = result["decision"]
    if not isinstance(decision_data, dict):
        logger.error(
            "Invalid advisory decision for strategy %s promotion: %r",
            request.strategy_id,
            decision_data,
        )
        raise HTTPException(
            status_code=502,
            detail="Advisory execution returned unexpected response structure",
        )
    try:
        decision = PromotionDecision.model_validate(decision_data)
    except ValidationError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid decision payload: {exc}") from exc

    audit_entries = result.get("audit_log_appended") or []
    if not isinstance(audit_entries, list) or not all(isinstance(e, str) for e in audit_entries):
        raise HTTPException(
            status_code=502,
            detail="Promotion decision result has invalid 'audit_log_appended' entries",
        )

    escalation = result.get("escalation_enqueued")
    queue_item: QueueItem | None = None
    if escalation is not None:
        if not isinstance(escalation, dict):
            raise HTTPException(
                status_code=502,
                detail="Promotion decision result has invalid 'escalation_enqueued' payload",
            )
        queue_name = escalation.get("queue")
        payload_id = escalation.get("payload_id")
        priority = escalation.get("priority")
        if not isinstance(queue_name, str) or not queue_name:
            raise HTTPException(
                status_code=502,
                detail="Promotion decision result has invalid escalation queue name",
            )
        if queue_name not in _workflow_state.queues:
            raise HTTPException(status_code=502, detail=f"Unknown escalation queue '{queue_name}'")
        if not isinstance(payload_id, str) or not payload_id:
            raise HTTPException(
                status_code=502,
                detail="Promotion decision result has invalid escalation payload_id",
            )
        if not isinstance(priority, str) or not priority:
            raise HTTPException(
                status_code=502,
                detail="Promotion decision result has invalid escalation priority",
            )
        queue_item = QueueItem(queue=queue_name, payload_id=payload_id, priority=priority)

    with _lock:
        _workflow_state.audit_log.extend(audit_entries)
        if queue_item is not None:
            _workflow_state.queues[queue_item.queue].append(queue_item)
    return PromotionDecisionResponse(
        strategy_id=request.strategy_id,
        decision=decision,
    )


@app.get("/workflow/status", response_model=WorkflowStatusResponse)
def workflow_status() -> WorkflowStatusResponse:
    """Get the current workflow state.

    Postconditions:
        Takes no inputs; always returns 200. Returns a ``_lock``-protected,
        internally consistent snapshot of ``_workflow_state``: ``mode``,
        ``audit_log``, and ``queue_counts`` are all read together under the
        same lock acquisition. ``audit_log`` is a shallow copy, safe from
        later mutation of the underlying state. ``queue_counts`` maps each
        queue name to its current length only — not its entries; see
        ``workflow_queues`` for the entries themselves. ``_workflow_state``
        is mutated elsewhere (e.g. by ``promotion_decision``), so repeated
        calls may return different snapshots.
    """
    with _lock:
        mode = _workflow_state.mode.value
        audit_log = list(_workflow_state.audit_log)
        queue_counts = {q: len(items) for q, items in _workflow_state.queues.items()}

    return WorkflowStatusResponse(mode=mode, audit_log=audit_log, queue_counts=queue_counts)


@app.get("/workflow/queues", response_model=QueuesResponse)
def workflow_queues() -> QueuesResponse:
    """Get the contents of all workflow queues.

    Postconditions:
        Takes no inputs; always returns 200. Returns a ``_lock``-protected
        snapshot of every queue name currently present in
        ``_workflow_state.queues`` — pre-populated with the fixed set of
        known queue names (``research``, ``portfolio_design``,
        ``validation``, ``promotion``, ``execution``, ``escalation``) by
        ``WorkflowState``'s ``default_factory``; ``promotion_decision``
        only appends to one of these existing queues after validating the
        escalation payload's ``queue`` value against them, it does not
        create new queue keys. Each queue name maps to its full ordered
        list of items, converted to ``QueueItemResponse``.
    """
    with _lock:
        queues = {}
        for q_name, items in _workflow_state.queues.items():
            queues[q_name] = [
                QueueItemResponse(
                    queue=item.queue, payload_id=item.payload_id, priority=item.priority
                )
                for item in items
            ]

    return QueuesResponse(queues=queues)


@app.post("/memos", response_model=CreateMemoResponse)
def create_memo(request: CreateMemoRequest) -> CreateMemoResponse:
    """Generate an investment committee memo (runs as a Temporal workflow).

    Postconditions:
        Delegates entirely to ``_execute_advisory("committee_memo", ...)``
        with ``key=request.user_id`` as the idempotency/ordering key; no
        local precondition checks are performed (no user/IPS existence
        check, unlike ``promotion_decision``). On success, returns the
        generated ``InvestmentCommitteeMemo``.

    Raises:
        - ``HTTPException(503)`` when Temporal is disabled/unavailable; on
          any other workflow failure, the ``HTTPException`` that
          ``_translate_advisory_failure`` maps it to.
        - ``HTTPException(502)`` when the advisory result is not a dict or
          lacks a dict-shaped ``memo`` payload.
        - ``HTTPException(500)`` if the ``memo`` field fails
          ``InvestmentCommitteeMemo`` validation.
    """
    result = _execute_advisory(
        "committee_memo",
        {
            "user_id": request.user_id,
            "recommendation": request.recommendation,
            "rationale": request.rationale,
            "dissenting_views": request.dissenting_views,
        },
        key=request.user_id,
    )
    if not isinstance(result, dict) or not isinstance(result.get("memo"), dict):
        logger.error("Invalid advisory response for memo (user %s): %r", request.user_id, result)
        raise HTTPException(
            status_code=502,
            detail="Advisory execution returned unexpected response structure",
        )
    try:
        memo = InvestmentCommitteeMemo.model_validate(result["memo"])
    except ValidationError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid memo payload: {exc}") from exc
    return CreateMemoResponse(memo=memo)


# ---------------------------------------------------------------------------
# Strategy Lab — ideation, backtesting, and analysis
# ---------------------------------------------------------------------------


def _run_real_data_backtest(
    strategy: StrategySpec,
    config: BacktestConfig,
) -> tuple[BacktestResult, List[TradeRecord]]:
    """
    Run a backtest using real historical market data.

    Fetches OHLCV data for the backtest period, then executes the
    Strategy-Lab-generated Python script through the subprocess sandbox —
    the same execution path used by the Strategy Lab orchestrator and the
    paper-trading step — and derives metrics from the resulting trades.

    Only Strategy-Lab-generated scripts may produce trades. The prior
    LLM-per-bar fallback has been removed; strategies without
    ``strategy_code`` raise ``MissingStrategyCodeError``.

    This is a service-layer helper — it raises the domain exceptions in
    ``investment_team.exceptions`` rather than ``HTTPException``, so it stays
    usable from non-HTTP callers (Temporal activities, CLI tools, tests).
    Callers with an HTTP request context are responsible for translating
    those exceptions into the appropriate ``HTTPException``.

    Returns (BacktestResult, trade_ledger).

    Raises:
        - ``MissingStrategyCodeError`` if ``strategy.strategy_code`` is unset.
        - ``MarketDataUnavailableError`` if no market data could be fetched
          for the requested symbols/range.
        - ``LookaheadViolationError`` if the generated script accessed
          look-ahead (future) market data.
        - ``StrategyExecutionError`` if the generated script otherwise fails
          during execution.
    """
    # Lazy import: yfinance is slow to import; defer until a request arrives.
    from investment_team.market_data_service import MarketDataService

    if not strategy.strategy_code:
        raise MissingStrategyCodeError(
            "strategy_code is required. The legacy LLM-per-bar backtest "
            "path has been removed; regenerate the strategy via the "
            "Strategy Lab ideation agent."
        )

    market_service = MarketDataService()
    # Issue #523 — honour explicit target_symbols; otherwise fall back to
    # the asset-class default universe (capped to 5; #525 removes the cap).
    symbols = market_service.resolve_strategy_symbols(strategy)

    logger.info(
        "Fetching historical data for %s backtest (%s to %s, %d symbols)...",
        strategy.asset_class,
        config.start_date,
        config.end_date,
        len(symbols),
    )
    market_data = market_service.fetch_multi_symbol_range(
        symbols, strategy.asset_class, config.start_date, config.end_date
    )

    if not market_data:
        raise MarketDataUnavailableError(
            "Failed to fetch historical market data. Please check the date range and try again."
        )

    from investment_team.trading_service.modes.backtest import run_backtest

    total_bars = sum(len(bars) for bars in market_data.values())
    logger.info(
        "Executing generated strategy script through TradingService for %s (%d symbols, %d bars)",
        strategy.strategy_id,
        len(market_data),
        total_bars,
    )

    run = run_backtest(strategy=strategy, config=config, market_data=market_data)
    service_result = run.service_result

    if service_result.lookahead_violation:
        raise LookaheadViolationError(
            f"Strategy code attempted to access look-ahead data: {(service_result.error or '')}"
        )
    if service_result.error:
        # Any service-level error must fail the request — mid-run crashes
        # append closed trades *before* raising, so a non-empty ledger here
        # still represents a partial/failed execution and must not be
        # reported as a successful backtest.
        raise StrategyExecutionError(f"Strategy code execution failed: {service_result.error}")

    logger.info(
        "Backtest complete for %s: %d trades",
        strategy.strategy_id,
        len(run.trades),
    )
    return run.result, run.trades


# Cap on record.paper_trading_error's length: long enough for a useful
# summary of the failure, short enough to keep persisted records compact.
_MAX_PAPER_TRADING_ERROR_LENGTH = 500


class _PaperTradingDataUnavailable(Exception):
    """Raised inside the strategy lab cycle when market data can't be fetched for paper trading.

    Converted to a non-fatal ``paper_trading_status = "skipped"`` outcome by the caller.
    """


def _run_paper_trading_step(
    *,
    strategy: StrategySpec,
    strategy_code: str,
    backtest_record: BacktestRecord,
    initial_capital: float,
    transaction_cost_bps: float,
    slippage_bps: float,
    lookback_days: int,
) -> PaperTradingSession:
    """Run a paper-trading session inside a strategy lab cycle.

    Fetches recent market data and executes the orchestrator-generated
    ``strategy_code`` through the ``PaperTradingAgent``'s sandbox. Raises
    ``_PaperTradingDataUnavailable`` when no market data is available (caller
    converts to a non-fatal ``skipped`` outcome). Any other exception should
    propagate so the cycle records a ``failed`` status with the error message.

    Preconditions:
        - ``strategy_code`` is non-empty.
        - ``lookback_days`` is positive.
        - ``initial_capital``, ``transaction_cost_bps``, and ``slippage_bps``
          are all non-negative.
    """
    assert strategy_code, "strategy_code must be non-empty"
    assert lookback_days > 0, f"lookback_days must be positive, got {lookback_days}"
    assert initial_capital >= 0, f"initial_capital must be non-negative, got {initial_capital}"
    assert transaction_cost_bps >= 0, (
        f"transaction_cost_bps must be non-negative, got {transaction_cost_bps}"
    )
    assert slippage_bps >= 0, f"slippage_bps must be non-negative, got {slippage_bps}"

    from investment_team.market_data_service import MarketDataService
    from investment_team.paper_trading_agent import PaperTradingAgent

    market_service = MarketDataService()
    # Issue #523 — paper trading must honour the same universe as the
    # backtest that promoted this spec, otherwise a winning QQQ-only
    # strategy gets paper-traded against AAPL/MSFT/... .
    symbols = market_service.resolve_strategy_symbols(strategy)

    logger.info(
        "Paper-trading step: fetching %d days of market data for %d symbols (%s) ...",
        lookback_days,
        len(symbols),
        strategy.asset_class,
    )
    market_data = market_service.fetch_multi_symbol(symbols, strategy.asset_class, lookback_days)
    if not market_data:
        raise _PaperTradingDataUnavailable(
            "Failed to fetch market data for paper trading from external sources."
        )

    agent = PaperTradingAgent()
    return agent.run_session(
        strategy=strategy,
        strategy_code=strategy_code,
        backtest_record=backtest_record,
        market_data=market_data,
        initial_capital=initial_capital,
        transaction_cost_bps=transaction_cost_bps,
        slippage_bps=slippage_bps,
    )


class RunStrategyLabRequest(BaseModel):
    """Run one or more sequential ideation + backtest + analysis (+ paper-trading) cycles."""

    start_date: str = Field(default="2021-01-01", description="Backtest start date")
    end_date: str = Field(default="2024-12-31", description="Backtest end date")
    initial_capital: float = Field(default=100000.0, gt=0)
    benchmark_symbol: str = Field(default="SPY")
    transaction_cost_bps: float = Field(default=5.0, ge=0)
    slippage_bps: float = Field(default=2.0, ge=0)
    batch_size: int = Field(
        default=10,
        ge=1,
        le=25,
        description="Strategies to generate per batch (each batch sees all prior batches' results).",
    )
    batch_count: int = Field(
        default=1,
        ge=1,
        le=_MAX_BATCH_COUNT,
        description=(
            "Number of batches to run back-to-back. Each new batch ideates with full context "
            "of every strategy from prior batches and refreshes the signal-intelligence brief. "
            "Upper bound is configurable via STRATEGY_LAB_MAX_BATCH_COUNT (default 100)."
        ),
    )
    max_parallel: int = Field(
        # Cap the default at the configured ceiling: Pydantic v2 doesn't validate
        # field defaults, so a bare `default=3` would slip past `le=_MAX_PARALLEL`
        # for an omitted request when an operator lowers STRATEGY_LAB_MAX_PARALLEL
        # below 3, bypassing the advertised cap.
        default=min(3, _MAX_PARALLEL),
        ge=1,
        le=_MAX_PARALLEL,
        description="Max strategies to generate in parallel per wave (within a batch).",
    )
    # Paper-trading step (only runs when a cycle's backtest is flagged as winning)
    paper_trading_enabled: bool = Field(
        default=True,
        description=(
            "When True (default), each winning strategy is paper-traded as part of the "
            "cycle. Losing strategies always skip paper trading regardless of this flag."
        ),
    )
    paper_trading_lookback_days: int = Field(
        # Cap the default at the configured ceiling: Pydantic v2 doesn't validate
        # field defaults, so a bare `default=365` would slip past
        # `le=_MAX_PAPER_TRADING_LOOKBACK_DAYS` for an omitted request when an
        # operator lowers STRATEGY_LAB_MAX_PAPER_TRADING_LOOKBACK_DAYS below 365,
        # bypassing the advertised cap (the UI omits this field).
        default=min(365, _MAX_PAPER_TRADING_LOOKBACK_DAYS),
        ge=30,
        le=_MAX_PAPER_TRADING_LOOKBACK_DAYS,
        description=(
            "Days of recent market data to fetch for paper trading. Upper "
            "bound is configurable via STRATEGY_LAB_MAX_PAPER_TRADING_LOOKBACK_DAYS "
            "(default 3650)."
        ),
    )
    allowed_asset_classes: Optional[List[str]] = Field(
        default=None,
        description=(
            "Asset categories the design agent is allowed to generate strategies for — "
            "a subset of: stocks, crypto, forex, futures, commodities. Common aliases "
            "('stock'/'equity'/'equities', 'fx', 'commodity'/'metal'/'energy', "
            "'cryptocurrency') are accepted and normalized. When omitted or null, every "
            "category is allowed. When provided it must resolve to at least one valid "
            "category; 'options' and unrecognized values are dropped."
        ),
    )

    @field_validator("allowed_asset_classes")
    @classmethod
    def _normalize_allowed_asset_classes(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        """Normalize the category selection to canonical, ideation-valid labels.

        Preconditions: ``value`` is ``None`` or a list of category strings.
        Postconditions: returns ``None`` (no constraint) when ``value`` is
        ``None``; otherwise a canonical-ordered, deduplicated, non-empty subset
        of the ideation-valid classes. Raises ``ValueError`` (surfaced by
        FastAPI as HTTP 422) when a non-null selection contains no valid
        category, so the lab never starts a run constrained to zero categories.
        """
        normalized = normalize_allowed_asset_classes(value)
        if normalized is None:
            return None
        if not normalized:
            raise ValueError(
                "allowed_asset_classes must contain at least one of: "
                + ", ".join(PROMPT_ASSET_CLASSES)
            )
        return normalized


class StrategyLabRunResponse(BaseModel):
    """Summary of a single completed ideation + backtest + analysis cycle.

    ``count`` is the number of records in ``records`` (0 or 1 for a single
    cycle) — enforced by a model_validator. Not currently used as a FastAPI
    ``response_model``.
    """

    records: List[StrategyLabRecord] = Field(default_factory=list)
    count: int = 0
    message: str = "Strategy ideated, backtested, and analysed successfully."

    @model_validator(mode="after")
    def _derive_count_from_records(self) -> "StrategyLabRunResponse":
        """Recompute ``count`` from ``records`` so it can never drift apart.

        Postconditions: ``count == len(records)`` regardless of what was
        passed in for ``count``.
        """
        self.count = len(self.records)
        return self


class StrategyLabResultsResponse(BaseModel):
    """Response body for ``GET /strategy-lab/results``.

    ``items``/``count``/``winning_count``/``losing_count`` are all derived
    from the same (already-filtered, when ``?winning=`` is given) ``items``
    list, so ``winning_count + losing_count == count`` always holds — a
    ``?winning=true`` request reports ``losing_count == 0`` rather than the
    unfiltered global losing count. (The UI's winning/losing tab chips call
    the endpoint unfiltered, so they always see the full-set counts.)
    """

    items: List[StrategyLabRecord] = Field(default_factory=list)
    count: int = 0
    winning_count: int = 0
    losing_count: int = 0

    @model_validator(mode="after")
    def _derive_counts_from_items(self) -> "StrategyLabResultsResponse":
        """Recompute all three count fields from ``items`` so they can never drift apart.

        Postconditions: ``count == len(items)``; ``winning_count``/
        ``losing_count`` equal the number of ``items`` with
        ``is_winning`` True/False, regardless of what was passed in for
        those fields.
        """
        self.count = len(self.items)
        self.winning_count = sum(1 for r in self.items if r.is_winning)
        self.losing_count = self.count - self.winning_count
        return self


def _normalize_strategy_lab_asset_class(raw: object) -> str:
    """Map LLM output to canonical labels used by the simulated ledger.

    Preconditions: ``raw`` may be any value, including ``None`` or an
    unrecognized string — no type check or membership check required of
    the caller.
    Postconditions: returns one of the six canonical asset-class labels
    (``stocks``, ``crypto``, ``forex``, ``options``, ``futures``,
    ``commodities``). ``None``, empty, or unrecognized input defaults to
    ``"stocks"``; recognized aliases (e.g. ``"equity"``, ``"fx"``,
    ``"cryptocurrency"``) map to their canonical class. Never raises —
    delegates entirely to :func:`normalize_asset_class`, which is total
    over ``object``.
    """
    return normalize_asset_class(raw)


# The timeframe values StrategySpec.timeframe (a strict Literal) accepts.
# Derived from the field itself so this can never drift out of sync with
# models.py.
_STRATEGY_SPEC_TIMEFRAMES: frozenset[str] = frozenset(
    get_args(StrategySpec.model_fields["timeframe"].annotation)
)


def _coerce_strategy_lab_timeframe(raw: object) -> str:
    """Return ``raw`` if it's a timeframe ``StrategySpec`` accepts, else ``"1d"``.

    Preconditions: ``raw`` may be any value, including ``None`` or an
    unrecognized/malformed string — no type check required of the caller.
    Postconditions: returns ``raw`` unchanged when it's a member of
    ``_STRATEGY_SPEC_TIMEFRAMES``; otherwise returns ``"1d"``. Never raises.
    """
    return raw if raw in _STRATEGY_SPEC_TIMEFRAMES else "1d"


def _normalize_strategy_lab_rule_list(raw: Any) -> List[Dict[str, Any]]:
    """Coerce a raw ideation ``entry_rules``/``exit_rules`` field to a list of dict entries.

    Preconditions:
        ``raw`` may be any type: absent (``None``), a list, a single dict
        (the LLM occasionally collapses a one-rule list to a bare dict), or
        something else entirely.
    Postconditions:
        - A ``dict`` is wrapped in a one-element list -- without this, ``dict
          or []`` evaluates truthy to the dict itself, and iterating a dict
          yields its string keys (none of which are dicts), so the entire
          rule set was silently discarded instead of recovered.
        - A ``list`` is filtered to its dict-valued entries (non-dict / non-DSL
          items are discarded so a malformed ideation LLM response doesn't
          crash the cycle).
        - Anything else (``None``, a string, a number, ...) returns ``[]``.
    """
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def _build_strategy_from_ideation(strategy_data: Dict[str, Any]) -> tuple[StrategySpec, str]:
    """Build a StrategySpec + strategy_id from raw ideation output.

    Preconditions: ``strategy_data`` must be a dict-like mapping (the raw
    parsed ideation payload) — this function reads it via ``.get()`` and
    iterates its list-valued fields. Malformed *values* within the mapping
    (non-dict rule entries, non-dict ``sizing``, etc.) are handled
    permissively and defaulted/discarded; only a non-mapping
    ``strategy_data`` itself is a contract violation.
    Postconditions: returns a valid ``StrategySpec`` plus the same
    freshly generated ``strat-lab-`` prefixed ``strategy_id``.

    Raises:
        TypeError: if ``strategy_data`` is not a mapping.
    """
    if not isinstance(strategy_data, dict):
        raise TypeError(f"strategy_data must be a mapping, got {type(strategy_data).__name__}")
    strategy_id = f"strat-lab-{uuid.uuid4().hex[:8]}"
    raw_sizing = strategy_data.get("sizing")
    sizing = raw_sizing if isinstance(raw_sizing, dict) else DEFAULT_SIZING_PAYLOAD
    strategy = StrategySpec(
        strategy_id=strategy_id,
        authored_by="strategy_ideation_agent",
        asset_class=_normalize_strategy_lab_asset_class(strategy_data.get("asset_class")),
        hypothesis=str(strategy_data.get("hypothesis", "")),
        signal_definition=str(strategy_data.get("signal_definition", "")),
        # Ideation must declare a timeframe StrategySpec accepts. Default to
        # "1d" when the LLM omitted the field or returned a value outside
        # the allowed set (e.g. "1x") -- the prompt makes a valid timeframe
        # mandatory, but this fallback keeps the cycle alive on a
        # clearly-resolvable omission/typo instead of raising a strict
        # pydantic ValidationError deep inside StrategySpec construction.
        timeframe=_coerce_strategy_lab_timeframe(strategy_data.get("timeframe")),
        entry_rules=_normalize_strategy_lab_rule_list(strategy_data.get("entry_rules")),
        exit_rules=_normalize_strategy_lab_rule_list(strategy_data.get("exit_rules")),
        sizing=sizing,
        risk_limits=strategy_data.get("risk_limits") or {},
        speculative=bool(strategy_data.get("speculative", False)),
    )
    return strategy, strategy_id


def _run_one_strategy_lab_cycle(
    config: BacktestConfig,
    orchestrator: "StrategyLabOrchestrator",
    *,
    precomputed_signal_brief: Optional[SignalIntelligenceBriefV1] = None,
    signal_brief_storage: Optional[Dict[str, Any]] = None,
    prior_records: Optional[List[StrategyLabRecord]] = None,
    on_phase: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    exclude_asset_classes: Optional[List[str]] = None,
    paper_trading_enabled: bool = True,
    paper_trading_lookback_days: int = 365,
) -> StrategyLabRecord:
    """Single ideation → validate → execute → refine → analyze (+ paper-trading) cycle via the v2 orchestrator.

    The orchestrator handles the full code-generation + sandboxed-execution pipeline
    internally, including up to 10 refinement rounds. Once it returns a complete
    ``StrategyLabRecord``, this function delegates paper-trading finalization and
    persistence to :func:`_finalize_strategy_lab_cycle_record` — see that function's
    docstring for the winning/publishable/disabled/failure semantics; this function
    does not implement any of that logic itself.

    Args:
        prior_records: Precomputed prior-record snapshot, supplied by the wave
            driver so the whole table isn't re-read + re-parsed per concurrent
            cycle. Precondition: it must reflect pre-wave state (the caller reads
            it once before launching the wave). When None, this cycle reads and
            parses the snapshot itself (the path direct/test callers take).
        paper_trading_enabled: Forwarded to :func:`_finalize_strategy_lab_cycle_record`.
        paper_trading_lookback_days: Forwarded to :func:`_finalize_strategy_lab_cycle_record`.

    Returns:
        The finalized ``StrategyLabRecord``, already durably persisted by
        :func:`_finalize_strategy_lab_cycle_record`.
    """

    # When the caller (the wave driver) precomputes the prior records once per
    # wave, reuse that snapshot instead of re-reading + re-parsing the whole
    # table in every concurrent cycle. Each cycle persists only at its very end,
    # long after every sibling has already read prior records at its start, so
    # all cycles in a wave observe the same pre-wave snapshot; reading it once up
    # front is equivalent (and strictly more deterministic).
    if prior_records is None:
        prior_records = _snapshot_prior_records()

    record = orchestrator.run_cycle(
        prior_records=prior_records,
        config=config,
        signal_brief=precomputed_signal_brief,
        on_phase=on_phase,
        exclude_asset_classes=exclude_asset_classes,
    )

    return _finalize_strategy_lab_cycle_record(
        record,
        signal_brief_storage=signal_brief_storage,
        paper_trading_enabled=paper_trading_enabled,
        paper_trading_lookback_days=paper_trading_lookback_days,
        on_phase=on_phase,
    )


def _persist_strategy_lab_record(record: StrategyLabRecord) -> None:
    """Durably persist a completed cycle's record + strategy + backtest.

    Preconditions:
        ``record`` is a fully assembled ``StrategyLabRecord`` (post
        paper-trading step) with ``record.strategy`` / ``record.backtest``
        populated.
    Postconditions:
        ``record`` / ``record.strategy`` / ``record.backtest`` are written to
        the ``JobServiceClient``-backed ``_strategy_lab_records`` /
        ``_strategies`` / ``_backtests`` stores, keyed by their respective
        ids. Extracted from ``_run_one_strategy_lab_cycle`` so a Temporal
        activity can reuse the identical write without duplicating it.

    Raises:
        ``ValueError``: ``record.strategy`` or ``record.backtest`` is
        ``None``. Checked before acquiring ``_lock``, so a caller that
        violates the precondition gets a clear contract failure instead of
        an opaque ``AttributeError`` from inside the locked section.
    """
    # StrategySpec/BacktestRecord are required (non-Optional) fields, but
    # Pydantic doesn't validate on assignment here, so a stray `record.strategy
    # = None` elsewhere would otherwise surface as an opaque AttributeError
    # below instead of a clear precondition failure at this boundary. A bare
    # `assert` would be stripped under `python -O`, silently admitting a None
    # value; raise explicitly so this precondition always holds.
    if record.strategy is None:
        raise ValueError("record.strategy must be populated before persisting")
    if record.backtest is None:
        raise ValueError("record.backtest must be populated before persisting")
    with _lock:
        _strategy_lab_records[record.lab_record_id] = record
        _strategies[record.strategy.strategy_id] = record.strategy
        _backtests[record.backtest.backtest_id] = record.backtest


def _finalize_strategy_lab_cycle_record(
    record: StrategyLabRecord,
    *,
    signal_brief_storage: Optional[Dict[str, Any]] = None,
    paper_trading_enabled: bool = True,
    paper_trading_lookback_days: int = 365,
    on_phase: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> StrategyLabRecord:
    """Attach the signal brief, run the gated paper-trading step, and persist.

    Extracted verbatim from ``_run_one_strategy_lab_cycle``'s tail so the
    Temporal ``finalize_cycle_record_activity`` and the thread-mode wave driver
    share one implementation of the post-``run_cycle`` finalization instead of
    duplicating (and risking drift on) the ~60-line paper-trading branch.

    Preconditions:
        ``record`` is a fully assembled ``StrategyLabRecord`` returned by
        ``StrategyLabOrchestrator.run_cycle`` (``record.strategy`` /
        ``record.backtest`` populated). ``on_phase`` is either ``None`` or a
        callable ``(phase: str, data: dict) -> None`` that receives
        phase-change notifications (``paper_trading``, ``paper_trading_complete``,
        ``paper_trading_skipped``, ``paper_trading_failed``); when ``None``, those
        events are silently dropped (the Temporal ``finalize_cycle_record_activity``
        passes ``None`` — SSE progress is a separate concern there).
    Postconditions:
        Returns the same ``record`` with its ``signal_intelligence_brief`` set
        (when ``signal_brief_storage`` is given and it was empty) and its
        ``paper_trading_*`` fields resolved — ``skipped`` for non-winning /
        non-publishable / disabled / no-code / no-data cases, ``completed`` on
        success, ``failed`` (non-fatal) on a paper-trading error. The record is
        durably persisted via :func:`_persist_strategy_lab_record` before
        returning. Any ``on_phase`` callback fires as a side effect only; it
        never affects the returned record — an exception raised by the
        callback is caught and logged, never propagated, so a broken
        callback can't skip persistence or abort finalization.
    """

    def _emit(phase: str, data: Optional[Dict[str, Any]] = None) -> None:
        if on_phase:
            try:
                on_phase(phase, data or {})
            except Exception:
                logger.exception("Strategy lab phase callback failed for phase %s", phase)

    # Attach signal brief before persisting (PersistentDict serializes at assignment)
    if signal_brief_storage and not record.signal_intelligence_brief:
        record.signal_intelligence_brief = signal_brief_storage

    # --- Paper-trading step (gated on publishable backtest) ---------------
    # Only publishable winners proceed to paper trading; failures are
    # non-fatal so the valid backtest record is still persisted. The
    # standalone /strategy-lab/paper-trade endpoint can be used to retry later.
    strategy_preview = {
        "asset_class": record.strategy.asset_class,
        "hypothesis": record.strategy.hypothesis,
    }
    if not record.is_winning:
        record.paper_trading_status = "skipped"
        record.paper_trading_skipped_reason = "not_winning"
        _emit("paper_trading_skipped", {"reason": "not_winning"})
    elif not record.is_publishable:
        reason = record.publishability_skip_reason or "not_publishable"
        record.paper_trading_status = "skipped"
        record.paper_trading_skipped_reason = reason
        _emit("paper_trading_skipped", {"reason": reason})
    elif not paper_trading_enabled:
        record.paper_trading_status = "skipped"
        record.paper_trading_skipped_reason = "disabled"
        _emit("paper_trading_skipped", {"reason": "disabled"})
    elif not record.strategy_code:
        # Orchestrator didn't produce runnable strategy code; nothing to paper-trade
        record.paper_trading_status = "skipped"
        record.paper_trading_skipped_reason = "no_strategy_code"
        _emit("paper_trading_skipped", {"reason": "no_strategy_code"})
    else:
        _emit("paper_trading", {"strategy": strategy_preview})
        try:
            # Use the backtest record's config which has asset-class-resolved
            # fees (the orchestrator may have overridden generic defaults).
            bt_config = record.backtest.config
            session = _run_paper_trading_step(
                strategy=record.strategy,
                strategy_code=record.strategy_code,
                backtest_record=record.backtest,
                initial_capital=bt_config.initial_capital,
                transaction_cost_bps=bt_config.transaction_cost_bps,
                slippage_bps=bt_config.slippage_bps,
                lookback_days=paper_trading_lookback_days,
            )
            session.lab_record_id = record.lab_record_id
            with _lock:
                _paper_trading_sessions[session.session_id] = session
            record.paper_trading_session_id = session.session_id
            record.paper_trading_status = "completed"
            record.paper_trading_verdict = session.verdict
            _emit(
                "paper_trading_complete",
                {
                    "session_id": session.session_id,
                    "verdict": session.verdict.value if session.verdict else None,
                    "trade_count": len(session.trades),
                },
            )
        except _PaperTradingDataUnavailable as exc:
            logger.warning("Paper trading step skipped due to missing market data: %s", exc)
            record.paper_trading_status = "skipped"
            record.paper_trading_skipped_reason = "no_market_data"
            _emit("paper_trading_skipped", {"reason": "no_market_data", "detail": str(exc)})
        except Exception as exc:
            logger.exception("Paper trading step failed (non-fatal)")
            record.paper_trading_status = "failed"
            record.paper_trading_error = str(exc)[:_MAX_PAPER_TRADING_ERROR_LENGTH]
            _emit("paper_trading_failed", {"detail": record.paper_trading_error})

    _persist_strategy_lab_record(record)

    return record


def _strategy_lab_signal_expert_enabled() -> bool:
    """Return whether the per-batch signal-intelligence expert is enabled.

    Gates ``_compute_signal_brief_snapshot`` (used by the Temporal
    ``compute_signal_brief_activity``): when disabled, that function skips
    the market-data fetch and ``SignalIntelligenceExpert`` call entirely and
    fails open with a ``{"skipped": True, ...}`` brief instead.

    Reads the ``STRATEGY_LAB_SIGNAL_EXPERT_ENABLED`` env var via
    ``shared.env_config.env_bool``, defaulting to enabled when unset.
    """
    return env_bool("STRATEGY_LAB_SIGNAL_EXPERT_ENABLED", default=True)


def _compute_signal_brief_snapshot(
    benchmark_symbol: str,
) -> tuple[Optional[SignalIntelligenceBriefV1], Dict[str, Any]]:
    """Build a per-batch signal brief over all currently-persisted prior records.

    Used by the Temporal ``compute_signal_brief_activity``. Called at the start
    of every batch so batch N+1 sees results from batches 1..N (and prior runs).

    Preconditions:
        ``benchmark_symbol`` is the run's benchmark ticker.
    Postconditions:
        Returns ``(brief, storage)``. ``storage`` is always a ``dict`` --
        never ``None`` -- even on failure. Fail-open: on disabled expert /
        provider-initialization failure / market-fetch failure / expert
        (including its own initialization) failure / provider-cleanup
        failure, it returns ``(None, {"skipped": True, ...})`` (or a
        degraded-market brief) rather than raising -- every step from
        provider construction through cleanup is guarded, not just
        ``expert.produce_signal_brief``'s body.
    """
    if not _strategy_lab_signal_expert_enabled():
        return None, {"skipped": True, "skipped_reason": "signal_expert_disabled"}

    try:
        provider = FreeTierMarketDataProvider()
    except Exception as exc:
        logger.warning("Failed to initialize market data provider: %s", exc)
        return None, {
            "skipped": True,
            "skipped_reason": "provider_init_failed",
            "error": str(exc),
        }

    try:
        try:
            market_ctx = provider.fetch_context(
                StrategyLabDataRequest(benchmark_symbol=benchmark_symbol)
            )
        except Exception as exc:
            logger.warning("Market data fetch failed: %s", exc)
            market_ctx = MarketLabContext(
                fetched_at=_now(),
                degraded=True,
                degraded_reason=str(exc),
                sources_used=[],
            )
        prior_for_brief = _snapshot_prior_records()

        try:
            expert = SignalIntelligenceExpert()
            t0 = datetime.now(tz=timezone.utc)
            brief = expert.produce_signal_brief(prior_for_brief, market_ctx)
            storage = brief.model_dump(mode="json")
            prov_text = market_ctx.as_prompt_text()
            storage["brief_provenance"] = {
                "expert": "signal_intelligence_v1",
                "market_snapshot_hash": hashlib.sha256(prov_text.encode()).hexdigest()[:16],
                "market_fetched_at": market_ctx.fetched_at,
                "market_degraded": market_ctx.degraded,
                "duration_ms": int((datetime.now(tz=timezone.utc) - t0).total_seconds() * 1000),
            }
            logger.info(
                "signal_intelligence brief_version=%s keys=%s degraded_market=%s",
                storage.get("brief_version"),
                # A cheap top-level-key count in place of len(str(storage)),
                # which serialized the entire brief to a string on every
                # call just to measure it.
                len(storage),
                market_ctx.degraded,
            )
            return brief, storage
        except Exception as exc:
            # Covers both SignalIntelligenceExpert() construction and
            # produce_signal_brief() itself -- either is an "expert
            # subsystem failed" outcome from the caller's perspective.
            logger.warning("Signal intelligence expert failed: %s", exc)
            return None, {
                "skipped": True,
                "skipped_reason": "expert_failed",
                "error": str(exc),
            }
    finally:
        # A cleanup failure here must not replace whatever the try block
        # already decided to return (a brief, or a skipped-with-reason
        # tuple) with an unhandled exception -- log and swallow instead.
        try:
            provider.close()
        except Exception as exc:
            logger.warning("Failed to close market data provider: %s", exc)


# Narrower than ``STRATEGY_LAB_TERMINAL_STATUSES`` (defined above): a run that
# reached ``completed``/``completed_with_errors`` ended on its own, not via an
# external stop signal, so those are deliberately excluded here.
_STRATEGY_LAB_EXTERNAL_TERMINAL_STATUSES = frozenset({"cancelled", "failed", "interrupted"})


def _strategy_lab_external_terminal_status(run_id: str) -> Optional[str]:
    """Return the run's persisted job-store status if it's an external stop signal.

    Preconditions:
        ``run_id`` is the strategy-lab run identifier.
    Postconditions:
        Returns the persisted job's exact ``status`` string ("cancelled",
        "failed", or "interrupted") when it is one of
        ``_STRATEGY_LAB_EXTERNAL_TERMINAL_STATUSES``; ``None`` on any read error or a
        non-terminal/absent status (never raises). Callers that need to
        distinguish a genuine user cancellation from another external stop
        (e.g. a service-wide "mark all interrupted" reconciliation, or an
        externally-recorded failure) must branch on the returned value
        rather than treating every non-``None`` result as "cancelled".
    """
    try:
        client = _get_lab_run_job_client()
        persisted = client.get_job(run_id)
        if persisted:
            status = persisted.get("status", "")
            if status in _STRATEGY_LAB_EXTERNAL_TERMINAL_STATUSES:
                return status
    except Exception:
        logger.debug("Failed to fetch external terminal status for run %s", run_id, exc_info=True)
    return None


def _is_strategy_lab_run_externally_stopped(run_id: str) -> bool:
    """Return True if the run's job-store status is any external stop signal.

    NOT limited to a genuine user cancellation -- True for
    ``cancelled``/``failed``/``interrupted`` alike (see
    ``_STRATEGY_LAB_EXTERNAL_TERMINAL_STATUSES``). Used by the Temporal
    ``is_run_cancelled_activity`` (whose activity name predates this rename
    and still reads "cancelled", but whose own docstring already documents
    this broader contract: the workflow uses it as a general "should I stop"
    check, not a cancellation-specific one).

    Preconditions:
        ``run_id`` is the strategy-lab run identifier.
    Postconditions:
        Returns True when the persisted job's ``status`` is one of
        ``cancelled``/``failed``/``interrupted``; False on any read error or a
        non-terminal/absent status (never raises). Callers that need to know
        WHICH of those three statuses triggered this (to avoid mislabeling
        one as another) should call ``_strategy_lab_external_terminal_status``
        directly instead; callers that need a genuine cancellation-only check
        should use ``_is_strategy_lab_run_cancelled`` instead.
    """
    return _strategy_lab_external_terminal_status(run_id) is not None


def _is_strategy_lab_run_cancelled(run_id: str) -> bool:
    """Return True only if the run's job-store status is exactly "cancelled".

    Precise counterpart to ``_is_strategy_lab_run_externally_stopped``: use
    this when a caller must distinguish a genuine user cancellation from a
    failure or interruption, both of which also stop a run externally but
    are not cancellations.

    Preconditions:
        ``run_id`` is the strategy-lab run identifier.
    Postconditions:
        Returns True when the persisted job's ``status`` is exactly
        ``"cancelled"``; False for ``"failed"``/``"interrupted"``/any
        non-terminal/absent status, or on any read error (never raises).
    """
    return _strategy_lab_external_terminal_status(run_id) == "cancelled"


def _dispatch_via_temporal(starter: Callable[[], None]) -> bool:
    """Dispatch a job through Temporal when it is enabled, else report failure.

    Centralizes the enable-check + graceful-degradation shared by every
    Temporal-aware endpoint: a dispatch failure must never poison the request
    (the caller has already registered in-memory/job state before calling), so
    any error is downgraded to ``False`` and the caller falls back to its
    in-process thread path.

    Preconditions:
        - ``starter`` is a no-arg callable that starts the durable workflow and
          returns ``None``; it may raise (e.g. ``RuntimeError`` when the worker
          client is not yet connected, or ``ImportError`` when Temporal support
          is absent).

    Postconditions:
        - Returns ``True`` iff the workflow was started — in which case the
          caller must NOT also run its thread path. Returns ``False`` when
          Temporal is disabled/unavailable, the enablement check itself
          raises, or the dispatch raised; every failure case is logged
          (except the expected "Temporal support not installed" ``ImportError``,
          which is silent). Never raises.
    """
    try:
        from shared.temporal import is_temporal_enabled

        temporal_enabled = is_temporal_enabled()
    except ImportError:
        return False
    except Exception:
        logger.exception("Temporal enablement check failed; falling back to in-process execution")
        return False
    if not temporal_enabled:
        return False
    try:
        starter()
        return True
    except Exception:
        logger.exception("Temporal dispatch failed; falling back to in-process execution")
        return False


def _dispatch_backtest_run(
    job_id: str,
    strategy: StrategySpec,
    config: BacktestConfig,
    submitted_by: str,
    notes: List[str],
) -> bool:
    """Dispatch a backtest job through Temporal (else report failure).

    Preconditions:
        - ``job_id`` names a backtest job already created in the job store.

    Postconditions:
        - Returns ``True`` iff the durable workflow was started; ``False`` (with
          the failure logged) otherwise, so the caller falls back to its
          daemon-thread path. Never raises.
    """

    def _start() -> None:
        from investment_team.temporal.start_workflow import start_backtest_workflow

        start_backtest_workflow(job_id, strategy, config, submitted_by, notes)

    return _dispatch_via_temporal(_start)


def _require_temporal() -> None:
    """Raise HTTP 503 when Temporal is unavailable.

    The paper-trading and advisory/orchestrator endpoints are Temporal-only (no
    in-process fallback), so they cannot run without a connected worker. This
    turns "``TEMPORAL_ADDRESS`` unset / support absent" into a clean 503 instead
    of a slow ``RuntimeError`` from the dispatch bridge.

    Preconditions:
        None.
    Postconditions:
        Returns ``None`` when Temporal is enabled; otherwise raises
        ``HTTPException(503)`` -- including when ``is_temporal_enabled()``
        itself raises (e.g. a misconfigured Temporal client), which is
        mapped to the same 503 rather than propagating as an unhandled 500.
    """
    try:
        from shared.temporal import is_temporal_enabled

        temporal_enabled = is_temporal_enabled()
    except ImportError as exc:  # pragma: no cover - shared.temporal always present
        raise HTTPException(
            status_code=503, detail="Temporal support is unavailable for this endpoint."
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="This endpoint requires a running Temporal worker (TEMPORAL_ADDRESS unset).",
        ) from exc
    if not temporal_enabled:
        raise HTTPException(
            status_code=503,
            detail="This endpoint requires a running Temporal worker (TEMPORAL_ADDRESS unset).",
        )


def _start_paper_trading(session_id: str, payload: Dict[str, Any]) -> None:
    """Dispatch ``PaperTradingWorkflow`` for a session (Temporal-only, fire-and-forget).

    Preconditions:
        - ``session_id`` names a session already created in ``running`` or
          ``OPENING`` status.
        - ``payload`` satisfies ``run_paper_trading_activity``'s preconditions.
    Postconditions:
        - The durable workflow is started. Raises ``HTTPException(503)`` when
          Temporal is disabled/unavailable.
    """
    _require_temporal()
    from investment_team.temporal.start_workflow import start_paper_trading_workflow

    start_paper_trading_workflow(session_id, payload)


def _signal_paper_trading_stop(session_id: str) -> None:
    """Signal ``PaperTradingWorkflow`` to stop a session (Temporal-only, idempotent).

    Preconditions:
        - ``session_id`` names a session whose workflow was started via
          :func:`_start_paper_trading`.
    Postconditions:
        - The ``stop`` signal is delivered. Raises ``HTTPException(503)`` when
          Temporal is disabled/unavailable.
    """
    _require_temporal()
    from investment_team.temporal.start_workflow import signal_paper_trading_stop

    signal_paper_trading_stop(session_id)


def _execute_advisory(op: str, payload: Dict[str, Any], *, key: str) -> Dict[str, Any]:
    """Execute an interactive advisory workflow and return its result (Temporal-only).

    Preconditions:
        - ``op`` is a known advisory operation; ``payload`` satisfies the
          corresponding activity's preconditions; ``key`` is a stable id for the
          logical operation.
    Postconditions:
        - Returns the raw workflow result dict verbatim; this helper does not
          validate the presence or shape of any expected key, so callers must
          guard the result themselves (an ``isinstance``/key-presence/value-type
          check) and raise ``HTTPException(502)`` for a malformed payload
          before indexing into it. Raises ``HTTPException(503)`` when Temporal
          is disabled/unavailable, when the Temporal client didn't become
          ready in time (a bare ``RuntimeError`` from
          ``shared.temporal._await_client``, mapped here to the same 503 as
          the up-front check), or when the ``investment_team.temporal.
          start_workflow`` module fails to import (a deployment/packaging
          defect, not a downstream advisory-workflow failure, so it is kept
          distinct from :func:`_translate_advisory_failure`'s 502 fallback).
          On any other workflow failure, raises the ``HTTPException``
          :func:`_translate_advisory_failure` maps it to (never an opaque
          unhandled exception).
    """
    _require_temporal()
    try:
        from investment_team.temporal.start_workflow import execute_advisory_workflow

        return execute_advisory_workflow(op, payload, key=key)
    except HTTPException:
        raise
    except (ImportError, ModuleNotFoundError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Required advisory workflow module is unavailable.",
        ) from exc
    except RuntimeError as exc:
        # ``shared.temporal._await_client`` raises exactly
        # RuntimeError("Temporal client not available; is the team's worker
        # running?") when TEMPORAL_ADDRESS is set but the worker's client
        # never became ready in time — the same "no running worker"
        # condition ``_require_temporal`` checks for up front, just
        # discovered later. Map only that specific condition to the same
        # 503; any other RuntimeError raised by workflow/activity code is a
        # distinct failure and belongs to _translate_advisory_failure like
        # every other exception type, not this 503.
        if "Temporal client not available" not in str(exc):
            raise _translate_advisory_failure(exc) from exc
        raise HTTPException(
            status_code=503,
            detail="This endpoint requires a running Temporal worker (client did not become ready in time).",
        ) from exc
    except Exception as exc:
        raise _translate_advisory_failure(exc) from exc


# Maps a Temporal ApplicationError's ``type`` (set by the advisory activities,
# e.g. ``ApplicationError(..., type="NotFound")``) to the HTTP status the API
# should return for that error condition.
_ADVISORY_ERROR_TYPE_STATUS: Dict[str, int] = {
    "NotFound": 404,
    "MissingFields": 400,
    "NoValidation": 400,
    "ValueError": 400,
}


def _translate_advisory_failure(exc: Exception) -> HTTPException:
    """Translate an advisory-workflow failure into the ``HTTPException`` a route documents.

    Preconditions:
        - ``exc`` is whatever ``execute_advisory_workflow``/``execute_workflow_sync``
          raised on a non-503 failure — typically a ``temporalio.client.
          WorkflowFailureError`` wrapping an ``ApplicationError``, but may also be
          a ``WorkflowAlreadyStartedError`` or a transport-level error (client not
          connected, RPC timeout).
    Postconditions:
        - Returns (does not raise) an ``HTTPException``, found by walking ``exc``'s
          cause chain: the mapped 404/400 for an ``ApplicationError`` whose
          ``type`` is a key in ``_ADVISORY_ERROR_TYPE_STATUS``; 502 (with the
          error's own message as detail) for an ``ApplicationError`` whose
          ``type`` is NOT a recognized key (``_ADVISORY_ERROR_TYPE_STATUS.get``'s
          fallback — so an unmapped advisory failure type never surfaces as an
          opaque unhandled 500); 409 for a ``WorkflowAlreadyStartedError``
          (workflow-id collision); or 502 with a generic dispatch-failure
          detail when the cause chain contains neither (e.g. a bare
          transport-level error) — the only case this function has no
          error-specific detail to surface, unlike the ``ApplicationError``
          case above, which always carries the underlying error's own message.
    """
    from temporalio.exceptions import ApplicationError as _AppErr
    from temporalio.exceptions import WorkflowAlreadyStartedError

    cause: Optional[BaseException] = exc
    seen: set[int] = set()
    # ``exc`` keeps its own __cause__ chain alive for this call's duration, so
    # id() reuse from garbage collection can't happen here — the depth cap is
    # pure belt-and-suspenders against a pathologically long or malformed chain.
    for _ in range(20):
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        if isinstance(cause, _AppErr):
            status = _ADVISORY_ERROR_TYPE_STATUS.get(cause.type or "", 502)
            return HTTPException(status_code=status, detail=cause.message)
        if isinstance(cause, WorkflowAlreadyStartedError):
            return HTTPException(
                status_code=409,
                detail="A request for this operation is already in progress; retry shortly.",
            )
        cause = cause.__cause__
    return HTTPException(status_code=502, detail=f"Advisory workflow dispatch failed: {exc}")


def _strategy_lab_run_failure(run_id: str) -> Optional[str]:
    """Return the error text when a strategy-lab run ended in a hard failure.

    Lets the Temporal activity surface a worker-level failure to Temporal (so it
    is visible/retried) instead of the worker swallowing the exception and the
    activity reporting success. Only the catastrophic ``"failed"`` terminal
    status counts — ``"completed_with_errors"`` (partial success) and
    ``"cancelled"`` (user action) are not failures.

    Preconditions:
        - ``run_id`` names a strategy-lab run.

    Postconditions:
        - Returns the run's ``error`` string (or a generic message) when its
          terminal status is ``"failed"``; otherwise returns ``None``.
    """
    state = _get_run_state(run_id)
    if state and state.get("status") == "failed":
        return str(state.get("error") or "strategy lab run failed")
    return None


def _backtest_job_status(job_id: str) -> Optional[str]:
    """Return the current status of a backtest job, or ``None`` if unknown.

    Preconditions:
        - ``job_id`` names a backtest job (may or may not exist).

    Postconditions:
        - Returns the job's ``status`` string when the job exists, else ``None``.
    """
    data = _bt_get_job(job_id)
    if data is None:
        return None
    return data.get("status")


@app.get("/strategy-lab/config", response_model=StrategyLabConfigResponse)
def get_strategy_lab_config() -> StrategyLabConfigResponse:
    """Return operator-tunable Strategy Lab limits for the UI to read on load."""
    return StrategyLabConfigResponse(
        batch_count_min=1,
        batch_count_max=_MAX_BATCH_COUNT,
        asset_categories=list(PROMPT_ASSET_CLASSES),
    )


@app.post("/strategy-lab/run", response_model=StrategyLabRunStartResponse)
def run_strategy_lab(request: RunStrategyLabRequest) -> StrategyLabRunStartResponse:
    """
    Start a strategy lab batch run in the background. Returns a run_id immediately.

    Use ``GET /strategy-lab/runs/{run_id}/stream`` for real-time SSE progress updates,
    or ``GET /strategy-lab/runs/{run_id}/status`` for polling.

    Raises ``HTTPException(409)`` when another run is already active, or
    (defense-in-depth, collision astronomically unlikely for a fresh uuid4)
    when another transition for this freshly-minted run_id is already in
    flight. An early, unlocked check runs before minting a run_id/acquiring
    its transition lock, so a rejected request never allocates a registry
    entry that would otherwise never be looked up again -- but that check
    alone can't stop two concurrent requests from both minting a run_id and
    reaching the ``_active_runs`` write before either observes the other, so
    the authoritative check is re-run atomically with that write, inside the
    same ``_lock`` acquisition (mirroring ``resume_strategy_lab_run``).

    If ``_persist_run_state`` raises after ``_active_runs[run_id]`` is set,
    the entry is removed before the exception propagates -- otherwise this
    run_id would stay advertised as active (``_ensure_no_active_run``/
    ``_no_active_run_locked`` both read ``_active_runs``) despite never
    having been persisted or dispatched, permanently 409ing every future
    request until process restart.
    """
    _ensure_no_active_run()

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    run_lock = _require_run_transition_lock(run_id)
    try:
        now = _now()
        total_cycles = request.batch_size * request.batch_count

        initial_state = _build_run_state(
            run_id,
            started_at=now,
            total_cycles=total_cycles,
            batch_size=request.batch_size,
            batch_count=request.batch_count,
            request_payload=request.model_dump(),
        )
        with _lock:
            _no_active_run_locked()
            _active_runs[run_id] = initial_state
        try:
            _persist_run_state(run_id, initial_state, create=True)
        except Exception:
            # Persistence failed: this run_id must not be left advertised as
            # active forever (_ensure_no_active_run/_no_active_run_locked both
            # read _active_runs), or every future /strategy-lab/run request
            # would 409 for the rest of the process's life over a run that
            # was never actually persisted or dispatched. Only remove the
            # entry if it's still the one we just installed -- an identity
            # check, not a bare pop, so a resume/restart that has since
            # replaced it with a new state object is never torn down.
            with _lock:
                if _active_runs.get(run_id) is initial_state:
                    _active_runs.pop(run_id, None)
            raise

        # Dispatch the run as a durable Temporal workflow so it survives a
        # worker/process restart and is visible in the Temporal UI.
        _dispatch_strategy_lab_run(run_id, request, generation=initial_state["generation"])
    finally:
        run_lock.release()

    return StrategyLabRunStartResponse(run_id=run_id, total_cycles=total_cycles)


@app.get("/strategy-lab/results", response_model=StrategyLabResultsResponse)
def get_strategy_lab_results(winning: Optional[bool] = None) -> StrategyLabResultsResponse:
    """
    Return all strategy lab records, sorted newest-first.
    Filter by winning/losing with ?winning=true or ?winning=false.

    Postconditions:
        - ``winning_count``/``losing_count`` are computed from the same
          (already-filtered, when ``winning`` is given) list as ``items``/
          ``count``, so ``winning_count + losing_count == count`` always
          holds -- a ``?winning=true`` request reports ``losing_count == 0``
          rather than the unfiltered global losing count. (The UI's
          winning/losing tab chips call this endpoint unfiltered, so they
          always see the full-set counts regardless.)
    """
    items = _snapshot_prior_records(reverse=True)

    if winning is not None:
        items = [r for r in items if r.is_winning == winning]

    return StrategyLabResultsResponse(items=items)


# ---------------------------------------------------------------------------
# Strategy Lab: jobs endpoint (for central Jobs Dashboard)
# ---------------------------------------------------------------------------


class InvestmentJobSummary(BaseModel):
    """Job summary for the central Jobs Dashboard."""

    job_id: str
    status: str
    label: str = ""
    progress: int = 0
    current_phase: Optional[str] = None
    created_at: Optional[str] = None


class InvestmentJobsListResponse(BaseModel):
    jobs: List[InvestmentJobSummary] = Field(default_factory=list)


@app.get(
    "/strategy-lab/jobs",
    response_model=InvestmentJobsListResponse,
    summary="List strategy lab runs as jobs",
)
def list_strategy_lab_jobs(running_only: bool = False) -> InvestmentJobsListResponse:
    """Return strategy lab runs in a format compatible with the central Jobs Dashboard.

    Preconditions:
        - None. ``running_only`` is an optional filter flag.
        - Every ``_active_runs`` entry has ``run_id`` and ``status`` set --
          guaranteed by construction, since the only writers of this dict
          (``_build_run_state`` and ``run_state.normalize_persisted_job``)
          unconditionally set both, and no mutation site ever removes them.

    Postconditions:
        - Merges in-memory ``_active_runs`` with persisted job-service records,
          deduplicated by run/job id (in-memory entries take precedence).
        - Side effect: for each in-memory run whose status is not in
          ``STRATEGY_LAB_TERMINAL_STATUSES``, ``_reconcile_run_progress`` is
          called, refreshing progress fields (and ``status``/``error`` on a
          terminal transition) in place. See its docstring for details.
        - When ``running_only`` is ``True``, the result is filtered to
          ``status in ("running", "pending")``.
        - Entries are sorted by ``created_at`` descending.

    Raises:
        - None on an expected job-service failure. ``current_cycle``/
          ``strategy`` reconciled from job-service data, and each persisted
          job's ``"data"`` field itself (defaulting to ``{}`` when it isn't a
          mapping), are not schema-validated at ingestion, so their shape is
          checked defensively before use rather than assumed -- including
          ``current_cycle["phase"]``, which is coerced to ``None`` unless it
          is already a ``str`` (the type ``InvestmentJobSummary.current_phase``
          requires), since a non-string value would otherwise fail Pydantic
          response validation with a 500. A job-service
          connection/transport failure (``httpx.HTTPError``) or an
          unconfigured ``JOB_SERVICE_URL`` (``RuntimeError``) is caught and
          logged around the ``list_jobs()`` call only, and the response falls
          back to the in-memory-only list; in that case this endpoint still
          returns 200. Each persisted record is then converted to an
          ``InvestmentJobSummary`` independently: a failure building any one
          record (e.g. a ``ValidationError`` from genuinely malformed data)
          is logged and that record is skipped -- it does not discard the
          other persisted records, nor the in-memory ones already collected.
    """
    jobs: List[InvestmentJobSummary] = []

    # Reconcile: refresh progress (and, on a terminal transition, status/error)
    # for every run we think is still active, before snapshotting. Mirrors the
    # call convention in `list_strategy_lab_runs`: the id set is read under
    # `_lock`, but `_reconcile_run_progress` itself must be called unlocked
    # since it acquires `_lock` internally (it is not reentrant).
    with _lock:
        running_ids = [
            rid
            for rid, r in _active_runs.items()
            if r.get("status") not in STRATEGY_LAB_TERMINAL_STATUSES
        ]
    for rid in running_ids:
        _reconcile_run_progress(rid)

    # Single consistent snapshot of in-memory runs, taken under one lock hold
    # (after reconciliation, so it reflects any refreshed values). Deriving
    # both the in-memory `jobs` entries and `in_memory_ids` from this same
    # snapshot (rather than re-reading `_active_runs` under a second
    # `with _lock:`) prevents a run added/removed between two separate reads
    # from being omitted from or duplicated in the merged result.
    with _lock:
        active_states = list(_active_runs.values())

    # Active in-memory runs
    for state in active_states:
        # `current_cycle` can arrive from unvalidated job-service data via
        # `_reconcile_run_progress` (which copies it in verbatim, with no
        # shape check), not just first-party code -- guard both levels
        # before indexing into them.
        cycle = state.get("current_cycle")
        cycle = cycle if isinstance(cycle, dict) else None
        phase = cycle.get("phase") if cycle else None
        phase = phase if isinstance(phase, str) else None
        hypothesis = ""
        if cycle:
            strategy = cycle.get("strategy")
            if isinstance(strategy, dict):
                hypothesis = strategy.get("hypothesis", "")[:60]
        completed = state.get("completed_cycles", 0)
        total = state.get("total_cycles", 1)
        progress = _job_progress_percent(completed, total)
        label = hypothesis or f"Strategy batch ({completed}/{total})"
        jobs.append(
            InvestmentJobSummary(
                job_id=state["run_id"],
                status=state["status"],
                label=label,
                progress=progress,
                current_phase=phase,
                created_at=state.get("started_at"),
            )
        )

    # Persisted runs from job service (completed runs not in memory). The
    # list_jobs() call itself is the only part wrapped in the narrowed
    # except below -- an expected, transient/environmental failure there
    # (job-service down, or JOB_SERVICE_URL unconfigured) legitimately means
    # "no persisted data available at all", so falling back to the
    # in-memory-only list for the whole block is correct. Once persisted
    # has been fetched, each record is converted independently (see the
    # per-record try/except inside the loop) so one malformed record can't
    # discard the rest.
    try:
        client = _get_lab_run_job_client()
        persisted = client.list_jobs() or []
    except (httpx.HTTPError, RuntimeError) as exc:
        # httpx.HTTPError: transport/connection/HTTP-status failures from the
        # job-service client. RuntimeError: JobServiceClient raises this when
        # JOB_SERVICE_URL is unconfigured. Both are expected, transient/
        # environmental failure modes -- fall back to the in-memory-only
        # list. Anything else (e.g. a TypeError/AttributeError from the
        # client itself) is a programming error and must propagate instead
        # of being silently swallowed here.
        logger.warning("Failed to load persisted strategy lab runs: %s", exc, exc_info=True)
        persisted = []

    in_memory_ids = {s["run_id"] for s in active_states}
    for job in persisted:
        jid = job.get("job_id", "")
        if jid in in_memory_ids:
            continue  # already included from in-memory
        try:
            data = job.get("data", job)
            if not isinstance(data, dict):
                # A malformed persisted record (e.g. "data" is a string/list/
                # None instead of a mapping) must degrade to sensible
                # defaults below, not raise AttributeError out of this route.
                data = {}
            completed = data.get("completed_cycles", 0)
            total = data.get("total_cycles", 1)
            progress = _job_progress_percent(completed, total)
            jobs.append(
                InvestmentJobSummary(
                    job_id=jid,
                    status=job.get("status", data.get("status", "completed")),
                    label=f"Strategy batch ({completed}/{total})",
                    progress=progress,
                    current_phase=None,
                    created_at=data.get("started_at"),
                )
            )
        except Exception:
            # A failure converting THIS ONE persisted record (e.g. a
            # genuinely malformed payload the isinstance guard above didn't
            # anticipate) must not discard every other persisted/in-memory
            # job already collected -- log distinctly and move on.
            logger.warning("Skipping malformed persisted strategy lab job %s", jid, exc_info=True)

    if running_only:
        jobs = [j for j in jobs if j.status in ("running", "pending")]

    jobs.sort(key=lambda j: j.created_at or "", reverse=True)
    return InvestmentJobsListResponse(jobs=jobs)


# ---------------------------------------------------------------------------
# Strategy Lab: run tracking endpoints (SSE + polling + list)
# ---------------------------------------------------------------------------


@app.post(
    "/strategy-lab/runs/{run_id}/resume",
    response_model=StrategyLabRunStartResponse,
    summary="Resume an interrupted strategy lab run",
    description="Resume from the last completed cycle. Skips cycles that already produced records.",
)
def resume_strategy_lab_run(run_id: str) -> StrategyLabRunStartResponse:
    """Resume a strategy lab run at the cycle it was interrupted.

    Preconditions:
        - ``run_id`` identifies a run whose status — read via ``_get_run_state``
          (the in-memory ``active_runs`` entry when present, else a durable
          fallback read; see its own docstring) INSIDE the transition lock
          below, not beforehand — is in
          ``RESUMABLE_STATUSES`` (pending/running/failed/interrupted/
          agent_crash). Reading it (or the counters/payload derived from it)
          before acquiring the lock could observe a stale snapshot: another
          transition for this same run_id could fully complete — write its
          own state, dispatch, and even reach a terminal status — before
          this request obtains the lock, at which point ``_ensure_no_active_run()``
          no longer sees a ``"running"`` entry to block on, and a resume
          built from the stale snapshot would rebuild the run from
          outdated counters/payload and dispatch duplicate work. Only a
          cheap existence check (``run_id`` resolves to *some* known run)
          runs before the lock, so a request for a nonexistent run_id never
          allocates a transition-lock entry.
        - The run's persisted ``request_payload`` is present and is a dict
          (the original ``RunStrategyLabRequest`` payload).
        - No other run currently has status ``"running"``. Checked and
          enforced atomically with the ``_active_runs[run_id]`` write below —
          both happen inside the same ``_lock`` acquisition (via
          ``_no_active_run_locked()``) — so a concurrent run/resume/restart
          for a DIFFERENT run_id can't interleave between an isolated check
          and an isolated write and also pass the check before either write
          lands.
        - No other run/resume/restart transition for this run_id is
          currently in flight (checked first, before re-reading state or
          calling ``_no_active_run_locked()``).

    Postconditions:
        - Re-seeds ``_active_runs[run_id]`` carrying forward all prior
          progress — ``completed_record_ids``/``errored_cycles``/
          ``errored_details``/``skipped_cycles``/``tracker_merge_error_count``
          — and persists the new state. The durable write omits
          ``generation`` (see ``_persist_run_state``'s ``exclude_fields``):
          the value read above is a snapshot, and a concurrent restart on
          another process/replica could mint a newer durable value in the
          gap before this write lands, so this write must never be able to
          regress the durable generation back down to that stale snapshot.
        - Re-reads the durable generation once more immediately before
          dispatch and, if it has since advanced past the earlier snapshot
          (a concurrent restart on another process/replica minted a newer
          one in the gap), dispatches with the newer value instead —
          narrowing, not eliminating, the window in which this resume's
          workflow could otherwise be dispatched carrying a generation that
          is already stale, which would permanently fence out its own
          activities.
        - Dispatches the durable Temporal workflow from the first
          not-yet-contiguously-completed cycle, so no already-persisted cycle
          is re-run (and thus never duplicated).
        - Returns the run's start response with the resume offset and total
          cycle count.

    Raises:
        - ``HTTPException`` 404: ``run_id`` does not resolve to any known run.
          Can also fire from the post-lock re-read if the run was deleted in
          the window between the pre-lock existence check and this request
          acquiring the transition lock (a concurrent delete winning that
          race), not just when ``run_id`` never existed at all.
        - ``HTTPException`` 400: the run's status is not in
          ``RESUMABLE_STATUSES``; its ``request_payload`` is missing/not a
          dict; or the stored ``request_payload`` fails
          ``RunStrategyLabRequest`` validation (e.g. a corrupted or
          schema-stale persisted payload).
        - ``HTTPException`` 409: another transition for this run_id is
          already in flight, or another run is already ``"running"``.
        - ``HTTPException`` 503: reading the current durable generation —
          either the initial carry-forward read or the pre-dispatch
          revalidation — failed. Covers both a job-service transport
          failure (unreachable/timed out) AND ``_get_run_generation_strict``
          raising ``ValueError`` for a malformed/corrupt persisted
          ``generation`` value — either way, the generation can't be
          reliably determined, so the request fails closed with the same
          503 rather than distinguishing the two causes. The revalidation
          failure additionally marks the run ``"failed"`` (state was
          already written by this point, so leaving it ``"running"`` with
          no workflow ever dispatched would wedge it).
    """
    # Cheap existence-only check (no lock): avoids growing the transition-lock
    # registry for a run_id that was never created (or already purged).
    if _get_run_state(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Strategy lab run '{run_id}' not found.")

    run_lock = _require_run_transition_lock(run_id)
    try:
        # Re-read + derive everything from state INSIDE the lock — see
        # Preconditions for why reading it beforehand risks a stale-snapshot
        # duplicate dispatch.
        state = _get_run_state(run_id)
        if state is None:
            # The cheap pre-lock check above saw the run exist, but it was
            # deleted before this request acquired the transition lock.
            # Mirror the early check's exact 404 rather than falling through
            # to `validate_job_for_action`'s generic "Job ... not found" text
            # so both races produce the same response shape.
            raise HTTPException(status_code=404, detail=f"Strategy lab run '{run_id}' not found.")
        try:
            validate_job_for_action(state, run_id, RESUMABLE_STATUSES, "resumed")
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except JobStateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        payload = state.get("request_payload")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Original request payload not available.")

        completed_cycles = state.get("completed_cycles", 0)
        # contiguous_cycles tracks the highest unbroken sequence from index 0
        # — safe to use as the resume offset (won't skip gaps or re-run finished cycles).
        contiguous_cycles = state.get("contiguous_cycles", completed_cycles)
        try:
            request = RunStrategyLabRequest(**payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid stored request payload: {exc}"
            ) from exc
        total_cycles = request.batch_size * request.batch_count
        completed_batches, _within = divmod(contiguous_cycles, request.batch_size)

        # The generation carried forward must come from the DURABLE store, not
        # `state` (which may be `_get_run_state`'s process-local `active_runs`
        # snapshot): in a multi-process/multi-replica deployment, a restart
        # handled by a different process already minted a newer generation
        # there, and copying a stale locally-cached value into
        # `_persist_run_state` below would regress the durable high-water
        # mark, un-fencing everything that restart just fenced out.
        try:
            current_generation = _get_run_generation_strict(
                run_id, client=_get_lab_run_job_client()
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Failed to read the current generation for this resume; job service unavailable.",
            ) from exc

        _ensure_no_active_run()

        # Re-initialize in-memory state
        resumed_state = _build_run_state(
            run_id,
            started_at=state.get("started_at", _now()),
            total_cycles=total_cycles,
            batch_size=request.batch_size,
            batch_count=request.batch_count,
            request_payload=payload,
            completed_cycles=completed_cycles,
            contiguous_cycles=contiguous_cycles,
            skipped_cycles=state.get("skipped_cycles", 0),
            errored_cycles=state.get("errored_cycles", 0),
            errored_details=state.get("errored_details", []),
            tracker_merge_error_count=state.get("tracker_merge_error_count", 0),
            completed_record_ids=state.get("completed_record_ids", []),
            completed_batches=completed_batches,
            # A resume continues the same incarnation rather than superseding
            # one, so it carries the current (durable, authoritative)
            # generation forward unchanged (unlike restart, which mints a new
            # one below).
            generation=current_generation,
        )
        # Check-and-write atomically under one `_lock` acquisition: an
        # isolated `_ensure_no_active_run()` call followed by a separate
        # `with _lock:` write would let a concurrent run/resume/restart for a
        # DIFFERENT run_id interleave between the two and also pass its own
        # check before either write lands, leaving two runs "running" at once.
        with _lock:
            _no_active_run_locked()
            _active_runs[run_id] = resumed_state
        # Exclude "generation" from this write: current_generation above is a
        # snapshot from just before this point, and a concurrent restart on
        # another process/replica could mint a newer durable value in the gap
        # between that read and this write. update_job/create_job merge
        # fields into the durable record rather than replacing it wholesale,
        # so omitting the key here means this write can never regress the
        # durable generation back down to a stale snapshot.
        _persist_run_state(run_id, resumed_state, exclude_fields=_GENERATION_EXCLUDE_FIELDS)

        # Revalidate the generation immediately before dispatch: `current_generation`
        # above is a snapshot from before this request's own state write, and a
        # concurrent restart on another process/replica could have minted (and
        # already dispatched under) a newer generation in that gap. Dispatching
        # this resume's workflow under the stale value would permanently fence
        # out its own activities the moment it tried to persist anything -- the
        # only live workflow for this run, wedged with no way to make progress.
        # Re-reading here narrows that window to the residual gap between this
        # read and the dispatch call itself; closing it fully would need
        # cross-replica atomic coordination over both generation selection and
        # workflow-id dispatch ownership, which is the same out-of-scope
        # multi-process limitation already accepted for the per-run_id
        # transition lock.
        try:
            dispatch_generation = _get_run_generation_strict(
                run_id, client=_get_lab_run_job_client()
            )
        except Exception as exc:
            _fail_strategy_lab_run(
                run_id, f"Failed to revalidate generation before dispatch: {exc}"
            )
            raise HTTPException(
                status_code=503,
                detail="Failed to revalidate the current generation before resuming; job service unavailable.",
            ) from exc
        if dispatch_generation > resumed_state["generation"]:
            # `resumed_state` is the same dict object installed at
            # `_active_runs[run_id]` above, so mutating it in place keeps
            # both in sync -- guard the mutation with `_lock` since a
            # concurrent same-process reader (e.g. the run-status endpoint)
            # may be iterating `_active_runs` at the same time.
            with _lock:
                resumed_state["generation"] = dispatch_generation
        elif dispatch_generation < resumed_state["generation"]:
            # The generation field is meant to be strictly monotonic (only
            # ever advanced via an atomic job-service increment), so a
            # LOWER value here can't represent a legitimate concurrent
            # mint -- it indicates a corrupted or otherwise malformed
            # durable record. Fail closed rather than dispatch under a
            # value that violates the invariant fencing depends on.
            _fail_strategy_lab_run(
                run_id,
                f"Durable generation regressed from {resumed_state['generation']} "
                f"to {dispatch_generation} during pre-dispatch revalidation",
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Detected a durable generation regression while revalidating "
                    "before dispatch; job service state is inconsistent."
                ),
            )

        # The Temporal activity derives its resume offset from the persisted
        # contiguous-cycle count (set above), so a durable resume picks up where the
        # run left off.
        _dispatch_strategy_lab_run(run_id, request, generation=resumed_state["generation"])
    finally:
        run_lock.release()

    return StrategyLabRunStartResponse(
        run_id=run_id,
        total_cycles=total_cycles,
        message=f"Run resumed from cycle {contiguous_cycles + 1} of {total_cycles}.",
    )


@app.post(
    "/strategy-lab/runs/{run_id}/restart",
    response_model=StrategyLabRunStartResponse,
    summary="Restart a strategy lab run from scratch",
    description="Reset the run and re-execute the full batch with the same inputs.",
)
def restart_strategy_lab_run(run_id: str) -> StrategyLabRunStartResponse:
    """Restart a strategy lab run from the beginning.

    Preconditions:
        - ``run_id`` identifies a run whose status — read via ``_get_run_state``
          (the in-memory ``active_runs`` entry when present, else a durable
          fallback read; see its own docstring) INSIDE the transition lock
          below, not beforehand — is in ``STRATEGY_LAB_RESTARTABLE_STATUSES``
          (completed/failed/cancelled/interrupted/agent_crash/completed_with_errors).
          Reading it before acquiring the lock could observe another
          transition's transiently-written "running" reset and misreport a
          genuine in-flight-elsewhere race as a permanent 400 instead of a
          retryable 409 ("running" is deliberately excluded from
          ``STRATEGY_LAB_RESTARTABLE_STATUSES``). Only a cheap existence check (``run_id``
          resolves to *some* known run) runs before the lock, so a request
          for a nonexistent run_id never allocates a transition-lock entry.
        - The run's persisted ``request_payload`` is present and is a dict.
        - No other run currently has status ``"running"`` — authoritatively
          checked (and enforced) atomically with the ``_active_runs[run_id]``
          write below, both inside the same ``_lock`` acquisition; the
          earlier, unlocked ``_ensure_no_active_run()`` call is only a
          fast-fail that skips the Temporal termination RPC below for an
          obviously-doomed request, and cannot by itself prevent a
          concurrent run/resume/restart for a DIFFERENT run_id from also
          passing it and writing "running" before this write lands.
        - No other run/resume/restart transition for this run_id is
          currently in flight (checked first, before re-reading state or
          calling ``_ensure_no_active_run()``).

    Postconditions:
        - Any prior Temporal execution still running under this run_id's
          deterministic workflow id is terminated and confirmed closed
          *before* any state is written — closing the window where that
          execution could observe (and act on) a transiently-optimistic
          "running" reset before a collision would otherwise be detected.
        - Rebuilds ``_active_runs[run_id]`` as a full reset — ``contiguous_cycles``
          is set to ``0`` and ``started_at`` is refreshed; unlike resume, prior
          ``completed_cycles``/``errored_*``/``completed_record_ids`` are NOT
          carried forward — and persists the new state. A freshly minted
          ``generation`` (atomically incremented in the job store) is set on
          the in-memory state, fencing out any write a still-in-flight
          activity from the just-terminated workflow attempts afterward. The
          durable write omits ``generation`` — ``apply_and_get`` already
          persisted it atomically, and re-asserting it here could regress an
          even newer value a different restart on another process/replica
          minted in the gap since (the same reasoning as the rollback's own
          write below).
        - Re-reads the durable generation once more immediately before
          dispatch and, if it has since advanced past this restart's own
          mint (a different restart on another process/replica minted a
          newer one in the gap), dispatches with the newer value instead —
          narrowing, not eliminating, the window in which this restart's
          workflow could otherwise win the deterministic workflow-id race
          while carrying a generation that is already stale, which would
          permanently fence out its own activities.
        - Dispatches the durable Temporal workflow starting at cycle 0. If
          that dispatch still 409s (a residual collision — e.g. a second
          restart/resume racing in after the termination check above), the
          reset is rolled back — ``_active_runs[run_id]`` and the persisted
          state are restored to their pre-restart status/counters — so the
          run isn't left wedged showing ``"running"`` and blocking every
          future run/resume/restart call. ``generation`` is the one field
          NOT restored to its pre-restart value: the durable write excludes
          it entirely, and the in-memory snapshot instead re-reads whatever
          the durable store currently holds — never this request's own
          (possibly superseded, in a multi-process race) mint — so a
          concurrently-dispatched newer transition's generation can never be
          regressed by this rollback.
        - Returns the run's start response with the full total cycle count.

    Raises:
        - ``HTTPException`` 404: ``run_id`` does not resolve to any known run.
        - ``HTTPException`` 400: the run's status is not restartable, or its
          ``request_payload`` is missing/not a dict.
        - ``HTTPException`` 409: another transition for this run_id is
          already in flight (#4028), another run is already ``"running"``,
          the prior execution couldn't be confirmed terminated within budget
          (retry shortly), or a residual dispatch collision occurred despite
          that confirmation (a collision the dispatch layer refuses to
          silently resume — see ``_dispatch_strategy_lab_run``'s
          ``allow_already_started`` parameter; the optimistic reset is rolled
          back in this case, see Postconditions).
        - ``HTTPException`` 503: Temporal is disabled/unavailable
          (``_require_temporal``); the prior execution couldn't be resolved
          because the worker client never became ready (``RuntimeError``) or
          a Temporal RPC itself failed (``temporalio.service.RPCError``) --
          the only two failure modes ``terminate_and_await_workflow_sync``
          raises for a genuine Temporal-side problem (besides
          ``TimeoutError``, mapped to 409 above; any other exception, e.g. a
          programming error in this block, is NOT caught here and propagates
          as an unhandled 500 instead of being misreported as Temporal
          unavailability); minting the new generation failed; persisting the
          optimistic reset state failed; or the pre-dispatch revalidation
          read of the generation failed (job service unavailable in any
          case). "Minting failed" covers both ways ``apply_and_get`` can
          fail: it raises on a transport error, or returns a falsy value
          when the job no longer exists in the job service — the latter also
          503s rather than some other status, since the prior workflow was
          already confirmed terminated above and the run cannot safely be
          left without a fencing generation. The persist-failure and
          revalidation-failure cases both additionally mark the run
          ``"failed"`` (state was already written in-memory by this point,
          so leaving it ``"running"`` with no durably-persisted/dispatched
          workflow would wedge it).

    Two concurrent restart/resume calls for the same run_id can no longer
    both pass the check-then-write window (#4028, closed by
    ``_require_run_transition_lock``, which reserves this run_id for the
    whole check→terminate→write→dispatch sequence below).

    Generation fencing substantially narrows the race left by confirming the
    old *workflow* terminated: that confirmation alone does not guarantee an
    already in-flight, non-heartbeating *activity* has stopped — Strategy
    Lab's activities aren't cooperatively cancellable — so without fencing
    one could still commit a cycle record or persist progress after the new
    cycle-0 workflow has started. The generation minted above is threaded
    through the new workflow's persist/finalize activities (see
    ``shared.fencing.check_fencing_token``), so a stale activity's write is
    checked against it rather than always silently landing. For
    ``persist_run_state_activity`` (a fast, synchronous write immediately
    adjacent to its check) this closes the realistic window outright. For
    ``finalize_cycle_record_activity`` (whose write happens after a
    market-data fetch and paper-trading execution — a real amount of time)
    the check happens both before and after that work, but the two checks
    still can't make the underlying write atomic against the generation; see
    ``strategy_lab.temporal.activities``'s module docstring for the full,
    honest accounting.

    Known, accepted residual limitation (tracked as a separate follow-up
    rather than fixed here — closing it is a real feature, not a quick
    patch): a stale in-flight activity from the terminated incarnation can
    still run to completion — burning time and cost on an LLM call, a
    backtest, or a paper trade — before its write is checked/rejected as
    fenced. Cooperative cancellation (checking
    ``activity.is_cancelled()``/heartbeating so a terminated workflow's
    activities actually stop executing) would close this remaining gap, and
    would also fully close ``finalize_cycle_record_activity``'s wider
    check-to-write window by stopping its execution outright rather than
    merely detecting staleness after the fact.
    """
    # Cheap existence-only check (no lock): avoids growing the transition-lock
    # registry for a run_id that was never created (or already purged),
    # mirroring run_strategy_lab's own check-before-lock ordering.
    if _get_run_state(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Strategy lab run '{run_id}' not found.")

    run_lock = _require_run_transition_lock(run_id)
    try:
        # Re-read + validate state INSIDE the lock: reading it beforehand
        # could observe a concurrent restart's transiently-written "running"
        # reset (written while that request still holds this same lock) and
        # incorrectly reject with 400 "not restartable" instead of the
        # promised retryable 409 — "running" is deliberately excluded from
        # RESTARTABLE_STATUSES, so status is only trustworthy once no other
        # transition for this run_id can be concurrently rewriting it.
        state = _get_run_state(run_id)
        if state is None:
            # A concurrent delete_strategy_lab_run completed in the window between
            # the pre-lock existence check above and this re-read; validate_job_for_action
            # below would also 404 via JobNotFoundError, but this mirrors the
            # pre-lock check's message for a consistent 404 body.
            raise HTTPException(status_code=404, detail=f"Strategy lab run '{run_id}' not found.")
        try:
            validate_job_for_action(state, run_id, STRATEGY_LAB_RESTARTABLE_STATUSES, "restarted")
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except JobStateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        payload = state.get("request_payload")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Original request payload not available.")

        _ensure_no_active_run()

        try:
            request = RunStrategyLabRequest(**payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid stored request payload: {exc}"
            ) from exc
        total_cycles = request.batch_size * request.batch_count

        # Resolve any prior execution BEFORE writing anything: a still-running
        # workflow polls persisted status between waves (strategy_lab_external_
        # terminal_status), so writing the optimistic "running" reset first would
        # let it observe that transient state and run an extra wave before a
        # dispatch collision is even detected.
        _require_temporal()
        from temporalio.service import RPCError

        from investment_team.strategy_lab.temporal import WORKFLOW_ID_PREFIX
        from shared.temporal import terminate_and_await_workflow_sync

        try:
            terminate_and_await_workflow_sync(
                f"{WORKFLOW_ID_PREFIX}{run_id}",
                reason=f"Restarted via /strategy-lab/runs/{run_id}/restart",
            )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=409,
                detail="A prior execution for this run is still winding down; retry shortly.",
            ) from exc
        except (RuntimeError, RPCError) as exc:
            # RuntimeError: the worker client never became ready (documented
            # by terminate_and_await_workflow_sync's own docstring).
            # RPCError: a genuine Temporal-side RPC failure (the underlying
            # temporalio client's exception type; a NOT_FOUND status is
            # already handled as a no-op inside terminate_and_await_workflow_sync
            # itself, so anything that reaches here is a real transport/RPC
            # problem). Anything else -- e.g. an ImportError from the imports
            # above, or a programming error -- is NOT one of the documented
            # Temporal-side failure modes and must propagate as an unhandled
            # 500 instead of being misreported as "Temporal worker unavailable."
            logger.exception(
                "Failed to terminate prior strategy-lab workflow for run %s before restart", run_id
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Failed to resolve the prior strategy-lab execution before "
                    "restarting; Temporal worker unavailable."
                ),
            ) from exc

        # Mint a new generation for this incarnation: the fresh cycle-0
        # workflow started below carries it through every persist/finalize
        # activity, so a stale write from the just-terminated workflow's
        # still-in-flight activity (terminating the workflow doesn't stop an
        # already-dispatched, non-heartbeating activity) is fenced instead of
        # silently landing. Atomic increment-and-read-back mirrors
        # software_engineering_team/job_store.py's claim_resume. The
        # increment amount itself accounts for a legacy (pre-fencing) run's
        # missing "generation" field -- see
        # `_legacy_generation_bootstrap_increment`'s own docstring.
        #
        # get_job (below) and apply_and_get (further down) are deliberately
        # two separate, non-atomic job-service calls -- not a bug. get_job's
        # result ONLY decides which increment amount apply_and_get applies
        # (+1 ordinary vs +2 legacy-bootstrap); it never supplies a value
        # apply_and_get treats as authoritative. apply_and_get's own
        # increment-and-read-back is what's atomic, and it always increments
        # from whatever the durable value actually is at the moment it runs,
        # regardless of what get_job saw. So a race in this gap (another
        # restart's apply_and_get already advanced the generation, or the
        # job was deleted) can only affect which increment amount THIS call
        # applies -- never which direction the generation moves (always up)
        # or whether the result is still a valid, monotonically-newer
        # fencing token. A deleted-job race is caught by apply_and_get's own
        # falsy-result handling below.
        #
        # A get_job *transport* failure is different: without seeing the
        # durable representation we cannot choose a safe increment (a blind
        # +2 would zero a durable numeric-string token like "5" and regress
        # fencing). Fail closed with 503 rather than minting.
        client = _get_lab_run_job_client()
        try:
            durable_job = client.get_job(run_id)
        except Exception as exc:
            logger.warning(
                "get_job failed during restart's legacy-generation check for %s: %s", run_id, exc
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Failed to mint a new generation for this restart; could not "
                    "read the durable generation to choose a safe increment."
                ),
            ) from exc
        # get_job's contract is Optional[Dict[str, Any]] -- a JSONB "data"
        # column merged into the top-level dict server-side (job_service.db's
        # _row_to_dict), never returned as a separate nested key. `or {}`
        # normalizes only the "no job" case; no further shape-guessing needed.
        durable_data = durable_job or {}
        try:
            generation_increment = _legacy_generation_bootstrap_increment(durable_data)
        except UnsafeDurableGenerationError as exc:
            # A parseable-but-non-native generation (e.g. "5") cannot be
            # advanced via increment without job-service zeroing it first and
            # regressing the fencing token. Fail closed rather than mint a
            # weaker generation that in-flight activities presenting the
            # conceptual value would still pass.
            raise HTTPException(
                status_code=503,
                detail=(
                    "Failed to mint a new generation for this restart; durable "
                    "generation is not a native integer and cannot be advanced safely."
                ),
            ) from exc
        try:
            updated_generation_record = client.apply_and_get(
                run_id, increment={"generation": generation_increment}
            )
        except Exception as exc:
            # apply_and_get raises on a transport failure (connection refused,
            # timeout, ...) rather than returning None — only a falsy return
            # (job not found) is the "expected" failure mode below, so a raised
            # exception needs its own translation to the same documented 503.
            raise HTTPException(
                status_code=503,
                detail="Failed to mint a new generation for this restart; job service transport error.",
            ) from exc
        if not updated_generation_record:
            raise HTTPException(
                status_code=503,
                detail="Failed to mint a new generation for this restart; run record not found in job service.",
            )
        try:
            # No default: a missing/non-int "generation" in the mint
            # response is a malformed reply from the job service, not a
            # legitimate absent-field case (apply_and_get's increment
            # target always comes back populated on success) -- treat it
            # the same as any other mint failure rather than silently
            # assuming a value that could itself be stale. Checked via
            # isinstance rather than a bare int(...) coercion: the latter
            # would silently truncate a float or accept a numeric string
            # instead of rejecting it as the malformed reply it is, and
            # would accept a bool (an int subclass in Python) as a
            # seemingly valid generation.
            raw_generation = updated_generation_record["generation"]
            if not isinstance(raw_generation, int) or isinstance(raw_generation, bool):
                raise ValueError(f"non-integer generation {raw_generation!r}")
            new_generation = raw_generation
            if new_generation <= 0:
                # The increment applied is always positive (see
                # _legacy_generation_bootstrap_increment), so a non-positive
                # result means the durable record itself was already
                # corrupt -- treat it the same as a malformed reply rather
                # than dispatching with a value that could match a stale
                # activity's default/legacy token and defeat fencing.
                raise ValueError(f"non-positive generation {new_generation!r}")
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Failed to mint a new generation for this restart; job service "
                    "returned an invalid generation value."
                ),
            ) from exc

        restarted_state = _build_run_state(
            run_id,
            started_at=_now(),
            total_cycles=total_cycles,
            batch_size=request.batch_size,
            batch_count=request.batch_count,
            request_payload=payload,
            # Reset the resume offset the Temporal activity reads, so a durable
            # restart re-runs from cycle 0 instead of resuming a prior run's
            # contiguous-cycle count persisted on this run_id.
            contiguous_cycles=0,
            generation=new_generation,
        )
        # Re-run the no-active-run check atomically with the write: the
        # early call above (before the Temporal termination RPC) is only a
        # cheap fast-fail that avoids that RPC's cost for an obviously-doomed
        # request -- on its own it can't stop a concurrent run/resume/restart
        # for a DIFFERENT run_id from also passing it and writing "running"
        # before this request's write lands. This second, locked check closes
        # that window (mirroring resume_strategy_lab_run's own pattern).
        with _lock:
            _no_active_run_locked()
            _active_runs[run_id] = restarted_state
        # Exclude "generation" from this write: `apply_and_get` above already
        # durably persisted it atomically, so re-asserting it here is
        # redundant at best -- and actively harmful in a multi-process/
        # multi-replica deployment, where a DIFFERENT restart on another
        # process/replica could mint (and already dispatch under) an even
        # newer generation in the gap between this request's own mint and
        # this write. Writing this request's now-stale minted value here
        # would regress the durable high-water mark and un-fence that
        # legitimately newer, already-running incarnation -- the exact
        # regression the rollback path below already guards against for its
        # own write; this is the same guard for the non-collision path.
        try:
            _persist_run_state(run_id, restarted_state, exclude_fields=_GENERATION_EXCLUDE_FIELDS)
        except Exception as exc:
            # _persist_run_state is documented to propagate job-service
            # failures uncaught rather than swallow them -- this call site
            # must translate that into the same documented 503 every other
            # job-service failure in this function produces, not let it
            # escape as a raw 500. _fail_strategy_lab_run is itself
            # best-effort/never-raises, so it's safe to call even though the
            # durable write we're reacting to just failed.
            _fail_strategy_lab_run(run_id, f"Failed to persist restarted run state: {exc}")
            raise HTTPException(
                status_code=503,
                detail="Failed to persist restarted run state; job service unavailable.",
            ) from exc

        # Revalidate the generation immediately before dispatch: `new_generation`
        # above is a snapshot from this restart's own mint, and a DIFFERENT
        # restart on another process/replica could have minted (and already
        # dispatched under) an even newer one in the gap since. If THIS
        # request's dispatch then wins the deterministic workflow-id race
        # (the other replica's collides instead), dispatching under the
        # stale value here would permanently fence out the only live
        # workflow's own activities the moment they tried to persist
        # anything. Re-reading here narrows that window to the residual gap
        # between this read and the dispatch call itself -- the same
        # narrowing already applied to resume's dispatch, and the same
        # out-of-scope multi-process limitation acknowledged there.
        try:
            dispatch_generation = _get_run_generation_strict(
                run_id, client=_get_lab_run_job_client()
            )
        except Exception as exc:
            _fail_strategy_lab_run(
                run_id, f"Failed to revalidate generation before dispatch: {exc}"
            )
            raise HTTPException(
                status_code=503,
                detail="Failed to revalidate the current generation before restarting; job service unavailable.",
            ) from exc
        if dispatch_generation > restarted_state["generation"]:
            # `restarted_state` is the same dict object installed at
            # `_active_runs[run_id]` above, so mutating it in place keeps
            # both in sync -- guard the mutation with `_lock` since a
            # concurrent same-process reader (e.g. the run-status endpoint)
            # may be iterating `_active_runs` at the same time.
            with _lock:
                restarted_state["generation"] = dispatch_generation
        elif dispatch_generation < restarted_state["generation"]:
            # Same invariant-violation guard as resume's identical check:
            # generation only ever advances via an atomic increment, so a
            # lower value here means the durable record is corrupted or
            # otherwise inconsistent -- fail closed instead of dispatching
            # under it.
            _fail_strategy_lab_run(
                run_id,
                f"Durable generation regressed from {restarted_state['generation']} "
                f"to {dispatch_generation} during pre-dispatch revalidation",
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Detected a durable generation regression while revalidating "
                    "before dispatch; job service state is inconsistent."
                ),
            )

        # Restart from scratch through Temporal (offset 0, per the reset
        # persisted state above). allow_already_started=False: unlike resume, a
        # collision here means an old, un-reset execution is still running, not
        # that the intended restart is already in flight.
        try:
            _dispatch_strategy_lab_run(
                run_id,
                request,
                generation=restarted_state["generation"],
                allow_already_started=False,
            )
        except HTTPException as exc:
            if exc.status_code == 409:
                # The reset above never actually took effect (an old execution
                # is still running under this run_id) — restore the pre-restart
                # status/counters so _ensure_no_active_run() doesn't wedge on a
                # phantom "running" entry, blocking every future run/resume/
                # restart call until the stale execution happens to overwrite it
                # on its own. The freshly minted generation is deliberately NOT
                # rolled back with the rest of the snapshot: the prior workflow
                # was already confirmed terminated above, so its activities must
                # stay fenced out regardless of whether this restart's own
                # dispatch succeeded — reverting to the pre-restart generation
                # would let a still-in-flight activity from that terminated
                # workflow pass fencing again, reopening the exact race
                # generation fencing exists to close.
                #
                # But this restart's OWN mint (new_generation) is only correct
                # to durably re-assert when it's still the current one. In a
                # multi-process/multi-replica deployment (the per-run_id
                # transition lock is process-local, see #4028's scope note),
                # a DIFFERENT restart on another process could have raced this
                # one, minted an even newer generation, and already dispatched
                # successfully — that's a real, legitimate active run, not a
                # stale leftover; overwriting its generation back down to this
                # request's stale mint would un-fence it. Re-read the current
                # durable value for the in-memory snapshot (falls back to this
                # request's own mint only if even that read fails), and
                # exclude "generation" from the durable write entirely so it
                # can never be regressed by this rollback either way.
                try:
                    current_generation = _get_run_generation_strict(
                        run_id, client=_get_lab_run_job_client()
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to re-read current generation during restart rollback "
                        "for run %s; falling back to minted generation %s: %s",
                        run_id,
                        new_generation,
                        exc,
                    )
                    current_generation = new_generation
                rolled_back_state = dict(state)
                rolled_back_state["generation"] = current_generation
                with _lock:
                    _active_runs[run_id] = rolled_back_state
                try:
                    _persist_run_state(
                        run_id, rolled_back_state, exclude_fields=_GENERATION_EXCLUDE_FIELDS
                    )
                except Exception:
                    # Best-effort rollback persist: a failure here must not
                    # replace the more actionable 409 below with an unrelated
                    # error from this cleanup step.
                    logger.warning(
                        "Failed to persist restart-rollback state for run %s",
                        run_id,
                        exc_info=True,
                    )
                # Known, accepted residual limitation (matches #4028's own
                # documented multi-process scope boundary — not new here):
                # this rollback can still overwrite a concurrently-dispatched
                # newer transition's status/counters with this request's
                # stale pre-restart snapshot in that same multi-process race.
                # Closing that fully would need real cross-process
                # coordination (e.g. an optimistic-concurrency/conditional
                # update on the whole record), out of scope for generation
                # fencing specifically.
            raise
    finally:
        run_lock.release()

    return StrategyLabRunStartResponse(
        run_id=run_id,
        total_cycles=total_cycles,
        message="Run restarted from scratch.",
    )


@app.delete(
    "/strategy-lab/runs/{run_id}",
    summary="Delete a strategy lab run",
    description="Remove a strategy lab run from the job store and in-memory tracking.",
)
def delete_strategy_lab_run(run_id: str) -> Dict[str, Any]:
    """Delete a strategy lab run by ID.

    Deletes from the job service before touching in-memory state, so a
    failed or exceptional job-service delete leaves ``_active_runs``
    untouched instead of dropping the entry while the persisted record
    still exists. Holds this run_id's transition lock for the whole
    delete-then-pop sequence so a concurrent run/resume/restart for the
    SAME run_id can't write a fresh "running" state (and dispatch a
    workflow) around a delete that's removing the record out from under
    it — the two would otherwise race to leave an orphaned in-memory
    "running" entry with no persisted record, or a workflow dispatched for
    a run_id whose record is already gone.

    Preconditions:
        - None on run status — any run can be deleted regardless of its
          current status.
        - The job service must have a record for ``run_id`` for the delete
          to succeed.
        - No other run/resume/restart/delete transition for this run_id is
          currently in flight.

    Postconditions:
        - The job-service record for ``run_id`` is deleted before
          ``_active_runs.pop(run_id, None)`` is attempted, so
          ``_active_runs`` is only mutated once the job-service delete has
          succeeded.
        - Returns ``{"job_id": run_id, "deleted": True}``.

    Raises:
        - ``HTTPException`` 404: ``client.delete_job(run_id)`` returns a
          falsy result (no such run in the job service).
        - ``HTTPException`` 409: another transition for this run_id is
          already in flight.
        - An exception raised by ``client.delete_job`` itself is not caught
          and propagates uncaught (surfaces as a 500) — ``_active_runs`` is
          left untouched in that case.
    """
    run_lock = _require_run_transition_lock(run_id)
    try:
        client = _get_lab_run_job_client()
        deleted = client.delete_job(run_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        with _lock:
            _active_runs.pop(run_id, None)
    finally:
        run_lock.release()
    return {"job_id": run_id, "deleted": True}


@app.get(
    "/strategy-lab/runs", response_model=ActiveRunsResponse, summary="List active strategy lab runs"
)
def list_strategy_lab_runs() -> ActiveRunsResponse:
    """Return all tracked runs (active and recently completed).

    Merges in-memory state with persisted job-service state so that
    running jobs are always visible — even after a page refresh that
    races with server startup or after the in-memory entry is evicted.

    Also reconciles: for each in-memory run whose status is not terminal,
    every progress field (and, if the job service reports a terminal status,
    ``status``/``error`` too) is refreshed from the durable job-service
    record — this handles both external cancellation (via the generic job
    proxy or the Jobs Dashboard) and ordinary mid-run progress that would
    otherwise stay frozen at dispatch-time values.

    Preconditions:
        - None.

    Postconditions:
        - Returns an ``ActiveRunsResponse`` merging in-memory ``_active_runs``
          with persisted job-service ``"running"``/``"pending"`` jobs not
          already tracked in-memory (those are added to the response only,
          not written back to ``_active_runs``).
        - Side effect: for each in-memory run whose status is not in
          ``STRATEGY_LAB_TERMINAL_STATUSES``, ``_reconcile_run_progress`` is
          called, refreshing progress fields (and ``status``/``error`` on a
          terminal transition) in place. See its docstring for details.

    Raises:
        - None. Job-service lookup/reconciliation failures are caught and
          logged (``logger.debug``), and the endpoint falls back to the
          in-memory-only snapshot; this endpoint always returns 200. An
          ``_active_runs`` entry missing a truthy ``run_id`` (malformed or
          partially-constructed) is skipped and logged rather than raising
          ``KeyError``. An entry whose response construction
          (``_run_state_to_response``) fails -- e.g. a field that cannot be
          coerced to its response type -- is likewise skipped and logged
          (``logger.warning``) rather than raising out of this endpoint.
    """

    def _in_memory_runs_by_id() -> Dict[str, Dict[str, Any]]:
        """Locked snapshot of ``_active_runs``, keyed by each entry's own ``run_id``.

        An entry missing (or with a falsy) ``run_id`` is skipped and logged
        instead of raising ``KeyError`` -- this endpoint must always return
        200, and a single malformed entry must not break the whole listing.
        """
        with _lock:
            snapshot = list(_active_runs.items())
        result: Dict[str, Dict[str, Any]] = {}
        for key, r in snapshot:
            rid = r.get("run_id")
            if not rid:
                logger.warning("Skipping _active_runs entry %r with missing/falsy run_id", key)
                continue
            result[rid] = r
        return result

    try:
        client = _get_lab_run_job_client()

        # Reconcile: refresh progress (and, on a terminal transition,
        # status/error) for every run we think is still active.
        with _lock:
            running_ids = [
                rid
                for rid, r in _active_runs.items()
                if r.get("status") not in STRATEGY_LAB_TERMINAL_STATUSES
            ]
        for rid in running_ids:
            _reconcile_run_progress(rid)

        in_memory = _in_memory_runs_by_id()

        # Merge running/pending jobs from the persistent job service that
        # may not be in _active_runs (e.g. after a server restart).
        persisted_list = client.list_jobs(statuses=["running", "pending"])
        for job in persisted_list:
            rid = job.get("job_id") or job.get("run_id", "")
            if rid and rid not in in_memory:
                in_memory[rid] = _normalize_persisted_job(
                    job, fallback_status="running", run_id=rid
                )
    except Exception:
        logger.debug("Job service fallback failed for run listing", exc_info=True)
        in_memory = _in_memory_runs_by_id()

    runs: List[StrategyLabRunStatusResponse] = []
    for rid, r in in_memory.items():
        try:
            runs.append(_run_state_to_response(r))
        except Exception:
            logger.warning(
                "Skipping run %r in listing; _run_state_to_response failed", rid, exc_info=True
            )
    return ActiveRunsResponse(runs=runs)


@app.get(
    "/strategy-lab/runs/{run_id}/status",
    response_model=StrategyLabRunStatusResponse,
    summary="Get strategy lab run status (polling fallback)",
)
def get_strategy_lab_run_status(run_id: str) -> StrategyLabRunStatusResponse:
    """Snapshot of a single run's progress. Use for polling when SSE is unavailable.

    Preconditions:
        - None. ``run_id`` may or may not resolve to a state in either
          ``_active_runs`` or the job-service fallback -- a missing id is
          normal input this function itself handles (see ``Raises``), not a
          caller obligation.

    Postconditions:
        - Side effect: delegates to ``_reconcile_run_progress(run_id)``,
          which — when the in-memory state's status is not in
          ``STRATEGY_LAB_TERMINAL_STATUSES`` — refreshes every progress field
          from the job service, and additionally mutates ``_active_runs[run_id]
          ["status"]``/``["error"]`` in place when the persisted status is
          itself terminal.
        - Returns a ``StrategyLabRunStatusResponse`` via
          ``_run_state_to_response(state)``.

    Raises:
        - ``HTTPException`` 404: no state found in either ``_active_runs`` or
          the job-service fallback. Job-service reconciliation failures are
          caught and logged (``logger.debug``), falling back to the
          in-memory state rather than propagating.
    """
    _reconcile_run_progress(run_id)

    with _lock:
        state = _active_runs.get(run_id)
    if not state:
        state = _load_run_from_job_service(run_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return _run_state_to_response(state)


@app.get(
    "/strategy-lab/runs/{run_id}/stream",
    summary="Stream strategy lab run progress via SSE",
    description=(
        "Server-Sent Events stream for real-time progress. Emits 'snapshot' on connect, "
        "'progress' at each phase, 'cycle_complete'/'cycle_skipped' per cycle, "
        "and a terminal 'complete', 'error', or 'cancelled' event."
    ),
)
async def stream_strategy_lab_run(run_id: str) -> StreamingResponse:
    """SSE endpoint. Connect-time reconciliation and job-service loading are
    synchronous, blocking job-service calls, so both are offloaded to
    Starlette's threadpool (``run_in_threadpool``) rather than run directly on
    this coroutine — otherwise they'd stall the asyncio event loop, and with
    it every other in-flight request on this worker, for the fetch+retry
    window. In-memory ``_active_runs`` lookups use ``_async_lock`` (not the
    threading ``_lock``) for the same reason. The streaming generator itself
    remains async so it doesn't block Uvicorn worker threads once connected.

    Preconditions:
        - ``run_id`` is a string (path param). It need not resolve to a known
          run -- a miss is normal input this function itself handles (see
          ``Raises``), not a caller obligation.

    Postconditions:
        - Returns a ``StreamingResponse`` (``media_type="text/event-stream"``).
        - If ``run_id`` is found in ``_active_runs``, its state is refreshed
          via ``_reconcile_run_progress`` (offloaded to the threadpool)
          before use; otherwise state is loaded via
          ``_load_run_from_job_service`` (also offloaded).
        - If the (possibly just-reconciled) status is in
          ``STRATEGY_LAB_TERMINAL_STATUSES``, the response body is a one-shot
          generator that yields a ``snapshot`` event followed by ``done`` and
          returns immediately, without subscribing to the live event bus.
        - Otherwise the response subscribes to the per-job event bus and
          streams ``snapshot``/``progress``/``cycle_complete``/
          ``cycle_skipped`` events, terminating on ``complete``/``error``/
          ``cancelled`` followed by a final ``done``.

    Raises:
        - ``HTTPException`` 404: ``run_id`` resolves to no state in either
          ``_active_runs`` or the job-service fallback.
    """
    # Deliberately local (not module-level): tests substitute a fake
    # subscribe/unsubscribe by monkeypatching them directly on the
    # investment_team.api.job_event_bus module object (not on this module),
    # relying on this import re-executing -- and so re-binding these names to
    # whatever job_event_bus.subscribe/unsubscribe currently are -- on every
    # call. A module-level `from ... import subscribe, unsubscribe` would
    # freeze these names to the real functions at main.py's own import time,
    # silently breaking that test-doubling and stalling the SSE stream in
    # requests that expect the fake driving it (verified: this exact swap
    # deadlocks test_stream_strategy_lab_run_emits_snapshot_update_and_terminates
    # and its siblings).
    from investment_team.api.job_event_bus import subscribe, unsubscribe

    async with _async_lock:
        state = _active_runs.get(run_id)
    if state:
        # Reconcile before the terminal check so an externally-completed run
        # (job-service status advanced past what this process's in-memory
        # entry still shows) is caught by the short-circuit below with
        # up-to-date data, and the live path's _snapshot_event() -- which
        # reads _active_runs.get(run_id, {}) fresh -- picks up these same
        # values automatically.
        await run_in_threadpool(_reconcile_run_progress, run_id)
        async with _async_lock:
            state = _active_runs.get(run_id, state)
    else:
        state = await run_in_threadpool(_load_run_from_job_service, run_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    # If the run is already terminal, send snapshot + done immediately.
    if state.get("status") in STRATEGY_LAB_TERMINAL_STATUSES:
        # Captured eagerly (not read lazily inside the generator) so the
        # terminal snapshot reflects exactly the state checked as terminal
        # above, even if `_active_runs[run_id]` is mutated in place by a
        # background thread before Starlette drains this generator.
        snapshot = _run_state_to_response(state).model_dump(mode="json")

        async def _terminal_gen():
            yield sse_line({"type": "snapshot", **snapshot})
            yield sse_line({"type": "done"})

        return StreamingResponse(_terminal_gen(), media_type="text/event-stream")

    async def _snapshot_event() -> Optional[dict]:
        # Skip the snapshot when there's no current in-memory state to send.
        async with _async_lock:
            current = _active_runs.get(run_id, {})
        if not current:
            return None
        return {"type": "snapshot", **_run_state_to_response(current).model_dump(mode="json")}

    return StreamingResponse(
        sse_job_stream_async(
            subscribe=subscribe,
            unsubscribe=unsubscribe,
            job_id=run_id,
            snapshot=_snapshot_event,
            terminal_types=("complete", "error", "cancelled"),
        ),
        media_type="text/event-stream",
    )


class ClearStrategyLabStorageResponse(BaseModel):
    """Counts of job-service rows removed (Postgres ``jobs`` or local file cache).

    A field is ``None`` rather than ``0`` when its unit didn't finish within
    the purge's shared deadline — see ``_purge_strategy_lab_job_storage``.
    ``None`` means "unknown, may still be deleting in the background," not
    "confirmed nothing was deleted."
    """

    deleted_lab_records: Optional[int] = 0
    deleted_lab_strategies: Optional[int] = 0
    deleted_lab_backtests: Optional[int] = 0
    deleted_paper_trading_sessions: Optional[int] = 0
    message: str = "Strategy lab and paper-trading session storage cleared."


class DeleteStrategyLabRecordResponse(BaseModel):
    """Returned by ``DELETE /strategy-lab/records/{lab_record_id}``.

    ``lab_record_id`` echoes the deleted record's id. ``deleted_strategy_id``/
    ``deleted_backtest_id`` are ``None`` unless the corresponding linked
    entity actually existed (and was deleted) — not a placeholder for
    "unknown." ``deleted_paper_trading_sessions`` is a count of linked
    paper-trading sessions removed, not an id.
    """

    lab_record_id: str
    deleted_strategy_id: Optional[str] = None
    deleted_backtest_id: Optional[str] = None
    deleted_paper_trading_sessions: int = 0


@app.delete(
    "/strategy-lab/records/{lab_record_id}",
    response_model=DeleteStrategyLabRecordResponse,
)
def delete_strategy_lab_record(lab_record_id: str) -> DeleteStrategyLabRecordResponse:
    """
    Delete one strategy lab run: lab card, linked lab strategy/backtest jobs, and any paper-trading
    sessions that reference this ``lab_record_id``.

    Preconditions:
        - ``lab_record_id`` may or may not resolve to a known lab record.

    Postconditions:
        - Paper-trading session cleanup (``_delete_paper_sessions_for_lab_record``,
          a job-service network call) runs *before* the lab record is removed
          from memory. If listing or other environmental failures occur (e.g.
          unconfigured ``JOB_SERVICE_URL``, job-service transport errors), the
          lab record and its linked strategy/backtest are left intact and the
          failure surfaces as **503** instead of being silently swallowed — a
          retry then re-attempts the same cleanup rather than 404ing against
          an already-deleted record while paper sessions sit orphaned in the
          job service. Other unexpected exceptions may still surface as 500.
        - ``_strategy_lab_records[lab_record_id]`` is removed only if it is
          still present by the time the in-memory-mutation step runs — a
          concurrent delete of the same ``lab_record_id`` may have already
          removed it while this call was doing paper-session cleanup, in
          which case this call reports no strategy/backtest deletion for it.
        - ``deleted_strategy_id``/``deleted_backtest_id`` are ``None`` unless
          the corresponding entry actually existed in ``_strategies``/
          ``_backtests`` *before* this call — ``_PersistentDict.__delitem__``
          never raises on a missing key (it discards the underlying
          ``delete_job`` bool), so an unconditional ``del`` can't be used to
          infer whether anything was actually removed; existence is checked
          via ``in`` first instead.
        - ``_strategies``/``_backtests`` are ``_PersistentDict`` instances
          (see their module-level construction), not plain in-memory dicts —
          deleting from them deletes the underlying ``investment_strategies``/
          ``investment_backtests`` job-service rows too, via
          ``_PersistentDict.__delitem__`` -> ``JobServiceClient.delete_job``.
          "linked lab strategy/backtest jobs" in the summary above refers to
          exactly this.

    Raises:
        - ``HTTPException`` 404: ``lab_record_id`` does not resolve to any
          known lab record.
    """
    with _lock:
        raw = _strategy_lab_records.get(lab_record_id)
        if raw is None:
            raise HTTPException(
                status_code=404,
                detail=f"Strategy lab record '{lab_record_id}' not found.",
            )
        record = StrategyLabRecord.parse_persisted(raw)
        strategy_id = record.strategy.strategy_id
        backtest_id = record.backtest.backtest_id

    # External cleanup (job-service network I/O) runs before any in-memory
    # mutation: if this raises, nothing below has been deleted yet, so the
    # record stays retryable instead of becoming an orphan-producing 404.
    paper_deleted = _delete_paper_sessions_for_lab_record(lab_record_id)

    with _lock:
        strategy_deleted = False
        backtest_deleted = False
        if lab_record_id in _strategy_lab_records:
            del _strategy_lab_records[lab_record_id]
            # _strategies/_backtests are _PersistentDict (JobServiceClient-backed), so
            # these deletes also remove the investment_strategies/investment_backtests
            # job-service rows, not just a process-local cache entry.
            strategy_deleted = strategy_id in _strategies
            if strategy_deleted:
                del _strategies[strategy_id]
            backtest_deleted = backtest_id in _backtests
            if backtest_deleted:
                del _backtests[backtest_id]

    return DeleteStrategyLabRecordResponse(
        lab_record_id=lab_record_id,
        deleted_strategy_id=strategy_id if strategy_deleted else None,
        deleted_backtest_id=backtest_id if backtest_deleted else None,
        deleted_paper_trading_sessions=paper_deleted,
    )


@app.delete("/strategy-lab/storage", response_model=ClearStrategyLabStorageResponse)
def clear_strategy_lab_storage() -> ClearStrategyLabStorageResponse:
    """
    Remove all persisted strategy lab data from the job service (Postgres ``khala_jobs.jobs``
    when ``JOB_SERVICE_URL`` is set, or local ``AGENT_CACHE`` files otherwise).

    Deletes:

    - Team ``investment_strategy_lab_records`` (all lab run cards).
    - Team ``investment_strategies`` rows whose job id starts with ``strat-lab-`` (lab-generated only).
    - Team ``investment_backtests`` rows whose job id starts with ``bt-lab-``.
    - Team ``investment_paper_trading_sessions`` (all paper trading runs tied to the lab flow).

    Does **not** remove advisor sessions, IPS, proposals, or strategies/backtests created via
    ``POST /strategies`` / ``POST /backtests`` outside the lab.

    Runs the purge without holding ``_lock``: ``_purge_strategy_lab_job_storage``
    only performs job-service I/O (bounded by its own internal deadline,
    ``_PURGE_TIMEOUT_S``) and never reads or writes any state ``_lock``
    protects (``_active_runs`` and friends). Holding ``_lock`` for that whole
    I/O span would serialize every unrelated run/resume/restart/delete
    request behind this purge for no reason.
    """
    counts = _purge_strategy_lab_job_storage()
    return ClearStrategyLabStorageResponse(
        deleted_lab_records=counts["deleted_lab_records"],
        deleted_lab_strategies=counts["deleted_lab_strategies"],
        deleted_lab_backtests=counts["deleted_lab_backtests"],
        deleted_paper_trading_sessions=counts["deleted_paper_trading_sessions"],
    )


# ---------------------------------------------------------------------------
# Paper Trading — simulated live trading with real market data
# ---------------------------------------------------------------------------


class RunPaperTradingRequest(BaseModel):
    """Start a paper trading session for a winning strategy.

    Live-mode fields (``provider_id``, ``min_fills``, ``max_hours``,
    ``warmup_bars``, ``timeframe``) are validated at the API boundary and
    take effect only when ``INVESTMENT_LIVE_PAPER_ENABLED=true``. When the
    flag is off (the default), the legacy recent-OHLCV path runs and the
    new fields are not used by the trading logic, but invalid values are
    still rejected to keep request validation consistent.
    """

    lab_record_id: str = Field(..., description="ID of a winning StrategyLabRecord to paper trade")
    initial_capital: float = Field(default=100000.0, gt=0)
    transaction_cost_bps: Optional[float] = Field(
        default=None,
        ge=0,
        description="Override tx cost (bps); auto-detected from asset class when omitted",
    )
    slippage_bps: Optional[float] = Field(
        default=None,
        ge=0,
        description="Override slippage (bps); auto-detected from asset class when omitted",
    )
    lookback_days: int = Field(
        default=365, ge=30, description="Days of recent market data to fetch (legacy path)"
    )
    # ------------------------------------------------------------------
    # Live-mode additions (honored only when INVESTMENT_LIVE_PAPER_ENABLED=true)
    # ------------------------------------------------------------------
    provider_id: Optional[str] = Field(
        default=None,
        description=(
            "Explicit provider override (e.g. 'binance', 'coinbase', 'polygon'). "
            "Omit to use registry default. See GET /providers for the configured list."
        ),
    )
    min_fills: int = Field(
        default=20,
        ge=1,
        le=10_000,
        description=(
            "Terminate the session once this many trades have closed. "
            "Values below 20 are accepted but add 'min_fills_below_recommended' to session.warnings."
        ),
    )
    max_hours: float = Field(
        default=72.0,
        gt=0.0,
        le=8_760.0,
        description=(
            "Wall-clock safety guard — session terminates after this many hours "
            "regardless of fill count. Capped at 8760h (1 year); an unbounded value "
            "would overflow the workflow's activity timeout computation."
        ),
    )
    warmup_bars: int = Field(
        default=500,
        ge=0,
        le=5_000,
        description="Historical bars to replay as ctx.is_warmup=True before the live feed starts.",
    )
    timeframe: Optional[Literal["1s", "15s", "30s", "1m", "5m", "15m", "30m", "1h", "4h", "1d"]] = (
        Field(
            default=None,
            description=(
                "Override the strategy's declared timeframe. Must be one of "
                "{'1s','15s','30s','1m','5m','15m','30m','1h','4h','1d'}."
            ),
        )
    )


class PaperTradingResponse(BaseModel):
    session: PaperTradingSession
    message: str = "Paper trading session completed."


class PaperTradingResultsResponse(BaseModel):
    items: List[PaperTradingSession] = Field(default_factory=list)
    count: int = 0
    ready_for_live_count: int = 0
    not_performant_count: int = 0

    @model_validator(mode="after")
    def _derive_counts_from_items(self) -> "PaperTradingResultsResponse":
        """Recompute the count fields from ``items`` so they can never drift apart.

        Postconditions: ``count == len(items)``, ``ready_for_live_count`` and
        ``not_performant_count`` equal the number of items with the matching
        ``verdict``, regardless of what was passed in for those fields.
        """
        self.count = len(self.items)
        self.ready_for_live_count = sum(
            1 for i in self.items if i.verdict == PaperTradingVerdict.READY_FOR_LIVE
        )
        self.not_performant_count = sum(
            1 for i in self.items if i.verdict == PaperTradingVerdict.NOT_PERFORMANT
        )
        return self


def _run_paper_trading_background(
    session_id: str,
    lab_record_id: str,
    strategy: StrategySpec,
    strategy_code: str,
    backtest_record: BacktestRecord,
    lookback_days: int,
    initial_capital: float,
    transaction_cost_bps: float,
    slippage_bps: float,
) -> None:
    """Background worker: fetch market data, run strategy, compare, and persist final session.

    Long-running (market data fetch + sandbox execution + LLM divergence analysis can
    take 2-3 minutes), so this runs off the request thread to avoid proxy timeouts.

    Preconditions:
        - ``session_id`` must already exist in ``_paper_trading_sessions`` with status
          RUNNING — validated at the top of the worker (before any market-data fetch
          or agent execution): a missing, unparseable, or non-RUNNING session logs a
          warning and returns early instead of doing the expensive work for a session
          that can't accept its result.
        - ``strategy`` must be a valid StrategySpec with resolvable symbols
        - ``backtest_record`` must contain valid backtest results for divergence analysis

    Postconditions:
        - On the success path, ``_paper_trading_sessions[session_id]`` is always written
          (COMPLETED or FAILED with ``completed_at`` set), which can recreate a concurrently
          deleted session — UNLESS the session is already terminal at write time (see below).
        - Import failures for ``MarketDataService``/``PaperTradingAgent`` (e.g. a missing
          dependency or circular import) are caught by the same handler as any other
          in-worker exception and also transition the session to FAILED
        - On the empty-data and exception paths, the terminal write runs only when the session
          entry still exists at write time; concurrent deletion (e.g. via
          ``DELETE /strategy-lab/records/{lab_record_id}``) then leaves no terminal record
        - Every terminal write (empty-data, success, crash) first checks
          ``_paper_trading_session_already_terminal`` and refuses to overwrite a
          session that's already COMPLETED/FAILED (logged, not silently
          skipped). This guards against a dispatch-failure-declared FAILED
          session (``run_paper_trading``'s ``_fail_paper_trading_session`` call)
          being clobbered by this worker landing late — e.g. an orphaned
          Temporal workflow that the dispatch failure's best-effort stop
          signal also failed to reach.

    Raises:
        - None, for ``Exception`` and its subclasses: import errors for the two
          lazily-imported dependencies, provider/network failures, and this
          function's own postcondition guards are all caught and logged; the
          session is marked FAILED instead. This includes a failure to re-parse
          the persisted session record while handling a crash: that secondary
          failure is itself caught and logged, so an unparseable record is left
          as-is (logged, not updated) rather than letting the parse error escape
          in place of the original crash.
        - ``KeyboardInterrupt`` / ``SystemExit`` / ``GeneratorExit`` (``BaseException``
          but not ``Exception``) are deliberately NOT caught here and propagate to
          the caller — silently converting a worker-thread interrupt or interpreter
          shutdown signal into a FAILED session would mask it instead of letting it
          terminate the worker.
    """
    try:
        # Validate the documented precondition — a missing, unparseable, or
        # non-RUNNING session — before doing any of the expensive work below
        # (market-data fetch, sandbox execution, LLM divergence analysis).
        # Without this check the worker would spend 2-3 minutes producing a
        # result for a session that was never RUNNING to begin with (e.g. it
        # was concurrently deleted, or something else already moved it to a
        # terminal state), then discover on write-back that there's nowhere
        # to persist it.
        with _lock:
            raw = _paper_trading_sessions.get(session_id)
            if raw is None:
                logger.warning(
                    "Paper trade %s: no session found at worker start; nothing to run.",
                    session_id,
                )
                return
            try:
                current_status = PaperTradingSession.parse_persisted(raw).status
            except Exception:
                logger.warning(
                    "Paper trade %s: session unparseable at worker start; nothing to run.",
                    session_id,
                    exc_info=True,
                )
                return
            if current_status != PaperTradingStatus.RUNNING:
                logger.warning(
                    "Paper trade %s: session status is %s (expected RUNNING) at worker "
                    "start; nothing to run.",
                    session_id,
                    current_status,
                )
                return

        from investment_team.market_data_service import MarketDataService
        from investment_team.paper_trading_agent import PaperTradingAgent

        market_service = MarketDataService()
        # Issue #523 — match the orchestrator backtest's universe choice.
        symbols = market_service.resolve_strategy_symbols(strategy)
        logger.info(
            "Paper trade %s: fetching %d days of market data for %d symbols (%s) ...",
            session_id,
            lookback_days,
            len(symbols),
            strategy.asset_class,
        )
        market_data = market_service.fetch_multi_symbol(
            symbols, strategy.asset_class, lookback_days
        )

        if not market_data:
            _fail_paper_trading_session(
                session_id, "Failed to fetch market data from external sources."
            )
            return

        agent = PaperTradingAgent()
        result_session = agent.run_session(
            strategy=strategy,
            strategy_code=strategy_code,
            backtest_record=backtest_record,
            market_data=market_data,
            initial_capital=initial_capital,
            transaction_cost_bps=transaction_cost_bps,
            slippage_bps=slippage_bps,
        )
        # Enforce the postcondition this function documents: run_session must
        # return a terminal session with completed_at set. A violation here is
        # a bug in the callee, not something to coerce around — raising lets
        # the except block below turn it into a FAILED record, same as any
        # other in-worker crash. Explicit raises (not ``assert``) so the guard
        # stays active under ``python -O``/``-OO``.
        if result_session.status not in (
            PaperTradingStatus.COMPLETED,
            PaperTradingStatus.FAILED,
        ):
            raise ValueError(
                f"PaperTradingAgent.run_session returned non-terminal status {result_session.status!r}"
            )
        if not result_session.completed_at:
            raise ValueError(
                "PaperTradingAgent.run_session returned a session with no completed_at"
            )
        # Preserve the session_id and lab_record_id that the caller committed to.
        result_session.session_id = session_id
        result_session.lab_record_id = lab_record_id

        with _lock:
            if _paper_trading_session_already_terminal(session_id):
                logger.warning(
                    "Paper trade %s: session already terminal when this worker's "
                    "result was about to be written; discarding the result instead "
                    "of clobbering it (a concurrent writer already finalized it).",
                    session_id,
                )
            else:
                _paper_trading_sessions[session_id] = result_session
                logger.info(
                    "Paper trade %s: completed (status=%s, verdict=%s, trades=%d)",
                    session_id,
                    result_session.status,
                    result_session.verdict,
                    len(result_session.trades),
                )
    except BaseException as exc:
        # Deliberately narrower than a bare `except BaseException`: KeyboardInterrupt /
        # SystemExit / GeneratorExit are BaseException but not Exception and must
        # propagate rather than being converted into a FAILED session — same
        # distinction shared.concurrency.parallel_map draws for worker exceptions.
        # Everything else (import failures, provider/network errors, programming
        # bugs) is still folded into a FAILED session below: nothing downstream
        # (run_paper_trading_activity) marks this session terminal if this worker
        # raises, so surfacing a non-interrupt exception here would leave the
        # session stuck RUNNING forever instead of failing cleanly.
        if not isinstance(exc, Exception):
            raise
        logger.exception("Paper trade %s: background worker crashed", session_id)
        # Nested try/except: parse_persisted() below can itself raise (e.g. a
        # corrupt persisted record) — that secondary exception is not caught by
        # this handler's own `except Exception as exc` clause (Python doesn't
        # let an except block catch its own body's exceptions), so left
        # unguarded it would escape the worker, contradicting this function's
        # documented "Raises: None" contract. Catch and log it here instead;
        # the record is left unparseable/unupdated rather than crashing the
        # worker over a failure to report a failure.
        try:
            with _lock:
                if _paper_trading_session_already_terminal(session_id):
                    logger.warning(
                        "Paper trade %s: session already terminal when the crash "
                        "handler's write was about to run; leaving it untouched "
                        "(a concurrent writer already finalized it).",
                        session_id,
                    )
                else:
                    raw = _paper_trading_sessions.get(session_id)
                    if raw is not None:
                        session = PaperTradingSession.parse_persisted(raw)
                        session.status = PaperTradingStatus.FAILED
                        session.error = f"Paper trading crashed: {exc}"
                        session.divergence_analysis = f"Paper trading crashed: {exc}"
                        session.completed_at = datetime.now(tz=timezone.utc).isoformat()
                        _paper_trading_sessions[session_id] = session
        except Exception:
            logger.exception(
                "Paper trade %s: failed to persist FAILED status for the crashed "
                "session (record may be unparseable, or the store rejected the write)",
                session_id,
            )


@app.post("/strategy-lab/paper-trade", response_model=PaperTradingResponse)
def run_paper_trading(request: RunPaperTradingRequest) -> PaperTradingResponse:
    """
    Start a paper trading session for a winning strategy. Returns immediately.

    Because paper trading can take 2-3 minutes (market data fetch + sandbox
    execution + LLM divergence analysis), this endpoint validates inputs, creates
    a session in ``OPENING`` status (live path) or ``running`` status (legacy
    path), then dispatches a durable ``PaperTradingWorkflow`` via
    ``_start_paper_trading`` (Temporal-only). Execution happens inside
    ``run_paper_trading_activity`` on the investment task queue, which runs
    either ``_run_live_paper_trading_background`` (live path) or
    ``_run_paper_trading_background`` (legacy recent-OHLCV path) depending on
    whether the live path is enabled. Clients should poll
    ``GET /strategy-lab/paper-trade/{session_id}`` for progress until
    ``status`` is ``completed`` or ``failed``.
    """
    # 1 — Look up the winning strategy lab record (synchronous validation)
    with _lock:
        raw_record = _strategy_lab_records.get(request.lab_record_id)

    if raw_record is None:
        raise HTTPException(
            status_code=404, detail=f"Strategy lab record '{request.lab_record_id}' not found."
        )

    try:
        lab_record = StrategyLabRecord.parse_persisted(raw_record)
    except Exception as exc:
        # The record exists but is internally corrupt (schema drift, a
        # missing required field, malformed JSON, ...) -- a server-side data
        # integrity problem, not a client input error, so this is a 500
        # rather than a 4xx. Log the full exception (which may include raw
        # persisted field values) server-side only; the client-facing detail
        # stays generic so it can't leak internal schema/validation details.
        logger.error(
            "Strategy lab record %s failed to parse: %s",
            request.lab_record_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Strategy lab record '{request.lab_record_id}' is corrupted and cannot be loaded.",
        ) from exc

    if not lab_record.is_winning:
        raise HTTPException(
            status_code=400,
            detail=f"Strategy '{request.lab_record_id}' is not a winning strategy. "
            f"Only winning strategies (annualized return >= {WINNING_THRESHOLD:g}%, the "
            "S&P-500 benchmark) can be paper traded.",
        )

    if not lab_record.is_publishable:
        reason = lab_record.publishability_skip_reason or "not_publishable"
        raise HTTPException(
            status_code=400,
            detail=(
                f"Strategy '{request.lab_record_id}' is not publishable "
                f"({reason}). Only strategies that clear realism, alignment, "
                "exit-rule conformance, and look-ahead gates can be paper traded."
            ),
        )

    strategy = lab_record.strategy
    # The backtest record is re-derived inside run_paper_trading_activity (the
    # legacy path needs it); the route only validates winning status + code here.
    strategy_code = lab_record.strategy_code or getattr(strategy, "strategy_code", None)
    if not strategy_code:
        raise HTTPException(
            status_code=400,
            detail=f"Strategy '{request.lab_record_id}' has no generated strategy code. "
            "Only strategies with executable code can be paper traded.",
        )

    # 2 — Create the initial session (OPENING for the live path, RUNNING for
    # the legacy recent-OHLCV path) and persist immediately
    now = datetime.now(tz=timezone.utc).isoformat()
    use_live = _live_paper_enabled()

    # 2a — Concurrency guard (spec §7.2): one live session per strategy_id.
    # Only enforced for the live path — the legacy recent-OHLCV path
    # completes in seconds and isn't subject to the "one at a time"
    # invariant. The scan, session construction, and dict insertion below all
    # happen under a single lock acquisition so two concurrent requests for
    # the same strategy_id can't both pass the scan before either inserts. A
    # record that fails to parse is logged and skipped rather than aborting
    # the whole request — see _fail_paper_trading_session for the same
    # pattern.
    with _lock:
        if use_live:
            for existing in _paper_trading_sessions.values():
                try:
                    existing_session = PaperTradingSession.parse_persisted(existing)
                except Exception:
                    bad_id = (
                        existing.get("session_id")
                        if isinstance(existing, dict)
                        else getattr(existing, "session_id", None)
                    )
                    logger.warning(
                        "Skipping unparseable paper-trading session while checking "
                        "the concurrency guard: %s",
                        bad_id,
                        exc_info=True,
                    )
                    continue
                if (
                    existing_session.strategy.strategy_id == strategy.strategy_id
                    and existing_session.status in _ACTIVE_PT_STATES
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Strategy '{strategy.strategy_id}' already has an "
                            f"active live paper-trading session "
                            f"'{existing_session.session_id}' "
                            f"(status={existing_session.status.value}). Stop it "
                            f"via POST /strategy-lab/paper-trade/"
                            f"{existing_session.session_id}/stop before "
                            f"starting a new one."
                        ),
                    )

        # Full UUID hex (128 bits of entropy) makes a collision astronomically
        # unlikely on its own, but this loop closes the gap completely: a
        # colliding id would otherwise silently overwrite an existing
        # active/terminal session's dict entry. Generated and checked while
        # already holding _lock so the check-then-insert below is atomic
        # against a concurrent request minting the same id.
        session_id = f"pt-{uuid.uuid4().hex}"
        while session_id in _paper_trading_sessions:
            session_id = f"pt-{uuid.uuid4().hex}"

        try:
            running_session = PaperTradingSession(
                session_id=session_id,
                lab_record_id=request.lab_record_id,
                strategy=strategy,
                status=PaperTradingStatus.OPENING if use_live else PaperTradingStatus.RUNNING,
                initial_capital=request.initial_capital,
                current_capital=request.initial_capital,
                symbols_traded=[],
                data_source="live" if use_live else "yahoo_finance",
                data_period_start="",
                data_period_end="",
                started_at=now,
            )
        except ValidationError as exc:
            # Mirrors create_profile's handling of the same failure mode: a
            # derived-field validation failure here is a 422 (bad/unbuildable
            # request-derived data), not an unhandled 500.
            raise HTTPException(
                status_code=422, detail=exc.errors(include_url=False, include_context=False)
            ) from exc
        _paper_trading_sessions[session_id] = running_session

    # 3 — Dispatch the durable paper-trading workflow (Temporal-only). The live
    # path (PR 2) is gated behind INVESTMENT_LIVE_PAPER_ENABLED so operators opt
    # in; otherwise the legacy recent-OHLCV replay path runs. Both execute inside
    # ``run_paper_trading_activity`` on the investment task queue, so a worker
    # crash resumes/records via the persisted session rather than a lost thread.
    # A dispatch failure (Temporal down / worker not connected) must not leave the
    # session stuck ``running`` — that would block future live starts for this
    # strategy via the concurrency guard — so roll it forward to ``failed``.
    try:
        _start_paper_trading(
            session_id,
            {
                "session_id": session_id,
                "lab_record_id": request.lab_record_id,
                "use_live": use_live,
                "request": request.model_dump(mode="json"),
                "max_hours": request.max_hours,
            },
        )
    except Exception as exc:
        # The dispatch RPC's ack-wait can time out even though the workflow
        # genuinely started server-side (the sync bridge's wait only bounds our
        # own wait, not the underlying start call) — so before declaring the
        # session failed, best-effort signal the deterministic workflow id to
        # stop it if it did start. A workflow that never started simply has no
        # handle to signal, so this is a harmless no-op in the common case; it
        # only matters for the ambiguous-timeout case, where it prevents an
        # orphaned, unstoppable live session.
        try:
            _signal_paper_trading_stop(session_id)
        except Exception:
            # Best-effort: if the workflow really did start server-side despite
            # the client-side timeout, and this stop signal ALSO fails to
            # deliver, it runs unsupervised. If it later reaches its own
            # terminal state, that write no longer silently overwrites the
            # ``failed`` status set just below — both background workers'
            # terminal writes check _paper_trading_session_already_terminal
            # first and refuse to clobber an already-terminal session. Still
            # log here so the compound failure is visible to operators, not
            # just silently absorbed.
            logger.warning(
                "Best-effort stop signal for possibly-orphaned paper-trading "
                "session %s failed to deliver; the session is marked failed but "
                "an orphaned workflow may still be running.",
                session_id,
                exc_info=True,
            )
        # Only the documented Temporal dispatch/worker failure modes get the
        # 503 below: HTTPException (raised by _require_temporal when Temporal
        # is disabled), RuntimeError (the worker client never became ready),
        # and TimeoutError/RPCError (the start ack timed out, or the RPC
        # itself failed) -- the exact set start_workflow_sync's docstring and
        # _await_client document. Anything else is a genuine bug (a bad
        # payload, a programming error) and must surface as its own 500
        # instead of being misreported as "Temporal worker unavailable."
        from temporalio.service import RPCError

        is_dispatch_failure = isinstance(exc, (HTTPException, RuntimeError, TimeoutError, RPCError))

        # Best-effort failure recording must not mask the original dispatch
        # error (especially an HTTPException with the intended status/detail).
        # If the session was concurrently removed or is unparseable,
        # _fail_paper_trading_session can raise — log and continue so the
        # caller still receives the original exception.
        try:
            _fail_paper_trading_session(
                session_id,
                "Failed to start the paper-trading workflow (Temporal unavailable)."
                if is_dispatch_failure
                else "Unexpected error starting the paper-trading workflow.",
            )
        except Exception:
            logger.exception(
                "Failed to mark paper-trading session %s as failed",
                session_id,
            )
        if isinstance(exc, HTTPException):
            raise
        if is_dispatch_failure:
            raise HTTPException(
                status_code=503,
                detail="Failed to start the paper-trading workflow; Temporal worker unavailable.",
            ) from exc
        logger.exception(
            "Unexpected error starting paper-trading workflow for session %s", session_id
        )
        raise HTTPException(
            status_code=500,
            detail="Unexpected error starting the paper-trading workflow.",
        ) from exc

    return PaperTradingResponse(
        session=running_session,
        message=f"Paper trading started. Poll GET /strategy-lab/paper-trade/{session_id} for progress.",
    )


# ---------------------------------------------------------------------------
# Live-mode paper trading (PR 2)
# ---------------------------------------------------------------------------
#
# The live path consumes a streaming market-data feed and drives the same
# TradingService used by backtests. It is gated behind INVESTMENT_LIVE_PAPER_ENABLED
# so existing deployments keep the legacy recent-OHLCV behavior until operators
# opt in. See ``system_design/pr2_live_data_and_paper_cutover.md``.


def _live_paper_enabled() -> bool:
    """Return True when the live paper-trading path is opted in via env var."""
    return env_bool("INVESTMENT_LIVE_PAPER_ENABLED", default=False)


# Per-session in-process StopController registry used by
# ``_run_live_paper_trading_background``: the worker registers a controller and
# polls it between bars. The POST /stop endpoint does not read this dict — it
# delivers stop via a Temporal signal (``_signal_paper_trading_stop``). Guarded
# by ``_lock`` shared with other session state.
_live_paper_stop_controllers: Dict[str, Any] = {}

# Paper-trading statuses that mean a session is still in flight. Used by the
# live-session concurrency guard and by ``/stop`` to stay idempotent (a terminal
# session has a closed workflow, so signalling it would error). ``RUNNING`` is
# the legacy pre-live value, treated as active too.
_ACTIVE_PT_STATES = {
    PaperTradingStatus.OPENING,
    PaperTradingStatus.WARMING_UP,
    PaperTradingStatus.LIVE,
    PaperTradingStatus.RUNNING,
}


def _paper_trading_session_already_terminal(session_id: str) -> bool:
    """Whether the currently-persisted session for ``session_id`` is already
    in a terminal status (``COMPLETED``/``FAILED``).

    Guards every terminal-status write below against clobbering a session an
    orphaned/delayed writer shouldn't be able to override — e.g. a dispatch
    failure declares a session FAILED via ``_fail_paper_trading_session``
    while its Temporal workflow may still be running server-side (the start
    RPC and the best-effort stop signal both failed); if that orphaned
    workflow later completes, its own terminal write must not silently
    overwrite the FAILED status already recorded.

    Preconditions:
        - Caller holds ``_lock`` for the duration of both this check and
          whatever conditional write it gates — checking without holding the
          lock across both would let a concurrent writer land in between.
    Postconditions:
        - Returns ``False`` when no session is persisted for ``session_id``.
        - Returns ``False`` when the persisted record fails to parse — an
          unparseable record can't be confirmed terminal, and a well-formed
          new terminal write is a strict improvement over leaving corrupt
          data in place, so callers should proceed with their write.
        - Otherwise returns whether the persisted status is not in
          ``_ACTIVE_PT_STATES``.
    """
    raw = _paper_trading_sessions.get(session_id)
    if raw is None:
        return False
    try:
        session = PaperTradingSession.parse_persisted(raw)
    except Exception:
        return False
    return session.status not in _ACTIVE_PT_STATES


def _apply_paper_trading_failure(
    session: PaperTradingSession,
    error: str,
    *,
    completed_at: Optional[str] = None,
    terminated_reason: Optional[str] = None,
    set_legacy_divergence_analysis: bool = False,
) -> None:
    """Mutate ``session`` in place to record a terminal failure.

    Shared by ``_fail_paper_trading_session`` (single-session, self-locking) and
    ``_recover_orphaned_paper_trading_sessions`` (whole-batch, caller-locked) so
    the terminal-write fields live in one place.

    Preconditions:
        - Caller holds ``_lock`` and has already decided ``session`` should be
          failed (e.g. confirmed it isn't already COMPLETED/FAILED).
    Postconditions:
        - session.status == FAILED and session.error == error.
        - session.completed_at is set to ``completed_at`` if given, else the
          current UTC time.
        - If ``terminated_reason`` is not None, session.terminated_reason is set.
        - If ``set_legacy_divergence_analysis``, session.divergence_analysis is
          mirrored from session.error.
    """
    session.status = PaperTradingStatus.FAILED
    session.error = error
    session.completed_at = completed_at or datetime.now(tz=timezone.utc).isoformat()
    if terminated_reason is not None:
        session.terminated_reason = terminated_reason
    if set_legacy_divergence_analysis:
        session.divergence_analysis = session.error


def _fail_paper_trading_session(session_id: str, error: str) -> None:
    """Mark a paper-trading session ``failed`` (best-effort, idempotent).

    Preconditions:
        - ``session_id`` may or may not exist in ``_paper_trading_sessions``.
    Postconditions:
        - If the session exists and is not already ``COMPLETED``/``FAILED``, its
          status is set to ``FAILED`` with ``error`` and a ``completed_at``
          stamp. A missing session, an already-terminal session, and
          unparseable persisted data are all left as no-ops. Never raises.
    """
    with _lock:
        raw = _paper_trading_sessions.get(session_id)
        if raw is None:
            return
        try:
            session = PaperTradingSession.parse_persisted(raw)
        except Exception:
            logger.warning(
                "Could not parse persisted paper-trading session %s while marking "
                "it failed; leaving it untouched.",
                session_id,
                exc_info=True,
            )
            return
        if session.status in (PaperTradingStatus.COMPLETED, PaperTradingStatus.FAILED):
            # Don't clobber a real terminal outcome that landed concurrently
            # (e.g. the workflow actually completed while this caller was
            # deciding to mark it failed).
            return
        try:
            _apply_paper_trading_failure(session, error)
            _paper_trading_sessions[session_id] = session
        except Exception:
            # The mutations above and the dict write (which round-trips
            # through JobServiceClient) are unguarded operations that can
            # themselves raise (e.g. a store RPC failure) — catch here so
            # this best-effort helper's documented "Never raises" contract
            # holds end to end, not just across the parse_persisted() call
            # above.
            logger.exception(
                "Paper-trade session %s: failed to persist FAILED status "
                "(mutation or store write raised); leaving it untouched.",
                session_id,
            )


# Fallback values for the env vars below — also what a caller sees if the
# operator hasn't set either var.
_DEFAULT_TX_COST_BPS = 5.0
_DEFAULT_SLIPPAGE_BPS = 2.0


def _default_tx_cost_bps() -> float:
    """Operator-tunable fallback ``transaction_cost_bps`` (basis points) used
    when a paper-trading request omits an explicit override.

    Read from ``INVESTMENT_DEFAULT_TX_COST_BPS`` (falls back to
    ``_DEFAULT_TX_COST_BPS``) on every call rather than once at import time,
    so operators can retune this business parameter without a redeploy.
    """
    return env_float(
        "INVESTMENT_DEFAULT_TX_COST_BPS", _DEFAULT_TX_COST_BPS, floor=0.0, ceiling=1000.0
    )


def _default_slippage_bps() -> float:
    """Operator-tunable fallback ``slippage_bps`` (basis points) used when a
    paper-trading request omits an explicit override.

    Read from ``INVESTMENT_DEFAULT_SLIPPAGE_BPS`` (falls back to
    ``_DEFAULT_SLIPPAGE_BPS``) on every call rather than once at import time,
    so operators can retune this business parameter without a redeploy.
    """
    return env_float(
        "INVESTMENT_DEFAULT_SLIPPAGE_BPS", _DEFAULT_SLIPPAGE_BPS, floor=0.0, ceiling=1000.0
    )


def _resolve_fee_overrides(
    request: "RunPaperTradingRequest", asset_class: Optional[str] = None
) -> tuple[float, float]:
    """Return ``(transaction_cost_bps, slippage_bps)`` for the live config.

    Uses explicit ``None`` checks instead of ``or`` so a caller asking for
    zero-fee / zero-slippage experiments isn't silently bumped to the
    defaults — ``0.0`` is falsy but semantically meaningful here.

    When ``asset_class`` is given, an omitted override falls back to
    ``get_fee_defaults(asset_class)`` so non-stock strategies (crypto,
    forex, ...) get correct per-asset-class fees instead of the flat
    stock-tier default. Callers that don't have an asset class in scope
    keep the operator-tunable env-var default.
    """
    fee_defaults = get_fee_defaults(asset_class) if asset_class is not None else None
    tx = (
        request.transaction_cost_bps
        if request.transaction_cost_bps is not None
        else (fee_defaults["transaction_cost_bps"] if fee_defaults else _default_tx_cost_bps())
    )
    slip = (
        request.slippage_bps
        if request.slippage_bps is not None
        else (fee_defaults["slippage_bps"] if fee_defaults else _default_slippage_bps())
    )
    return tx, slip


def _run_live_paper_trading_background(
    session_id: str,
    lab_record_id: str,
    strategy: StrategySpec,
    request: "RunPaperTradingRequest",
) -> None:
    """Background worker for the PR 2 live paper-trading path.

    Resolves a provider, opens the live stream, drives ``TradingService``
    until termination, then writes the final ``PaperTradingSession``.

    Postconditions:
        Both the success and crash-handler terminal writes first check
        ``_paper_trading_session_already_terminal`` and refuse to overwrite a
        session that's already COMPLETED/FAILED (logged, not silently
        skipped) — see ``_run_paper_trading_background``'s docstring for why
        this guard exists (a dispatch-failure-declared FAILED session must
        survive an orphaned workflow landing late).

    Raises:
        - None. All failures are caught and logged; the session is marked
          FAILED instead. This includes a failure to re-parse the persisted
          session record while handling a crash: that secondary failure is
          itself caught and logged, so an unparseable record is left as-is
          (logged, not updated) rather than letting the parse error escape in
          place of the original crash.
    """
    from investment_team.models import BacktestConfig as _BC
    from investment_team.trading_service.modes.paper_trade import (
        PaperTradeConfig,
        StopController,
        run_paper_trade,
    )

    controller = StopController()
    with _lock:
        _live_paper_stop_controllers[session_id] = controller

    try:
        # Issue #523 — honour target_symbols when set; otherwise fall back
        # to the asset-class default universe (capped at 5; #525 removes
        # the magic cap).
        from investment_team.market_data_service import MarketDataService

        market_service = MarketDataService()
        symbols = market_service.resolve_strategy_symbols(strategy)
        if not symbols:
            raise RuntimeError("no symbols resolved for strategy")

        strategy_timeframe = request.timeframe or getattr(strategy, "timeframe", None) or "1m"

        tx_cost, slip = _resolve_fee_overrides(request, asset_class=strategy.asset_class)
        # Captured once: two separate datetime.now() calls could straddle
        # midnight and produce a start/end date that spans two days for a
        # config meant to represent a single live trading day.
        today = datetime.now(tz=timezone.utc).date().isoformat()
        bt_config = _BC(
            start_date=today,
            end_date=today,
            initial_capital=request.initial_capital,
            transaction_cost_bps=tx_cost,
            slippage_bps=slip,
        )
        paper_cfg = PaperTradeConfig(
            symbols=symbols,
            asset_class=strategy.asset_class,
            strategy_timeframe=strategy_timeframe,
            min_fills=request.min_fills,
            max_hours=request.max_hours,
            warmup_bars=request.warmup_bars,
            provider_id=request.provider_id,
        )

        run_result = run_paper_trade(
            strategy=strategy,
            backtest_config=bt_config,
            paper_config=paper_cfg,
            stop_controller=controller,
        )

        # Persist the completed session.
        with _lock:
            raw = _paper_trading_sessions.get(session_id)
            if raw is None:
                logger.warning(
                    "Live paper trade %s: session removed before results could "
                    "be persisted; discarding run result.",
                    session_id,
                )
                return
            session = PaperTradingSession.parse_persisted(raw)
            if session.status not in _ACTIVE_PT_STATES:
                logger.warning(
                    "Live paper trade %s: session already terminal (%s) when this "
                    "worker's result was about to be written; discarding the "
                    "result instead of clobbering it (a concurrent writer already "
                    "finalized it).",
                    session_id,
                    session.status,
                )
                return
            session.trades = run_result.trades
            session.fill_count = run_result.fill_count
            session.cutover_ts = run_result.cutover_ts
            session.provider_id = run_result.provider_id
            session.terminated_reason = run_result.terminated_reason
            session.warnings = run_result.warnings
            session.error = run_result.error or None
            session.symbols_traded = symbols
            session.data_source = f"live:{run_result.provider_id}"
            # Issue #376 — surface the warm-up snapshot fingerprint on the
            # persisted session so reproducibility checks can refer back
            # to the exact bars that drove warm-up.
            session.dataset_fingerprint = run_result.dataset_fingerprint
            session.completed_at = datetime.now(tz=timezone.utc).isoformat()
            if run_result.error or run_result.terminated_reason in {
                "lookahead_violation",
                "provider_error",
                "region_blocked",
                "no_provider",
            }:
                session.status = PaperTradingStatus.FAILED
            else:
                session.status = PaperTradingStatus.COMPLETED
            _paper_trading_sessions[session_id] = session
        logger.info(
            "Live paper trade %s: terminated (%s), provider=%s, fills=%d, trades=%d",
            session_id,
            run_result.terminated_reason,
            run_result.provider_id,
            run_result.fill_count,
            len(run_result.trades),
        )
    except Exception as exc:
        logger.exception("Live paper trade %s: background worker crashed", session_id)
        # Nested try/except: parse_persisted() below can itself raise (e.g. a
        # corrupt persisted record) — that secondary exception is not caught by
        # this handler's own `except Exception as exc` clause (Python doesn't
        # let an except block catch its own body's exceptions), so left
        # unguarded it would escape the worker, contradicting this function's
        # documented "Raises: None" contract. Catch and log it here instead;
        # the record is left unparseable/unupdated rather than crashing the
        # worker over a failure to report a failure.
        try:
            with _lock:
                if _paper_trading_session_already_terminal(session_id):
                    logger.warning(
                        "Live paper trade %s: session already terminal when the crash "
                        "handler's write was about to run; leaving it untouched (a "
                        "concurrent writer already finalized it).",
                        session_id,
                    )
                else:
                    raw = _paper_trading_sessions.get(session_id)
                    if raw is not None:
                        session = PaperTradingSession.parse_persisted(raw)
                        session.status = PaperTradingStatus.FAILED
                        session.error = str(exc)
                        session.completed_at = datetime.now(tz=timezone.utc).isoformat()
                        _paper_trading_sessions[session_id] = session
        except Exception:
            logger.exception(
                "Live paper trade %s: failed to persist FAILED status for the "
                "crashed session (record may be unparseable, or the store "
                "rejected the write)",
                session_id,
            )
    finally:
        with _lock:
            _live_paper_stop_controllers.pop(session_id, None)


@app.post("/strategy-lab/paper-trade/{session_id}/stop", response_model=PaperTradingResponse)
def stop_live_paper_trading(session_id: str) -> PaperTradingResponse:
    """Idempotent user-stop for a live paper-trading session.

    Delivers a durable stop signal to the running ``PaperTradingWorkflow`` via
    ``_signal_paper_trading_stop`` (a Temporal signal); the live loop terminates
    when the activity's background heartbeat observes the Temporal cancellation
    and trips the session's ``StopController`` (registered in
    ``_live_paper_stop_controllers``). Returns the session's current state
    (still ``live`` / ``warming_up`` if the worker hasn't yet noticed — clients poll
    ``GET /strategy-lab/paper-trade/{session_id}`` for the final record).
    """
    if not _live_paper_enabled():
        raise HTTPException(
            status_code=404,
            detail="Live paper trading is not enabled (set INVESTMENT_LIVE_PAPER_ENABLED=true).",
        )
    with _lock:
        raw = _paper_trading_sessions.get(session_id)
    if raw is None:
        raise HTTPException(
            status_code=404, detail=f"Paper trading session '{session_id}' not found."
        )
    session = PaperTradingSession.parse_persisted(raw)

    # Idempotent: a terminal session's workflow is already closed, and Temporal
    # rejects signals to closed executions — so only signal an in-flight session
    # and return a terminal one unchanged.
    if session.status not in _ACTIVE_PT_STATES:
        return PaperTradingResponse(
            session=session,
            message="Session already finished; nothing to stop.",
        )

    # Deliver the stop durably: the ``stop`` signal cancels the running
    # ``PaperTradingWorkflow`` activity, which trips the session's StopController
    # so the live loop ends at the next bar (replacing the old in-process poke).
    # A race where the workflow closes between our read and the signal must not
    # 500 the (idempotent) stop route — but only the genuine "already closed"
    # case is swallowed; a real delivery failure (client not connected, RPC
    # timeout, any other RPC error) must surface, not be silently reported as a
    # successful stop on a live-trading kill switch.
    #
    # Imported here (rather than inside the except block below) so an import
    # failure can't mask the real _signal_paper_trading_stop exception being
    # handled — an ImportError raised while already handling exc would
    # discard exc and propagate the ImportError instead.
    from temporalio.service import RPCError, RPCStatusCode

    try:
        _signal_paper_trading_stop(session_id)
    except HTTPException:
        raise
    except Exception as exc:
        if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
            logger.info(
                "Stop signal for paper-trading session %s found no running workflow "
                "(already closed); treating as already-stopped.",
                session_id,
            )
            # Re-read under lock rather than returning the pre-signal ``session``
            # snapshot taken above — the RPC was a real network call, and the
            # session may have been deleted concurrently (e.g. its lab record
            # was deleted) while it was in flight. Returning the stale snapshot
            # would resurrect a session that no longer exists.
            with _lock:
                fresh_raw = _paper_trading_sessions.get(session_id)
                if fresh_raw is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Paper trading session '{session_id}' not found.",
                    ) from exc
                try:
                    fresh_session = PaperTradingSession.parse_persisted(fresh_raw)
                except Exception as parse_exc:
                    logger.warning(
                        "Stop signal for paper-trading session %s: persisted record "
                        "could not be re-parsed after a NOT_FOUND signal response.",
                        session_id,
                        exc_info=True,
                    )
                    raise HTTPException(
                        status_code=404,
                        detail=f"Paper trading session '{session_id}' not found.",
                    ) from parse_exc
            return PaperTradingResponse(
                session=fresh_session,
                message="Session already finished; nothing to stop.",
            )
        logger.exception("Stop signal for paper-trading session %s failed to deliver", session_id)
        raise HTTPException(
            status_code=502,
            detail=(
                f"Failed to deliver the stop signal for session '{session_id}'; "
                "the session may still be running. Retry."
            ),
        ) from exc

    # Merge the stop timestamp onto whatever the CURRENT persisted state is
    # (re-read under lock right before writing) rather than the pre-signal
    # snapshot taken above — the signal call is a real, non-trivial network RPC,
    # and the background worker can independently reach a terminal state (real
    # trades/status=COMPLETED) in that window; overwriting with the stale
    # snapshot would silently discard that result. If the session was deleted
    # entirely in that window (e.g. its lab record was deleted concurrently),
    # do not resurrect it by writing the stale pre-signal snapshot back —
    # report it as gone instead.
    with _lock:
        fresh_raw = _paper_trading_sessions.get(session_id)
        if fresh_raw is None:
            raise HTTPException(
                status_code=404, detail=f"Paper trading session '{session_id}' not found."
            )
        try:
            fresh_session = PaperTradingSession.parse_persisted(fresh_raw)
        except Exception as exc:
            # Concurrent corruption or a serialization issue here must not
            # raise an unhandled exception after the signal has already been
            # sent — apply the same parse guard the missing-record branch
            # above already gets, reporting the session as unavailable
            # rather than 500ing on a stop request that already succeeded.
            logger.warning(
                "Stop signal delivered for paper-trading session %s, but the "
                "persisted record could not be re-parsed afterward.",
                session_id,
                exc_info=True,
            )
            raise HTTPException(
                status_code=404, detail=f"Paper trading session '{session_id}' not found."
            ) from exc
        fresh_session.user_stop_requested_at = datetime.now(tz=timezone.utc).isoformat()
        _paper_trading_sessions[session_id] = fresh_session
    return PaperTradingResponse(
        session=fresh_session,
        message="Stop requested. Poll the session to see the final state.",
    )


class ProviderDescriptor(BaseModel):
    """One row of the ``GET /providers`` response."""

    name: str
    supports: List[str] = Field(default_factory=list)
    is_paid: bool = False
    has_key: bool = False
    implemented: bool = True
    is_default_for: List[str] = Field(default_factory=list)
    historical_timeframes: List[str] = Field(default_factory=list)
    live_timeframes: List[str] = Field(default_factory=list)


class ProvidersListResponse(BaseModel):
    live_paper_enabled: bool
    providers: List[ProviderDescriptor] = Field(default_factory=list)


@app.get("/providers", response_model=ProvidersListResponse)
def list_providers() -> ProvidersListResponse:
    """Enumerate registered market-data providers and their capabilities.

    Postconditions:
        Takes no inputs. Iterates ``registry.describe_all()`` from the
        process-wide ``default_registry()`` singleton
        (``investment_team.trading_service.providers``), constructing one
        ``ProviderDescriptor`` per row. Raises ``HTTPException(500,
        "Provider registry contains invalid data")`` if any row fails
        Pydantic validation (logged via ``logger.exception`` before being
        raised). On success, returns a ``ProvidersListResponse`` with
        ``providers`` set to the constructed rows and
        ``live_paper_enabled`` from ``_live_paper_enabled()``.
    """
    from investment_team.trading_service.providers import default_registry

    registry = default_registry()
    try:
        rows = [ProviderDescriptor(**row) for row in registry.describe_all()]
    except ValidationError as exc:
        logger.exception("Provider registry returned invalid data")
        raise HTTPException(
            status_code=500, detail="Provider registry contains invalid data"
        ) from exc
    return ProvidersListResponse(
        live_paper_enabled=_live_paper_enabled(),
        providers=rows,
    )


def _parse_iso_timestamp_for_sort(value: str) -> datetime:
    """Parse an ISO-8601 timestamp string for use as a sort key.

    Comparing raw ISO-8601 strings lexicographically only matches
    chronological order when every timestamp shares the same timezone
    offset. This repo's own writers always stamp UTC via
    ``datetime.now(tz=timezone.utc).isoformat()``, but persisted or
    hand-constructed records could carry a different offset (or the "Z"
    UTC designator, which ``datetime.fromisoformat`` doesn't accept on
    Python versions before 3.11), so lexicographic order isn't a safe
    assumption to build the sort on.

    Postconditions:
        Returns a timezone-aware ``datetime`` reflecting ``value``'s actual
        instant regardless of its offset notation. An empty or unparseable
        ``value`` returns ``datetime.min`` (UTC) so such records sort last
        under ``reverse=True`` (oldest) rather than raising or corrupting
        their siblings' order. A parsed-but-naive value (no offset in the
        string) is assumed UTC, matching this codebase's sole convention.
    """
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@app.get("/strategy-lab/paper-trade/results", response_model=PaperTradingResultsResponse)
def get_paper_trading_results(
    verdict: Optional[PaperTradingVerdict] = None,
) -> PaperTradingResultsResponse:
    """
    Return all paper trading sessions, sorted newest-first: terminal
    (completed/failed) sessions first, newest-completed to oldest, followed by
    every still-active session, newest-started to oldest. Filter by verdict
    with ?verdict=ready_for_live or ?verdict=not_performant; an unrecognized
    value is rejected with a 422 rather than silently matching nothing. The
    response's ``count``, ``ready_for_live_count``, and ``not_performant_count``
    are derived from the returned ``items`` by ``PaperTradingResultsResponse``
    itself, so they always match the (possibly filtered) list.

    Postconditions:
        A session record that fails ``PaperTradingSession.parse_persisted`` is
        logged and excluded from ``items`` rather than raising — matching the
        recovery pass in ``_recover_orphaned_paper_trading_sessions`` and the
        single-session lookup in ``get_paper_trading_session``. One corrupt
        in-memory record must not 500 this bulk endpoint for every caller.

        Ordering: sorting by ``completed_at or started_at`` in one pass would
        let an in-flight session's ``started_at`` (no ``completed_at`` yet)
        outrank a genuinely more-recent completed session's ``completed_at`` —
        the two timestamps aren't comparable "recency" in the same sense.
        Terminal sessions (status not in ``_ACTIVE_PT_STATES``) are grouped
        first and sorted by ``completed_at`` descending; active sessions are
        grouped after and sorted by ``started_at`` descending. Each group's
        own sort is independent of the other's timestamps. Both sorts key on
        ``_parse_iso_timestamp_for_sort`` rather than comparing the raw
        strings, so a mix of timezone offsets across records can't produce a
        lexicographic order that disagrees with chronological order.
    """
    with _lock:
        raw = list(_paper_trading_sessions.values())

    items: List[PaperTradingSession] = []
    for r in raw:
        try:
            items.append(PaperTradingSession.parse_persisted(r))
        except Exception:
            logger.warning(
                "Paper-trade results: skipping unparseable session record",
                exc_info=True,
            )
    terminal_items = [s for s in items if s.status not in _ACTIVE_PT_STATES]
    active_items = [s for s in items if s.status in _ACTIVE_PT_STATES]
    terminal_items.sort(key=lambda s: _parse_iso_timestamp_for_sort(s.completed_at), reverse=True)
    active_items.sort(key=lambda s: _parse_iso_timestamp_for_sort(s.started_at), reverse=True)
    items = terminal_items + active_items

    if verdict is not None:
        items = [s for s in items if s.verdict == verdict]

    # Counts are derived from ``items`` by the response model itself, so they
    # always match whatever list (filtered or not) is returned here.
    return PaperTradingResultsResponse(items=items)


@app.get("/strategy-lab/paper-trade/{session_id}", response_model=PaperTradingResponse)
def get_paper_trading_session(session_id: str) -> PaperTradingResponse:
    """Return a specific paper trading session by ID.

    Postconditions:
        A session record that fails ``PaperTradingSession.parse_persisted`` is
        logged and reported as a 500, not left to leak the raw parse/
        validation exception as an unhandled 500 with no useful client-facing
        detail.
    """
    with _lock:
        raw = _paper_trading_sessions.get(session_id)

    if raw is None:
        raise HTTPException(
            status_code=404, detail=f"Paper trading session '{session_id}' not found."
        )

    try:
        session = PaperTradingSession.parse_persisted(raw)
    except Exception as exc:
        # The record exists but is internally corrupt (schema drift, a
        # missing required field, malformed JSON, ...) -- a server-side data
        # integrity problem, not a client input error, so this is a 500
        # rather than a 4xx. Log the full exception (which may include raw
        # persisted field values) server-side only; the client-facing detail
        # stays generic so it can't leak internal schema/validation details.
        logger.error(
            "Paper trading session %s failed to parse: %s",
            session_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Paper trading session '{session_id}' is corrupted and cannot be loaded.",
        ) from exc
    return PaperTradingResponse(session=session)


def _recover_orphaned_paper_trading_sessions() -> None:
    """Mark sessions left in an active status by a previous process as ``failed``.

    Active statuses covered: ``RUNNING``, ``OPENING``, ``WARMING_UP``, or ``LIVE``.

    The paper-trade worker runs in a non-daemon thread so graceful shutdowns wait
    for it, but SIGKILL/crashes can still orphan a session. Without this recovery
    pass, such sessions would sit in an active status forever and clients would
    poll indefinitely with no terminal transition.

    Called from ``_startup()`` (``create_team_app``'s ``on_startup`` hook) rather
    than decorated with ``@app.on_event("startup")`` — a custom ``lifespan=`` (set
    by ``create_team_app``) replaces FastAPI's default lifespan context that
    ``on_event`` handlers rely on, so an ``on_event``-registered handler here would
    silently never run.
    """
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    recovered = 0
    try:
        # Held for the whole enumerate/parse/mutate/write pass so a concurrent
        # writer can't have its update clobbered between the read and the
        # write-back. Safe only because this handler runs once at startup,
        # before request handling begins.
        with _lock:
            raw_sessions = list(_paper_trading_sessions.values())
            for raw in raw_sessions:
                try:
                    session = PaperTradingSession.parse_persisted(raw)
                except Exception:
                    # ``warning``, not ``debug`` — debug logs are typically
                    # disabled in production, which would let corrupted
                    # records go unnoticed indefinitely. Best-effort session
                    # id: ``raw`` failed to parse as a PaperTradingSession, so
                    # it's whatever malformed shape the store handed back
                    # (usually still a dict with a readable id, but not
                    # guaranteed).
                    raw_session_id = raw.get("session_id") if isinstance(raw, dict) else None
                    logger.warning(
                        "Paper-trade recovery: skipping unparseable session record (session_id=%s)",
                        raw_session_id or "unknown",
                        exc_info=True,
                    )
                    continue
                if session.status not in _ACTIVE_PT_STATES:
                    continue
                try:
                    _apply_paper_trading_failure(
                        session,
                        (
                            "Paper trading did not complete — the worker process exited "
                            "before finalizing the session. Re-run the paper trade from "
                            "the Strategy Lab."
                        ),
                        completed_at=now_iso,
                        terminated_reason="process_exit",
                        set_legacy_divergence_analysis=True,
                    )
                    _paper_trading_sessions[session.session_id] = session
                    recovered += 1
                except Exception:
                    logger.exception(
                        "Paper-trade recovery: failed to persist failed status for %s",
                        session.session_id,
                    )
    except Exception:
        # ``exception`` (ERROR level + traceback), not ``debug`` — this is the
        # catch-all around the whole enumerate/parse/mutate/write pass, so it
        # also covers non-recoverable infrastructure failures (e.g. the
        # persisted-session store itself being unreachable/misconfigured),
        # not just a single malformed record. Debug logs are typically
        # disabled in production, which would leave orphaned sessions
        # unrecovered with no operator-visible signal.
        logger.exception("Paper-trade recovery: could not enumerate sessions")
        return

    if recovered:
        logger.info(
            "Paper-trade recovery: marked %d orphaned active session(s) as failed",
            recovered,
        )


# ---------------------------------------------------------------------------
# Financial Advisor — conversational profile builder
# ---------------------------------------------------------------------------


class StartAdvisorSessionRequest(BaseModel):
    user_id: str = Field(..., min_length=1, description="Unique user identifier")


class StartAdvisorSessionResponse(BaseModel):
    session_id: str
    advisor_message: str
    session: AdvisorSession


class SendAdvisorMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User's message to the advisor")


class SendAdvisorMessageResponse(BaseModel):
    advisor_message: str
    session_status: str
    current_topic: str
    missing_fields: List[str] = Field(default_factory=list)


class GetAdvisorSessionResponse(BaseModel):
    session: Optional[AdvisorSession] = None
    found: bool = True


class CompleteAdvisorSessionResponse(BaseModel):
    user_id: str
    ips: IPS
    message: str = "Investment Policy Statement created from advisor session."


@app.post("/advisor/sessions", response_model=StartAdvisorSessionResponse)
def start_advisor_session(request: StartAdvisorSessionRequest) -> StartAdvisorSessionResponse:
    """Start a new financial advisor conversation (runs as a Temporal workflow).

    Preconditions:
        - ``request.user_id`` identifies the user starting the session.

    Postconditions:
        - Returns the new session id, the advisor's opening message, and the
          created ``AdvisorSession``.

    Raises:
        - ``HTTPException(502)`` if the advisory workflow returns a non-dict
          result, a dict missing ``advisor_message`` or ``session``, or whose
          ``advisor_message``/``session`` values are not a str/dict respectively.
        - ``HTTPException(500)`` if ``session`` fails ``AdvisorSession``
          validation.
    """
    session_id = f"adv-{uuid.uuid4().hex}"
    result = _execute_advisory(
        "advisor_start",
        {"session_id": session_id, "user_id": request.user_id},
        key=session_id,
    )
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("advisor_message"), str)
        or not isinstance(result.get("session"), dict)
    ):
        raise HTTPException(
            status_code=502,
            detail="Advisor execution returned unexpected response structure",
        )
    try:
        session = AdvisorSession.model_validate(result["session"])
    except ValidationError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Advisor execution returned unexpected response structure: {exc}",
        ) from exc
    return StartAdvisorSessionResponse(
        session_id=session_id,
        advisor_message=result["advisor_message"],
        session=session,
    )


@app.post("/advisor/sessions/{session_id}/messages", response_model=SendAdvisorMessageResponse)
def send_advisor_message(
    session_id: str, request: SendAdvisorMessageRequest
) -> SendAdvisorMessageResponse:
    """Send a message to the financial advisor and receive a response.

    Preconditions:
        - ``session_id`` identifies a previously started advisor session.

    Postconditions:
        - Returns the advisor's reply along with the session's updated status,
          current topic, and any still-missing required fields.

    Raises:
        - ``HTTPException(404)`` if ``session_id`` does not match a known session
          — either when this route is first called, or if the session is
          concurrently removed between the initial check and workflow dispatch.
        - ``HTTPException(502)`` if the advisory workflow returns a result
          missing ``advisor_message``, ``session_status``, ``current_topic``,
          or ``missing_fields``, or whose ``advisor_message``/
          ``session_status``/``current_topic`` values are not a str or whose
          ``missing_fields`` value is not a list.
    """
    with _lock:
        session = _advisor_sessions.get(session_id)

    if not session:
        raise HTTPException(status_code=404, detail=f"Advisor session {session_id} not found")

    # Re-check immediately before dispatch: the lock above is released for
    # this synchronous, potentially slow _execute_advisory call (holding a
    # single process-wide lock across that I/O would serialize every route
    # in this module), which leaves a window for another request to remove
    # the session between the check above and here. Narrow it as tightly as
    # possible rather than dispatching against a session that may already be
    # gone.
    with _lock:
        if session_id not in _advisor_sessions:
            raise HTTPException(status_code=404, detail=f"Advisor session {session_id} not found")

    result = _execute_advisory(
        "advisor_message",
        {"session_id": session_id, "message": request.message},
        key=session_id,
    )
    required_keys = ("advisor_message", "session_status", "current_topic", "missing_fields")
    if (
        not isinstance(result, dict)
        or any(key not in result for key in required_keys)
        or not isinstance(result["advisor_message"], str)
        or not isinstance(result["session_status"], str)
        or not isinstance(result["current_topic"], str)
        or not isinstance(result["missing_fields"], list)
    ):
        raise HTTPException(
            status_code=502,
            detail="Advisor execution returned unexpected response structure",
        )
    return SendAdvisorMessageResponse(
        advisor_message=result["advisor_message"],
        session_status=result["session_status"],
        current_topic=result["current_topic"],
        missing_fields=result["missing_fields"],
    )


@app.get("/advisor/sessions/{session_id}", response_model=GetAdvisorSessionResponse)
def get_advisor_session(session_id: str) -> GetAdvisorSessionResponse:
    """Get the current state of an advisor session.

    Preconditions:
        - None. Unknown session IDs are tolerated.

    Postconditions:
        - Returns the session with `found=True` if `session_id` matches a
          known session.
        - Returns `found=False` with `session=None` if no session matches
          (a missing session is not an error).

    Raises:
        - None (intentionally avoids standard 404 errors for missing sessions).
    """
    with _lock:
        session = _advisor_sessions.get(session_id)

    if not session:
        return GetAdvisorSessionResponse(session=None, found=False)
    return GetAdvisorSessionResponse(session=session, found=True)


@app.post("/advisor/sessions/{session_id}/complete", response_model=CompleteAdvisorSessionResponse)
def complete_advisor_session(session_id: str) -> CompleteAdvisorSessionResponse:
    """Finalize the advisor session and create an IPS from collected data.

    Can be called at any point once all required fields have been collected
    from the session — the endpoint does not itself check ``session.status``.

    Preconditions:
        - ``session_id`` identifies a previously started advisor session.

    Postconditions:
        - Returns the user id and the ``IPS`` created from the session's
          collected data.

    Raises:
        - ``HTTPException(404)`` if ``session_id`` does not match a known session.
        - ``HTTPException(400)`` if required fields are still missing.
        - ``HTTPException(502)`` if the advisory workflow returns a result
          missing ``user_id`` or ``ips``, or whose ``user_id``/``ips`` values
          are not a str/dict respectively.
        - ``HTTPException(500)`` if ``ips`` fails ``IPS`` validation.
    """
    with _lock:
        raw_session = _advisor_sessions.get(session_id)

    if not raw_session:
        raise HTTPException(status_code=404, detail=f"Advisor session {session_id} not found")

    session = (
        raw_session
        if isinstance(raw_session, AdvisorSession)
        else AdvisorSession.model_validate(raw_session)
    )
    missing = _get_advisor_agent().missing_fields(session.collected)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot finalize — missing required fields: {', '.join(missing)}",
        )

    result = _execute_advisory("advisor_complete", {"session_id": session_id}, key=session_id)
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("user_id"), str)
        or not isinstance(result.get("ips"), dict)
    ):
        raise HTTPException(
            status_code=502,
            detail="Advisor completion returned unexpected response structure",
        )
    try:
        ips = IPS.model_validate(result["ips"])
    except ValidationError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Advisor completion returned unexpected response structure: {exc}",
        ) from exc
    return CompleteAdvisorSessionResponse(user_id=result["user_id"], ips=ips)
