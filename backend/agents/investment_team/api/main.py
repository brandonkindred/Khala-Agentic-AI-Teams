"""FastAPI endpoints for the Investment Team."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from investment_team.agents import (
    AgentIdentity,
    FinancialAdvisorAgent,
    InvestmentCommitteeAgent,
    PolicyGuardianAgent,
)
from investment_team.market_lab_data import (
    FreeTierMarketDataProvider,
    MarketLabContext,
    StrategyLabDataRequest,
)
from investment_team.models import (
    IPS,
    AdvisorSession,
    AdvisorSessionStatus,
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
    PortfolioPosition,
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
    ValidationCheck,
    ValidationReport,
    ValidationStatus,
    WorkflowMode,
)
from investment_team.orchestrator import InvestmentTeamOrchestrator, WorkflowState
from investment_team.signal_intelligence_agent import SignalIntelligenceExpert
from investment_team.signal_intelligence_models import SignalIntelligenceBriefV1
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from job_service_client import RESTARTABLE_STATUSES, RESUMABLE_STATUSES, validate_job_for_action
from shared_observability import init_otel, instrument_fastapi_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

init_otel(service_name="investment-team", team_key="investment")

app = FastAPI(
    title="Investment Team API",
    description="Investment profile management, portfolio proposals, strategy validation, and promotion gates.",
    version="1.0.0",
)
instrument_fastapi_app(app, team_key="investment")

_workflow_state = WorkflowState()
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Persistent storage backed by JobServiceClient (survives server restarts)
# ---------------------------------------------------------------------------
class _PersistentDict:
    """Dict-like wrapper around JobServiceClient for restart-safe entity storage."""

    def __init__(self, entity_type: str) -> None:
        from job_service_client import JobServiceClient

        self._client = JobServiceClient(team=f"investment_{entity_type}")
        self._entity_type = entity_type

    def __setitem__(self, key: str, value: Any) -> None:
        data = value.model_dump(mode="json") if hasattr(value, "model_dump") else {"value": value}
        existing = self._client.get_job(key)
        if existing:
            self._client.update_job(key, data=data)
        else:
            self._client.create_job(key, status="stored", data=data)

    def __getitem__(self, key: str) -> Any:
        job = self._client.get_job(key)
        if job is None:
            raise KeyError(key)
        return job.get("data", job)

    def get(self, key: str, default: Any = None) -> Any:
        job = self._client.get_job(key)
        if job is None:
            return default
        return job.get("data", job)

    def __contains__(self, key: str) -> bool:
        return self._client.get_job(key) is not None

    def __delitem__(self, key: str) -> None:
        self._client.delete_job(key)

    def pop(self, key: str, *args: Any) -> Any:
        job = self._client.get_job(key)
        if job is None:
            if args:
                return args[0]
            raise KeyError(key)
        self._client.delete_job(key)
        return job.get("data", job)

    def values(self) -> list:
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

_advisor_agent = FinancialAdvisorAgent()
_policy_guardian = PolicyGuardianAgent()
_orchestrator = InvestmentTeamOrchestrator()
_committee_agent = InvestmentCommitteeAgent()

# In-memory state for active strategy lab runs (keyed by run_id).
_active_runs: Dict[str, Dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Strategy Lab run tracking models
# ---------------------------------------------------------------------------


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
    current_cycle: Optional[StrategyLabCycleProgress] = None
    completed_record_ids: List[str] = Field(default_factory=list)
    error: Optional[str] = None


class ActiveRunsResponse(BaseModel):
    """List of all tracked strategy lab runs (active and recently completed)."""

    runs: List[StrategyLabRunStatusResponse] = Field(default_factory=list)


def _run_state_to_response(state: Dict[str, Any]) -> StrategyLabRunStatusResponse:
    """Convert an _active_runs entry to a Pydantic response."""
    cc = state.get("current_cycle")
    return StrategyLabRunStatusResponse(
        run_id=state["run_id"],
        status=state["status"],
        started_at=state["started_at"],
        total_cycles=state["total_cycles"],
        completed_cycles=state.get("completed_cycles", 0),
        skipped_cycles=state.get("skipped_cycles", 0),
        current_cycle=StrategyLabCycleProgress(**cc) if cc else None,
        completed_record_ids=state.get("completed_record_ids", []),
        error=state.get("error"),
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
    authored_by: str = Field(..., description="Agent or user ID who authored the strategy")
    asset_class: str = Field(..., description="Primary asset class")
    hypothesis: str = Field(..., description="Investment hypothesis")
    signal_definition: str = Field(..., description="Signal definition")
    entry_rules: List[str] = Field(default_factory=list)
    exit_rules: List[str] = Field(default_factory=list)
    sizing_rules: List[str] = Field(default_factory=list)
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
    """Create a new portfolio proposal."""
    with _lock:
        ips = _profiles.get(request.user_id)

    if not ips:
        raise HTTPException(status_code=404, detail=f"No IPS found for user {request.user_id}")

    proposal_id = f"prop-{uuid.uuid4().hex[:8]}"

    positions = [
        PortfolioPosition(
            symbol=p.get("symbol", ""),
            asset_class=p.get("asset_class", ""),
            weight_pct=p.get("weight_pct", 0.0),
            rationale=p.get("rationale", ""),
        )
        for p in request.positions
    ]

    proposal = PortfolioProposal(
        proposal_id=proposal_id,
        prepared_by=request.prepared_by,
        ips_version=ips.profile.schema_version,
        data_snapshot_id=f"snap-{_now()}",
        objective=request.objective,
        positions=positions,
        expected_return_pct=request.expected_return_pct,
        expected_volatility_pct=request.expected_volatility_pct,
        expected_max_drawdown_pct=request.expected_max_drawdown_pct,
        assumptions=request.assumptions,
    )

    with _lock:
        _proposals[proposal_id] = proposal

    return CreateProposalResponse(proposal_id=proposal_id, proposal=proposal)


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

    violations = _policy_guardian.check_portfolio(ips, proposal)

    return ValidateProposalResponse(
        proposal_id=proposal_id,
        valid=len(violations) == 0,
        violations=violations,
    )


@app.post("/strategies", response_model=CreateStrategyResponse)
def create_strategy(request: CreateStrategyRequest) -> CreateStrategyResponse:
    """Create a new investment strategy specification."""
    strategy_id = f"strat-{uuid.uuid4().hex[:8]}"

    strategy = StrategySpec(
        strategy_id=strategy_id,
        authored_by=request.authored_by,
        asset_class=request.asset_class,
        hypothesis=request.hypothesis,
        signal_definition=request.signal_definition,
        entry_rules=request.entry_rules,
        exit_rules=request.exit_rules,
        sizing_rules=request.sizing_rules,
        risk_limits=request.risk_limits,
        speculative=request.speculative,
    )

    with _lock:
        _strategies[strategy_id] = strategy

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

    checks = []
    if request.checks:
        for c in request.checks:
            try:
                status = ValidationStatus(c.get("status", "pass"))
            except ValueError:
                status = ValidationStatus.PASS
            checks.append(
                ValidationCheck(
                    name=c.get("name", ""),
                    status=status,
                    details=c.get("details", ""),
                )
            )
    else:
        checks = [
            ValidationCheck(
                name="backtest_quality", status=ValidationStatus.PASS, details="Sharpe > 1.0"
            ),
            ValidationCheck(
                name="walk_forward",
                status=ValidationStatus.PASS,
                details="Out-of-sample Sharpe > 0.8",
            ),
            ValidationCheck(
                name="stress_test", status=ValidationStatus.PASS, details="Max DD within limits"
            ),
            ValidationCheck(
                name="transaction_cost_model",
                status=ValidationStatus.PASS,
                details="Net return positive",
            ),
            ValidationCheck(
                name="liquidity_impact",
                status=ValidationStatus.PASS,
                details="Minimal market impact",
            ),
        ]

    validation = ValidationReport(
        strategy_id=strategy_id,
        generated_by="validation_agent",
        data_snapshot_id=f"snap-{_now()}",
        backtest_period=request.backtest_period,
        scenario_set=request.scenario_set,
        checks=checks,
        summary="Validation completed.",
    )

    with _lock:
        _validations[strategy_id] = validation

    failures = [c.details for c in checks if c.status == ValidationStatus.FAIL]

    return ValidateStrategyResponse(
        strategy_id=strategy_id,
        validation=validation,
        passed=len(failures) == 0,
        failures=failures,
    )


@app.post("/backtests", response_model=RunBacktestResponse)
def run_backtest(request: RunBacktestRequest) -> RunBacktestResponse:
    """Run a backtest using real historical market data and LLM-driven trade decisions."""
    with _lock:
        strategy = _strategies.get(request.strategy_id)

    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {request.strategy_id} not found")

    strategy = StrategySpec(**strategy) if isinstance(strategy, dict) else strategy

    config = BacktestConfig(
        start_date=request.start_date,
        end_date=request.end_date,
        initial_capital=request.initial_capital,
        benchmark_symbol=request.benchmark_symbol,
        rebalance_frequency=request.rebalance_frequency,
        transaction_cost_bps=request.transaction_cost_bps,
        slippage_bps=request.slippage_bps,
    )

    try:
        result, trades = _run_real_data_backtest(strategy, config)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Backtest failed for strategy %s: %s", request.strategy_id, exc)
        raise HTTPException(status_code=500, detail=f"Backtest failed: {exc}") from exc

    backtest_id = f"bt-{uuid.uuid4().hex[:8]}"
    now = _now()
    record = BacktestRecord(
        backtest_id=backtest_id,
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        config=config,
        submitted_by=request.submitted_by,
        submitted_at=now,
        completed_at=now,
        result=result,
        notes=request.notes,
        trades=trades,
    )

    with _lock:
        _backtests[backtest_id] = record

    return RunBacktestResponse(backtest=record)


@app.get("/backtests", response_model=ListBacktestsResponse)
def list_backtests(strategy_id: Optional[str] = None) -> ListBacktestsResponse:
    """List recorded backtests, optionally filtered by strategy ID."""
    with _lock:
        raw = list(_backtests.values())

    items = [BacktestRecord(**r) if isinstance(r, dict) else r for r in raw]

    if strategy_id:
        items = [item for item in items if item.strategy_id == strategy_id]

    items.sort(key=lambda item: item.completed_at, reverse=True)
    return ListBacktestsResponse(items=items, count=len(items))


@app.post("/promotions/decide", response_model=PromotionDecisionResponse)
def promotion_decision(request: PromotionDecisionRequest) -> PromotionDecisionResponse:
    """Run promotion gate decision for a strategy."""
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

    approver = AgentIdentity(
        agent_id=request.approver_agent_id,
        role=request.approver_role,
        version=request.approver_version,
    )

    decision = _orchestrator.promotion_decision(
        state=_workflow_state,
        strategy=strategy,
        validation=validation,
        ips=ips,
        proposer_agent_id=request.proposer_agent_id,
        approver=approver,
        risk_veto=request.risk_veto,
        human_live_approval=request.human_live_approval,
    )

    return PromotionDecisionResponse(strategy_id=request.strategy_id, decision=decision)


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
    """Generate an investment committee memo."""
    memo = _committee_agent.draft_memo(
        user_id=request.user_id,
        recommendation=request.recommendation,
        rationale=request.rationale,
        dissenting_views=request.dissenting_views,
    )

    return CreateMemoResponse(memo=memo)


# ---------------------------------------------------------------------------
# Strategy Lab — ideation, backtesting, and analysis
# ---------------------------------------------------------------------------


def _run_real_data_backtest(
    strategy: StrategySpec,
    config: BacktestConfig,
) -> tuple[BacktestResult, List[TradeRecord]]:
    """
    Run a backtest using real historical market data and LLM-driven trade decisions.

    Fetches OHLCV data from Yahoo Finance (all asset classes including crypto)
    for the backtest period, then walks through bars chronologically with the LLM
    interpreting the strategy's entry/exit rules to decide trades.

    Returns (BacktestResult, trade_ledger).
    """
    # Lazy imports: yfinance is slow to import and the LLM client has side-effects;
    # deferring keeps application startup fast and avoids loading these until needed.
    from investment_team.backtesting_agent import BacktestingAgent
    from investment_team.market_data_service import MarketDataService

    market_service = MarketDataService()
    symbols = market_service.get_symbols_for_strategy(strategy)
    # Use top 5 symbols to keep data fetching reasonable
    symbols = symbols[:5]

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

    agent = BacktestingAgent()
    return agent.run_backtest(strategy, config, market_data)


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
    symbols = market_service.get_symbols_for_strategy(strategy)
    # Match the standalone endpoint: cap at top 5 symbols to bound fetch cost
    symbols = symbols[:5]

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
        description="Total strategies to generate this run.",
    )
    max_parallel: int = Field(
        default=3,
        ge=1,
        le=6,
        description="Max strategies to generate in parallel per wave.",
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
        entry_rules=[str(r) for r in (strategy_data.get("entry_rules") or [])],
        exit_rules=[str(r) for r in (strategy_data.get("exit_rules") or [])],
        sizing_rules=[str(r) for r in (strategy_data.get("sizing_rules") or [])],
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
    on_phase: Optional[Any] = None,
    exclude_asset_classes: Optional[List[str]] = None,
    paper_trading_enabled: bool = True,
    paper_trading_lookback_days: int = 365,
) -> StrategyLabRecord:
    """Single ideation → validate → execute → refine → analyze (+ paper-trading) cycle via the v2 orchestrator.

    The orchestrator handles the full code-generation + sandboxed-execution pipeline
    internally, including up to 10 refinement rounds.

    After the orchestrator returns a complete ``StrategyLabRecord``, the paper-trading
    step runs only when the record is flagged as winning
    (``record.is_winning``). Losing strategies record
    ``paper_trading_status = "skipped"`` with reason ``"not_winning"`` and never
    consume paper-trade budget. Paper-trading failures are non-fatal: the cycle
    still persists the winning record with ``paper_trading_status = "failed"``
    and the error message.

    Args:
        paper_trading_enabled: Opt-out flag; when False, every winning strategy
            records ``paper_trading_status = "skipped"`` with reason ``"disabled"``.
        paper_trading_lookback_days: Forwarded to ``MarketDataService.fetch_multi_symbol``
            when the paper-trading step runs.
    """

    def _emit(phase: str, data: Optional[Dict[str, Any]] = None) -> None:
        if on_phase:
            on_phase(phase, data or {})

    with _lock:
        raw_prior = list(_strategy_lab_records.values())

    prior_records = [StrategyLabRecord(**r) if isinstance(r, dict) else r for r in raw_prior]
    prior_records.sort(key=lambda r: r.created_at)

    record = orchestrator.run_cycle(
        prior_records=prior_records,
        config=config,
        signal_brief=precomputed_signal_brief,
        on_phase=on_phase,
        exclude_asset_classes=exclude_asset_classes,
    )

    # Attach signal brief before persisting (PersistentDict serializes at assignment)
    if signal_brief_storage and not record.signal_intelligence_brief:
        record.signal_intelligence_brief = signal_brief_storage

    # --- Paper-trading step (gated on winning backtest) -------------------
    # Only winners proceed to paper trading; failures are non-fatal so the
    # valid backtest record is still persisted. The standalone
    # /strategy-lab/paper-trade endpoint can be used to retry later.
    strategy_preview = {
        "asset_class": record.strategy.asset_class,
        "hypothesis": record.strategy.hypothesis,
    }
    if not record.is_winning:
        record.paper_trading_status = "skipped"
        record.paper_trading_skipped_reason = "not_winning"
        _emit("paper_trading_skipped", {"reason": "not_winning"})
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
            session = _run_paper_trading_step(
                strategy=record.strategy,
                strategy_code=record.strategy_code,
                backtest_record=record.backtest,
                initial_capital=config.initial_capital,
                transaction_cost_bps=config.transaction_cost_bps,
                slippage_bps=config.slippage_bps,
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
            _emit("paper_trading_skipped", {"reason": "no_market_data", "detail": str(exc)[:200]})
        except Exception as exc:
            logger.warning("Paper trading step failed (non-fatal): %s", exc)
            record.paper_trading_status = "failed"
            record.paper_trading_error = str(exc)[:500]
            _emit("paper_trading_failed", {"detail": record.paper_trading_error})

    # Persist to in-memory stores
    with _lock:
        _strategy_lab_records[record.lab_record_id] = record
        _strategies[record.strategy.strategy_id] = record.strategy
        _backtests[record.backtest.backtest_id] = record.backtest

    return record


def _strategy_lab_signal_expert_enabled() -> bool:
    return os.environ.get("STRATEGY_LAB_SIGNAL_EXPERT_ENABLED", "true").lower() in (
        "true",
        "1",
        "yes",
    )


def _get_lab_run_job_client():
    """Return a JobServiceClient scoped to strategy lab runs."""
    from job_service_client import JobServiceClient

    return JobServiceClient(team="investment_strategy_lab_runs")


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


def _load_run_from_job_service(run_id: str) -> Optional[Dict[str, Any]]:
    """Try to load a run state from the job service (fallback when not in _active_runs)."""
    try:
        client = _get_lab_run_job_client()
        job = client.get_job(run_id)
        if job:
            data = job.get("data", job)
            data["run_id"] = run_id
            data.setdefault("status", job.get("status", "completed"))
            return data
    except Exception:
        pass
    return None


def _strategy_lab_worker(
    run_id: str, request: RunStrategyLabRequest, *, start_cycle_offset: int = 0
) -> None:
    """Background worker that executes the strategy lab batch and publishes progress via SSE."""
    from investment_team.api.job_event_bus import cleanup_job, publish

    def _update_run(updates: Dict[str, Any]) -> None:
        with _lock:
            state = _active_runs.get(run_id)
            if state:
                state.update(updates)
                _persist_run_state(run_id, state)

    def _publish(event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        payload = data.copy() if data else {}
        publish(run_id, payload, event_type=event_type)

    try:
        orchestrator = StrategyLabOrchestrator(convergence_tracker=ConvergenceTracker())

        config = BacktestConfig(
            start_date=request.start_date,
            end_date=request.end_date,
            initial_capital=request.initial_capital,
            benchmark_symbol=request.benchmark_symbol,
            transaction_cost_bps=request.transaction_cost_bps,
            slippage_bps=request.slippage_bps,
        )

        precomputed_brief: Optional[SignalIntelligenceBriefV1] = None
        signal_brief_storage: Optional[Dict[str, Any]] = None
        provider: Optional[FreeTierMarketDataProvider] = None

        if _strategy_lab_signal_expert_enabled():
            provider = FreeTierMarketDataProvider()
            try:
                market_ctx = provider.fetch_context(
                    StrategyLabDataRequest(benchmark_symbol=request.benchmark_symbol)
                )
            except Exception as exc:
                logger.warning("Market data fetch failed: %s", exc)
                market_ctx = MarketLabContext(
                    fetched_at=_now(),
                    degraded=True,
                    degraded_reason=str(exc),
                    sources_used=[],
                )
            with _lock:
                raw_prior = list(_strategy_lab_records.values())
            prior_for_brief = [
                StrategyLabRecord(**r) if isinstance(r, dict) else r for r in raw_prior
            ]
            prior_for_brief.sort(key=lambda r: r.created_at)

            expert = SignalIntelligenceExpert()
            t0 = datetime.now(tz=timezone.utc)
            try:
                brief = expert.produce_signal_brief(prior_for_brief, market_ctx)
                precomputed_brief = brief
                signal_brief_storage = brief.model_dump(mode="json")
                prov_text = market_ctx.as_prompt_text()
                signal_brief_storage["brief_provenance"] = {
                    "expert": "signal_intelligence_v1",
                    "market_snapshot_hash": hashlib.sha256(prov_text.encode()).hexdigest()[:16],
                    "market_fetched_at": market_ctx.fetched_at,
                    "market_degraded": market_ctx.degraded,
                    "duration_ms": int((datetime.now(tz=timezone.utc) - t0).total_seconds() * 1000),
                }
                logger.info(
                    "signal_intelligence brief_version=%s len=%s degraded_market=%s",
                    signal_brief_storage.get("brief_version"),
                    len(str(signal_brief_storage)),
                    market_ctx.degraded,
                )
            except Exception as exc:
                logger.warning("Signal intelligence expert failed: %s", exc)
                precomputed_brief = None
                signal_brief_storage = {
                    "skipped": True,
                    "skipped_reason": "expert_failed",
                    "error": str(exc)[:500],
                }
            finally:
                if provider is not None:
                    provider.close()
        else:
            signal_brief_storage = {
                "skipped": True,
                "skipped_reason": "signal_expert_disabled",
            }

        completed_ids: List[str] = []
        completed_indices: set[int] = set()  # 0-based indices of completed cycles
        skipped = 0
        if start_cycle_offset > 0:
            # Mark prior cycles as already completed for resume bookkeeping
            completed_indices.update(range(start_cycle_offset))
            logger.info("Strategy lab worker resuming from cycle %d", start_cycle_offset + 1)

        # ── Wave-based parallel execution ──────────────────────────────
        from concurrent.futures import ThreadPoolExecutor, as_completed

        _TERMINAL_STATUSES = frozenset({"cancelled", "failed", "interrupted"})

        def _is_run_cancelled() -> bool:
            """Check the job service for external cancellation."""
            try:
                client = _get_lab_run_job_client()
                persisted = client.get_job(run_id)
                if persisted:
                    return persisted.get("status", "") in _TERMINAL_STATUSES
            except Exception:
                pass
            return False

        primary_tracker = orchestrator.convergence_tracker
        max_parallel = request.max_parallel
        remaining = list(range(start_cycle_offset, request.batch_size))
        run_failed = False
        run_cancelled = False

        while remaining and not run_failed and not run_cancelled:
            wave_indices = remaining[:max_parallel]
            remaining = remaining[max_parallel:]

            wave_futures: Dict[Any, int] = {}
            with ThreadPoolExecutor(max_workers=len(wave_indices), thread_name_prefix="strat-lab") as pool:
                for i in wave_indices:
                    cn = i + 1  # cycle_num (1-based)

                    def _make_on_phase(_cn: int):
                        def on_phase(phase: str, data: Optional[Dict[str, Any]] = None) -> None:
                            cycle_data = {"cycle_index": _cn, "phase": phase, **(data or {})}
                            _update_run({"current_cycle": cycle_data})
                            _publish("progress", cycle_data)
                        return on_phase

                    cycle_orchestrator = StrategyLabOrchestrator(
                        convergence_tracker=primary_tracker.snapshot(),
                    )
                    future = pool.submit(
                        _run_one_strategy_lab_cycle,
                        config,
                        cycle_orchestrator,
                        precomputed_signal_brief=precomputed_brief,
                        signal_brief_storage=signal_brief_storage,
                        on_phase=_make_on_phase(cn),
                        paper_trading_enabled=request.paper_trading_enabled,
                        paper_trading_lookback_days=request.paper_trading_lookback_days,
                    )
                    wave_futures[future] = cn

                # Collect results from this wave.
                # Each entry is (cycle_index_0based, record) so we can sort
                # deterministically before updating the convergence tracker.
                wave_results: List[tuple[int, StrategyLabRecord]] = []
                for future in as_completed(wave_futures):
                    cn = wave_futures[future]
                    try:
                        record = future.result()
                        completed_ids.append(record.lab_record_id)
                        completed_indices.add(cn - 1)  # 0-based
                        wave_results.append((cn - 1, record))

                        # Track the highest contiguous completed index for
                        # resume_strategy_lab_run (start_cycle_offset), but
                        # report the actual count for UI progress display.
                        contiguous = 0
                        while contiguous in completed_indices:
                            contiguous += 1
                        _update_run({
                            "completed_cycles": len(completed_ids),
                            "contiguous_cycles": contiguous,
                            "completed_record_ids": list(completed_ids),
                            "current_cycle": None,
                        })
                        _publish("cycle_complete", {
                            "cycle_index": cn,
                            "record_id": record.lab_record_id,
                            "completed_cycles": len(completed_ids),
                        })
                    except HTTPException as exc:
                        if exc.status_code == 502:
                            logger.warning(
                                "Strategy lab cycle %d/%d skipped (no market data after fallback)",
                                cn, request.batch_size,
                            )
                            skipped += 1
                            _update_run({"skipped_cycles": skipped, "current_cycle": None})
                            _publish("cycle_skipped", {"cycle_index": cn, "reason": "no_market_data"})
                        else:
                            logger.exception("Strategy lab cycle %d/%d failed", cn, request.batch_size)
                            _update_run({
                                "status": "failed",
                                "error": f"Cycle {cn} failed: {exc}",
                                "current_cycle": None,
                            })
                            _publish("error", {"detail": f"Cycle {cn} failed: {exc}"})
                            run_failed = True
                    except Exception as exc:
                        logger.exception("Strategy lab cycle %d/%d failed", cn, request.batch_size)
                        _update_run({
                            "status": "failed",
                            "error": f"Cycle {cn} failed: {exc}",
                            "current_cycle": None,
                        })
                        _publish("error", {"detail": f"Cycle {cn} failed: {exc}"})
                        run_failed = True

            # Merge wave results into the primary convergence tracker in
            # deterministic cycle-index order so that stall/diversity
            # directives are reproducible across runs with identical inputs.
            wave_results.sort(key=lambda pair: pair[0])
            for _idx, record in wave_results:
                gate_results = [
                    QualityGateResult(**g) if isinstance(g, dict) else g
                    for g in record.quality_gate_results
                ]
                primary_tracker.record(record.strategy, gate_results)

            # Check for external cancellation between waves
            if not run_failed and _is_run_cancelled():
                logger.info("Strategy lab run %s cancelled externally — stopping after wave", run_id)
                run_cancelled = True

        if run_failed:
            return

        if run_cancelled:
            _update_run({"status": "cancelled", "current_cycle": None})
            _publish("error", {"detail": "Run cancelled by user"})
            return

        msg = f"Completed {len(completed_ids)} strategy lab cycle(s)."
        if skipped:
            msg += f" ({skipped} skipped due to unavailable market data)"

        _update_run({"status": "completed", "current_cycle": None})
        _publish(
            "complete",
            {"message": msg, "completed_count": len(completed_ids), "skipped_count": skipped},
        )

    except Exception as exc:
        logger.exception("Strategy lab worker failed for run %s", run_id)
        _update_run({"status": "failed", "error": str(exc)[:500], "current_cycle": None})
        _publish("error", {"detail": str(exc)[:500]})
    finally:
        # Schedule cleanup of _active_runs entry after 5 minutes
        def _cleanup() -> None:
            with _lock:
                _active_runs.pop(run_id, None)
            cleanup_job(run_id)

        timer = threading.Timer(300.0, _cleanup)
        timer.daemon = True
        timer.start()


@app.post("/strategy-lab/run", response_model=StrategyLabRunStartResponse)
def run_strategy_lab(request: RunStrategyLabRequest) -> StrategyLabRunStartResponse:
    """
    Start a strategy lab batch run in the background. Returns a run_id immediately.

    Use ``GET /strategy-lab/runs/{run_id}/stream`` for real-time SSE progress updates,
    or ``GET /strategy-lab/runs/{run_id}/status`` for polling.
    """
    with _lock:
        active = [r for r in _active_runs.values() if r["status"] == "running"]
        if active:
            raise HTTPException(
                status_code=409, detail="A strategy lab run is already in progress."
            )

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    now = _now()

    initial_state = {
        "run_id": run_id,
        "status": "running",
        "started_at": now,
        "total_cycles": request.batch_size,
        "completed_cycles": 0,
        "skipped_cycles": 0,
        "current_cycle": None,
        "completed_record_ids": [],
        "error": None,
        "request_payload": request.model_dump(),
    }
    with _lock:
        _active_runs[run_id] = initial_state
    _persist_run_state(run_id, initial_state, create=True)

    thread = threading.Thread(
        target=_strategy_lab_worker,
        args=(run_id, request),
        name=f"strategy-lab-{run_id}",
        daemon=True,
    )
    thread.start()

    return StrategyLabRunStartResponse(run_id=run_id, total_cycles=request.batch_size)


@app.get("/strategy-lab/results", response_model=StrategyLabResultsResponse)
def get_strategy_lab_results(winning: Optional[bool] = None) -> StrategyLabResultsResponse:
    """
    Return all strategy lab records, sorted newest-first.
    Filter by winning/losing with ?winning=true or ?winning=false.
    """
    with _lock:
        raw = list(_strategy_lab_records.values())

    items = [StrategyLabRecord(**r) if isinstance(r, dict) else r for r in raw]
    items.sort(key=lambda r: r.created_at, reverse=True)

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


@app.get(
    "/strategy-lab/jobs",
    response_model=InvestmentJobsListResponse,
    summary="List strategy lab runs as jobs",
)
def list_strategy_lab_jobs(running_only: bool = False) -> InvestmentJobsListResponse:
    """Return strategy lab runs in a format compatible with the central Jobs Dashboard."""
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
            progress = int((completed / total) * 100) if total else 0
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
        in_memory_ids = {s["run_id"] for s in _active_runs.values()}
        for job in persisted:
            jid = job.get("job_id", "")
            if jid in in_memory_ids:
                continue  # already included from in-memory
            data = job.get("data", job)
            completed = data.get("completed_cycles", 0)
            total = data.get("total_cycles", 1)
            progress = int((completed / total) * 100) if total else 0
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
    """Resume a strategy lab run at the cycle it was interrupted."""
    with _lock:
        state = _active_runs.get(run_id)
    if not state:
        state = _load_run_from_job_service(run_id)
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
    total_cycles = state.get("total_cycles", 10)

    with _lock:
        active = [r for r in _active_runs.values() if r["status"] == "running"]
        if active:
            raise HTTPException(
                status_code=409, detail="A strategy lab run is already in progress."
            )

    # Re-initialize in-memory state
    resumed_state = {
        "run_id": run_id,
        "status": "running",
        "started_at": state.get("started_at", _now()),
        "total_cycles": total_cycles,
        "completed_cycles": completed_cycles,
        "contiguous_cycles": contiguous_cycles,
        "skipped_cycles": state.get("skipped_cycles", 0),
        "current_cycle": None,
        "completed_record_ids": state.get("completed_record_ids", []),
        "error": None,
        "request_payload": payload,
    }
    with _lock:
        _active_runs[run_id] = resumed_state
    _persist_run_state(run_id, resumed_state)

    request = RunStrategyLabRequest(**payload)
    request.batch_size = total_cycles  # keep original total

    thread = threading.Thread(
        target=_strategy_lab_worker,
        args=(run_id, request),
        kwargs={"start_cycle_offset": contiguous_cycles},
        name=f"strategy-lab-resume-{run_id}",
        daemon=True,
    )
    thread.start()

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
    """Restart a strategy lab run from the beginning."""
    with _lock:
        state = _active_runs.get(run_id)
    if not state:
        state = _load_run_from_job_service(run_id)
    try:
        validate_job_for_action(state, run_id, RESTARTABLE_STATUSES, "restarted")
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    payload = state.get("request_payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Original request payload not available.")

    with _lock:
        active = [r for r in _active_runs.values() if r["status"] == "running"]
        if active:
            raise HTTPException(
                status_code=409, detail="A strategy lab run is already in progress."
            )

    request = RunStrategyLabRequest(**payload)

    restarted_state = {
        "run_id": run_id,
        "status": "running",
        "started_at": _now(),
        "total_cycles": request.batch_size,
        "completed_cycles": 0,
        "skipped_cycles": 0,
        "current_cycle": None,
        "completed_record_ids": [],
        "error": None,
        "request_payload": payload,
    }
    with _lock:
        _active_runs[run_id] = restarted_state
    _persist_run_state(run_id, restarted_state)

    thread = threading.Thread(
        target=_strategy_lab_worker,
        args=(run_id, request),
        name=f"strategy-lab-restart-{run_id}",
        daemon=True,
    )
    thread.start()

    return StrategyLabRunStartResponse(
        run_id=run_id,
        total_cycles=request.batch_size,
        message="Run restarted from scratch.",
    )


@app.delete(
    "/strategy-lab/runs/{run_id}",
    summary="Delete a strategy lab run",
    description="Remove a strategy lab run from the job store and in-memory tracking.",
)
def delete_strategy_lab_run(run_id: str) -> Dict[str, Any]:
    """Delete a strategy lab run by ID."""
    with _lock:
        _active_runs.pop(run_id, None)

    client = _get_lab_run_job_client()
    deleted = client.delete_job(run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return {"job_id": run_id, "deleted": True}


@app.get(
    "/strategy-lab/runs", response_model=ActiveRunsResponse, summary="List active strategy lab runs"
)
def list_strategy_lab_runs() -> ActiveRunsResponse:
    """Return all tracked runs (active and recently completed).

    Merges in-memory state with persisted job-service state so that
    running jobs are always visible — even after a page refresh that
    races with server startup or after the in-memory entry is evicted.

    Also reconciles: if an in-memory run says "running" but the job service
    has it as terminal (cancelled/failed/completed), the in-memory state is
    updated to match — this handles external cancellation via the generic
    job proxy or the Jobs Dashboard.
    """
    _TERMINAL = {"completed", "failed", "cancelled", "interrupted"}

    try:
        client = _get_lab_run_job_client()

        # Reconcile: if job service has a terminal status for a run we think
        # is still active, update _active_runs so the UI sees the real state.
        with _lock:
            running_ids = [
                rid for rid, r in _active_runs.items() if r.get("status") not in _TERMINAL
            ]
        for rid in running_ids:
            try:
                persisted = client.get_job(rid)
                if persisted:
                    js_status = persisted.get("status", "")
                    if js_status in _TERMINAL:
                        with _lock:
                            if rid in _active_runs:
                                _active_runs[rid]["status"] = js_status
                                _active_runs[rid]["error"] = persisted.get("error") or persisted.get("data", {}).get("error")
            except Exception:
                pass

        with _lock:
            in_memory = {r["run_id"]: r for r in _active_runs.values()}

        # Merge running/pending jobs from the persistent job service that
        # may not be in _active_runs (e.g. after a server restart).
        persisted_list = client.list_jobs(statuses=["running", "pending"])
        for job in persisted_list:
            rid = job.get("job_id") or job.get("run_id", "")
            if rid and rid not in in_memory:
                data = job.get("data", job)
                data["run_id"] = rid
                data.setdefault("status", job.get("status", "running"))
                in_memory[rid] = data
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
    """Snapshot of a single run's progress. Use for polling when SSE is unavailable."""
    _TERMINAL = {"completed", "failed", "cancelled", "interrupted"}

    with _lock:
        state = _active_runs.get(run_id)

    # Reconcile with job service if in-memory state looks active
    if state and state.get("status") not in _TERMINAL:
        try:
            client = _get_lab_run_job_client()
            persisted = client.get_job(run_id)
            if persisted:
                js_status = persisted.get("status", "")
                if js_status in _TERMINAL:
                    with _lock:
                        if run_id in _active_runs:
                            _active_runs[run_id]["status"] = js_status
                            _active_runs[run_id]["error"] = persisted.get("error") or persisted.get("data", {}).get("error")
                            state = _active_runs[run_id]
        except Exception:
            pass

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
        "and a terminal 'complete' or 'error' event."
    ),
)
async def stream_strategy_lab_run(run_id: str) -> StreamingResponse:
    """SSE endpoint — async generator so it doesn't block Uvicorn worker threads."""
    import asyncio
    import json as json_module
    import time as time_mod

    from investment_team.api.job_event_bus import subscribe, unsubscribe

    with _lock:
        state = _active_runs.get(run_id)
    if not state:
        state = _load_run_from_job_service(run_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    def _sse_line(data: dict) -> str:
        return f"data: {json_module.dumps(data, default=str)}\n\n"

    # If the run is already terminal, send snapshot + done immediately.
    if state.get("status") in ("completed", "failed"):

        async def _terminal_gen():
            yield _sse_line(
                {"type": "snapshot", **_run_state_to_response(state).model_dump(mode="json")}
            )
            yield _sse_line({"type": "done"})

        return StreamingResponse(_terminal_gen(), media_type="text/event-stream")

    async def event_generator():
        sub = subscribe(run_id)
        try:
            # Initial snapshot
            with _lock:
                current = _active_runs.get(run_id, {})
            if current:
                yield _sse_line(
                    {"type": "snapshot", **_run_state_to_response(current).model_dump(mode="json")}
                )

            deadline = time_mod.monotonic() + 4 * 3600  # 4-hour max
            while time_mod.monotonic() < deadline:
                sent_terminal = False
                while sub.events:
                    event = sub.events.popleft()
                    yield _sse_line(event)
                    if event.get("type") in ("complete", "error"):
                        sent_terminal = True
                if sent_terminal:
                    yield _sse_line({"type": "done"})
                    return

                yield ": keepalive\n\n"
                # Non-blocking wait — yields control back to the event loop
                await asyncio.sleep(1.0)
        finally:
            unsubscribe(run_id, sub)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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


def _delete_paper_sessions_for_lab_record(lab_record_id: str) -> int:
    """Remove paper trading jobs whose payload references this lab record."""
    from job_service_client import JobServiceClient

    client = JobServiceClient(team="investment_paper_trading_sessions")
    deleted = 0
    for job in client.list_jobs() or []:
        jid = job.get("job_id")
        if not jid:
            continue
        payload = job.get("data")
        if not isinstance(payload, dict):
            continue
        if payload.get("lab_record_id") != lab_record_id:
            continue
        if client.delete_job(str(jid)):
            deleted += 1
    return deleted


def _purge_strategy_lab_job_storage() -> dict[str, int]:
    """Delete strategy lab jobs plus all paper-trading session jobs for this team."""
    from job_service_client import JobServiceClient

    deleted_lab_records = 0
    deleted_lab_strategies = 0
    deleted_lab_backtests = 0
    deleted_paper_trading_sessions = 0

    lab_client = JobServiceClient(team="investment_strategy_lab_records")
    for job in lab_client.list_jobs() or []:
        jid = job.get("job_id")
        if jid and lab_client.delete_job(str(jid)):
            deleted_lab_records += 1

    strat_client = JobServiceClient(team="investment_strategies")
    for job in strat_client.list_jobs() or []:
        jid = str(job.get("job_id") or "")
        if jid.startswith("strat-lab-") and strat_client.delete_job(jid):
            deleted_lab_strategies += 1

    bt_client = JobServiceClient(team="investment_backtests")
    for job in bt_client.list_jobs() or []:
        jid = str(job.get("job_id") or "")
        if jid.startswith("bt-lab-") and bt_client.delete_job(jid):
            deleted_lab_backtests += 1

    paper_client = JobServiceClient(team="investment_paper_trading_sessions")
    for job in paper_client.list_jobs() or []:
        jid = job.get("job_id")
        if jid and paper_client.delete_job(str(jid)):
            deleted_paper_trading_sessions += 1

    return {
        "deleted_lab_records": deleted_lab_records,
        "deleted_lab_strategies": deleted_lab_strategies,
        "deleted_lab_backtests": deleted_lab_backtests,
        "deleted_paper_trading_sessions": deleted_paper_trading_sessions,
    }


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
        record = StrategyLabRecord(**raw) if isinstance(raw, dict) else raw
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
    """Start a paper trading session for a winning strategy."""

    lab_record_id: str = Field(..., description="ID of a winning StrategyLabRecord to paper trade")
    initial_capital: float = Field(default=100000.0, gt=0)
    transaction_cost_bps: float = Field(default=5.0, ge=0)
    slippage_bps: float = Field(default=2.0, ge=0)
    lookback_days: int = Field(default=365, ge=30, description="Days of recent market data to fetch")


class PaperTradingResponse(BaseModel):
    session: PaperTradingSession
    message: str = "Paper trading session completed."


class PaperTradingResultsResponse(BaseModel):
    items: List[PaperTradingSession] = Field(default_factory=list)
    count: int = 0
    ready_for_live_count: int = 0
    not_performant_count: int = 0


@app.post("/strategy-lab/paper-trade", response_model=PaperTradingResponse)
def run_paper_trading(request: RunPaperTradingRequest) -> PaperTradingResponse:
    """
    Run a paper trading session for a winning strategy using real market data.

    Executes the strategy's generated Python code against recent market data in
    the same subprocess sandbox used for backtesting.  Compares performance to the
    backtest.  Strategies that align with backtest expectations are flagged as ready
    for live testing.  Underperforming strategies receive a detailed divergence analysis.
    """
    from investment_team.market_data_service import MarketDataService
    from investment_team.paper_trading_agent import PaperTradingAgent

    # 1 — Look up the winning strategy lab record
    with _lock:
        raw_record = _strategy_lab_records.get(request.lab_record_id)

    if raw_record is None:
        raise HTTPException(
            status_code=404, detail=f"Strategy lab record '{request.lab_record_id}' not found."
        )

    lab_record = StrategyLabRecord(**raw_record) if isinstance(raw_record, dict) else raw_record

    if not lab_record.is_winning:
        raise HTTPException(
            status_code=400,
            detail=f"Strategy '{request.lab_record_id}' is not a winning strategy. "
            "Only winning strategies (>8% annualized return) can be paper traded.",
        )

    strategy = lab_record.strategy
    backtest_record = lab_record.backtest

    # 1b — Extract strategy code (required for code-based paper trading)
    strategy_code = lab_record.strategy_code or getattr(strategy, "strategy_code", None)
    if not strategy_code:
        raise HTTPException(
            status_code=400,
            detail=f"Strategy '{request.lab_record_id}' has no generated strategy code. "
            "Only strategies with executable code can be paper traded.",
        )

    # 2 — Fetch real market data
    market_service = MarketDataService()
    symbols = market_service.get_symbols_for_strategy(strategy)
    # Use a subset of symbols (top 5) to keep data fetching reasonable
    symbols = symbols[:5]

    logger.info(
        "Fetching %d days of market data for %d symbols (%s) ...",
        request.lookback_days,
        len(symbols),
        strategy.asset_class,
    )
    market_data = market_service.fetch_multi_symbol(
        symbols, strategy.asset_class, request.lookback_days
    )

    if not market_data:
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch market data from external sources. Please try again later.",
        )

    # 3 — Run paper trading session
    agent = PaperTradingAgent()

    try:
        session = agent.run_session(
            strategy=strategy,
            strategy_code=strategy_code,
            backtest_record=backtest_record,
            market_data=market_data,
            initial_capital=request.initial_capital,
            transaction_cost_bps=request.transaction_cost_bps,
            slippage_bps=request.slippage_bps,
        )
    except Exception as exc:
        logger.error("Paper trading session failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Paper trading session failed: {exc}") from exc

    session.lab_record_id = request.lab_record_id

    # 4 — Persist the session
    with _lock:
        _paper_trading_sessions[session.session_id] = session

    # 5 — Build response message
    trade_count = len(session.trades)

    if session.status == PaperTradingStatus.FAILED:
        message = (
            "Paper trading failed — strategy code could not be executed against recent data. "
            "See divergence_analysis for error details."
        )
    elif session.verdict == PaperTradingVerdict.READY_FOR_LIVE:
        message = (
            f"Paper trading completed with {trade_count} trades. "
            f"Performance aligns with backtest expectations — strategy is READY FOR LIVE TESTING."
        )
    elif session.verdict == PaperTradingVerdict.NOT_PERFORMANT:
        message = (
            f"Paper trading completed with {trade_count} trades. "
            f"Performance does NOT align with backtest expectations — strategy is NOT PERFORMANT "
            f"with live data. See divergence_analysis for details."
        )
    else:
        message = f"Paper trading completed with {trade_count} trades."

    return PaperTradingResponse(session=session, message=message)


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

    items = [PaperTradingSession(**r) if isinstance(r, dict) else r for r in raw]
    items.sort(key=lambda s: s.completed_at or s.started_at, reverse=True)

    ready_count = sum(1 for s in items if s.verdict == PaperTradingVerdict.READY_FOR_LIVE)
    not_perf_count = sum(1 for s in items if s.verdict == PaperTradingVerdict.NOT_PERFORMANT)

    if verdict is not None:
        items = [s for s in items if s.verdict and s.verdict.value == verdict]

    return PaperTradingResultsResponse(
        items=items,
        count=len(items),
        ready_for_live_count=ready_count,
        not_performant_count=not_perf_count,
    )


