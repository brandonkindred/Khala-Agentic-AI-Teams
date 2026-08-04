"""FastAPI endpoints for the Investment Team."""

from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from investment_team.agents import (
    FinancialAdvisorAgent,
    InvestmentCommitteeAgent,
    PolicyGuardianAgent,
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
    update_job as _bt_update_job,
)
from investment_team.signal_intelligence_agent import SignalIntelligenceExpert
from investment_team.signal_intelligence_models import SignalIntelligenceBriefV1
from investment_team.strategy_lab.config import (
    MAX_BATCH_COUNT as _MAX_BATCH_COUNT,
)
from investment_team.strategy_lab.config import (
    MAX_PARALLEL as _MAX_PARALLEL,
)
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.run_state import (
    acquire_run_transition_lock as _acquire_run_transition_lock,
)
from investment_team.strategy_lab.run_state import (
    active_runs as _active_runs,
)
from investment_team.strategy_lab.run_state import (
    get_lab_run_job_client as _get_lab_run_job_client,
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
)
from job_service_client import RESTARTABLE_STATUSES, RESUMABLE_STATUSES, validate_job_for_action
from shared.app import create_team_app
from shared.concurrency import parallel_map

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _startup() -> None:
    """Start the Temporal worker backstop (best-effort).

    The team_service entrypoint normally starts the worker via
    ``TEAM_TEMPORAL_WORKER_MODULE`` before uvicorn accepts requests; this
    backstop covers running the app standalone (``uvicorn ...:app``) and a
    wrapper start that silently failed.

    Preconditions:
        - None (safe to call once at app startup; idempotent per team).

    Postconditions:
        - Starts the worker thread when Temporal is enabled; a no-op when
          ``TEMPORAL_ADDRESS`` is unset. Never raises — failures are logged
          so they cannot abort app boot (this runs as an ``on_startup`` hook).
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


def _run_investment_service_shutdown() -> (
    None
):  # pragma: no cover - process-lifecycle shutdown hook driven by uvicorn; the meaningful exercise needs a live server. The body is a defensive try/except around the event-bus reaper teardown.
    """Stop the per-job event-bus reaper thread before process exit.

    Postconditions:
        - The event-bus reaper thread is stopped (idempotent; a missing or
          already-stopped reaper is a no-op). Never raises — teardown failures are
          logged and swallowed so they cannot abort process shutdown.
    """
    try:
        from investment_team.api.job_event_bus import shutdown as _shutdown_event_bus

        _shutdown_event_bus()
    except Exception:
        logger.debug("Investment event-bus reaper shutdown skipped", exc_info=True)


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
class _PersistentDict:
    """Dict-like wrapper around JobServiceClient for restart-safe entity storage.

    Usage:
        store = _PersistentDict('profiles')
        store['key'] = some_model_instance  # persists via JobServiceClient
        value = store.get('key', default)   # returns stored data dict, not the original object

    Invariants:
        - Keys are strings.
        - Values with ``model_dump`` are persisted via ``model_dump(mode="json")``;
          other values are wrapped as ``{"value": value}`` before persistence.
        - Reads (``__getitem__``, ``get``, ``pop``, ``values``) return the
          persisted data dict, not a reconstructed model instance.
        - Storage is namespaced under JobServiceClient team
          ``investment_{entity_type}``.
    """

    def __init__(self, entity_type: str) -> None:
        """Bind a JobServiceClient namespaced to this entity store.

        Preconditions:
            - ``entity_type`` is a ``str`` used as the store namespace suffix.
        Postconditions:
            - ``self._client`` targets team ``investment_{entity_type}``.
            - ``self._entity_type`` equals ``entity_type``.
        """
        from job_service_client import JobServiceClient

        self._client = JobServiceClient(team=f"investment_{entity_type}")
        self._entity_type = entity_type

    def __setitem__(self, key: str, value: Any) -> None:
        """Persist ``value`` under ``key`` (create or update).

        Preconditions:
            - ``key`` is a ``str``.
        Postconditions:
            - Values with ``model_dump`` are stored via ``model_dump(mode="json")``;
              other values are stored as ``{"value": value}``.
            - An existing job for ``key`` is updated; otherwise a new job is
              created with status ``"stored"``.
        """
        data = value.model_dump(mode="json") if hasattr(value, "model_dump") else {"value": value}
        existing = self._client.get_job(key)
        if existing:
            self._client.update_job(key, data=data)
        else:
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

    def pop(self, key: str, *args: Any) -> Any:
        """Remove ``key`` and return its persisted data dict.

        Preconditions:
            - ``key`` is a ``str``.
            - If the job is missing and ``args`` is empty, raises ``KeyError``.
        Postconditions:
            - When present: deletes the job and returns its ``data`` payload
              (or the job mapping if ``data`` is absent).
            - When missing and a default is provided in ``args``: returns that
              default without deleting.
        """
        job = self._client.get_job(key)
        if job is None:
            if args:
                return args[0]
            raise KeyError(key)
        self._client.delete_job(key)
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


_advisor_agent = FinancialAdvisorAgent()
_policy_guardian = PolicyGuardianAgent()
_orchestrator = InvestmentTeamOrchestrator()
_committee_agent = InvestmentCommitteeAgent()


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Strategy Lab run tracking models
# ---------------------------------------------------------------------------

# All terminal statuses a strategy lab run can land in. Kept local to this
# module because "completed_with_errors" is a lab-specific concept and the
# shared job_service_client constants don't know about it. Used by the SSE
# stream short-circuit, status reconciliation, and restart gating so a
# freshly-introduced terminal state can't silently diverge.
STRATEGY_LAB_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "completed_with_errors", "failed", "cancelled", "interrupted"}
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


def _run_state_to_response(state: Dict[str, Any]) -> StrategyLabRunStatusResponse:
    """Convert an ``_active_runs`` entry to a Pydantic response.

    Preconditions:
        ``state`` is an ``_active_runs`` entry (or a persisted job dict of the
        same shape); ``state["run_id"]`` is present. Every other field —
        including ``status``, ``started_at``, and ``total_cycles`` — is read
        with a default, so a partially-populated merged/resume/snapshot dict
        (e.g. a job-service entry that only guarantees ``run_id``/``status``)
        is safe.
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
        Pure: ``state`` is not mutated.
    """
    cc = state.get("current_cycle")
    return StrategyLabRunStatusResponse(
        run_id=state["run_id"],
        status=state.get("status", "unknown"),
        started_at=state.get("started_at", ""),
        total_cycles=state.get("total_cycles", 0),
        completed_cycles=state.get("completed_cycles", 0),
        skipped_cycles=state.get("skipped_cycles", 0),
        errored_cycles=state.get("errored_cycles", 0),
        errored_details=state.get("errored_details", []),
        tracker_merge_error_count=state.get("tracker_merge_error_count", 0),
        current_cycle=StrategyLabCycleProgress(**cc) if cc else None,
        completed_record_ids=state.get("completed_record_ids", []),
        error=state.get("error"),
        batch_size=state.get("batch_size", state.get("total_cycles", 1)),
        batch_count=state.get("batch_count", 1),
        completed_batches=state.get("completed_batches", 0),
        current_batch=state.get("current_batch"),
    )


class CreateProfileRequest(BaseModel):
    user_id: str = Field(..., description="Unique user identifier")
    risk_tolerance: str = Field(..., description="low, medium, high, or very_high")
    max_drawdown_tolerance_pct: float = Field(..., ge=0, le=100)
    time_horizon_years: int = Field(..., ge=1)
    annual_gross_income: float = Field(..., ge=0)
    income_stability: str = Field(default="stable")
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
    esg_preference: str = Field(default="none")
    crypto_allowed: bool = Field(default=True)
    options_allowed: bool = Field(default=True)
    leverage_allowed: bool = Field(default=False)
    goals: List[Dict[str, Any]] = Field(default_factory=list)
    max_single_position_pct: float = Field(default=10.0)
    max_asset_class_pct: Dict[str, float] = Field(default_factory=dict)
    live_trading_enabled: bool = Field(default=False)
    human_approval_required_for_live: bool = Field(default=True)
    speculative_sleeve_cap_pct: float = Field(default=10.0)
    rebalance_frequency: str = Field(default="quarterly")
    default_mode: str = Field(default="monitor_only")
    notes: List[str] = Field(default_factory=list)


class CreateProfileResponse(BaseModel):
    user_id: str
    ips: IPS
    message: str = "Investment Policy Statement created successfully."


class GetProfileResponse(BaseModel):
    user_id: str
    ips: Optional[IPS] = None
    found: bool = True


class CreateProposalRequest(BaseModel):
    prepared_by: str = Field(..., description="Agent or user ID who prepared this proposal")
    user_id: str = Field(..., description="User ID whose IPS this is for")
    objective: str = Field(..., description="Investment objective")
    positions: List[Dict[str, Any]] = Field(..., description="List of portfolio positions")
    expected_return_pct: Optional[float] = None
    expected_volatility_pct: Optional[float] = None
    expected_max_drawdown_pct: Optional[float] = None
    assumptions: List[str] = Field(default_factory=list)


class CreateProposalResponse(BaseModel):
    proposal_id: str
    proposal: PortfolioProposal
    message: str = "Portfolio proposal created successfully."


class GetProposalResponse(BaseModel):
    proposal_id: str
    proposal: Optional[PortfolioProposal] = None
    found: bool = True


class ValidateProposalRequest(BaseModel):
    user_id: str = Field(..., description="User ID to get IPS for validation")


class ValidateProposalResponse(BaseModel):
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
    strategy_id: str
    strategy: StrategySpec
    message: str = "Strategy created successfully."


class ValidateStrategyRequest(BaseModel):
    backtest_period: str = Field(default="2020-01-01 to 2024-12-31")
    scenario_set: List[str] = Field(default_factory=lambda: ["baseline", "stress", "monte_carlo"])
    checks: List[Dict[str, Any]] = Field(default_factory=list)


class ValidateStrategyResponse(BaseModel):
    strategy_id: str
    validation: ValidationReport
    passed: bool
    failures: List[str] = Field(default_factory=list)


class RunBacktestRequest(BaseModel):
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
    backtest: BacktestRecord
    message: str = "Backtest completed and recorded successfully."


class ListBacktestsResponse(BaseModel):
    items: List[BacktestRecord] = Field(default_factory=list)
    count: int = 0


class PromotionDecisionRequest(BaseModel):
    strategy_id: str = Field(..., description="Strategy ID to promote")
    user_id: str = Field(..., description="User ID for IPS lookup")
    proposer_agent_id: str = Field(..., description="ID of agent who proposed the strategy")
    approver_agent_id: str = Field(..., description="ID of independent approver agent")
    approver_role: str = Field(default="approver")
    approver_version: str = Field(default="1.0")
    risk_veto: bool = Field(default=False)
    human_live_approval: bool = Field(default=False)


class PromotionDecisionResponse(BaseModel):
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
    queue: str
    payload_id: str
    priority: str = "normal"


class QueuesResponse(BaseModel):
    queues: Dict[str, List[QueueItemResponse]] = Field(default_factory=dict)


class CreateMemoRequest(BaseModel):
    user_id: str
    recommendation: str
    rationale: str
    dissenting_views: List[str] = Field(default_factory=list)


class CreateMemoResponse(BaseModel):
    memo: InvestmentCommitteeMemo


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok", "timestamp": _now()}


@app.post("/profiles", response_model=CreateProfileResponse)
def create_profile(request: CreateProfileRequest) -> CreateProfileResponse:
    """Create an Investment Policy Statement (IPS) for a user."""
    try:
        risk_tol = RiskTolerance(request.risk_tolerance)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid risk_tolerance: {request.risk_tolerance}. Must be one of: low, medium, high, very_high",
        )

    try:
        workflow_mode = WorkflowMode(request.default_mode)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid default_mode: {request.default_mode}. Must be one of: advisory, paper, live, monitor_only",
        )

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
        savings_rate=SavingsRate(monthly=request.monthly_savings, annual=request.annual_savings),
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

    with _lock:
        _profiles[request.user_id] = ips

    return CreateProfileResponse(user_id=request.user_id, ips=ips)


@app.get("/profiles/{user_id}", response_model=GetProfileResponse)
def get_profile(user_id: str) -> GetProfileResponse:
    """Get the Investment Policy Statement for a user."""
    with _lock:
        ips = _profiles.get(user_id)
    if not ips:
        return GetProfileResponse(user_id=user_id, ips=None, found=False)
    return GetProfileResponse(user_id=user_id, ips=ips, found=True)


@app.post("/proposals/create", response_model=CreateProposalResponse)
def create_proposal(request: CreateProposalRequest) -> CreateProposalResponse:
    """Create a new portfolio proposal (runs as a Temporal workflow)."""
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
    return CreateProposalResponse(
        proposal_id=proposal_id, proposal=PortfolioProposal.model_validate(result["proposal"])
    )


@app.get("/proposals/{proposal_id}", response_model=GetProposalResponse)
def get_proposal(proposal_id: str) -> GetProposalResponse:
    """Get a portfolio proposal by ID."""
    with _lock:
        proposal = _proposals.get(proposal_id)
    if not proposal:
        return GetProposalResponse(proposal_id=proposal_id, proposal=None, found=False)
    return GetProposalResponse(proposal_id=proposal_id, proposal=proposal, found=True)


@app.post("/proposals/{proposal_id}/validate", response_model=ValidateProposalResponse)
def validate_proposal(
    proposal_id: str, request: ValidateProposalRequest
) -> ValidateProposalResponse:
    """Validate a portfolio proposal against the user's IPS."""
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
    return ValidateProposalResponse(
        proposal_id=proposal_id,
        valid=result["valid"],
        violations=result["violations"],
    )


