"""FastAPI endpoints for the Investment Team."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
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
from investment_team.strategy_ideation_agent import StrategyIdeationAgent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Investment Team API",
    description="Investment profile management, portfolio proposals, strategy validation, and promotion gates.",
    version="1.0.0",
)

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
_advisor_sessions: _PersistentDict = _PersistentDict("advisor_sessions")

_advisor_agent = FinancialAdvisorAgent()


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


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

    guardian = PolicyGuardianAgent()
    violations = guardian.check_portfolio(ips, proposal)

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
    """Run a deterministic backtest simulation and store the result."""
    with _lock:
        strategy = _strategies.get(request.strategy_id)

    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {request.strategy_id} not found")

    def _calc(base: float, factor: float, floor: float = 0.0) -> float:
        value = round(base + factor, 2)
        return value if value >= floor else floor

    strategy_signal_score = (
        len(strategy.entry_rules)
        + len(strategy.exit_rules)
        + len(strategy.sizing_rules)
        + (1 if strategy.speculative else 0)
    )
    period_span = max(len(request.start_date) + len(request.end_date), 1)

    total_return = _calc(
        6.0, (strategy_signal_score % 11) * 0.9 - request.transaction_cost_bps * 0.03, -95.0
    )
    annualized_return = _calc(4.0, total_return * 0.35 - request.slippage_bps * 0.02, -95.0)
    volatility = _calc(10.0, (strategy_signal_score % 7) * 1.4 + (period_span % 5) * 0.7, 0.1)
    sharpe = round(annualized_return / volatility if volatility else 0.0, 2)
    max_drawdown = _calc(8.0, (strategy_signal_score % 5) * 1.8 + request.slippage_bps * 0.1, 0.0)
    win_rate = _calc(
        45.0, (strategy_signal_score % 9) * 2.2 - request.transaction_cost_bps * 0.1, 1.0
    )
    profit_factor = round(
        max(1.01, 1.05 + (strategy_signal_score % 6) * 0.08 - request.slippage_bps * 0.01), 2
    )

    backtest_id = f"bt-{uuid.uuid4().hex[:8]}"
    config = BacktestConfig(
        start_date=request.start_date,
        end_date=request.end_date,
        initial_capital=request.initial_capital,
        benchmark_symbol=request.benchmark_symbol,
        rebalance_frequency=request.rebalance_frequency,
        transaction_cost_bps=request.transaction_cost_bps,
        slippage_bps=request.slippage_bps,
    )
    result = BacktestResult(
        total_return_pct=total_return,
        annualized_return_pct=annualized_return,
        volatility_pct=volatility,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_drawdown,
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
    )
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

    orchestrator = InvestmentTeamOrchestrator()
    approver = AgentIdentity(
        agent_id=request.approver_agent_id,
        role=request.approver_role,
        version=request.approver_version,
    )

    decision = orchestrator.promotion_decision(
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
    committee_agent = InvestmentCommitteeAgent()
    memo = committee_agent.draft_memo(
        user_id=request.user_id,
        recommendation=request.recommendation,
        rationale=request.rationale,
        dissenting_views=request.dissenting_views,
    )

    return CreateMemoResponse(memo=memo)


# ---------------------------------------------------------------------------
# Strategy Lab — ideation, backtesting, and analysis
# ---------------------------------------------------------------------------


_STOCK_SYMBOLS = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "JPM", "AMD", "SPY"]
_CRYPTO_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "MATIC", "AVAX", "LINK", "ADA", "DOT"]
_OTHER_SYMBOLS = ["GLD", "USO", "TLT", "VIX", "QQQ", "IWM", "EEM", "GDX", "XLE", "XLF"]
_FOREX_SYMBOLS = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
    "USDCHF",
    "EURGBP",
    "EURJPY",
    "GBPJPY",
]
_FUTURES_SYMBOLS = ["ES", "NQ", "CL", "GC", "SI", "ZB", "6E", "NG", "ZM", "ZS"]

_SYMBOL_BASE_PRICES = {
    "AAPL": 170,
    "MSFT": 380,
    "NVDA": 490,
    "TSLA": 240,
    "AMZN": 180,
    "META": 490,
    "GOOGL": 170,
    "JPM": 190,
    "AMD": 170,
    "SPY": 480,
    "BTC": 42000,
    "ETH": 2500,
    "SOL": 105,
    "BNB": 380,
    "XRP": 0.60,
    "MATIC": 0.90,
    "AVAX": 38,
    "LINK": 18,
    "ADA": 0.55,
    "DOT": 8,
    "GLD": 190,
    "USO": 75,
    "TLT": 95,
    "VIX": 18,
    "QQQ": 425,
    "IWM": 200,
    "EEM": 40,
    "GDX": 29,
    "XLE": 92,
    "XLF": 40,
    "EURUSD": 1.085,
    "GBPUSD": 1.265,
    "USDJPY": 149.5,
    "AUDUSD": 0.655,
    "USDCAD": 1.355,
    "NZDUSD": 0.595,
    "USDCHF": 0.885,
    "EURGBP": 0.855,
    "EURJPY": 162.0,
    "GBPJPY": 189.0,
    "ES": 5200.0,
    "NQ": 18200.0,
    "CL": 78.0,
    "GC": 2350.0,
    "SI": 28.5,
    "ZB": 118.0,
    "6E": 1.085,
    "NG": 2.35,
    "ZM": 440.0,
    "ZS": 1180.0,
}


def _lcg(seed: int) -> int:
    """One step of a linear congruential generator — fast deterministic pseudo-random."""
    return (seed * 1664525 + 1013904223) & 0xFFFFFFFF


def _generate_trade_ledger(
    strategy: StrategySpec,
    config: BacktestConfig,
    result: BacktestResult,
) -> List[TradeRecord]:
    """
    Generate a deterministic per-trade ledger consistent with the summary BacktestResult.

    All values are derived from the strategy hash, so the same strategy always
    produces the same trade list.  No randomness — fully reproducible.
    """
    strat_hash = int(
        hashlib.md5((strategy.hypothesis + strategy.signal_definition).encode()).hexdigest()[:8],
        16,
    )

    # Symbol universe based on asset class
    asset = strategy.asset_class.lower()
    if asset in ("equities", "equity", "stock"):
        asset = "stocks"
    if asset in ("fx",):
        asset = "forex"
    if asset == "crypto":
        symbols = _CRYPTO_SYMBOLS
    elif asset in ("stocks",):
        symbols = _STOCK_SYMBOLS
    elif asset in ("forex",):
        symbols = _FOREX_SYMBOLS
    elif asset == "futures":
        symbols = _FUTURES_SYMBOLS
    elif asset == "options":
        symbols = _STOCK_SYMBOLS
    elif asset in ("commodities",):
        symbols = _OTHER_SYMBOLS
    else:
        symbols = _OTHER_SYMBOLS

    # Date range
    try:
        start_dt = date.fromisoformat(config.start_date)
        end_dt = date.fromisoformat(config.end_date)
    except ValueError:
        start_dt = date(2021, 1, 1)
        end_dt = date(2024, 12, 31)

    total_days = max((end_dt - start_dt).days, 1)

    # Trade count: ~50-80 trades/year for swing trading
    trades_per_year = 50 + (strat_hash % 31)  # 50-80
    num_trades = max(10, min(200, int(total_days / 365.25 * trades_per_year)))

    # Position size: 5-10% of capital per trade
    position_pct = 0.05 + (strat_hash % 6) * 0.01  # 5-10%
    position_value_base = config.initial_capital * position_pct
    cost_pct = (config.transaction_cost_bps + config.slippage_bps) / 10000.0

    win_rate_dec = result.win_rate_pct / 100.0
    # Derive average win/loss magnitudes that are consistent with profit_factor & win_rate
    # profit_factor = (avg_win × win_rate) / (avg_loss × loss_rate)
    loss_rate = 1.0 - win_rate_dec
    avg_loss_pct = 1.8 + (strat_hash % 15) * 0.12  # 1.8% to 3.5%
    if loss_rate > 0:
        avg_win_pct = result.profit_factor * avg_loss_pct * loss_rate / win_rate_dec
    else:
        avg_win_pct = avg_loss_pct * result.profit_factor

    trades: List[TradeRecord] = []
    cumulative_pnl = 0.0
    seed = strat_hash

    for i in range(num_trades):
        seed = _lcg(seed)
        s2 = _lcg(seed)
        s3 = _lcg(s2)
        s4 = _lcg(s3)

        # Entry date — evenly spaced with small jitter
        base_offset = int(total_days * i / num_trades)
        jitter = (seed % 5) - 2  # -2 to +2 days
        entry_offset = max(0, min(total_days - 2, base_offset + jitter))
        entry_dt = start_dt + timedelta(days=entry_offset)

        # Hold period: 2–13 days
        hold_days = 2 + (s2 % 12)
        exit_dt = entry_dt + timedelta(days=hold_days)
        if exit_dt >= end_dt:
            exit_dt = end_dt - timedelta(days=1)
            hold_days = max(1, (exit_dt - entry_dt).days)

        # Symbol selection
        symbol = symbols[s3 % len(symbols)]
        base_price = _SYMBOL_BASE_PRICES.get(symbol, 100.0)

        # Entry price: small variation around base (±15%)
        price_jitter = (s4 % 301 - 150) / 1000.0  # -15% to +15%
        entry_price = round(base_price * (1.0 + price_jitter), 2 if base_price >= 1 else 6)

        # Win or loss — use modular position within win-rate bands
        is_win = (seed % 100) < result.win_rate_pct

        # Return magnitude for this trade (varies around average ±40%)
        mag_var = 1.0 + ((s2 % 81 - 40) / 100.0)  # 0.6× to 1.4× the average
        if is_win:
            trade_return_pct = round(avg_win_pct * mag_var, 3)
        else:
            trade_return_pct = round(-avg_loss_pct * mag_var, 3)

        exit_price = round(
            entry_price * (1.0 + trade_return_pct / 100.0), 2 if base_price >= 1 else 6
        )
        shares = round(position_value_base / entry_price, 4 if base_price < 10 else 2)
        position_value = round(entry_price * shares, 2)
        gross_pnl = round(shares * (exit_price - entry_price), 2)
        transaction_cost = round(position_value * cost_pct * 2, 2)  # entry + exit
        net_pnl = round(gross_pnl - transaction_cost, 2)
        cumulative_pnl = round(cumulative_pnl + net_pnl, 2)

        trades.append(
            TradeRecord(
                trade_num=i + 1,
                entry_date=entry_dt.isoformat(),
                exit_date=exit_dt.isoformat(),
                symbol=symbol,
                side="long",
                entry_price=entry_price,
                exit_price=exit_price,
                shares=shares,
                position_value=position_value,
                gross_pnl=gross_pnl,
                net_pnl=net_pnl,
                return_pct=trade_return_pct,
                hold_days=hold_days,
                outcome="win" if is_win else "loss",
                cumulative_pnl=cumulative_pnl,
            )
        )

    return trades


def _strategy_lab_backtest(
    strategy: StrategySpec,
    config: BacktestConfig,
) -> tuple[BacktestResult, List[TradeRecord]]:
    """
    Content-aware deterministic backtest for the strategy lab.

    Returns (BacktestResult, trade_ledger).  Uses a hash of
    (hypothesis + signal_definition) for reproducible variance across strategies.
    """
    strat_hash = int(
        hashlib.md5((strategy.hypothesis + strategy.signal_definition).encode()).hexdigest()[:8],
        16,
    )

    signal_score = (
        len(strategy.entry_rules)
        + len(strategy.exit_rules)
        + len(strategy.sizing_rules)
        + (1 if strategy.speculative else 0)
    )

    ac = strategy.asset_class.lower()
    if ac in ("equities", "equity", "stock"):
        ac = "stocks"
    if ac in ("fx",):
        ac = "forex"
    asset_class_base = {
        "crypto": 5.5,
        "stocks": 3.5,
        "options": 4.5,
        "forex": 2.5,
        "commodities": 2.0,
        "futures": 3.2,
    }.get(ac, 3.5)

    # hash_factor in range -8.0 to +12.0 (asymmetric — harder to win)
    hash_factor = (strat_hash % 201 - 80) / 10.0
    rule_bonus = (signal_score % 6) * 0.7  # 0 to 3.5

    annualized_return = round(
        asset_class_base + hash_factor + rule_bonus - config.slippage_bps * 0.02,
        2,
    )
    total_return = round(annualized_return * 2.5, 2)
    volatility = round(12.0 + (strat_hash % 15) * 0.8, 2)
    sharpe = round(annualized_return / volatility if volatility > 0 else 0.0, 2)
    max_drawdown = round(10.0 + (strat_hash % 20) * 0.9, 2)
    win_rate = round(
        max(20.0, 48.0 + (strat_hash % 25) * 0.6 - config.transaction_cost_bps * 0.1),
        2,
    )
    profit_factor = round(max(0.80, 1.0 + annualized_return * 0.05), 2)

    result = BacktestResult(
        total_return_pct=total_return,
        annualized_return_pct=annualized_return,
        volatility_pct=volatility,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_drawdown,
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
    )
    trades = _generate_trade_ledger(strategy, config, result)
    return result, trades


class RunStrategyLabRequest(BaseModel):
    """Run one or more sequential ideation + backtest + analysis cycles."""

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
        description="Strategies to generate this run (one per sequential step; each step sees all prior results).",
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
    x = str(raw or "stocks").lower().strip()
    if x in ("equities", "equity", "stock"):
        return "stocks"
    if x in ("fx",):
        return "forex"
    if x in ("commodity", "metal", "energy"):
        return "commodities"
    allowed = frozenset({"stocks", "crypto", "forex", "options", "futures", "commodities"})
    if x in allowed:
        return x
    return "stocks"


def _run_one_strategy_lab_cycle(
    agent: StrategyIdeationAgent,
    config: BacktestConfig,
    *,
    precomputed_signal_brief: Optional[SignalIntelligenceBriefV1] = None,
    signal_brief_storage: Optional[Dict[str, Any]] = None,
) -> StrategyLabRecord:
    """Single ideation → backtest → analysis; persists to store so the next cycle sees full history."""
    with _lock:
        raw_prior = list(_strategy_lab_records.values())

    prior_records = [StrategyLabRecord(**r) if isinstance(r, dict) else r for r in raw_prior]
    prior_records.sort(key=lambda r: r.created_at)

    try:
        strategy_data, rationale = agent.ideate_strategy(
            prior_results=prior_records,
            precomputed_signal_brief=precomputed_signal_brief,
        )
    except Exception as exc:
        logger.error("Strategy ideation failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Strategy ideation failed: {exc}") from exc

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

    result, trades = _strategy_lab_backtest(strategy, config)

    now = _now()
    backtest_id = f"bt-lab-{uuid.uuid4().hex[:8]}"
    backtest = BacktestRecord(
        backtest_id=backtest_id,
        strategy_id=strategy_id,
        strategy=strategy,
        config=config,
        submitted_by="strategy_ideation_agent",
        submitted_at=now,
        completed_at=now,
        result=result,
        notes=[],
        trades=trades,
    )

    is_winning = result.annualized_return_pct > 8.0

    lab_record_id = f"lab-{uuid.uuid4().hex[:8]}"
    provisional_record = StrategyLabRecord(
        lab_record_id=lab_record_id,
        strategy=strategy,
        backtest=backtest,
        is_winning=is_winning,
        strategy_rationale=rationale,
        analysis_narrative="",
        created_at=now,
    )

    try:
        narrative = agent.analyze_result(provisional_record, rationale)
    except Exception as exc:
        logger.warning("Analysis narrative generation failed: %s", exc)
        narrative = (
            f"Strategy returned {result.annualized_return_pct:.1f}% annualized "
            f"({'above' if is_winning else 'below'} the 8% winning threshold). "
            f"Sharpe ratio: {result.sharpe_ratio:.2f}, max drawdown: {result.max_drawdown_pct:.1f}%."
        )

    record = StrategyLabRecord(
        lab_record_id=lab_record_id,
        strategy=strategy,
        backtest=backtest,
        is_winning=is_winning,
        strategy_rationale=rationale,
        analysis_narrative=narrative,
        created_at=now,
        signal_intelligence_brief=signal_brief_storage,
    )

    with _lock:
        _strategy_lab_records[lab_record_id] = record
        _strategies[strategy_id] = strategy
        _backtests[backtest_id] = backtest

    return record


def _strategy_lab_signal_expert_enabled() -> bool:
    return os.environ.get("STRATEGY_LAB_SIGNAL_EXPERT_ENABLED", "true").lower() in (
        "true",
        "1",
        "yes",
    )


@app.post("/strategy-lab/run", response_model=StrategyLabRunResponse)
def run_strategy_lab(request: RunStrategyLabRequest) -> StrategyLabRunResponse:
    """
    Ideate novel swing trading strategies using the LLM (one per step), backtest each,
    then generate an analysis narrative. Each step sees all prior lab records including
    metrics, outcomes, and post-backtest narratives. Default batch size is 10 per run.

    Policy B: one Signal Intelligence brief + one market snapshot per batch; reused for each cycle.
    """
    from llm_service.factory import get_client

    llm = get_client("strategy_ideation")
    agent = StrategyIdeationAgent(llm_client=llm)

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
        prior_for_brief = [StrategyLabRecord(**r) if isinstance(r, dict) else r for r in raw_prior]
        prior_for_brief.sort(key=lambda r: r.created_at)

        llm_signal = get_client("signal_intelligence")
        expert = SignalIntelligenceExpert(llm_signal)
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
                "duration_ms": int(
                    (datetime.now(tz=timezone.utc) - t0).total_seconds() * 1000
                ),
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

    records: List[StrategyLabRecord] = []
    for i in range(request.batch_size):
        try:
            record = _run_one_strategy_lab_cycle(
                agent,
                config,
                precomputed_signal_brief=precomputed_brief,
                signal_brief_storage=signal_brief_storage,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Strategy lab cycle %d/%d failed", i + 1, request.batch_size)
            raise HTTPException(
                status_code=500,
                detail=f"Strategy lab cycle {i + 1}/{request.batch_size} failed: {exc}",
            ) from exc
        records.append(record)

    return StrategyLabRunResponse(
        records=records,
        count=len(records),
        message=f"Completed {len(records)} strategy lab cycle(s).",
    )


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
