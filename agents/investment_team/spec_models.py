"""Codex-friendly implementation models for the multi-asset investment organization spec."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class StrategyValidationType(str, Enum):
    BACKTEST = "backtest"
    WALK_FORWARD = "walk_forward"
    STRESS_TEST = "stress_test"
    PAPER_TRADE = "paper_trade"
    SCENARIO_UNDERWRITE = "scenario_underwrite"
    DILIGENCE_VALIDATION = "diligence_validation"


class PromotionSubjectType(str, Enum):
    STRATEGY = "strategy"
    PORTFOLIO = "portfolio"
    DEAL = "deal"


class PromotionStageV1(str, Enum):
    DESIGN = "design"
    BACKTEST = "backtest"
    WALK_FORWARD = "walk_forward"
    PAPER = "paper"
    LIVE_CANDIDATE = "live_candidate"
    LIVE = "live"


class PromotionDecisionType(str, Enum):
    REJECT = "reject"
    REVISE = "revise"
    PROMOTE_TO_PAPER = "promote_to_paper"
    PROMOTE_TO_LIVE_CANDIDATE = "promote_to_live_candidate"
    APPROVE_LIVE = "approve_live"
    DEFER = "defer"


class PlannedLargeExpenseV1(BaseModel):
    name: str
    amount: float
    date: str


class LiquidityNeedsV1(BaseModel):
    emergency_fund_months: int = 6
    planned_large_expenses: List[PlannedLargeExpenseV1] = Field(default_factory=list)


class IncomeV1(BaseModel):
    annual_gross: float
    stability: Literal["stable", "variable", "uncertain"]


class NetWorthV1(BaseModel):
    total: float
    investable_assets: float


class SavingsRateV1(BaseModel):
    monthly: float
    annual: float


class TaxProfileV1(BaseModel):
    country: str
    state: str
    account_types: List[str] = Field(default_factory=list)


class PreferencesV1(BaseModel):
    excluded_asset_classes: List[str] = Field(default_factory=list)
    excluded_industries: List[str] = Field(default_factory=list)
    esg_preference: Literal["none", "light", "strict"] = "none"
    crypto_allowed: bool = True
    options_allowed: bool = True
    leverage_allowed: bool = False


class GoalV1(BaseModel):
    name: str
    target_amount: float
    target_date: str
    priority: Literal["high", "medium", "low"]


class ProfileConstraintsV1(BaseModel):
    max_single_position_pct: float = 10
    max_asset_class_pct: Dict[str, float] = Field(default_factory=dict)


class InvestmentProfileV1(BaseModel):
    schema_version: str = "1.0"
    user_id: str
    created_at: str
    risk_tolerance: Literal["low", "medium", "high", "very_high"]
    max_drawdown_tolerance_pct: float
    time_horizon_years: int
    liquidity_needs: LiquidityNeedsV1
    income: IncomeV1
    net_worth: NetWorthV1
    savings_rate: SavingsRateV1
    tax_profile: TaxProfileV1
    preferences: PreferencesV1
    goals: List[GoalV1] = Field(default_factory=list)
    constraints: ProfileConstraintsV1


class ObjectiveV1(BaseModel):
    primary: Literal["growth", "income", "preservation", "balanced"]
    target_return_annual_pct: float
    notes: str = ""


class IPSRiskLimitsV1(BaseModel):
    max_portfolio_drawdown_pct: float
    max_portfolio_volatility_annual_pct: float
    max_var_1d_pct: float
    max_loss_1d_pct: float
    max_loss_1w_pct: float
    max_loss_1m_pct: float


class SleeveRangeV1(BaseModel):
    min_pct: float
    max_pct: float


class SleevesV1(BaseModel):
    core: SleeveRangeV1
    tactical: SleeveRangeV1
    speculative: SleeveRangeV1


class PositionCapsV1(BaseModel):
    single_security_max_pct: float
    single_crypto_max_pct: float
    single_option_structure_max_pct: float
    single_private_deal_max_pct: float


class AllocationPolicyV1(BaseModel):
    sleeves: SleevesV1
    asset_class_caps_pct: Dict[str, float] = Field(default_factory=dict)
    position_caps_pct: PositionCapsV1


class InstrumentPolicyV1(BaseModel):
    enabled: bool
    allowed_types: List[str] = Field(default_factory=list)
    shorting: Optional[bool] = None
    credit_quality_min: Optional[str] = None
    naked_short_calls: Optional[bool] = None
    naked_short_puts: Optional[bool] = None
    max_days_to_expiration: Optional[int] = None
    perps_enabled: Optional[bool] = None
    leverage_max: Optional[float] = None
    max_lockup_months: Optional[int] = None
    futures_enabled: Optional[bool] = None


class AllowedInstrumentsV1(BaseModel):
    equities: InstrumentPolicyV1
    bonds: InstrumentPolicyV1
    treasuries: InstrumentPolicyV1
    options: InstrumentPolicyV1
    crypto: InstrumentPolicyV1
    fx: InstrumentPolicyV1
    real_estate: InstrumentPolicyV1
    commodities: InstrumentPolicyV1


class LiquidityConstraintsV1(BaseModel):
    min_avg_daily_dollar_volume: float
    max_pct_of_avg_daily_volume: float
    max_private_deal_lockup_months: int


class VolatilityDeleveragingV1(BaseModel):
    enabled: bool
    trigger_vol_annual_pct: float


class RebalancingPolicyV1(BaseModel):
    cadence: Literal["monthly", "quarterly", "threshold"]
    drift_threshold_pct: float
    volatility_deleveraging: VolatilityDeleveragingV1


class ExecutionPermissionsV1(BaseModel):
    mode: Literal["advisory", "paper", "assistive_live", "autonomous_live"]
    human_approval_required: bool
    auto_trade_allowed_assets: List[str] = Field(default_factory=list)


class ComplianceV1(BaseModel):
    disclaimer_required: bool = True
    no_legal_advice: bool = True
    no_tax_advice: bool = True


class IPSV1(BaseModel):
    schema_version: str = "1.0"
    ips_id: str
    user_id: str
    created_at: str
    objectives: ObjectiveV1
    risk_limits: IPSRiskLimitsV1
    allocation_policy: AllocationPolicyV1
    allowed_instruments: AllowedInstrumentsV1
    liquidity_constraints: LiquidityConstraintsV1
    rebalancing_policy: RebalancingPolicyV1
    execution_permissions: ExecutionPermissionsV1
    compliance: ComplianceV1


class AssetLiquidityV1(BaseModel):
    avg_daily_dollar_volume: float
    spread_bps_est: float


class AssetV1(BaseModel):
    asset_id: str
    asset_class: str
    symbol: str
    venue: str
    currency: str
    liquidity: AssetLiquidityV1
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AssetUniverseV1(BaseModel):
    schema_version: str = "1.0"
    universe_id: str
    ips_id: str
    as_of: str
    assets: List[AssetV1] = Field(default_factory=list)


class HypothesisV1(BaseModel):
    thesis: str
    why_it_should_work: List[str] = Field(default_factory=list)
    how_it_fails: List[str] = Field(default_factory=list)
    invalidation_triggers: List[str] = Field(default_factory=list)


class SignalV1(BaseModel):
    name: str
    definition: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    data_requirements: List[str] = Field(default_factory=list)


class PositionSizingV1(BaseModel):
    method: Literal["fixed_fraction", "vol_targeting", "risk_parity", "kelly_capped"]
    max_position_pct: float
    risk_per_trade_pct: float


class StopTakeProfitV1(BaseModel):
    type: Literal["atr", "pct", "structure_defined", "time"]
    value: float


class StrategyRiskControlsV1(BaseModel):
    max_leverage: float
    stop_loss: StopTakeProfitV1
    take_profit: StopTakeProfitV1
    max_open_positions: int
    max_correlated_exposure_pct: float


class CostModelV1(BaseModel):
    fees_bps: float
    slippage_bps: float
    spread_bps: float
    options_fill_model: Literal["mid", "mid_plus", "worst_case"]
    crypto_fee_tier: str


class StrategyConstraintsV1(BaseModel):
    ips_must_hold: bool = True
    no_lookahead: bool = True
    no_survivorship_bias: bool = True


class VersioningV1(BaseModel):
    spec_hash: str
    created_at: str
    author_agent: str


class StrategySpecV1(BaseModel):
    schema_version: str = "1.0"
    strategy_id: str
    name: str
    asset_class: str
    universe_ref: str
    timeframe: Dict[str, str] = Field(default_factory=dict)
    hypothesis: HypothesisV1
    signals: List[SignalV1] = Field(default_factory=list)
    entry_rules: List[str] = Field(default_factory=list)
    exit_rules: List[str] = Field(default_factory=list)
    position_sizing: PositionSizingV1
    risk_controls: StrategyRiskControlsV1
    cost_model: CostModelV1
    constraints: StrategyConstraintsV1
    versioning: VersioningV1


class ValidationCheckV1(BaseModel):
    name: str
    passed: bool
    notes: str = ""


class DataIntegrityV1(BaseModel):
    passed: bool
    checks: List[ValidationCheckV1] = Field(default_factory=list)


class RobustnessPointV1(BaseModel):
    cost_multiplier: Optional[float] = None
    delay_bars: Optional[int] = None
    cagr_pct: float
    max_drawdown_pct: float


class ParameterSensitivityV1(BaseModel):
    stable: bool
    notes: str = ""


class RegimeResultV1(BaseModel):
    regime: str
    cagr_pct: float
    max_drawdown_pct: float


class PerformanceV1(BaseModel):
    period_start: str
    period_end: str
    cagr_pct: float
    volatility_annual_pct: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    win_rate_pct: float
    profit_factor: float
    turnover_annual: float
    avg_holding_time: str


class RobustnessV1(BaseModel):
    cost_sensitivity: List[RobustnessPointV1] = Field(default_factory=list)
    delay_sensitivity: List[RobustnessPointV1] = Field(default_factory=list)
    parameter_sensitivity: ParameterSensitivityV1
    regime_results: List[RegimeResultV1] = Field(default_factory=list)


class RiskFitV1(BaseModel):
    ips_compliant: bool
    violations: List[str] = Field(default_factory=list)
    portfolio_impact_summary: str


class FindingsV1(BaseModel):
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    red_flags: List[str] = Field(default_factory=list)
    recommended_actions: List[str] = Field(default_factory=list)


class ArtifactsV1(BaseModel):
    equity_curve_ref: str = ""
    trades_ref: str = ""
    model_ref: str = ""
    doc_refs: List[str] = Field(default_factory=list)


class ValidationReportV1(BaseModel):
    schema_version: str = "1.0"
    validation_id: str
    strategy_or_deal_id: str
    type: StrategyValidationType
    data_integrity: DataIntegrityV1
    performance: PerformanceV1
    robustness: RobustnessV1
    risk_fit: RiskFitV1
    findings: FindingsV1
    artifacts: ArtifactsV1
    created_at: str
    created_by_agent: str


class PromotionDecisionV1(BaseModel):
    schema_version: str = "1.0"
    decision_id: str
    subject_id: str
    subject_type: PromotionSubjectType
    stage: PromotionStageV1
    decision: PromotionDecisionType
    reasons: List[str] = Field(default_factory=list)
    required_changes: List[str] = Field(default_factory=list)
    risk_manager_veto: bool = False
    human_approval_required: bool = True
    created_at: str
    created_by_agent: str


class SourceV1(BaseModel):
    name: str
    url: str
    listing_date: str


class BusinessV1(BaseModel):
    industry: str
    location: Dict[str, str] = Field(default_factory=dict)
    asking_price: float
    stated_revenue: float
    stated_ebitda_or_sde: float
    includes_real_estate: bool
    owner_involvement_hours_week: float
    reason_for_sale: str
    notes: str = ""


class DealCardV1(BaseModel):
    schema_version: str = "1.0"
    deal_id: str
    source: SourceV1
    business: BusinessV1
    initial_flags: List[str] = Field(default_factory=list)
    created_at: str


class AddbackV1(BaseModel):
    name: str
    amount: float
    evidence_ref: str
    accepted: bool


class ValuationRangeV1(BaseModel):
    low: float
    base: float
    high: float


class FinancingAssumptionsV1(BaseModel):
    rate_pct: float
    term_years: float
    down_payment_pct: float


class FinancingV1(BaseModel):
    structure: Literal["all_cash", "debt", "seller_financing", "earnout", "hybrid"]
    assumptions: FinancingAssumptionsV1
    dscr: float


class ScenarioResultV1(BaseModel):
    scenario: str
    irr_pct: float
    cash_on_cash_pct: float
    notes: str = ""


class UnderwritingSummaryV1(BaseModel):
    schema_version: str = "1.0"
    deal_id: str
    normalized_financials: Dict[str, Any] = Field(default_factory=dict)
    valuation: Dict[str, Any] = Field(default_factory=dict)
    financing: FinancingV1
    scenario_results: List[ScenarioResultV1] = Field(default_factory=list)
    key_assumptions: List[str] = Field(default_factory=list)
    key_risks: List[str] = Field(default_factory=list)
    created_at: str
    created_by_agent: str


class DiscrepancyV1(BaseModel):
    item: str
    amount: float
    notes: str
    severity: Literal["low", "medium", "high"]


class ReconciliationV1(BaseModel):
    tax_returns_match_pl: bool
    bank_statements_support_revenue: bool
    discrepancies: List[DiscrepancyV1] = Field(default_factory=list)


class RiskRegisterItemV1(BaseModel):
    category: Literal["financial", "operational", "commercial", "legal", "regulatory", "market"]
    risk: str
    evidence_ref: str
    likelihood: Literal["low", "medium", "high"]
    impact: Literal["low", "medium", "high"]
    mitigation: str
    deal_protection: str


class DiligenceFindingsV1(BaseModel):
    schema_version: str = "1.0"
    deal_id: str
    documents_received: List[str] = Field(default_factory=list)
    reconciliation: ReconciliationV1
    risk_register: List[RiskRegisterItemV1] = Field(default_factory=list)
    go_no_go: Literal["go", "no_go", "conditional"]
    conditions: List[str] = Field(default_factory=list)
    created_at: str
    created_by_agent: str


class FitToIPSV1(BaseModel):
    ips_id: str
    compliant: bool
    notes: str = ""


class KeyNumbersV1(BaseModel):
    expected_return_pct: float
    max_drawdown_pct: float
    allocation_pct: float


class InvestmentCommitteeMemoV1(BaseModel):
    schema_version: str = "1.0"
    memo_id: str
    user_id: str
    subject_type: PromotionSubjectType
    subject_id: str
    executive_summary: str
    recommendation: Literal["approve", "reject", "watchlist", "request_more_info"]
    fit_to_ips: FitToIPSV1
    key_numbers: KeyNumbersV1
    thesis: str
    counterarguments: List[str] = Field(default_factory=list)
    risks_and_mitigations: List[str] = Field(default_factory=list)
    validation_evidence: List[str] = Field(default_factory=list)
    next_steps: List[str] = Field(default_factory=list)
    appendix_refs: List[str] = Field(default_factory=list)
    created_at: str