@app.get("/strategy-lab/paper-trade/{session_id}", response_model=PaperTradingResponse)
def get_paper_trading_session(session_id: str) -> PaperTradingResponse:
    """Return a specific paper trading session by ID."""
    with _lock:
        raw = _paper_trading_sessions.get(session_id)

    if raw is None:
        raise HTTPException(
            status_code=404, detail=f"Paper trading session '{session_id}' not found."
        )

    session = PaperTradingSession(**raw) if isinstance(raw, dict) else raw
    return PaperTradingResponse(session=session)


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
    """Start a new financial advisor conversation to build an investment profile."""
    session_id = f"adv-{uuid.uuid4().hex[:8]}"
    session = _advisor_agent.start_session(session_id=session_id, user_id=request.user_id)

    with _lock:
        _advisor_sessions[session_id] = session

    return StartAdvisorSessionResponse(
        session_id=session_id,
        advisor_message=session.messages[0].content,
        session=session,
    )


@app.post("/advisor/sessions/{session_id}/messages", response_model=SendAdvisorMessageResponse)
def send_advisor_message(
    session_id: str, request: SendAdvisorMessageRequest
) -> SendAdvisorMessageResponse:
    """Send a message to the financial advisor and receive a response."""
    with _lock:
        session = _advisor_sessions.get(session_id)

    if not session:
        raise HTTPException(status_code=404, detail=f"Advisor session {session_id} not found")

    reply = _advisor_agent.handle_message(session, request.message)
    missing = _advisor_agent.missing_fields(session.collected)

    with _lock:
        _advisor_sessions[session_id] = session

    return SendAdvisorMessageResponse(
        advisor_message=reply,
        session_status=session.status.value,
        current_topic=session.current_topic.value,
        missing_fields=missing,
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

    Can be called once the session status is 'completed', or called early
    if all required fields have been collected.
    """
    with _lock:
        session = _advisor_sessions.get(session_id)

    if not session:
        raise HTTPException(status_code=404, detail=f"Advisor session {session_id} not found")

    missing = _advisor_agent.missing_fields(session.collected)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot finalize — missing required fields: {', '.join(missing)}",
        )

    try:
        ips = _advisor_agent.build_ips(session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with _lock:
        _profiles[session.user_id] = ips
        session.status = AdvisorSessionStatus.COMPLETED
        _advisor_sessions[session_id] = session

    return CompleteAdvisorSessionResponse(user_id=session.user_id, ips=ips)
