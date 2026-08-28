"""Data models for the multi-asset investment organization."""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from .execution.risk_filter import RiskLimits
from .strategy_lab.alignment_findings import AlignmentFinding
from .strategy_lab.spec_dsl import (
    EntryRule,
    ExitRule,
    FixedFractionSizing,
    OcoBracketRule,
    SignalExitRule,
    SizingRule,
)

# S&P 500 amortized average annual return (%). A backtested strategy is
# classified WINNING when its annualized return meets or beats this benchmark
# (``annualized_return_pct >= WINNING_THRESHOLD``) on a valid run, and LOSING
# otherwise — beating the index is what justifies trading a strategy over
# simply holding it. This is the single deterministic verdict threshold used
# across the Strategy Lab on every path; robustness diagnostics (walk-forward
# acceptance, alignment, conformance, realism, runtime look-ahead) are recorded
# as caveats but never change this label.
WINNING_THRESHOLD = 8.0


def _coerce_legacy_strategy_spec_dict(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate a legacy persisted StrategySpec dict to the current strict schema.

    Older persisted rows (pre Issue #537) stored ``entry_rules`` / ``exit_rules``
    as prose strings and sometimes omitted ``timeframe`` entirely. The current
    schema requires structured DSL nodes and a ``timeframe`` literal. This helper
    rewrites the raw dict so that:

      * prose entries in ``entry_rules`` / ``exit_rules`` are moved into
        ``unparsed_rules`` and ``requires_redesign`` is set to True;
      * missing ``timeframe`` defaults to ``"1d"`` so the strict Literal validator
        passes.

    Preconditions: ``raw`` is a dict (caller checks); behaviour is undefined for
    other types. Postcondition: returns a new dict safe to pass to
    ``StrategySpec.model_validate``.
    """
    coerced = dict(raw)
    coerced.setdefault("timeframe", "1d")

    unparsed = list(coerced.get("unparsed_rules") or [])
    requires_redesign = bool(coerced.get("requires_redesign", False))

    for key in ("entry_rules", "exit_rules"):
        items = coerced.get(key) or []
        kept: list = []
        for item in items:
            if isinstance(item, str):
                unparsed.append(item)
                requires_redesign = True
            else:
                kept.append(item)
        coerced[key] = kept

    coerced["unparsed_rules"] = unparsed
    coerced["requires_redesign"] = requires_redesign
    return coerced


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


class ExpectancyForecast(BaseModel):
    """The DesignAgent's pre-commit performance forecast for a strategy.

    The dual-objective design contract requires the agent, before emitting a
    spec, to forecast its win rate, reward:risk, trade frequency, and the
    resulting projected annual return, and to show they are mutually
    self-consistent (a 1% take-profit against a 5% stop must defend the ~84%
    win rate it needs to clear costs). This record captures that forecast as
    structured data so downstream consumers can read it without parsing prose.

    The forecast is advisory — it is never gated on. A spec emitted without
    one (or a persisted record predating this field) carries ``None`` for the
    owning ``StrategySpec.expectancy_forecast`` and is still valid.

    Preconditions:
        - ``forecast_win_rate`` is a probability; *finite* values outside
          ``[0, 1]`` are clamped to the nearest bound rather than rejected.
        - ``reward_risk`` and ``trades_per_year`` are non-negative; negatives
          are clamped to ``0.0``.
        - Non-finite inputs (``NaN`` / ``±inf``) on any numeric field — which a
          malformed LLM payload can produce — are sanitized to ``0.0`` so a slip
          never propagates a ``NaN``/``inf`` into downstream consumers.
    Postconditions:
        - A pure data record with no side effects. After construction every
          numeric field is finite: ``forecast_win_rate`` ∈ ``[0, 1]``,
          ``reward_risk`` / ``trades_per_year`` ≥ ``0``, and
          ``projected_annual_return_pct`` is a finite float (a negative
          projected return is legitimate and preserved).
    Invariants:
        - Holds no references to engine or LLM state; safe to serialize into a
          persisted ``StrategySpec``.
    """

    forecast_win_rate: float = 0.0
    reward_risk: float = 0.0
    trades_per_year: float = 0.0
    projected_annual_return_pct: float = 0.0
    consistency_note: str = ""

    @field_validator("forecast_win_rate", mode="after")
    @classmethod
    def _clamp_win_rate(cls, v: float) -> float:
        # A probability. The designer emits it as a fraction; a model slip
        # (a negative, or 84 emitted for "84%") is clamped into [0, 1] rather
        # than rejected, since the forecast is advisory and never gated.
        # NaN/±inf (which `<`/`>` would silently pass through) collapse to 0.0.
        if not math.isfinite(v):
            return 0.0
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return v

    @field_validator("reward_risk", "trades_per_year", mode="after")
    @classmethod
    def _floor_non_negative(cls, v: float) -> float:
        # Floor at 0.0. Non-finite (NaN/±inf) also collapses to 0.0 so a
        # malformed forecast never carries a NaN/inf forward.
        if not math.isfinite(v) or v <= 0.0:
            return 0.0
        return v

    @field_validator("projected_annual_return_pct", mode="after")
    @classmethod
    def _sanitize_projected_return(cls, v: float) -> float:
        # A negative projected return is a legitimate (if undesirable) forecast,
        # so finite values pass through unchanged; only non-finite collapses.
        return v if math.isfinite(v) else 0.0


class StrategySpec(BaseModel):
    strategy_id: str
    authored_by: str
    # Canonicalized at construction by ``_canonicalize_asset_class`` to one of
    # the canonical labels (stocks/crypto/forex/options/futures/commodities).
    # Accepted aliases (equity/equities/stock/etf/etfs, fx, commodity/metal/
    # energy) are mapped to their canonical form; unknown classes are rejected
    # on live construction so every downstream asset_class-keyed gate can
    # compare against the canonical set without re-encoding the alias table.
    asset_class: str
    hypothesis: str
    signal_definition: str
    timeframe: Literal["1m", "5m", "15m", "1h", "1d"]
    entry_rules: List[EntryRule] = Field(default_factory=list)
    exit_rules: List[ExitRule] = Field(default_factory=list)
    sizing: SizingRule = Field(default_factory=lambda: FixedFractionSizing(fraction=0.02))
    # Issue #523 — explicit list of tickers the strategy is designed to
    # trade. When non-empty, the fetch path uses this list verbatim
    # instead of the asset-class default universe, so a hypothesis naming
    # QQQ doesn't get silently backtested on AAPL.
    target_symbols: List[str] = Field(default_factory=list)
    # Phase 3: risk_limits is validated at spec construction time.  Dicts
    # authored by the LLM (or persisted before this field was typed) are
    # accepted and routed through ``RiskLimits.from_legacy_dict``, which
    # silently drops unknown keys so old specs stay deserializable.
    risk_limits: RiskLimits = Field(default_factory=RiskLimits)
    speculative: bool = False
    strategy_code: Optional[str] = None
    requires_redesign: bool = False
    # False: orchestrator compiles ``strategy_code`` deterministically
    # from the structured rules. True: keep LLM-authored code verbatim
    # because the spec falls outside the deterministic compiler's
    # expressible subset. Orthogonal to ``requires_redesign``.
    requires_custom_code: bool = False
    unparsed_rules: List[str] = Field(default_factory=list)
    # The DesignAgent's pre-commit forecast of win rate, reward:risk, trade
    # frequency, and projected annual return — the expectancy reasoning behind
    # the spec. Advisory and never gated; ``None`` for specs (or legacy
    # persisted records) authored without it.
    expectancy_forecast: Optional[ExpectancyForecast] = None
    audit: AuditContext = Field(default_factory=AuditContext)

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_when_loading(cls, data: Any, info: ValidationInfo) -> Any:
        """When validated with ``context={'legacy_spec': True}``, rewrite prose
        rules into ``unparsed_rules`` and default a missing ``timeframe``.

        This lets persisted rows authored before the strict DSL schema deserialize
        cleanly. Live construction (no context) is unaffected so the strict
        construction tests for prose rejection still hold.
        """
        if not isinstance(data, dict):
            return data
        if not (info.context and info.context.get("legacy_spec")):
            return data
        return _coerce_legacy_strategy_spec_dict(data)

    @classmethod
    def parse_persisted(cls, raw: Any) -> "StrategySpec":
        """Deserialize a (possibly legacy) persisted row into a StrategySpec.

        Accepts a ``StrategySpec`` (returned as-is) or a dict. Raw dicts are
        validated with the legacy-coercion context so prose rules and missing
        ``timeframe`` migrate to the current schema instead of raising.
        """
        if isinstance(raw, cls):
            return raw
        return cls.model_validate(raw, context={"legacy_spec": True})

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

    @field_validator("target_symbols", mode="before")
    @classmethod
    def _normalize_target_symbols(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("target_symbols must be a list of strings")
        seen: set[str] = set()
        out: List[str] = []
        for item in v:
            if not isinstance(item, str):
                raise ValueError("target_symbols entries must be strings")
            sym = item.strip().upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            out.append(sym)
        return out

    @field_validator("asset_class", mode="after")
    @classmethod
    def _canonicalize_asset_class(cls, v: str, info: ValidationInfo) -> str:
        """Canonicalize and enforce the ``asset_class`` vocabulary at the boundary.

        Preconditions: ``v`` is the raw asset_class string (already validated as
        a ``str`` by pydantic).
        Postconditions: returns one of the canonical labels
        (stocks/crypto/forex/options/futures/commodities). Accepted aliases
        (equity/equities/stock/etf/etfs, fx, commodity/metal/energy,
        cryptocurrency/cryptocurrencies) map to their canonical form.

        Live construction (no validation context) **rejects** an unrecognised
        class — raising ``ValueError`` → ``ValidationError`` — so a typo'd or
        unsupported class surfaces as a defect at the boundary rather than
        silently bypassing the asset_class-keyed quality gates. Legacy
        deserialization (context ``legacy_spec=True``) instead falls back to the
        permissive mapping (unknown → ``stocks``) so pre-existing persisted rows
        authored before this enforcement still load cleanly.

        The orchestrator's LLM design path coerces an off-vocabulary class with
        the permissive normalizer *before* construction (see
        ``_build_spec_from_dict``) so a model slip is repaired rather than
        crashing the cycle; this strict path governs direct/API construction.
        """
        # Local import: ``strategy_lab_context`` imports from this module at
        # module load, so a top-level import here would be circular.
        from .strategy_lab_context import normalize_asset_class, normalize_asset_class_strict

        if info.context and info.context.get("legacy_spec"):
            return normalize_asset_class(v)
        return normalize_asset_class_strict(v)

    @model_validator(mode="after")
    def _validate_single_limit_stop(self) -> "StrategySpec":
        """At most one limit-style stop-loss per spec.

        A ``style="limit"`` stop rests a STOP_LIMIT that the engine tracks across
        bars and de-duplicates against on re-trigger. Allowing several of them
        introduces ambiguous "which resting order is this rule's" bookkeeping
        (same/overlapping stop levels, layered fallbacks) for a configuration no
        real strategy needs — the designer authors a single protective
        stop-limit. Bounding it to one keeps the resting-exit dispatch
        unambiguous.

        Preconditions: ``exit_rules`` is the validated rule list.
        Postconditions: returns ``self`` when at most one exit rule is a
        limit-style stop; raises ``ValueError`` otherwise.
        """
        limit_stops = sum(1 for r in self.exit_rules if getattr(r, "style", "market") == "limit")
        if limit_stops > 1:
            raise ValueError(
                f"at most one limit-style stop-loss (style='limit') is allowed "
                f"per spec; got {limit_stops}"
            )
        return self

    @model_validator(mode="after")
    def _validate_oco_bracket_exclusivity(self) -> "StrategySpec":
        """An ``oco_bracket`` is a self-contained full-position OCO exit.

        The bracket is attached to the entry order and materialized by the engine
        into resting OCO children sized to the whole position; a coexisting
        ``stop_loss`` / ``take_profit`` / ``scaled_take_profit`` would be
        evaluated independently by the bar-by-bar exit dispatcher and fight the
        bracket (double protection, ambiguous sizing). So at most one bracket is
        allowed, and when present it must be the sole engine-handled *price*
        exit. A ``signal_exit`` may coexist as a secondary discretionary trigger.

        Preconditions: ``exit_rules`` is the validated rule list.
        Postconditions: returns ``self`` when at most one bracket is present and,
        if one is, the only other exits are ``signal_exit`` rules; raises
        ``ValueError`` otherwise.
        """
        brackets = [r for r in self.exit_rules if isinstance(r, OcoBracketRule)]
        if not brackets:
            return self
        # A bracket attaches to engine-EMITTED entry orders, so it only functions
        # when entries are engine-managed. With ``requires_custom_code=True`` the
        # runtime routes entries through strategy code (``entry_rules=None`` is
        # passed to the engine), the entry dispatcher never attaches the bracket,
        # and the exit evaluator skips it — the bracket would be silently inert and
        # close nothing. Reject the combination so it is never mistaken for a
        # working exit. (The orchestrator can still flip the flag *after*
        # construction via ``model_copy`` / assignment, which Pydantic does not
        # re-validate — the SpecReadinessGate enforces the same invariant on the
        # final spec for that path.)
        if self.requires_custom_code:
            raise ValueError(
                "oco_bracket is not usable with requires_custom_code=True: the bracket "
                "attaches only to engine-managed entries, so on the custom-code path it is "
                "inert and closes nothing. Remove the bracket (use stop_loss / take_profit "
                "/ signal_exit), or set requires_custom_code=False."
            )
        if len(brackets) > 1:
            raise ValueError(
                f"at most one oco_bracket exit rule is allowed per spec; got {len(brackets)}"
            )
        # Allowlist (not a hardcoded conflict denylist): a bracket may coexist
        # ONLY with the bracket itself and ``signal_exit`` (a non-price,
        # indicator-based trigger). Every other exit kind — current or
        # future — is a conflicting engine-handled price exit by default, so a
        # new price-exit kind added to the union is rejected with a bracket
        # without needing this validator to be updated. Maintenance note: if a
        # future NON-price exit kind is added that is meant to coexist with a
        # bracket (like ``signal_exit``), add it to this allowlist tuple.
        conflicting = [
            (i, r)
            for i, r in enumerate(self.exit_rules)
            if not isinstance(r, (OcoBracketRule, SignalExitRule))
        ]
        if conflicting:
            # Report the offending rules by ``exit_rules`` index AND kind so a
            # spec with many exits can locate them directly.
            offenders = ", ".join(f"[{i}] {r.kind}" for i, r in conflicting)
            raise ValueError(
                "an oco_bracket is a full-position OCO exit and must be the sole "
                f"engine-handled price exit; remove the coexisting rule(s) {offenders} "
                "(a signal_exit may still accompany the bracket)"
            )
        return self


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
    exit_rule_trailing_replay_enabled: bool = Field(
        default=False,
        description=(
            "Opt-in. When True, the exit-rule conformance gate runs a "
            "bar-by-bar replay for trailing stop-loss rules "
            "(``basis='trailing_high'``/``'trailing_low'``): it reconstructs "
            "the per-bar high/low watermark from cached bars and surfaces a "
            "``warning`` (never a ``critical``) when the engine's trailing "
            "floor was breached on a bar strictly before the position closed "
            "but the engine did not fire. Defaults False so the gate stays "
            "deterministic/post-hoc with no replay."
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


OrderLifecycleEventType = Literal[
    "emitted",
    "accepted",
    "rejected",
    "unfilled",
    "warmup_dropped",
    "entry_filled",
    "exit_filled",
    # A matched entry signal that the engine dispatcher's risk sizing reduced to
    # zero (a sub-1 whole-share order whose one-share floor would push past
    # max_position_pct). Recorded so a zero-trade run is explainable rather than a
    # silent no-emit; carries no order (it never reached the order book).
    "risk_capped_skip",
    # A stop-limit order that triggered (stop level crossed) but gapped through
    # its limit price, so it could not fill this bar and stays resting with the
    # position open — the intended, defining risk of a stop-limit order.
    "stop_limit_unfilled",
]

ZeroTradeCategory = Literal[
    "NO_ORDERS_EMITTED",
    "ALL_ENTRIES_RISK_CAPPED",
    "ONLY_WARMUP_ORDERS",
    "ORDERS_REJECTED",
    "ORDERS_UNFILLED",
    "ENTRY_WITH_NO_EXIT",
    "UNKNOWN_ZERO_TRADE_PATH",
]


class OrderLifecycleEvent(BaseModel):
    """Compact order lifecycle event retained for backtest diagnostics."""

    model_config = ConfigDict(extra="ignore")

    event_type: OrderLifecycleEventType
    timestamp: Optional[str] = None
    symbol: Optional[str] = None
    side: Optional[str] = None
    order_type: Optional[str] = None
    reason: str = ""
    detail: str = ""


class OpenPositionDiagnostic(BaseModel):
    """Snapshot of an open position at the end of a diagnostic backtest."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    side: str
    qty: float
    entry_price: float
    entry_timestamp: str


def scaled_level_key(rule_index: int, level_index: int) -> str:
    """Diagnostics key for one scaled-take-profit rung.

    Single source of the ``scaled_take_profit_level_firings`` key format, shared by
    the emitter (``_record_emission``) and the conformance gate so the two never
    drift. Preconditions: both indices are non-negative — enforced with an explicit
    raise (not ``assert``) so the diagnostics-key contract holds even under
    ``python -O``. Postconditions: returns ``"<rule_index>:<level_index>"`` (e.g.
    ``"0:1"``).
    """
    if rule_index < 0 or level_index < 0:
        raise ValueError(
            f"scaled_level_key indices must be non-negative: ({rule_index}, {level_index})"
        )
    return f"{rule_index}:{level_index}"


class BacktestExecutionDiagnostics(BaseModel):
    """Structured execution-path diagnostics for sparse or zero-trade backtests."""

    model_config = ConfigDict(extra="ignore")

    zero_trade_category: Optional[ZeroTradeCategory] = None
    summary: str = ""
    bars_processed: int = Field(default=0, ge=0)
    orders_emitted: int = Field(default=0, ge=0)
    orders_accepted: int = Field(default=0, ge=0)
    orders_rejected: int = Field(default=0, ge=0)
    orders_rejection_reasons: Dict[str, int] = Field(default_factory=dict)
    orders_unfilled: int = Field(default=0, ge=0)
    warmup_orders_dropped: int = Field(default=0, ge=0)
    # Matched entry signals the engine dispatcher's risk sizing reduced to zero
    # (a sub-1 whole-share order whose one-share floor would push past
    # max_position_pct). Drives the ``ALL_ENTRIES_RISK_CAPPED`` zero-trade category
    # so a run suppressed by risk sizing is not mis-triaged as a dead entry predicate.
    risk_capped_entries: int = Field(default=0, ge=0)
    entries_filled: int = Field(default=0, ge=0)
    exits_emitted: int = Field(default=0, ge=0)
    closed_trades: int = Field(default=0, ge=0)
    # Issue #527 — count of engine-emitted exit orders, keyed by rule kind
    # (``stop_loss`` / ``take_profit``). Counts close-order submissions,
    # not fills; fills land in the existing trade ledger.
    exit_rule_firings: Dict[str, int] = Field(default_factory=dict)
    # Per-symbol breakdown of ``exit_rule_firings`` — same counts, keyed by
    # ``symbol → rule_kind → count``. The aggregate field above is the
    # row-sum across symbols. Conformance checks consume the per-symbol
    # view so a stop_loss firing on one symbol can't mask a missed
    # firing on another (cross-symbol leak attribution).
    exit_rule_firings_by_symbol: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    # Finer-grained breakdown of ``exit_rule_firings`` that distinguishes a
    # trailing stop fire from a fixed stop fire, keyed by ``"<rule_kind>:<basis>"``
    # for stop-loss firings (``stop_loss:entry_price`` / ``stop_loss:trailing_high``
    # / ``stop_loss:trailing_low``) and by bare ``<rule_kind>`` for rules without a
    # basis (``take_profit`` / ``signal_exit``). This is additive metadata: the
    # close ``reason`` and ``exit_rule_firings`` above stay byte-stable so the
    # exact-match conformance + alignment gates are unaffected, while analysis and
    # operability surfaces gain per-basis visibility.
    exit_rule_firings_by_basis: Dict[str, int] = Field(default_factory=dict)
    # Per-rung firing counts for laddered ``ScaledTakeProfitRule`` exits, keyed by
    # ``"<rule_index>:<level_index>"`` (e.g. ``"0:0"`` / ``"0:1"``). Each rung
    # scales out at most once per position, so these distinguish which targets a
    # ladder actually realised. Additive metadata: the ``scaled_take_profit``
    # aggregate stays in ``exit_rule_firings`` and the close ``reason`` is
    # byte-stable, so the exact-match conformance + alignment gates are unaffected.
    scaled_take_profit_level_firings: Dict[str, int] = Field(default_factory=dict)
    # Fill-based counterpart of ``exit_rule_firings`` — counts engine-SUBMITTED
    # exit orders that actually FILLED (closed a position), keyed by rule kind,
    # with a per-symbol breakdown below. Counted off ``engine_exit_filled``
    # diagnostic events keyed by the order's un-reconciled ``engine_exit:<kind>``
    # reason, so a *strategy* close whose ``TradeRecord.exit_reason`` was
    # reconciled to ``engine_exit:*`` is NOT counted as an engine fill. For a
    # market close emission == fill, so these mirror ``exit_rule_firings``; for a
    # ``style="limit"`` stop they diverge, because a STOP_LIMIT can fire (emit)
    # but gap through its limit unfilled. Surfaced as observability telemetry (the
    # fire-vs-fill gap), NOT as the conformance leak-check denominator: the gate
    # reconciles against the independent emission firings.
    exit_rule_fills: Dict[str, int] = Field(default_factory=dict)
    exit_rule_fills_by_symbol: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    # Count of stop-limit orders that triggered (stop level crossed) but could not
    # fill on the trigger bar because the bar gapped through the limit price. A
    # triggered-but-unfilled stop-limit leaves the position open — the defining,
    # intended risk of the order type — so it is surfaced as informational
    # telemetry rather than silently dropped.
    stop_limit_unfilled_triggers: int = Field(default=0, ge=0)
    open_positions_at_end: List[OpenPositionDiagnostic] = Field(default_factory=list)
    last_order_events: List[OrderLifecycleEvent] = Field(default_factory=list)


class CoverageCategory(str, Enum):
    """Why a backtest produced zero or sparse entries (#406)."""

    COVERAGE_OK = "COVERAGE_OK"
    ENTRY_CONDITION_NEVER_TRUE = "ENTRY_CONDITION_NEVER_TRUE"
    WARMUP_EXCEEDS_HISTORY = "WARMUP_EXCEEDS_HISTORY"
    TARGET_SYMBOL_MISSING = "TARGET_SYMBOL_MISSING"
    INDICATOR_FILTER_TOO_RESTRICTIVE = "INDICATOR_FILTER_TOO_RESTRICTIVE"
    CONJUNCTION_NEVER_TRUE = "CONJUNCTION_NEVER_TRUE"
    INSUFFICIENT_BARS = "INSUFFICIENT_BARS"
    UNKNOWN_LOW_COVERAGE = "UNKNOWN_LOW_COVERAGE"


class LikelyBlocker(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str
    evidence: str = ""
    hit_rate: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class SubconditionCoverage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str
    hit_count: int = Field(default=0, ge=0)
    hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    last_true_bar: Optional[str] = None


class CoverageReport(BaseModel):
    """Deterministic rule-coverage probe output for zero/low-trade backtests."""

    model_config = ConfigDict(extra="ignore")

    coverage_category: CoverageCategory = CoverageCategory.UNKNOWN_LOW_COVERAGE
    summary: str = ""
    symbols_checked: int = Field(default=0, ge=0)
    bars_checked: int = Field(default=0, ge=0)
    warmup_bars_required: int = Field(default=0, ge=0)
    entry_orders_emitted: int = Field(default=0, ge=0)
    subconditions: List[SubconditionCoverage] = Field(default_factory=list)
    likely_blockers: List[LikelyBlocker] = Field(default_factory=list)


class RuleIndex(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rules: dict[str, str] = Field(default_factory=dict)


class BacktestResult(BaseModel):
    total_return_pct: float
    annualized_return_pct: float
    volatility_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown_duration_days: int = 0
    risk_free_rate: float = 0.0
    alpha_pct: Optional[float] = None
    beta: Optional[float] = None
    information_ratio: Optional[float] = None
    # Phase 3: set when the drawdown circuit-breaker or a hard termination
    # condition (look-ahead, data error) short-circuited the run.  None
    # means the run completed through end-of-stream.
    terminated_reason: Optional[str] = None
    # Phase 4: liquidity- and cost-stress diagnostics.
    signals_per_bar: Optional[float] = None
    cost_stress_results: Optional[List[Dict[str, Any]]] = None
    reject_reason: Optional[str] = None
    execution_diagnostics: Optional[BacktestExecutionDiagnostics] = None
    coverage_report: Optional[CoverageReport] = None
    # Issue #247 — walk-forward + DSR diagnostics. Required as of #378;
    # callers without a walk-forward run pass 0.0 explicitly.
    deflated_sharpe: float
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

    @field_validator("risk_free_rate", mode="before")
    @classmethod
    def _coerce_risk_free_rate_none_to_zero(cls, v: object) -> object:
        # Legacy rows may persist ``risk_free_rate`` as ``None``; coerce to
        # ``0.0`` so they rehydrate. ``sortino_ratio``/``calmar_ratio``/
        # ``deflated_sharpe`` became required in #378 — no None coercion.
        return 0.0 if v is None else v

    @field_validator("max_drawdown_duration_days", mode="before")
    @classmethod
    def _coerce_int_none_to_zero(cls, v: object) -> object:
        return 0 if v is None else v


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
    # Execution detail (populated by trading_service's FillSimulator; optional for
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
    # Issue #527 — close-order attribution. Mirrors
    # ``OrderRequest.reason`` for the order that closed the position.
    # Engine-fired structured-exit closes carry a
    # ``"engine_exit:<rule_kind>"`` prefix; strategy-emitted closes
    # carry whatever ``reason`` the strategy set (typically empty
    # / None). Used by ``ExitRuleConformanceGate`` to distinguish
    # engine-closed from strategy-closed trades so a gap-down
    # strategy exit below a structured stop-loss floor is not
    # mis-attributed as an engine leak.
    entry_reason: Optional[str] = None
    exit_reason: Optional[str] = None


class DataProvenance(BaseModel):
    """Issue #533 — symbol & data-source audit trail for one backtest run.

    Lets reviewers answer "spec asked for QQQ → fetcher returned
    AAPL/MSFT/NVDA/TSLA/AMZN → ledger traded TSLA" from structured data
    without grepping narrative prose or strategy code.

    ``target_symbols`` is the explicit list the spec asked for
    (``spec.target_symbols``); distinct from ``BacktestRecord.requested_symbols``
    which is the *resolved* universe (may be the asset-class fallback when
    ``target_symbols`` is empty).
    """

    target_symbols: List[str] = Field(default_factory=list)
    fetched_symbols: List[str] = Field(default_factory=list)
    traded_symbols: List[str] = Field(default_factory=list)
    provider_used: Dict[str, str] = Field(default_factory=dict)
    as_of: Optional[str] = None
    legacy_fingerprint: Optional[str] = None


class BacktestRecord(BaseModel):
    @classmethod
    def parse_persisted(cls, raw: Any) -> "BacktestRecord":
        """Deserialize a persisted backtest row, migrating legacy nested specs."""
        if isinstance(raw, cls):
            return raw
        return cls.model_validate(raw, context={"legacy_spec": True})

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
    # Issue #525 — symbol-fetch audit trail. ``requested_symbols`` is what
    # the orchestrator asked ``MarketDataService.fetch_multi_symbol_range``
    # to retrieve (post target_symbols / asset-class fallback resolution);
    # ``fetched_symbols`` is the subset that returned usable bars. Both
    # default to ``[]`` so older persisted rows deserialize cleanly.
    requested_symbols: List[str] = Field(default_factory=list)
    fetched_symbols: List[str] = Field(default_factory=list)
    # Issue #533 — structured provenance (target/fetched/traded symbols,
    # per-symbol provider, ``as_of`` snapshot id, dataset fingerprint).
    # Default-empty so legacy persisted rows deserialize cleanly.
    data_provenance: DataProvenance = Field(default_factory=DataProvenance)
    # Per-trade, per-rule alignment findings emitted by the deterministic
    # ``DeterministicAlignmentChecker``. Carries the final-iteration
    # ledger of which alignment checks passed / failed on each trade so
    # the Strategy Lab dashboard can render fine-grained pass/fail rows
    # instead of a single LLM verdict. Default-empty so legacy persisted
    # rows deserialize cleanly.
    alignment_findings: List[AlignmentFinding] = Field(default_factory=list)


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


# ---------------------------------------------------------------------------
# Drift-observability models (spec/code revision ledger, gate timeline,
# rule-implementation coverage)
# ---------------------------------------------------------------------------

StrategyLabPhase = Literal["design", "design_review", "synthesis", "verification"]


class SpecRevision(BaseModel):
    """One mutation of the strategy spec during a lab cycle.

    Preconditions:
      - ``before_hash`` / ``after_hash`` are 64-char hex SHA-256 digests
        produced by ``phases.hash_spec``.
      - ``before_hash != after_hash`` (no-op mutations are not recorded).
    Postconditions:
      - ``diff`` is a unified-diff string of the spec's sorted-key
        pretty-printed JSON, suitable for human review.
    """

    phase: StrategyLabPhase
    agent: str
    timestamp: str
    before_hash: str = Field(..., min_length=64, max_length=64)
    after_hash: str = Field(..., min_length=64, max_length=64)
    diff: str
    reason: str
    gate_failures: List[str] = Field(default_factory=list)


class CodeRevision(BaseModel):
    """One mutation of the strategy code during a lab cycle.

    Preconditions:
      - ``before_hash`` / ``after_hash`` are 64-char hex SHA-256 digests
        produced by ``phases.hash_code``.
      - ``before_hash != after_hash`` (no-op mutations are not recorded).
    Postconditions:
      - ``diff`` is a unified-diff string of the raw code text.
    """

    phase: StrategyLabPhase
    agent: str
    timestamp: str
    before_hash: str = Field(..., min_length=64, max_length=64)
    after_hash: str = Field(..., min_length=64, max_length=64)
    diff: str
    reason: str
    gate_failures: List[str] = Field(default_factory=list)


class GateEvent(BaseModel):
    """Chronological record of a quality-gate evaluation."""

    phase: str
    gate_name: str
    passed: bool
    severity: Literal["info", "warning", "critical"]
    details: str
    timestamp: str


class DesignAttemptCheckpoint(BaseModel):
    """Durable checkpoint of one design attempt's Phase 1 (design + review) output.

    Taken at the design/synthesis boundary inside ``_run_design_attempt``
    (``strategy_lab/orchestrator_design.py``), so a resumed attempt can skip
    re-running Phase 1's LLM calls entirely. See ``ADR-012``
    (``system_design/adr/ADR-012-strategy-lab-design-attempt-checkpoint-contract.md``)
    for the full contract this DTO implements: checkpoint identity/scoping,
    the persisted-field set, fencing treatment, and resumability semantics.

    ``gate_results`` is typed as ``List[Dict[str, Any]]`` rather than
    ``List[QualityGateResult]`` to match ``StrategyLabRecord.quality_gate_results``'s
    existing precedent: importing ``strategy_lab.quality_gates.models`` from this
    module would trigger ``strategy_lab/__init__.py``, reintroducing the circular
    import that module's own docstring documents avoiding.

    ``cycle_scope`` disambiguates concurrent ``StrategyLabCycleWorkflow``
    children sharing one ``run_id``: ``StrategyLabBatchWorkflow`` runs up to
    ``max_parallel`` cycles per wave, and each cycle's own design-attempt
    loop independently starts at ``design_attempt=0`` -- without this field,
    two concurrent cycles could collide on the same ``(run_id,
    design_attempt)`` pair and one could silently resume with the other's
    checkpointed spec. It is opaque: never parsed for a ``run_id``/
    ``cycle_index`` substring, just compared for equality. Populated from the
    current activity's own Temporal ``workflow_id`` (see
    ``temporal.activities._infer_cycle_scope_from_activity_context``).

    Preconditions:
        ``run_id``/``cycle_scope``/``design_attempt`` identify exactly the
        attempt this checkpoint was taken during; ``generation`` is the
        fencing generation active at write time (see
        ``shared.fencing.check_fencing_token``).
    Postconditions:
        Instances are immutable snapshots of Phase 1's output plus the
        attempt-local drift/gate/budget state needed to resume Phase 1b
        onward without redoing or double-charging Phase 1's work.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    cycle_scope: str = Field(..., min_length=1)
    design_attempt: int = Field(..., ge=0)
    generation: int = Field(..., ge=1)
    spec: StrategySpec
    rationale: str
    design_context: Dict[str, Any]
    spec_history: List[SpecRevision] = Field(default_factory=list)
    code_history: List[CodeRevision] = Field(default_factory=list)
    gate_timeline: List[GateEvent] = Field(default_factory=list)
    gate_results: List[Dict[str, Any]] = Field(default_factory=list)
    budget_calls: int = Field(..., ge=0)


class RuleImplementationMap(BaseModel):
    """Per-rule trade coverage: how many trades exercised each spec rule.

    ``code_line_refs`` is best-effort AST analysis — empty when the
    generated code cannot be parsed or the rule cannot be located.
    Each inner list is ``[start_line, end_line]``.
    """

    rule_id: str
    code_line_refs: List[List[int]] = Field(default_factory=list)
    traded_count: int = 0


class StrategyLabRecord(BaseModel):
    """Result of one strategy ideation + backtest + analysis (+ optional paper trading) cycle.

    When ``is_publishable`` is True and paper trading is enabled on the run, the
    cycle also executes a paper-trading step and stores the session id and
    verdict here so clients can surface "winner + paper-trade verdict" without
    a separate lookup. Losing strategies short-circuit with
    ``paper_trading_status = "skipped"`` and ``paper_trading_skipped_reason = "not_winning"``.
    Winning-but-not-publishable strategies short-circuit with the joined
    gate codes from ``publishability_skip_reason``.
    """

    @classmethod
    def parse_persisted(cls, raw: Any) -> "StrategyLabRecord":
        """Deserialize a persisted lab record, migrating legacy nested specs."""
        if isinstance(raw, cls):
            return raw
        return cls.model_validate(raw, context={"legacy_spec": True})

    lab_record_id: str
    strategy: StrategySpec
    backtest: BacktestRecord
    is_winning: bool  # deterministic: annualized_return_pct >= WINNING_THRESHOLD (8% S&P benchmark) on a valid run; robustness gates record caveats but never flip this
    is_publishable: bool = Field(
        default=False,
        description=(
            "True when is_winning and realism/alignment/exit-rule/lookahead "
            "gates all clear. Paper-trading gates on this flag. Missing on "
            "legacy rows → False."
        ),
    )
    publishability_skip_reason: Optional[str] = Field(
        default=None,
        description=(
            "Comma-joined failing publishability gate codes in veto order "
            "(exit_rule_conformance_failed, realism_failed, alignment_unresolved, "
            "lookahead_violation). None when is_publishable is True or on legacy rows."
        ),
    )
    strategy_rationale: str  # why the agent chose this strategy
    analysis_narrative: str  # LLM post-backtest analysis
    created_at: str
    refinement_rounds: int = 0
    design_rounds: int = Field(
        default=0,
        description=(
            "Number of design ↔ design-review iterations the cycle ran "
            "before code synthesis. 0 on legacy rows; >=1 on rows from the "
            "split design pipeline (one design call + zero or more review "
            "rounds)."
        ),
    )
    spec_implementability_phase_backs: int = Field(
        default=0,
        description=(
            "Number of times this cycle phased back to the design step "
            "because a downstream phase raised SpecImplementabilityError. "
            "Each phase-back also contributes one trial to the convergence "
            "tracker so DSR deflation reflects the multiple-testing cost "
            "of the failed attempts. 0 on the happy path and on legacy rows."
        ),
    )
    critiques: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Serialized SpecCritique entries (model_dump) the reviewer "
            "produced during the design ↔ design-review loop. Empty on "
            "legacy rows; carries one entry per round on rows from the "
            "split design pipeline."
        ),
    )
    quality_gate_results: List[Dict[str, Any]] = Field(default_factory=list)
    strategy_code: Optional[str] = None
    original_spec: Optional[StrategySpec] = Field(
        default=None,
        description="Design-time spec before any refinement-driven mutation; null on legacy rows.",
    )
    original_code: Optional[str] = Field(
        default=None,
        description="Design-time strategy code before refinement; null on legacy rows.",
    )
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
        description=(
            "'not_winning' | joined publishability gate codes | 'disabled' | "
            "'no_market_data' | 'no_strategy_code'; only set when status=='skipped'."
        ),
    )
    paper_trading_error: Optional[str] = None
    paper_trading_verdict: Optional[PaperTradingVerdict] = None
    # Drift-observability ledgers
    spec_history: List[SpecRevision] = Field(default_factory=list)
    code_history: List[CodeRevision] = Field(default_factory=list)
    gate_timeline: List[GateEvent] = Field(default_factory=list)
    rule_implementation_map: List[RuleImplementationMap] = Field(default_factory=list)
    loop_telemetry: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-cycle generation-funnel telemetry: design-review round count "
            "and stop reason, critique-ledger totals (resolved / regressed / "
            "final open), per-gate pass/fail histograms, and the "
            "compiled-vs-custom (requires_custom_code) flag. Empty {} on legacy "
            "rows and on paths that bypass the design loop."
        ),
    )
    ran_on_non_conforming_code: bool = Field(
        default=False,
        description=(
            "True when the cycle's backtest executed custom code that failed "
            "the predicate-conformance gate and was demoted to a warning past "
            "the configured retry budget rather than being repaired. False on "
            "legacy rows, compiled-path rows, and conforming custom-code rows. "
            "Mirrors loop_telemetry['ran_on_non_conforming_code']."
        ),
    )


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

    @classmethod
    def parse_persisted(cls, raw: Any) -> "PaperTradingSession":
        """Deserialize a persisted paper-trading session, migrating legacy specs."""
        if isinstance(raw, cls):
            return raw
        return cls.model_validate(raw, context={"legacy_spec": True})

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
