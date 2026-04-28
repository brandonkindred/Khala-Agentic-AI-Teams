"""Data models for the multi-asset investment organization."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .execution.risk_filter import RiskLimits


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
    # Phase 3: risk_limits is validated at spec construction time.  Dicts
    # authored by the LLM (or persisted before this field was typed) are
    # accepted and routed through ``RiskLimits.from_legacy_dict``, which
    # silently drops unknown keys so old specs stay deserializable.
    risk_limits: RiskLimits = Field(default_factory=RiskLimits)
    speculative: bool = False
    strategy_code: Optional[str] = None
    audit: AuditContext = Field(default_factory=AuditContext)

    @field_validator("risk_limits", mode="before")
    @classmethod
    def _coerce_risk_limits(cls, v: Any) -> Any:
        if v is None:
            return RiskLimits()
        if isinstance(v, RiskLimits):
            return v
        if isinstance(v, dict):
            return RiskLimits.from_legacy_dict(v)
        return v


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
    # Phase 1: metrics engine selection + risk-free rate override.
    # ``metrics_engine`` defaults to the new daily-equity-curve estimator.
    # ``"legacy"`` routes through the inter-trade-return estimator used before
    # the Phase 1 refactor — kept for one release to allow side-by-side diffs.
    metrics_engine: str = Field(default="daily", pattern=r"^(daily|legacy)$")
    risk_free_rate: Optional[float] = Field(
        default=None,
        description=(
            "Annualized risk-free rate as a fraction (e.g. 0.04 = 4%). "
            "``None`` resolves via STRATEGY_LAB_RISK_FREE_RATE env → FRED "
            "DGS3MO (when FRED_API_KEY is set) → RFR_DEFAULT=0.04."
        ),
    )
    # Phase 4: liquidity & cost-stress knobs.
    cost_stress: bool = Field(
        default=False,
        description=(
            "When True, run_backtest replays the strategy at each cost "
            "multiplier in ``cost_stress_multipliers`` and records the "
            "resulting Sharpe/return/MaxDD in ``BacktestResult.cost_stress_results``."
        ),
    )
    cost_stress_multipliers: List[float] = Field(
        default_factory=lambda: [1.0, 2.0, 3.0],
        description="Multipliers applied to transaction_cost_bps and slippage_bps.",
    )
    min_sharpe_at_2x: Optional[float] = Field(
        default=None,
        description=(
            "When set and cost_stress is enabled, run_backtest fails the "
            "strategy (reject_reason='fails_cost_stress') if its Sharpe at "
            "the 2x multiplier drops below this threshold."
        ),
    )
    min_signals_per_bar: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Minimum trades/bar ratio required for the run to be considered "
            "informative.  Set to 0 to disable (default).  Non-zero values "
            "produce reject_reason='low_signals_per_bar' when violated."
        ),
    )
    # Phase 5 (partial): intraday_mode opts the run into stricter data-source
    # checks — specifically, CoinGecko's ``/market_chart`` OHLCV is
    # reconstructed from hourly snapshots and is unreliable as an intraday
    # signal source.  ``check_intraday_data_source`` raises
    # ``IntradayDataError`` when ``intraday_mode=True`` and the only provider
    # that supplied bars for a symbol is CoinGecko.
    intraday_mode: bool = Field(
        default=False,
        description=(
            "True opts the run into intraday data-source safety checks. "
            "Must be explicit; timeframe alone is not enough because the "
            "strategy may still be daily-bar even when minute data is "
            "available."
        ),
    )
    # Issue #247 — purged walk-forward + DSR acceptance gate. All optional so
    # existing BacktestConfig callers keep legacy single-window behavior; the
    # orchestrator opts in via ``walk_forward_enabled``.
    walk_forward_enabled: bool = Field(
        default=True,
        description=(
            "When True, the Strategy Lab orchestrator evaluates the terminal "
            "acceptance gate on purged walk-forward folds instead of the "
            "legacy single-window annualized-return threshold."
        ),
    )
    n_folds: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of walk-forward folds (contiguous test blocks).",
    )
    embargo_days: int = Field(
        default=0,
        ge=0,
        description=(
            "Calendar-day embargo between a test fold and the subsequent "
            "training segment. 0 means derive from ``max(TradeRecord.hold_days)`` "
            "at runtime."
        ),
    )
    min_oos_trades: int = Field(
        default=30,
        ge=0,
        description=(
            "Minimum number of out-of-sample trades required for the "
            "composite acceptance gate to pass."
        ),
    )
    dsr_threshold: float = Field(
        default=1.0,
        description=(
            "OOS Deflated Sharpe Ratio threshold for the acceptance gate. "
            "Values are probabilities in [0, 1]; the default 1.0 reserves "
            "use of a stricter interpretation via quality-gate config."
        ),
    )
    max_is_oos_degradation_pct: float = Field(
        default=30.0,
        ge=0,
        le=100,
        description=(
            "Maximum allowed percentage degradation from in-sample to OOS "
            "Sharpe before the acceptance gate rejects the strategy."
        ),
    )
    benchmark_composition: str = Field(
        default="60_40",
        description=(
            "Benchmark blend for the regime-conditional gate. ``60_40`` "
            "compounds a 60/40 SPY+AGG equity series; future options can "
            "support per-asset-class blends."
        ),
    )
    # Issue #248 — pluggable execution model. ``realistic`` is the new
    # default; ``optimistic`` preserves the legacy fill geometry and is
    # used by the golden simulator-invariants suite (which sets
    # ``KHALA_ALLOW_OPTIMISTIC_FILLS=1`` to silence the warning).
    execution_model: Literal["optimistic", "realistic"] = Field(
        default="realistic",
        description=(
            "Fill geometry. ``realistic`` (default) fixes the limit-gap-"
            "through 'free alpha' bug, applies a participation cap, and "
            "layers an adverse-selection haircut on limit fills using "
            "one-bar lookahead. ``optimistic`` preserves the legacy "
            "geometry verbatim for parity tests."
        ),
    )
    fill_participation_cap: float = Field(
        default=0.10,
        gt=0,
        le=1,
        description=(
            "Maximum fraction of a bar's dollar volume an order may "
            "consume in one fill under the realistic execution model. "
            "Orders sized above the cap are partially filled to the cap; "
            "the remainder is dropped. Ignored by the optimistic model."
        ),
    )


# Asset-class-aware fee defaults.  Crypto uses Kraken taker fees (lowest
# volume tier).  Other classes use representative retail broker fees.
ASSET_CLASS_FEE_DEFAULTS: dict[str, dict[str, float]] = {
    "crypto": {"transaction_cost_bps": 26.0, "slippage_bps": 10.0},
    "forex": {"transaction_cost_bps": 8.0, "slippage_bps": 5.0},
    "stocks": {"transaction_cost_bps": 5.0, "slippage_bps": 2.0},
    "futures": {"transaction_cost_bps": 10.0, "slippage_bps": 5.0},
    "commodities": {"transaction_cost_bps": 12.0, "slippage_bps": 5.0},
    "options": {"transaction_cost_bps": 15.0, "slippage_bps": 8.0},
}


def get_fee_defaults(asset_class: str) -> dict[str, float]:
    """Return transaction_cost_bps and slippage_bps for a given asset class."""
    return ASSET_CLASS_FEE_DEFAULTS.get(
        asset_class.lower(),
        {"transaction_cost_bps": 10.0, "slippage_bps": 5.0},
    )


class BacktestResult(BaseModel):
    total_return_pct: float
    annualized_return_pct: float
    volatility_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float
    # Phase 1 — daily-equity-curve metrics. Optional for backwards compat with
    # ``BacktestRecord`` rows persisted before the refactor landed.
    sortino_ratio: Optional[float] = None
    calmar_ratio: Optional[float] = None
    max_drawdown_duration_days: Optional[int] = None
    risk_free_rate: Optional[float] = None
    alpha_pct: Optional[float] = None
    beta: Optional[float] = None
    information_ratio: Optional[float] = None
    metrics_engine: str = "legacy"
    # Phase 3: set when the drawdown circuit-breaker or a hard termination
    # condition (look-ahead, data error) short-circuited the run.  None
    # means the run completed through end-of-stream.
    terminated_reason: Optional[str] = None
    # Phase 4: liquidity- and cost-stress diagnostics.
    signals_per_bar: Optional[float] = None
    cost_stress_results: Optional[List[Dict[str, Any]]] = None
    reject_reason: Optional[str] = None
    # Issue #247 — walk-forward + DSR diagnostics. All Optional so legacy
    # single-window runs omit these fields entirely.
    deflated_sharpe: Optional[float] = None
    sharpe_ci_low: Optional[float] = None
    sharpe_ci_high: Optional[float] = None
    is_sharpe: Optional[float] = None
    oos_sharpe: Optional[float] = None
    is_oos_degradation_pct: Optional[float] = None
    oos_trade_count: Optional[int] = None
    n_trials_when_accepted: Optional[int] = None
    acceptance_reason: Optional[str] = None
    regime_results: Optional[List[Dict[str, Any]]] = None
    fold_results: Optional[List[Dict[str, Any]]] = None
    # Issue #375 — preflight market-data integrity report. Stored as a dict
    # to avoid a forward-reference cycle between this module and
    # ``execution.data_quality``; the typed model
    # (``DataQualityReport``) lives there and is ``model_dump()``-ed at
    # the boundary. None on legacy rows.
    data_quality_report: Optional[Dict[str, Any]] = None
    # Issue #376 — content-addressed dataset fingerprint (SHA256) covering
    # every bar fed to the run.  Two runs with the same ``BacktestConfig``
    # against the cached snapshot at the same ``as_of`` produce an
    # identical fingerprint, enabling byte-exact reproducibility checks.
    # None on legacy rows and on runs where the data path could not be
    # captured (e.g. fully external pre-fetched dicts that bypass the
    # cache).
    dataset_fingerprint: Optional[str] = None


class TradeRecord(BaseModel):
    """A single simulated trade from a backtest or paper trading session.

    ``entry_price``/``exit_price`` are retained for backward compat and equal
    the fill (post-slippage) prices. ``entry_bid_price``/``exit_bid_price``
    record the raw reference close before slippage was applied, which enables
    analysis of realized slippage vs modeled slippage. ``entry_order_type`` /
    ``exit_order_type`` default to ``"market"`` since the simulator fills at
    close; the fields exist so future limit-order simulation slots in without
    another model migration.
    """

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
    # Execution detail (populated by TradeSimulationEngine; optional for
    # backward compatibility with records persisted before these fields existed)
    entry_bid_price: Optional[float] = None
    entry_fill_price: Optional[float] = None
    exit_bid_price: Optional[float] = None
    exit_fill_price: Optional[float] = None
    entry_order_type: str = "market"
    exit_order_type: str = "market"
    # Partial-fill accounting populated by RealisticExecutionModel in
    # #386 (Trading 5/5 Step 4). Default ``None`` means "engine has not
    # annotated this trade" — which is more honest than claiming
    # ``participation_clipped=False`` / counts of ``0`` for trades that
    # the engine actually clipped at the participation cap. Step 4 will
    # populate real values; until then consumers should treat ``None``
    # as "unknown".
    participation_clipped: Optional[bool] = None
    partial_fill_count: Optional[int] = None
    total_unfilled_qty: Optional[float] = None


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


# ---------------------------------------------------------------------------
# Paper Trading enums (defined before StrategyLabRecord so it can link to them)
# ---------------------------------------------------------------------------


class PaperTradingStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    # PR 2 live-mode states. The legacy three values remain for backwards
    # compatibility with records created before live streaming landed; the
    # paper-trade mode reports its progress through the new ones.
    OPENING = "opening"
    WARMING_UP = "warming_up"
    LIVE = "live"


class PaperTradingVerdict(str, Enum):
    READY_FOR_LIVE = "ready_for_live"
    NOT_PERFORMANT = "not_performant"


class StrategyLabRecord(BaseModel):
    """Result of one strategy ideation + backtest + analysis (+ optional paper trading) cycle.

    When ``is_winning`` is True and paper trading is enabled on the run, the
    cycle also executes a paper-trading step and stores the session id and
    verdict here so clients can surface "winner + paper-trade verdict" without
    a separate lookup. Losing strategies short-circuit with
    ``paper_trading_status = "skipped"`` and ``paper_trading_skipped_reason = "not_winning"``.
    """

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
    # Paper-trading integration (populated by the strategy lab cycle when the
    # winner gate passes; null on records created before paper trading was an
    # integrated step)
    paper_trading_session_id: Optional[str] = None
    paper_trading_status: Optional[str] = Field(
        default=None,
        description="'skipped' | 'completed' | 'failed'; null for legacy rows.",
    )
    paper_trading_skipped_reason: Optional[str] = Field(
        default=None,
        description="'not_winning' | 'disabled' | 'no_market_data'; only set when status=='skipped'.",
    )
    paper_trading_error: Optional[str] = None
    paper_trading_verdict: Optional[PaperTradingVerdict] = None


# ---------------------------------------------------------------------------
# Paper Trading models
# ---------------------------------------------------------------------------


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

    # PR 2 live-mode fields (all optional; null on legacy records).
    provider_id: Optional[str] = Field(
        default=None,
        description="Resolved live provider id (e.g. 'binance', 'coinbase'). Null for legacy rows.",
    )
    cutover_ts: Optional[str] = Field(
        default=None,
        description="ISO-8601 timestamp of the first live bar — boundary between warm-up and live phase.",
    )
    fill_count: int = Field(
        default=0,
        description="Running count of closed trades during the live phase.",
    )
    terminated_reason: Optional[str] = Field(
        default=None,
        description=(
            "'fill_target_reached' | 'user_stop' | 'max_hours' | 'max_drawdown' "
            "| 'provider_error' | 'region_blocked' | 'lookahead_violation' "
            "| 'no_provider' | 'provider_end' | 'upstream_end'; null for legacy rows."
        ),
    )
    user_stop_requested_at: Optional[str] = Field(
        default=None,
        description="ISO-8601 instant the user invoked POST /stop; null if not stopped.",
    )
    warnings: List[str] = Field(
        default_factory=list,
        description="Non-fatal advisories (e.g. 'min_fills_below_recommended').",
    )
    error: Optional[str] = Field(
        default=None,
        description="Truncated error text if the session ended abnormally.",
    )
    # Issue #375 — preflight data-quality report captured at warm-up time
    # (``validate_market_data(mode='warn')``).  Live-bar gap warnings
    # accumulate on ``warnings`` instead — this field holds only the
    # warm-up snapshot.  Stored as a dict to mirror BacktestResult.
    data_quality_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Preflight data-quality report captured at warm-up; null for legacy rows.",
    )
    # Issue #376 — fingerprint of the warm-up window, taken at cut-over.
    # Live bars are not cached, so this hashes the historical warm-up
    # only.  Null for legacy rows and for sessions that ended before
    # cut-over.
    dataset_fingerprint: Optional[str] = Field(
        default=None,
        description="SHA256 fingerprint of the warm-up snapshot; null for legacy rows.",
    )


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