@app.post("/strategies", response_model=CreateStrategyResponse)
def create_strategy(request: CreateStrategyRequest) -> CreateStrategyResponse:
    """Create a new investment strategy specification."""
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
    """Run validation checks on a strategy."""
    with _lock:
        strategy = _strategies.get(strategy_id)

    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

    result = _execute_advisory(
        "validate_strategy",
        {"strategy_id": strategy_id, "request": request.model_dump(mode="json")},
        key=strategy_id,
    )
    return ValidateStrategyResponse(
        strategy_id=strategy_id,
        validation=ValidationReport.model_validate(result["validation"]),
        passed=result["passed"],
        failures=result["failures"],
    )


class BacktestJobSubmission(BaseModel):
    job_id: str
    status: str = _BT_JOB_STATUS_PENDING


class BacktestJobStatus(BaseModel):
    job_id: str
    status: str
    strategy_id: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BacktestJobListItem(BaseModel):
    job_id: str
    status: str
    strategy_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BacktestJobListResponse(BaseModel):
    jobs: List[BacktestJobListItem]


def _run_backtest_background(
    job_id: str,
    strategy: StrategySpec,
    config: BacktestConfig,
    submitted_by: str,
    notes: List[str],
) -> None:
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
          serialized ``RunBacktestResponse``; a new ``BacktestRecord`` is stored
          under ``_backtests[backtest_id]``
        - On ``HTTPException`` or other exceptions: job status becomes FAILED with
          an error string, unless a cancel check already returned
        - If ``_bt_is_job_cancelled(job_id)`` is true at a check point, return
          without writing COMPLETED or FAILED so the cancelled status visible at
          that check is preserved. Updates use unconditional ``_bt_update_job``,
          so a cancel that lands between a check and the next update can still be
          overwritten with RUNNING, COMPLETED, or FAILED.
    """
    try:
        if _bt_is_job_cancelled(job_id):
            return
        _bt_update_job(job_id, status=_BT_JOB_STATUS_RUNNING)
        result, trades = _run_real_data_backtest(strategy, config)
        if _bt_is_job_cancelled(job_id):
            return
        backtest_id = f"bt-{uuid.uuid4().hex[:8]}"
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
        _bt_update_job(
            job_id,
            status=_BT_JOB_STATUS_COMPLETED,
            result=RunBacktestResponse(backtest=record).model_dump(mode="json"),
            backtest_id=backtest_id,
        )
    except HTTPException as exc:
        if _bt_is_job_cancelled(job_id):
            return
        _bt_update_job(job_id, status=_BT_JOB_STATUS_FAILED, error=str(exc.detail))
    except Exception as exc:
        logger.exception("Backtest job %s failed", job_id)
        if _bt_is_job_cancelled(job_id):
            return
        _bt_update_job(job_id, status=_BT_JOB_STATUS_FAILED, error=str(exc))


@app.post("/backtests", response_model=BacktestJobSubmission)
def run_backtest(request: RunBacktestRequest) -> BacktestJobSubmission:
    """Submit a backtest job against real historical market data.

    Returns `{job_id, status}` immediately; poll
    `GET /backtests/status/{job_id}` for the completed ``RunBacktestResponse``
    in the ``result`` field. Strategies with generated ``strategy_code`` run
    in a sandbox (the normal Strategy Lab path); strategies without
    ``strategy_code`` are rejected with a 422.
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


@app.post("/backtests/jobs/{job_id}/cancel")
def cancel_backtest_job(job_id: str) -> Dict[str, Any]:
    data = _bt_get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if _bt_cancel_job(job_id):
        return {"job_id": job_id, "status": _BT_JOB_STATUS_CANCELLED, "success": True}
    return {
        "job_id": job_id,
        "status": data.get("status"),
        "success": False,
        "message": f"Cannot cancel job in status {data.get('status')}",
    }


