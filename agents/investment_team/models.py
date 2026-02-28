"""Data models for the multi-asset investment organization."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RiskTolerance(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


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
