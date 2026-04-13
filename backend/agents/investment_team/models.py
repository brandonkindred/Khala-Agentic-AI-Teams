"""Data models for the multi-asset investment organization."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class RiskTolerance(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class AdvisorSessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class AdvisorTopic(str, Enum):
    """Conversation topics the advisor walks through to build an InvestmentProfile."""

    GREETING = "greeting"
    RISK_TOLERANCE = "risk_tolerance"
    TIME_HORIZON = "time_horizon"
    INCOME = "income"
    NET_WORTH = "net_worth"
    SAVINGS = "savings"
    TAX = "tax"
    LIQUIDITY = "liquidity"
    GOALS = "goals"
    PREFERENCES = "preferences"
    CONSTRAINTS = "constraints"
    TRADING_PREFERENCES = "trading_preferences"
    REVIEW = "review"


class PromotionStage(str, Enum):
    REJECT = "reject"
    REVISE = "revise"
    PAPER = "paper"
    LIVE = "live"


class ValidationStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class WorkflowMode(str, Enum):
    ADVISORY = "advisory"
    PAPER = "paper"
    LIVE = "live"
    MONITOR_ONLY = "monitor_only"


class PromotionGate(str, Enum):
    SEPARATION_OF_DUTIES = "separation_of_duties"
    RISK_VETO = "risk_veto"
    VALIDATION = "validation"
    IPS_PERMISSION = "ips_permission"
    HUMAN_APPROVAL = "human_approval"


class GateResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


class AuditContext(BaseModel):
    data_snapshot_id: str = ""
    assumptions: List[str] = Field(default_factory=list)
    calc_artifacts: List[str] = Field(default_factory=list)
    gate_trace: List[str] = Field(default_factory=list)
    agent_versions: Dict[str, str] = Field(default_factory=dict)


class PlannedLargeExpense(BaseModel):
    name: str
    amount: float
    date: str


class LiquidityNeeds(BaseModel):
    emergency_fund_months: int = 6
    planned_large_expenses: List[PlannedLargeExpense] = Field(default_factory=list)


class IncomeProfile(BaseModel):
    annual_gross: float
    stability: str


class NetWorth(BaseModel):
    total: float
    investable_assets: float


class SavingsRate(BaseModel):
    monthly: float
    annual: float


class TaxProfile(BaseModel):
    country: str
    state: str = ""
    account_types: List[str] = Field(default_factory=list)


class UserPreferences(BaseModel):
    excluded_asset_classes: List[str] = Field(default_factory=list)
    excluded_industries: List[str] = Field(default_factory=list)
    esg_preference: str = "none"
    crypto_allowed: bool = True
    options_allowed: bool = True
    leverage_allowed: bool = False


class UserGoal(BaseModel):
    name: str
    target_amount: float
    target_date: str
    priority: str


class PortfolioConstraints(BaseModel):
    max_single_position_pct: float = 10
    max_asset_class_pct: Dict[str, float] = Field(default_factory=dict)


class InvestmentProfile(BaseModel):
    schema_version: str = "1.0"
    user_id: str
    created_at: str
    risk_tolerance: RiskTolerance
    max_drawdown_tolerance_pct: float
    time_horizon_years: int
    liquidity_needs: LiquidityNeeds
    income: IncomeProfile
    net_worth: NetWorth
    savings_rate: SavingsRate
    tax_profile: TaxProfile
    preferences: UserPreferences
    goals: List[UserGoal] = Field(default_factory=list)
    constraints: PortfolioConstraints


class IPS(BaseModel):
    profile: InvestmentProfile
    live_trading_enabled: bool = False
    human_approval_required_for_live: bool = True
    speculative_sleeve_cap_pct: float = 10
    rebalance_frequency: str = "quarterly"
    default_mode: WorkflowMode = WorkflowMode.MONITOR_ONLY
    notes: List[str] = Field(default_factory=list)


class AssetUniverse(BaseModel):
    as_of: str
    allowed_assets: List[str] = Field(default_factory=list)
    banned_assets: List[str] = Field(default_factory=list)
    data_snapshot_id: str


class PortfolioPosition(BaseModel):
    symbol: str
    asset_class: str
    weight_pct: float
    rationale: str


class PortfolioProposal(BaseModel):
    proposal_id: str
    prepared_by: str
    ips_version: str
    data_snapshot_id: str
    objective: str
    positions: List[PortfolioPosition]
    expected_return_pct: Optional[float] = None
    expected_volatility_pct: Optional[float] = None
    expected_max_drawdown_pct: Optional[float] = None
    assumptions: List[str] = Field(default_factory=list)
    audit: AuditContext = Field(default_factory=AuditContext)


class StrategySpec(BaseModel):
    strategy_id: str
    authored_by: str
    asset_class: str
    hypothesis: str
    signal_definition: str
    entry_rules: List[str] = Field(default_factory=list)
    exit_rules: List[str] = Field(default_factory=list)
    sizing_rules: List[str] = Field(default_factory=list)
    risk_limits: Dict[str, Any] = Field(default_factory=dict)
    speculative: bool = False
    strategy_code: Optional[str] = None
    audit: AuditContext = Field(default_factory=AuditContext)


class ValidationCheck(BaseModel):
    name: str
    status: ValidationStatus
    details: str


class ValidationReport(BaseModel):
    strategy_id: str
    generated_by: str
    data_snapshot_id: str
    backtest_period: str
    scenario_set: List[str] = Field(default_factory=list)
    checks: List[ValidationCheck] = Field(default_factory=list)
    summary: str = ""
    audit: AuditContext = Field(default_factory=AuditContext)


class BacktestConfig(BaseModel):
    start_date: str
    end_date: str
    initial_capital: float = Field(default=100000.0, gt=0)
    benchmark_symbol: str = "SPY"
    rebalance_frequency: str = "monthly"
    transaction_cost_bps: float = Field(default=5.0, ge=0)
    slippage_bps: float = Field(default=2.0, ge=0)


class BacktestResult(BaseModel):
    total_return_pct: float
    annualized_return_pct: float
    volatility_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float


class TradeRecord(BaseModel):
    """A single simulated trade from a backtest."""

    trade_num: int
    entry_date: str
    exit_date: str
    symbol: str
    side: str  # "long" or "short"
    entry_price: float
    exit_price: float
    shares: float
    position_value: float  # entry_price × shares
    gross_pnl: float
    net_pnl: float  # after transaction costs & slippage
    return_pct: float
    hold_days: int
    outcome: str  # "win" or "loss"
    cumulative_pnl: float  # running total net P&L


class BacktestRecord(BaseModel):
    backtest_id: str
    strategy_id: str
    strategy: StrategySpec
    config: BacktestConfig
    submitted_by: str
    submitted_at: str
    completed_at: str
    status: str = "completed"
    result: BacktestResult
    notes: List[str] = Field(default_factory=list)
    trades: List[TradeRecord] = Field(default_factory=list)


class GateCheckResult(BaseModel):
    gate: PromotionGate
    result: GateResult
    details: str


class PromotionDecision(BaseModel):
    strategy_id: str
    decided_by: str
    outcome: PromotionStage
    rationale: str
    required_actions: List[str] = Field(default_factory=list)
    gate_results: List[GateCheckResult] = Field(default_factory=list)
    audit: AuditContext = Field(default_factory=AuditContext)


class OrderIntent(BaseModel):
    strategy_id: str
    symbol: str
    side: str
    qty: float
    order_type: str
    risk_context: Dict[str, Any] = Field(default_factory=dict)


class ExecutionReport(BaseModel):
    strategy_id: str
    broker_order_id: str
    status: str
    avg_fill_price: Optional[float] = None
    slippage_bps: Optional[float] = None
    reconciled: bool = False


class DealCard(BaseModel):
    deal_id: str
    source: str
    sector: str
    asking_price: float
    revenue: Optional[float] = None
    ebitda: Optional[float] = None


class UnderwritingSummary(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    deal_id: str
    model_version: str
    base_case_irr_pct: float
    downside_case_irr_pct: float
    key_risks: List[str] = Field(default_factory=list)


class DiligenceFindings(BaseModel):
    deal_id: str
    findings: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)


class InvestmentCommitteeMemo(BaseModel):
    memo_id: str
    prepared_for_user_id: str
    recommendation: str
    rationale: str
    dissenting_views: List[str] = Field(default_factory=list)
    attachments: List[str] = Field(default_factory=list)
    audit: AuditContext = Field(default_factory=AuditContext)


class StrategyLabRecord(BaseModel):
    """Result of one strategy ideation + backtest + analysis cycle."""

    lab_record_id: str
    strategy: StrategySpec
    backtest: BacktestRecord
    is_winning: bool  # annualized_return_pct > 8.0
    strategy_rationale: str  # why the agent chose this strategy
    analysis_narrative: str  # LLM post-backtest analysis
    created_at: str
    refinement_rounds: int = 0
    quality_gate_results: List[Dict[str, Any]] = Field(default_factory=list)
    strategy_code: Optional[str] = None
    signal_intelligence_brief: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Signal Intelligence Expert JSON (brief_version, themes, …) or skipped metadata; null for legacy rows.",
    )


# ---------------------------------------------------------------------------
# Paper Trading models
# ---------------------------------------------------------------------------


class PaperTradingStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PaperTradingVerdict(str, Enum):
    READY_FOR_LIVE = "ready_for_live"
    NOT_PERFORMANT = "not_performant"


class PaperTradingComparison(BaseModel):
    """Side-by-side comparison of paper trading vs backtest metrics."""

    backtest_win_rate_pct: float
    paper_win_rate_pct: float
    backtest_annualized_return_pct: float
    paper_annualized_return_pct: float
    backtest_sharpe_ratio: float
    paper_sharpe_ratio: float
    backtest_max_drawdown_pct: float
    paper_max_drawdown_pct: float
    backtest_profit_factor: float
    paper_profit_factor: float
    win_rate_aligned: bool
    return_aligned: bool
    sharpe_aligned: bool
    drawdown_aligned: bool
    profit_factor_aligned: bool = True
    overall_aligned: bool


class PaperTradingSession(BaseModel):
    """Full state of a paper trading session."""

    session_id: str
    lab_record_id: str
    strategy: StrategySpec
    status: PaperTradingStatus
    initial_capital: float
    current_capital: float
    trades: List[TradeRecord] = Field(default_factory=list)
    trade_decisions: List[Dict[str, Any]] = Field(default_factory=list)
    result: Optional[BacktestResult] = None
    comparison: Optional[PaperTradingComparison] = None
    verdict: Optional[PaperTradingVerdict] = None
    divergence_analysis: Optional[str] = None
    symbols_traded: List[str] = Field(default_factory=list)
    data_source: str = ""
    data_period_start: str = ""
    data_period_end: str = ""
    started_at: str = ""
    completed_at: str = ""


# ---------------------------------------------------------------------------
# Financial Advisor chatbot models
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    """A single message in an advisor conversation."""

    role: str  # "user" or "advisor"
    content: str
    timestamp: str


class CollectedProfileData(BaseModel):
    """Partial profile data accumulated during the advisor conversation."""

    risk_tolerance: Optional[str] = None
    max_drawdown_tolerance_pct: Optional[float] = None
    time_horizon_years: Optional[int] = None
    annual_gross_income: Optional[float] = None
    income_stability: Optional[str] = None
    total_net_worth: Optional[float] = None
    investable_assets: Optional[float] = None
    monthly_savings: Optional[float] = None
    annual_savings: Optional[float] = None
    tax_country: Optional[str] = None
    tax_state: Optional[str] = None
    account_types: List[str] = Field(default_factory=list)
    emergency_fund_months: Optional[int] = None
    planned_large_expenses: List[PlannedLargeExpense] = Field(default_factory=list)
    goals: List[UserGoal] = Field(default_factory=list)
    excluded_asset_classes: List[str] = Field(default_factory=list)
    excluded_industries: List[str] = Field(default_factory=list)
    esg_preference: Optional[str] = None
    crypto_allowed: Optional[bool] = None
    options_allowed: Optional[bool] = None
    leverage_allowed: Optional[bool] = None
    max_single_position_pct: Optional[float] = None
    max_asset_class_pct: Dict[str, float] = Field(default_factory=dict)
    live_trading_enabled: Optional[bool] = None
    human_approval_required_for_live: Optional[bool] = None
    speculative_sleeve_cap_pct: Optional[float] = None
    rebalance_frequency: Optional[str] = None
    default_mode: Optional[str] = None


class AdvisorSession(BaseModel):
    """State of a financial advisor conversation."""

    session_id: str
    user_id: str
    status: AdvisorSessionStatus = AdvisorSessionStatus.ACTIVE
    current_topic: AdvisorTopic = AdvisorTopic.GREETING
    messages: List[ChatMessage] = Field(default_factory=list)
    collected: CollectedProfileData = Field(default_factory=CollectedProfileData)
    created_at: str
    updated_at: str