@app.delete("/backtests/jobs/{job_id}")
def delete_backtest_job(job_id: str) -> Dict[str, Any]:
    if _bt_get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _bt_delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "deleted": True}


@app.get("/backtests", response_model=ListBacktestsResponse)
def list_backtests(strategy_id: Optional[str] = None) -> ListBacktestsResponse:
    """List recorded backtests, optionally filtered by strategy ID."""
    with _lock:
        raw = list(_backtests.values())

    items = [BacktestRecord.parse_persisted(r) for r in raw]

    if strategy_id:
        items = [item for item in items if item.strategy_id == strategy_id]

    items.sort(key=lambda item: item.completed_at, reverse=True)
    return ListBacktestsResponse(items=items, count=len(items))


@app.post("/promotions/decide", response_model=PromotionDecisionResponse)
def promotion_decision(request: PromotionDecisionRequest) -> PromotionDecisionResponse:
    """Run promotion gate decision for a strategy.

    Postconditions:
        The decision is computed by ``promotion_decision_activity``, which may
        run in a different Temporal worker process. This route — always the API
        process that also serves ``/workflow/status``/``/workflow/queues`` —
        applies the activity's returned audit-log/escalation delta to the local
        ``_workflow_state`` so those reads stay consistent regardless of which
        process ran the activity.
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
    with _lock:
        _workflow_state.audit_log.extend(result.get("audit_log_appended") or [])
        escalation = result.get("escalation_enqueued")
        if escalation:
            _workflow_state.queues[escalation["queue"]].append(
                QueueItem(
                    queue=escalation["queue"],
                    payload_id=escalation["payload_id"],
                    priority=escalation["priority"],
                )
            )
    return PromotionDecisionResponse(
        strategy_id=request.strategy_id,
        decision=PromotionDecision.model_validate(result["decision"]),
    )


@app.get("/workflow/status", response_model=WorkflowStatusResponse)
def workflow_status() -> WorkflowStatusResponse:
    """Get the current workflow state."""
    with _lock:
        mode = _workflow_state.mode.value
        audit_log = list(_workflow_state.audit_log)
        queue_counts = {q: len(items) for q, items in _workflow_state.queues.items()}

    return WorkflowStatusResponse(mode=mode, audit_log=audit_log, queue_counts=queue_counts)


@app.get("/workflow/queues", response_model=QueuesResponse)
def workflow_queues() -> QueuesResponse:
    """Get the contents of all workflow queues."""
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
    """Generate an investment committee memo (runs as a Temporal workflow)."""
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
    return CreateMemoResponse(memo=InvestmentCommitteeMemo.model_validate(result["memo"]))


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
    ``strategy_code`` now return 422.

    Returns (BacktestResult, trade_ledger).
    """
    # Lazy import: yfinance is slow to import; defer until a request arrives.
    from investment_team.market_data_service import MarketDataService

    if not strategy.strategy_code:
        raise HTTPException(
            status_code=422,
            detail=(
                "strategy_code is required. The legacy LLM-per-bar backtest "
                "path has been removed; regenerate the strategy via the "
                "Strategy Lab ideation agent."
            ),
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
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch historical market data. Please check the date range and try again.",
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
        raise HTTPException(
            status_code=422,
            detail=(
                f"Strategy code attempted to access look-ahead data: {(service_result.error or '')}"
            ),
        )
    if service_result.error:
        # Any service-level error must fail the request — mid-run crashes
        # append closed trades *before* raising, so a non-empty ledger here
        # still represents a partial/failed execution and must not be
        # reported as a successful backtest.
        raise HTTPException(
            status_code=422,
            detail=f"Strategy code execution failed: {service_result.error}",
        )

    logger.info(
        "Backtest complete for %s: %d trades",
        strategy.strategy_id,
        len(run.trades),
    )
    return run.result, run.trades


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
    """
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
        default=365,
        ge=30,
        description="Days of recent market data to fetch for paper trading.",
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
                "stocks, crypto, forex, futures, commodities"
            )
        return normalized


class StrategyLabRunResponse(BaseModel):
    records: List[StrategyLabRecord] = Field(default_factory=list)
    count: int = 0
    message: str = "Strategy ideated, backtested, and analysed successfully."


class StrategyLabResultsResponse(BaseModel):
    items: List[StrategyLabRecord] = Field(default_factory=list)
    count: int = 0
    winning_count: int = 0
    losing_count: int = 0


def _normalize_strategy_lab_asset_class(raw: object) -> str:
    """Map LLM output to canonical labels used by the simulated ledger."""
    from investment_team.strategy_lab_context import normalize_asset_class

    return normalize_asset_class(raw)


def _build_strategy_from_ideation(strategy_data: Dict[str, Any]) -> tuple[StrategySpec, str]:
    """Build a StrategySpec + strategy_id from raw ideation output."""
    strategy_id = f"strat-lab-{uuid.uuid4().hex[:8]}"
    strategy = StrategySpec(
        strategy_id=strategy_id,
        authored_by="strategy_ideation_agent",
        asset_class=_normalize_strategy_lab_asset_class(strategy_data.get("asset_class")),
        hypothesis=str(strategy_data.get("hypothesis", "")),
        signal_definition=str(strategy_data.get("signal_definition", "")),
        # Issue #537: ideation must declare a timeframe. Default to "1d"
        # only when the LLM forgot the field — the prompt makes it
        # mandatory; this fallback keeps the cycle alive rather than
        # forcing a re-run for a clearly-resolvable omission.
        timeframe=strategy_data.get("timeframe") or "1d",
        # Issue #551/#554: pass structured rule payloads through to
        # Pydantic; non-dict / non-DSL items are discarded so a malformed
        # ideation LLM response doesn't crash the cycle.
        entry_rules=[r for r in (strategy_data.get("entry_rules") or []) if isinstance(r, dict)],
        exit_rules=[r for r in (strategy_data.get("exit_rules") or []) if isinstance(r, dict)],
        sizing=strategy_data.get("sizing")
        if isinstance(strategy_data.get("sizing"), dict)
        else DEFAULT_SIZING_PAYLOAD,
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
    internally, including up to 10 refinement rounds.

    After the orchestrator returns a complete ``StrategyLabRecord``, the paper-trading
    step runs only when the record is flagged as publishable
    (``record.is_publishable``). Losing strategies record
    ``paper_trading_status = "skipped"`` with reason ``"not_winning"``; winning
    but non-publishable strategies skip with the joined gate codes from
    ``publishability_skip_reason``. Paper-trading failures are non-fatal: the
    cycle still persists the record with ``paper_trading_status = "failed"``
    and the error message.

    Args:
        prior_records: Precomputed prior-record snapshot, supplied by the wave
            driver so the whole table isn't re-read + re-parsed per concurrent
            cycle. Precondition: it must reflect pre-wave state (the caller reads
            it once before launching the wave). When None, this cycle reads and
            parses the snapshot itself (the path direct/test callers take).
        paper_trading_enabled: Opt-out flag; when False, every winning strategy
            records ``paper_trading_status = "skipped"`` with reason ``"disabled"``.
        paper_trading_lookback_days: Forwarded to ``MarketDataService.fetch_multi_symbol``
            when the paper-trading step runs.
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
    """
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
        never affects the returned record.
    """

    def _emit(phase: str, data: Optional[Dict[str, Any]] = None) -> None:
        if on_phase:
            on_phase(phase, data or {})

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
            logger.warning("Paper trading step failed (non-fatal): %s", exc)
            record.paper_trading_status = "failed"
            record.paper_trading_error = str(exc)[:500]
            _emit("paper_trading_failed", {"detail": record.paper_trading_error})

    _persist_strategy_lab_record(record)

    return record


def _strategy_lab_signal_expert_enabled() -> bool:
    return os.environ.get("STRATEGY_LAB_SIGNAL_EXPERT_ENABLED", "true").lower() in (
        "true",
        "1",
        "yes",
    )


def _compute_signal_brief_snapshot(
    benchmark_symbol: str,
) -> tuple[Optional[SignalIntelligenceBriefV1], Optional[Dict[str, Any]]]:
    """Build a per-batch signal brief over all currently-persisted prior records.

    Used by the Temporal ``compute_signal_brief_activity``. Called at the start
    of every batch so batch N+1 sees results from batches 1..N (and prior runs).

    Preconditions:
        ``benchmark_symbol`` is the run's benchmark ticker.
    Postconditions:
        Returns ``(brief, storage)``. Fail-open: on disabled expert / market-fetch
        failure / expert failure it returns ``(None, {"skipped": True, ...})`` (or a
        degraded-market brief) rather than raising.
    """
    if not _strategy_lab_signal_expert_enabled():
        return None, {"skipped": True, "skipped_reason": "signal_expert_disabled"}

    provider = FreeTierMarketDataProvider()
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

        expert = SignalIntelligenceExpert()
        t0 = datetime.now(tz=timezone.utc)
        try:
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
                "signal_intelligence brief_version=%s len=%s degraded_market=%s",
                storage.get("brief_version"),
                len(str(storage)),
                market_ctx.degraded,
            )
            return brief, storage
        except Exception as exc:
            logger.warning("Signal intelligence expert failed: %s", exc)
            return None, {
                "skipped": True,
                "skipped_reason": "expert_failed",
                "error": str(exc),
            }
    finally:
        provider.close()


# Narrower than ``STRATEGY_LAB_TERMINAL_STATUSES`` (defined above): a run that
# reached ``completed``/``completed_with_errors`` is NOT an external cancellation,
# so those are deliberately excluded from the cancel check.
_STRATEGY_LAB_CANCEL_STATUSES = frozenset({"cancelled", "failed", "interrupted"})


def _strategy_lab_external_terminal_status(run_id: str) -> Optional[str]:
    """Return the run's persisted job-store status if it's an external stop signal.

    Preconditions:
        ``run_id`` is the strategy-lab run identifier.
    Postconditions:
        Returns the persisted job's exact ``status`` string ("cancelled",
        "failed", or "interrupted") when it is one of
        ``_STRATEGY_LAB_CANCEL_STATUSES``; ``None`` on any read error or a
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
            if status in _STRATEGY_LAB_CANCEL_STATUSES:
                return status
    except Exception:
        logger.debug("Failed to fetch external terminal status for run %s", run_id, exc_info=True)
    return None


def _is_strategy_lab_run_cancelled(run_id: str) -> bool:
    """Return True if the run's job-store status is terminal (external cancel).

    Used by the Temporal ``is_run_cancelled_activity``.

    Preconditions:
        ``run_id`` is the strategy-lab run identifier.
    Postconditions:
        Returns True when the persisted job's ``status`` is one of
        ``cancelled``/``failed``/``interrupted``; False on any read error or a
        non-terminal/absent status (never raises). Callers that need to know
        WHICH of those three statuses triggered this (to avoid mislabeling
        one as another) should call ``_strategy_lab_external_terminal_status``
        directly instead.
    """
    return _strategy_lab_external_terminal_status(run_id) is not None


def _persist_run_state(run_id: str, state: Dict[str, Any], *, create: bool = False) -> None:
    """Write the run state to the job service so it survives restarts."""
    try:
        client = _get_lab_run_job_client()
        fields = {k: v for k, v in state.items() if k not in ("run_id", "status")}
        if create:
            client.create_job(run_id, status=state.get("status", "running"), **fields)
        else:
            client.update_job(run_id, status=state.get("status", "running"), **fields)
    except Exception as exc:
        logger.warning("Failed to persist run state for %s: %s", run_id, exc)


# Fields the Temporal workflow's persist-state activity writes as partial
# deltas over a run's lifetime (see strategy_lab/temporal/workflows.py
# _persist_state call sites): per-batch-start (current_batch), per-wave
# (completed_cycles, contiguous_cycles, completed_record_ids, errored_cycles,
# skipped_cycles, errored_details, tracker_merge_error_count),
# per-batch-complete (completed_batches). current_cycle is included
# defensively even though no persist point currently sets it -- the ``if
# field in data`` guard in ``_reconcile_run_progress`` makes it inert until,
# and unless, that changes.
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
          the ``job.get("data", job)`` fallback used elsewhere in this file)
          is copied onto ``_active_runs[run_id]``; a key absent from the
          persisted record is left untouched (a sparse/early persisted record
          can never erase a more-complete in-memory value). ``status``/
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
        state = _active_runs.get(run_id)
    if not state or state.get("status") in STRATEGY_LAB_TERMINAL_STATUSES:
        return
    try:
        client = _get_lab_run_job_client()
        persisted = client.get_job(run_id)
    except Exception:
        logger.debug("Job service reconciliation failed for run %s", run_id, exc_info=True)
        return
    if not persisted:
        return
    data = persisted.get("data", persisted)
    with _lock:
        current = _active_runs.get(run_id)
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
          Temporal is disabled/unavailable or the dispatch raised; the failure
          is logged. Never raises.
    """
    try:
        from shared.temporal import is_temporal_enabled
    except ImportError:
        return False
    if not is_temporal_enabled():
        return False
    try:
        starter()
        return True
    except Exception:
        logger.exception("Temporal dispatch failed; falling back to in-process execution")
        return False


def _fail_strategy_lab_run(run_id: str, error: str) -> None:
    """Mark a strategy-lab run "failed" (best-effort, idempotent).

    Preconditions:
        - ``run_id`` may or may not exist in ``_active_runs``.
    Postconditions:
        - If the run exists and isn't already in
          ``STRATEGY_LAB_TERMINAL_STATUSES``, its status becomes ``"failed"``
          with ``error`` recorded, the new state persisted, and a delayed
          cleanup of the ``_active_runs`` entry scheduled 900s out — so a
          dispatch failure (e.g. a Temporal outage) doesn't leak the entry
          forever. That cleanup is a no-op if
          ``run_id`` gets resumed (and thus a new state object installed)
          before the delay elapses, so it never tears down a live resumed
          run. A missing run and an already-terminal run are both no-ops.
          Never raises.
    """
    try:
        with _lock:
            state = _active_runs.get(run_id)
            if state is None or state.get("status") in STRATEGY_LAB_TERMINAL_STATUSES:
                return
            state["status"] = "failed"
            state["error"] = error
            state["current_cycle"] = None
            _persist_run_state(run_id, state)

        from investment_team.api.job_event_bus import cleanup_job

        def _cleanup() -> None:
            with _lock:
                # resume/restart always replace the entry with a new dict
                # rather than mutate this one in place, so an identity check
                # reliably detects a run that got resumed within this delay
                # window — pop/cleanup would otherwise tear down a live,
                # freshly-resumed run's tracking state.
                if _active_runs.get(run_id) is not state:
                    return
                _active_runs.pop(run_id, None)
            cleanup_job(run_id)

        timer = threading.Timer(900.0, _cleanup)
        timer.daemon = True
        timer.start()
    except Exception:
        logger.warning(
            "Failed to mark strategy-lab run %s failed: %s", run_id, error, exc_info=True
        )


def _dispatch_strategy_lab_run(
    run_id: str, request: RunStrategyLabRequest, *, allow_already_started: bool = True
) -> None:
    """Dispatch a strategy-lab run (initial / resume / restart) through Temporal (Temporal-only).

    Preconditions:
        - ``run_id``'s state is already registered in ``_active_runs`` and
          persisted (the activity reads its resume offset from that state).

    Postconditions:
        - The durable workflow is started. A collision with an already-running
          workflow under this run_id's deterministic id (e.g. a resume issued
          after an API-process restart, while the durable workflow itself kept
          running) is handled per ``allow_already_started``:
          - ``True`` (the default — used by the initial run and resume,
            whose intent matches what's already running): treated as a
            successful dispatch, a no-op; the run is NOT marked failed.
          - ``False`` (used by restart, whose reset-to-cycle-0 intent does
            NOT match a lingering old execution): raises
            ``HTTPException(409)`` instead — also without marking the run
            failed, since the old workflow may still be healthy and marking
            it failed would cause that workflow to observe the status and
            abort itself.
          On any other failure (Temporal disabled/unavailable, or the start
          RPC raising for any other reason), ``run_id`` is marked ``"failed"``
          via ``_fail_strategy_lab_run`` and ``HTTPException(503)`` is raised
          — never spawns a thread.
    """
    try:
        _require_temporal()
        from investment_team.strategy_lab.temporal.start_workflow import (
            start_strategy_lab_batch_workflow,
        )

        start_strategy_lab_batch_workflow(run_id, request)
    except Exception as exc:
        from temporalio.exceptions import WorkflowAlreadyStartedError

        if isinstance(exc, WorkflowAlreadyStartedError):
            if allow_already_started:
                # The durable workflow for this run_id is already running
                # (most commonly: resume was called after an API-process
                # restart wiped _active_runs, but the workflow itself
                # survived). Marking the run "failed" here would be observed
                # by that still-running workflow as an external stop signal
                # (via strategy_lab_external_terminal_status) and abort a
                # healthy run, so treat the collision as the dispatch
                # already having succeeded.
                logger.info(
                    "Strategy-lab workflow for run %s is already running; "
                    "treating dispatch as a no-op success.",
                    run_id,
                )
                return
            # Restart's reset-to-cycle-0 intent does NOT match a lingering
            # old execution the way resume's does — silently succeeding
            # would tell the caller "restarted from scratch" while the old
            # execution (old input, old progress) is what's actually still
            # running. Reject distinctly from a real Temporal-down 503 so
            # callers can tell "retry shortly" from "Temporal is down", and
            # don't mark the run failed — the old workflow may still be
            # healthy, and failing it would cause it to observe that status
            # and abort itself.
            raise HTTPException(
                status_code=409,
                detail="A prior execution for this run is still winding down; retry shortly.",
            ) from exc
        _fail_strategy_lab_run(
            run_id, "Failed to start the strategy-lab workflow (Temporal unavailable)."
        )
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=503,
            detail="Failed to start the strategy-lab workflow; Temporal worker unavailable.",
        ) from exc


def _ensure_no_active_run() -> None:
    """Raise 409 if any strategy-lab run is currently running.

    Shared 409-guard for the run/resume/restart endpoints, which each allow at
    most one concurrent strategy-lab run.

    Preconditions:
        - None.

    Postconditions:
        - Returns ``None`` when no entry in ``_active_runs`` has status
          ``"running"``; otherwise raises ``HTTPException(409)``. Does not mutate
          ``_active_runs``.
    """
    with _lock:
        active = [r for r in _active_runs.values() if r["status"] == "running"]
    if active:
        raise HTTPException(status_code=409, detail="A strategy lab run is already in progress.")


def _require_run_transition_lock(run_id: str) -> threading.Lock:
    """Acquire run_id's transition lock, or raise 409 when another transition
    for the same run_id is already in flight.

    Shared guard for the run/resume/restart endpoints: serializes same-run_id
    transitions so two concurrent calls (e.g. two restarts, or a resume
    racing a restart) for the same run_id can't both pass the check-then-act
    window between ``_ensure_no_active_run()`` and this run_id's state being
    written (#4028).

    Preconditions:
        - None.

    Postconditions:
        - Returns the acquired ``threading.Lock`` — held by the caller, who
          MUST release it (``try/finally: run_lock.release()``) — when no
          other run/resume/restart transition for this ``run_id`` is
          currently in flight.

    Raises:
        - ``HTTPException`` 409 when another transition for this ``run_id``
          is already in flight. Never blocks; holds nothing in that case.
    """
    run_lock = _acquire_run_transition_lock(run_id)
    if run_lock is None:
        raise HTTPException(
            status_code=409,
            detail="Another transition for this run is already in progress; retry shortly.",
        )
    return run_lock


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
        ``HTTPException(503)``.
    """
    try:
        from shared.temporal import is_temporal_enabled
    except ImportError as exc:  # pragma: no cover - shared.temporal always present
        raise HTTPException(
            status_code=503, detail="Temporal support is unavailable for this endpoint."
        ) from exc
    if not is_temporal_enabled():
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
        - Returns the workflow's result dict. Raises ``HTTPException(503)`` when
          Temporal is disabled/unavailable; on any other workflow failure,
          raises the ``HTTPException`` :func:`_translate_advisory_failure` maps
          it to (never an opaque unhandled exception).
    """
    _require_temporal()
    from investment_team.temporal.start_workflow import execute_advisory_workflow

    try:
        return execute_advisory_workflow(op, payload, key=key)
    except HTTPException:
        raise
    except RuntimeError as exc:
        # ``shared.temporal._await_client`` raises a bare RuntimeError when
        # TEMPORAL_ADDRESS is set but the worker's client never became ready in
        # time — the same "no running worker" condition ``_require_temporal``
        # checks for up front, just discovered later. Map it to the same 503
        # instead of letting it fall through to _translate_advisory_failure's
        # generic 502.
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
        - Returns (does not raise) an ``HTTPException``: the mapped 404/400 for a
          well-known ``ApplicationError`` type (found by walking ``exc``'s cause
          chain), 409 for a workflow-id collision, or 502 for anything else — so
          a route caller never sees an opaque unhandled 500 with no detail.
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
            status = _ADVISORY_ERROR_TYPE_STATUS.get(cause.type or "", 500)
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
    flight (#4028). The global check runs before minting a run_id/acquiring
    its transition lock, so a rejected request never allocates a registry
    entry that would otherwise never be looked up again.
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
            _active_runs[run_id] = initial_state
        _persist_run_state(run_id, initial_state, create=True)

        # Dispatch the run as a durable Temporal workflow so it survives a
        # worker/process restart and is visible in the Temporal UI.
        _dispatch_strategy_lab_run(run_id, request)
    finally:
        run_lock.release()

    return StrategyLabRunStartResponse(run_id=run_id, total_cycles=total_cycles)


@app.get("/strategy-lab/results", response_model=StrategyLabResultsResponse)
def get_strategy_lab_results(winning: Optional[bool] = None) -> StrategyLabResultsResponse:
    """
    Return all strategy lab records, sorted newest-first.
    Filter by winning/losing with ?winning=true or ?winning=false.
    """
    items = _snapshot_prior_records(reverse=True)

    winning_count = sum(1 for r in items if r.is_winning)
    losing_count = len(items) - winning_count

    if winning is not None:
        items = [r for r in items if r.is_winning == winning]

    return StrategyLabResultsResponse(
        items=items,
        count=len(items),
        winning_count=winning_count,
        losing_count=losing_count,
    )


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


def _job_progress_percent(completed: int, total: int) -> int:
    """Compute a job's completion percentage, tolerating a non-positive total.

    Preconditions:
        - ``completed`` and ``total`` are integers (possibly 0 or negative,
          e.g. from malformed persisted state).

    Postconditions:
        - Returns ``0`` when ``total <= 0`` (never divides by a non-positive
          total, so this can never raise ``ZeroDivisionError``).
        - Otherwise returns ``int((completed / total) * 100)``.
    """
    if total <= 0:
        return 0
    return int((completed / total) * 100)


@app.get(
    "/strategy-lab/jobs",
    response_model=InvestmentJobsListResponse,
    summary="List strategy lab runs as jobs",
)
def list_strategy_lab_jobs(running_only: bool = False) -> InvestmentJobsListResponse:
    """Return strategy lab runs in a format compatible with the central Jobs Dashboard.

    Preconditions:
        - None. ``running_only`` is an optional filter flag.

    Postconditions:
        - Returns a read-only snapshot; never mutates ``_active_runs``.
        - Merges in-memory ``_active_runs`` with persisted job-service records,
          deduplicated by run/job id (in-memory entries take precedence).
        - When ``running_only`` is ``True``, the result is filtered to
          ``status in ("running", "pending")``.
        - Entries are sorted by ``created_at`` descending.

    Raises:
        - None. Job-service merge failures are caught and logged, and the
          response falls back to the in-memory-only list; this endpoint always
          returns 200.
    """
    jobs: List[InvestmentJobSummary] = []

    # Active in-memory runs
    with _lock:
        for state in _active_runs.values():
            cycle = state.get("current_cycle")
            phase = cycle.get("phase") if cycle else None
            hypothesis = ""
            if cycle and cycle.get("strategy"):
                hypothesis = cycle["strategy"].get("hypothesis", "")[:60]
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

    # Persisted runs from job service (completed runs not in memory)
    try:
        client = _get_lab_run_job_client()
        persisted = client.list_jobs() or []
        with _lock:
            in_memory_ids = {s["run_id"] for s in _active_runs.values()}
        for job in persisted:
            jid = job.get("job_id", "")
            if jid in in_memory_ids:
                continue  # already included from in-memory
            data = job.get("data", job)
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
    except Exception as exc:
        logger.warning("Failed to load persisted strategy lab runs: %s", exc)

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
        - ``run_id`` identifies a run whose persisted status — read INSIDE
          the transition lock below, not beforehand — is in
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
        - No other run currently has status ``"running"``.
        - No other run/resume/restart transition for this run_id is
          currently in flight (checked first, before re-reading state or
          calling ``_ensure_no_active_run()``).

    Postconditions:
        - Re-seeds ``_active_runs[run_id]`` carrying forward all prior
          progress — ``completed_record_ids``/``errored_cycles``/
          ``errored_details``/``skipped_cycles``/``tracker_merge_error_count``
          — and persists the new state.
        - Dispatches the durable Temporal workflow from the first
          not-yet-contiguously-completed cycle, so no already-persisted cycle
          is re-run (and thus never duplicated).
        - Returns the run's start response with the resume offset and total
          cycle count.

    Raises:
        - ``HTTPException`` 404: ``run_id`` does not resolve to any known run.
        - ``HTTPException`` 400: the run's status is not in
          ``RESUMABLE_STATUSES``, or its ``request_payload`` is missing/not a
          dict.
        - ``HTTPException`` 409: another transition for this run_id is
          already in flight (#4028), or another run is already ``"running"``.
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
        try:
            validate_job_for_action(state, run_id, RESUMABLE_STATUSES, "resumed")
        except ValueError as exc:
            code = 404 if "not found" in str(exc) else 400
            raise HTTPException(status_code=code, detail=str(exc)) from exc

        payload = state.get("request_payload")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Original request payload not available.")

        completed_cycles = state.get("completed_cycles", 0)
        # contiguous_cycles tracks the highest unbroken sequence from index 0
        # — safe to use as the resume offset (won't skip gaps or re-run finished cycles).
        contiguous_cycles = state.get("contiguous_cycles", completed_cycles)
        request = RunStrategyLabRequest(**payload)
        total_cycles = request.batch_size * request.batch_count
        completed_batches, _within = divmod(contiguous_cycles, request.batch_size)

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
        )
        with _lock:
            _active_runs[run_id] = resumed_state
        _persist_run_state(run_id, resumed_state)

        # The Temporal activity derives its resume offset from the persisted
        # contiguous-cycle count (set above), so a durable resume picks up where the
        # run left off.
        _dispatch_strategy_lab_run(run_id, request)
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
        - ``run_id`` identifies a run whose persisted status — read INSIDE
          the transition lock below, not beforehand — is in
          ``RESTARTABLE_STATUSES | {"completed_with_errors"}``
          (completed/failed/cancelled/interrupted/agent_crash/completed_with_errors).
          Reading it before acquiring the lock could observe another
          transition's transiently-written "running" reset and misreport a
          genuine in-flight-elsewhere race as a permanent 400 instead of a
          retryable 409 ("running" is deliberately excluded from
          ``RESTARTABLE_STATUSES``). Only a cheap existence check (``run_id``
          resolves to *some* known run) runs before the lock, so a request
          for a nonexistent run_id never allocates a transition-lock entry.
        - The run's persisted ``request_payload`` is present and is a dict.
        - No other run currently has status ``"running"``.
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
          carried forward — and persists the new state.
        - Dispatches the durable Temporal workflow starting at cycle 0. If
          that dispatch still 409s (a residual collision — e.g. a second
          restart/resume racing in after the termination check above), the
          reset is rolled back — both ``_active_runs[run_id]`` and the
          persisted state are restored to their pre-restart snapshot — so
          the run isn't left wedged showing ``"running"`` and blocking every
          future run/resume/restart call.
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
        - ``HTTPException`` 503: Temporal is disabled/unavailable, or the
          prior execution couldn't be resolved due to a Temporal-side error.

    Two concurrent restart/resume calls for the same run_id can no longer
    both pass the check-then-write window (#4028, closed by
    ``_require_run_transition_lock``, which reserves this run_id for the
    whole check→terminate→write→dispatch sequence below).

    Known, accepted residual race (requires multiple unlikely events to
    align; closing it is a real feature, not a quick patch — tracked as a
    follow-up rather than fixed here):
        - Confirming the old *workflow* terminated does not guarantee an
          already in-flight, non-heartbeating *activity* has stopped —
          Strategy Lab's activities aren't cooperatively cancellable, so one
          can still commit a cycle record or paper trade after the new
          cycle-0 workflow has started (tracked as #4029).
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
        # "completed_with_errors" is a terminal outcome of the same workflow as
        # "completed" and must be restartable. Extend the shared set locally
        # rather than leaking a lab-specific status into job_service_client.
        _lab_restartable = RESTARTABLE_STATUSES | {"completed_with_errors"}
        try:
            validate_job_for_action(state, run_id, _lab_restartable, "restarted")
        except ValueError as exc:
            code = 404 if "not found" in str(exc) else 400
            raise HTTPException(status_code=code, detail=str(exc)) from exc

        payload = state.get("request_payload")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Original request payload not available.")

        _ensure_no_active_run()

        request = RunStrategyLabRequest(**payload)
        total_cycles = request.batch_size * request.batch_count

        # Resolve any prior execution BEFORE writing anything: a still-running
        # workflow polls persisted status between waves (strategy_lab_external_
        # terminal_status), so writing the optimistic "running" reset first would
        # let it observe that transient state and run an extra wave before a
        # dispatch collision is even detected.
        _require_temporal()
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
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Failed to resolve the prior strategy-lab execution before "
                    "restarting; Temporal worker unavailable."
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
        )
        with _lock:
            _active_runs[run_id] = restarted_state
        _persist_run_state(run_id, restarted_state)

        # Restart from scratch through Temporal (offset 0, per the reset
        # persisted state above). allow_already_started=False: unlike resume, a
        # collision here means an old, un-reset execution is still running, not
        # that the intended restart is already in flight.
        try:
            _dispatch_strategy_lab_run(run_id, request, allow_already_started=False)
        except HTTPException as exc:
            if exc.status_code == 409:
                # The reset above never actually took effect (an old execution
                # is still running under this run_id) — restore the pre-restart
                # snapshot so _ensure_no_active_run() doesn't wedge on a phantom
                # "running" entry, blocking every future run/resume/restart call
                # until the stale execution happens to overwrite it on its own.
                with _lock:
                    _active_runs[run_id] = state
                _persist_run_state(run_id, state)
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
    still exists.

    Preconditions:
        - None on run status — any run can be deleted regardless of its
          current status.
        - The job service must have a record for ``run_id`` for the delete
          to succeed.

    Postconditions:
        - The job-service record for ``run_id`` is deleted before
          ``_active_runs.pop(run_id, None)`` is attempted, so
          ``_active_runs`` is only mutated once the job-service delete has
          succeeded.
        - Returns ``{"job_id": run_id, "deleted": True}``.

    Raises:
        - ``HTTPException`` 404: ``client.delete_job(run_id)`` returns a
          falsy result (no such run in the job service).
        - An exception raised by ``client.delete_job`` itself is not caught
          and propagates uncaught (surfaces as a 500) — ``_active_runs`` is
          left untouched in that case.
    """
    client = _get_lab_run_job_client()
    deleted = client.delete_job(run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    with _lock:
        _active_runs.pop(run_id, None)
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
          in-memory-only snapshot; this endpoint always returns 200.
    """
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

        with _lock:
            in_memory = {r["run_id"]: r for r in _active_runs.values()}

        # Merge running/pending jobs from the persistent job service that
        # may not be in _active_runs (e.g. after a server restart).
        persisted_list = client.list_jobs(statuses=["running", "pending"])
        for job in persisted_list:
            rid = job.get("job_id") or job.get("run_id", "")
            if rid and rid not in in_memory:
                in_memory[rid] = _normalize_persisted_job(job, fallback_status="running", run_id=rid)
    except Exception:
        logger.debug("Job service fallback failed for run listing", exc_info=True)
        with _lock:
            in_memory = {r["run_id"]: r for r in _active_runs.values()}

    runs = [_run_state_to_response(r) for r in in_memory.values()]
    return ActiveRunsResponse(runs=runs)


@app.get(
    "/strategy-lab/runs/{run_id}/status",
    response_model=StrategyLabRunStatusResponse,
    summary="Get strategy lab run status (polling fallback)",
)
def get_strategy_lab_run_status(run_id: str) -> StrategyLabRunStatusResponse:
    """Snapshot of a single run's progress. Use for polling when SSE is unavailable.

    Preconditions:
        - ``run_id`` must resolve to a state via ``_active_runs`` or the
          job-service fallback (``_load_run_from_job_service``).

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
    window. The streaming generator itself remains async so it doesn't block
    Uvicorn worker threads once connected.
    """
    from starlette.concurrency import run_in_threadpool

    from investment_team.api.job_event_bus import subscribe, unsubscribe
    from shared.sse import sse_job_stream_async, sse_line

    with _lock:
        state = _active_runs.get(run_id)
    if state:
        # Reconcile before the terminal check so an externally-completed run
        # (job-service status advanced past what this process's in-memory
        # entry still shows) is caught by the short-circuit below with
        # up-to-date data, and the live path's _snapshot_event() -- which
        # reads _active_runs.get(run_id, {}) fresh -- picks up these same
        # values automatically.
        await run_in_threadpool(_reconcile_run_progress, run_id)
        with _lock:
            state = _active_runs.get(run_id, state)
    else:
        state = await run_in_threadpool(_load_run_from_job_service, run_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    # If the run is already terminal, send snapshot + done immediately.
    if state.get("status") in STRATEGY_LAB_TERMINAL_STATUSES:

        async def _terminal_gen():
            yield sse_line(
                {"type": "snapshot", **_run_state_to_response(state).model_dump(mode="json")}
            )
            yield sse_line({"type": "done"})

        return StreamingResponse(_terminal_gen(), media_type="text/event-stream")

    def _snapshot_event() -> Optional[dict]:
        # Skip the snapshot when there's no current in-memory state to send.
        with _lock:
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
    """Counts of job-service rows removed (Postgres ``jobs`` or local file cache)."""

    deleted_lab_records: int = 0
    deleted_lab_strategies: int = 0
    deleted_lab_backtests: int = 0
    deleted_paper_trading_sessions: int = 0
    message: str = "Strategy lab and paper-trading session storage cleared."


class DeleteStrategyLabRecordResponse(BaseModel):
    lab_record_id: str
    deleted_strategy_id: str
    deleted_backtest_id: str
    deleted_paper_trading_sessions: int = 0


# Bounded thread-pool ceiling for the job-service fan-out helpers below. These
# issue blocking sync HTTP calls, so threads (not asyncio) are the right tool;
# the cap keeps a large server-side job list from spawning unbounded threads.
# NB: _purge_strategy_lab_job_storage runs the four teams on an outer pool of 4,
# so a full purge peaks at 4 x _PURGE_MAX_WORKERS = 64 transient threads against
# the job service — keep both widths in mind when tuning either.
_PURGE_MAX_WORKERS = 16

# Overall wall-clock ceiling for a full purge fan-out. Each underlying HTTP call
# is already bounded by the job-service client's per-request timeout + finite
# retries, but a pathological straggler must never wedge the endpoint, so the
# collection below stops waiting past this deadline and abandons any unfinished
# unit (counting it as 0 deleted) rather than blocking a server thread.
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
    """
    from job_service_client import JobServiceClient

    client = JobServiceClient(team="investment_paper_trading_sessions")
    matching_ids: list[str] = []
    for job in client.list_jobs() or []:
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


def _purge_strategy_lab_job_storage() -> dict[str, int]:
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
        - Each count equals the number of matching jobs successfully deleted and
          is independent of the order in which the concurrent units/deletes
          finish; the returned dict always has exactly these four keys.
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
        # overruns is counted as 0 deleted; a unit that *raises* still propagates
        # (preserving the prior error contract).
        deadline = time.monotonic() + _PURGE_TIMEOUT_S
        results: dict[str, int] = {}
        for key, future in units.items():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                results[key] = future.result(timeout=remaining)
            except concurrent.futures.TimeoutError:
                logger.warning(
                    "purge unit %s did not finish within %.0fs; counted as 0 deleted",
                    key,
                    _PURGE_TIMEOUT_S,
                )
                results[key] = 0
        return results
    finally:
        # Never block on a straggler: in-flight HTTP deletes are themselves bounded
        # by the client's per-request timeout, so abandoning the worker thread leaks
        # nothing unbounded. cancel_futures drops any unit that hasn't started.
        pool.shutdown(wait=False, cancel_futures=True)


@app.delete(
    "/strategy-lab/records/{lab_record_id}",
    response_model=DeleteStrategyLabRecordResponse,
)
def delete_strategy_lab_record(lab_record_id: str) -> DeleteStrategyLabRecordResponse:
    """
    Delete one strategy lab run: lab card, linked lab strategy/backtest jobs, and any paper-trading
    sessions that reference this ``lab_record_id``.
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

        del _strategy_lab_records[lab_record_id]
        try:
            del _strategies[strategy_id]
        except KeyError:
            pass
        try:
            del _backtests[backtest_id]
        except KeyError:
            pass

    paper_deleted = _delete_paper_sessions_for_lab_record(lab_record_id)

    return DeleteStrategyLabRecordResponse(
        lab_record_id=lab_record_id,
        deleted_strategy_id=strategy_id,
        deleted_backtest_id=backtest_id,
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
    """
    with _lock:
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

    PR 2 live-mode fields (``provider_id``, ``min_fills``, ``max_hours``,
    ``warmup_bars``, ``timeframe``) take effect only when
    ``INVESTMENT_LIVE_PAPER_ENABLED=true``. When the flag is off (the
    default), the legacy recent-OHLCV path runs and the new fields are
    ignored so existing clients and tests remain unaffected.
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
        - ``session_id`` must already exist in ``_paper_trading_sessions`` with status RUNNING
        - ``strategy`` must be a valid StrategySpec with resolvable symbols
        - ``backtest_record`` must contain valid backtest results for divergence analysis

    Postconditions:
        - On the success path, ``_paper_trading_sessions[session_id]`` is always written
          (COMPLETED or FAILED with ``completed_at`` set), which can recreate a concurrently
          deleted session
        - Import failures for ``MarketDataService``/``PaperTradingAgent`` (e.g. a missing
          dependency or circular import) are caught by the same handler as any other
          in-worker exception and also transition the session to FAILED
        - On the empty-data and exception paths, the terminal write runs only when the session
          entry still exists at write time; concurrent deletion (e.g. via
          ``DELETE /strategy-lab/records/{lab_record_id}``) then leaves no terminal record

    Raises:
        - None. All failures, including import errors for the two lazily-imported
          dependencies, are caught and logged; the session is marked FAILED instead.
    """
    try:
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
            with _lock:
                raw = _paper_trading_sessions.get(session_id)
                if raw is not None:
                    session = PaperTradingSession.parse_persisted(raw)
                    session.status = PaperTradingStatus.FAILED
                    session.error = "Failed to fetch market data from external sources."
                    session.divergence_analysis = session.error
                    session.completed_at = datetime.now(tz=timezone.utc).isoformat()
                    _paper_trading_sessions[session_id] = session
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
        # other in-worker crash.
        assert result_session.status in (
            PaperTradingStatus.COMPLETED,
            PaperTradingStatus.FAILED,
        ), f"PaperTradingAgent.run_session returned non-terminal status {result_session.status!r}"
        assert result_session.completed_at, (
            "PaperTradingAgent.run_session returned a session with no completed_at"
        )
        # Preserve the session_id and lab_record_id that the caller committed to.
        result_session.session_id = session_id
        result_session.lab_record_id = lab_record_id

        with _lock:
            _paper_trading_sessions[session_id] = result_session
        logger.info(
            "Paper trade %s: completed (status=%s, verdict=%s, trades=%d)",
            session_id,
            result_session.status,
            result_session.verdict,
            len(result_session.trades),
        )
    except Exception as exc:
        logger.exception("Paper trade %s: background worker crashed", session_id)
        with _lock:
            raw = _paper_trading_sessions.get(session_id)
            if raw is not None:
                session = PaperTradingSession.parse_persisted(raw)
                session.status = PaperTradingStatus.FAILED
                session.error = f"Paper trading crashed: {exc}"
                session.divergence_analysis = f"Paper trading crashed: {exc}"
                session.completed_at = datetime.now(tz=timezone.utc).isoformat()
                _paper_trading_sessions[session_id] = session


@app.post("/strategy-lab/paper-trade", response_model=PaperTradingResponse)
def run_paper_trading(request: RunPaperTradingRequest) -> PaperTradingResponse:
    """
    Start a paper trading session for a winning strategy. Returns immediately.

    Because paper trading can take 2-3 minutes (market data fetch + sandbox
    execution + LLM divergence analysis), this endpoint validates inputs, creates
    a session in ``OPENING`` status (live path) or ``running`` status (legacy
    path), kicks off a background worker, and returns that session immediately.
    Clients should poll ``GET /strategy-lab/paper-trade/{session_id}`` for
    progress until ``status`` is ``completed`` or ``failed``.
    """
    # 1 — Look up the winning strategy lab record (synchronous validation)
    with _lock:
        raw_record = _strategy_lab_records.get(request.lab_record_id)

    if raw_record is None:
        raise HTTPException(
            status_code=404, detail=f"Strategy lab record '{request.lab_record_id}' not found."
        )

    lab_record = StrategyLabRecord.parse_persisted(raw_record)

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

    # 2 — Create initial "running" session and persist immediately
    session_id = f"pt-{uuid.uuid4().hex[:8]}"
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
            # deliver, it runs unsupervised — if it later reaches its own
            # terminal state, that write can silently overwrite the ``failed``
            # status set just below. No automatic reconciliation catches this
            # narrow compound-failure case (the startup orphan sweep only
            # covers a crashed process, not a live unreachable workflow); log
            # so it's at least visible to operators instead of silent.
            logger.warning(
                "Best-effort stop signal for possibly-orphaned paper-trading "
                "session %s failed to deliver; the session is marked failed but "
                "an orphaned workflow may still be running.",
                session_id,
                exc_info=True,
            )
        _fail_paper_trading_session(
            session_id, "Failed to start the paper-trading workflow (Temporal unavailable)."
        )
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=503,
            detail="Failed to start the paper-trading workflow; Temporal worker unavailable.",
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
    return os.environ.get("INVESTMENT_LIVE_PAPER_ENABLED", "false").lower() in {
        "true",
        "1",
        "yes",
    }


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
        session.status = PaperTradingStatus.FAILED
        session.error = error
        session.completed_at = datetime.now(tz=timezone.utc).isoformat()
        _paper_trading_sessions[session_id] = session


# Default fees used when the request omits explicit overrides. Sits at module
# scope so tests can exercise the resolution logic directly.
_DEFAULT_TX_COST_BPS = 5.0
_DEFAULT_SLIPPAGE_BPS = 2.0


def _resolve_fee_overrides(request: "RunPaperTradingRequest") -> tuple[float, float]:
    """Return ``(transaction_cost_bps, slippage_bps)`` for the live config.

    Uses explicit ``None`` checks instead of ``or`` so a caller asking for
    zero-fee / zero-slippage experiments isn't silently bumped to the
    defaults — ``0.0`` is falsy but semantically meaningful here.
    """
    tx = (
        request.transaction_cost_bps
        if request.transaction_cost_bps is not None
        else _DEFAULT_TX_COST_BPS
    )
    slip = request.slippage_bps if request.slippage_bps is not None else _DEFAULT_SLIPPAGE_BPS
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

        tx_cost, slip = _resolve_fee_overrides(request)
        bt_config = _BC(
            start_date=datetime.now(tz=timezone.utc).date().isoformat(),
            end_date=datetime.now(tz=timezone.utc).date().isoformat(),
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
                return
            session = PaperTradingSession.parse_persisted(raw)
            session.trades = run_result.trades
            session.fill_count = run_result.fill_count
            session.cutover_ts = run_result.cutover_ts
            session.provider_id = run_result.provider_id
            session.terminated_reason = run_result.terminated_reason
            session.warnings = run_result.warnings
            session.error = (run_result.error or "") or None
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
        with _lock:
            raw = _paper_trading_sessions.get(session_id)
            if raw is not None:
                session = PaperTradingSession.parse_persisted(raw)
                session.status = PaperTradingStatus.FAILED
                session.error = str(exc)
                session.completed_at = datetime.now(tz=timezone.utc).isoformat()
                _paper_trading_sessions[session_id] = session
    finally:
        with _lock:
            _live_paper_stop_controllers.pop(session_id, None)


@app.post("/strategy-lab/paper-trade/{session_id}/stop", response_model=PaperTradingResponse)
def stop_live_paper_trading(session_id: str) -> PaperTradingResponse:
    """Idempotent user-stop for a live paper-trading session.

    Sets the session's stop flag; the background worker terminates at the next
    bar boundary. Returns the session's current state (still ``live`` /
    ``warming_up`` if the worker hasn't yet noticed — clients poll
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
    try:
        _signal_paper_trading_stop(session_id)
    except HTTPException:
        raise
    except Exception as exc:
        from temporalio.service import RPCError, RPCStatusCode

        if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
            logger.info(
                "Stop signal for paper-trading session %s found no running workflow "
                "(already closed); treating as already-stopped.",
                session_id,
            )
            return PaperTradingResponse(
                session=session,
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
        fresh_session = PaperTradingSession.parse_persisted(fresh_raw)
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
    """Enumerate registered market-data providers and their capabilities."""
    from investment_team.trading_service.providers import default_registry

    registry = default_registry()
    rows = [ProviderDescriptor(**row) for row in registry.describe_all()]
    return ProvidersListResponse(
        live_paper_enabled=_live_paper_enabled(),
        providers=rows,
    )


@app.get("/strategy-lab/paper-trade/results", response_model=PaperTradingResultsResponse)
def get_paper_trading_results(
    verdict: Optional[str] = None,
) -> PaperTradingResultsResponse:
    """
    Return all paper trading sessions, sorted newest-first.
    Filter by verdict with ?verdict=ready_for_live or ?verdict=not_performant.
    """
    with _lock:
        raw = list(_paper_trading_sessions.values())

    items = [PaperTradingSession.parse_persisted(r) for r in raw]
    items.sort(key=lambda s: s.completed_at or s.started_at, reverse=True)

    if verdict is not None:
        items = [s for s in items if s.verdict and s.verdict.value == verdict]

    # Counts are derived from ``items`` by the response model itself, so they
    # always match whatever list (filtered or not) is returned here.
    return PaperTradingResultsResponse(items=items)


@app.get("/strategy-lab/paper-trade/{session_id}", response_model=PaperTradingResponse)
def get_paper_trading_session(session_id: str) -> PaperTradingResponse:
    """Return a specific paper trading session by ID."""
    with _lock:
        raw = _paper_trading_sessions.get(session_id)

    if raw is None:
        raise HTTPException(
            status_code=404, detail=f"Paper trading session '{session_id}' not found."
        )

    session = PaperTradingSession.parse_persisted(raw)
    return PaperTradingResponse(session=session)


@app.on_event("startup")
def _recover_orphaned_paper_trading_sessions() -> None:
    """Mark sessions left in an active status by a previous process as ``failed``.

    Active statuses covered: ``RUNNING``, ``OPENING``, ``WARMING_UP``, or ``LIVE``.

    The paper-trade worker runs in a non-daemon thread so graceful shutdowns wait
    for it, but SIGKILL/crashes can still orphan a session. Without this recovery
    pass, such sessions would sit in an active status forever and clients would
    poll indefinitely with no terminal transition.
    """
    try:
        with _lock:
            raw_sessions = list(_paper_trading_sessions.values())
    except Exception:
        logger.debug("Paper-trade recovery: could not enumerate sessions", exc_info=True)
        return

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    # Active statuses that indicate an in-flight session. PR 1 only used
    # RUNNING; PR 2's live path transitions through OPENING → WARMING_UP →
    # LIVE. A SIGKILL during any of those leaves the row orphaned; without
    # recovery the new per-strategy concurrency guard (409) would lock out
    # future runs for that strategy indefinitely.
    _active_statuses = {
        PaperTradingStatus.RUNNING,
        PaperTradingStatus.OPENING,
        PaperTradingStatus.WARMING_UP,
        PaperTradingStatus.LIVE,
    }
    recovered = 0
    for raw in raw_sessions:
        try:
            session = PaperTradingSession.parse_persisted(raw)
        except Exception:
            logger.debug(
                "Paper-trade recovery: skipping unparseable session record",
                exc_info=True,
            )
            continue
        if session.status not in _active_statuses:
            continue
        session.status = PaperTradingStatus.FAILED
        session.completed_at = now_iso
        session.terminated_reason = "process_exit"
        session.error = (
            "Paper trading did not complete — the worker process exited before "
            "finalizing the session. Re-run the paper trade from the Strategy Lab."
        )
        # Preserve the legacy free-form field too so older clients still read a message.
        session.divergence_analysis = session.error
        try:
            with _lock:
                _paper_trading_sessions[session.session_id] = session
            recovered += 1
        except Exception:
            logger.exception(
                "Paper-trade recovery: failed to persist failed status for %s",
                session.session_id,
            )

    if recovered:
        logger.info(
            "Paper-trade recovery: marked %d orphaned active session(s) as failed",
            recovered,
        )


# ---------------------------------------------------------------------------
# Financial Advisor — conversational profile builder
# ---------------------------------------------------------------------------


class StartAdvisorSessionRequest(BaseModel):
    user_id: str = Field(..., description="Unique user identifier")


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
        - ``HTTPException(500)`` if the advisory workflow returns a result
          missing ``advisor_message`` or ``session``.
    """
    session_id = f"adv-{uuid.uuid4().hex}"
    result = _execute_advisory(
        "advisor_start",
        {"session_id": session_id, "user_id": request.user_id},
        key=session_id,
    )
    if "advisor_message" not in result or "session" not in result:
        raise HTTPException(
            status_code=500,
            detail="Advisor execution returned unexpected response structure",
        )
    return StartAdvisorSessionResponse(
        session_id=session_id,
        advisor_message=result["advisor_message"],
        session=AdvisorSession.model_validate(result["session"]),
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
        - ``HTTPException(404)`` if ``session_id`` does not match a known session.
        - ``HTTPException(500)`` if the advisory workflow returns a result
          missing ``advisor_message``, ``session_status``, ``current_topic``,
          or ``missing_fields``.
    """
    with _lock:
        session = _advisor_sessions.get(session_id)

    if not session:
        raise HTTPException(status_code=404, detail=f"Advisor session {session_id} not found")

    result = _execute_advisory(
        "advisor_message",
        {"session_id": session_id, "message": request.message},
        key=session_id,
    )
    required_keys = ("advisor_message", "session_status", "current_topic", "missing_fields")
    if any(key not in result for key in required_keys):
        raise HTTPException(
            status_code=500,
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
    """Get the current state of an advisor session."""
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
        - ``HTTPException(500)`` if the advisory workflow returns a result
          missing ``user_id`` or ``ips``.
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
    missing = _advisor_agent.missing_fields(session.collected)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot finalize — missing required fields: {', '.join(missing)}",
        )

    result = _execute_advisory("advisor_complete", {"session_id": session_id}, key=session_id)
    if "user_id" not in result or "ips" not in result:
        raise HTTPException(
            status_code=500,
            detail="Advisor completion returned unexpected response structure",
        )
    return CompleteAdvisorSessionResponse(
        user_id=result["user_id"], ips=IPS.model_validate(result["ips"])
    )
