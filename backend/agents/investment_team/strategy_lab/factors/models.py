"""Genome schema for the Strategy Lab factor DSL (issue #249, Phase A).

The genome is a recursive Pydantic union describing one trading strategy:

* numeric nodes (return ``float``) compose into indicators and weighted sums,
* boolean nodes (return ``bool``) compose entry / exit conditions,
* a sizing node decides position size,
* a top-level ``Genome`` glues those together with asset class, risk limits,
  hypothesis text, and free-form metadata.

Each node type carries a ``type: Literal[...]`` tag so the union is discriminable
both by Pydantic and by tools (UI, diff, tree-edit distance).  The compiler in
``compiler.py`` walks this tree to emit a ``contract.Strategy`` subclass.

Phase A primitives include OHLCV-derivable factors (SMA/EMA/RSI/MACD/Bollinger/
ATR/ADX/Stochastic/VWAP/Momentum/ZScore/Skew/VolRegime) plus two cross-asset
factors (TermStructureSlope, FundingRateDeviation) which compile to NaN-emitting
helpers until the cross-asset data feed lands in a follow-up issue.
"""

from __future__ import annotations

from typing import Annotated, Dict, List, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...execution.risk_filter import RiskLimits

AssetClass = Literal["stocks", "crypto", "forex", "options", "futures", "commodities"]

PriceField = Literal["open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# Numeric primitives — return ``float`` evaluated at the most recent bar.
# ---------------------------------------------------------------------------


class _NodeBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Price(_NodeBase):
    type: Literal["price"] = "price"
    field: PriceField = "close"


class Const(_NodeBase):
    type: Literal["const"] = "const"
    value: float


class SMA(_NodeBase):
    type: Literal["sma"] = "sma"
    period: int = Field(ge=2, le=400)


class EMA(_NodeBase):
    type: Literal["ema"] = "ema"
    period: int = Field(ge=2, le=400)


class RSI(_NodeBase):
    type: Literal["rsi"] = "rsi"
    period: int = Field(default=14, ge=2, le=200)


class MACDSignal(_NodeBase):
    """MACD signal-line value (EMA of the MACD line).

    Lookback is ``slow + signal - 1`` (the signal-EMA seeds once ``signal``
    macd-line samples exist, the first of which needs ``slow`` bars) — see
    ``compiler._lookback``'s ``MACDSignal`` branch for the full formula and
    its cross-reference to ``registry_metadata.lookback_for``. Prior
    revisions used ``slow + signal`` (one bar later); genomes compiled
    against that older formula fire signals one bar earlier now, so
    persisted backtest output predating the fix will not bit-reproduce
    against re-runs through the corrected compiler.
    """

    type: Literal["macd_signal"] = "macd_signal"
    fast: int = Field(default=12, ge=2, le=200)
    slow: int = Field(default=26, ge=3, le=400)
    signal: int = Field(default=9, ge=2, le=100)


class BollingerZ(_NodeBase):
    """Z-score of close vs ``period``-bar mean, in units of rolling stdev."""

    type: Literal["bollinger_z"] = "bollinger_z"
    period: int = Field(default=20, ge=5, le=200)


class ATR(_NodeBase):
    type: Literal["atr"] = "atr"
    period: int = Field(default=14, ge=2, le=200)


class ADX(_NodeBase):
    type: Literal["adx"] = "adx"
    period: int = Field(default=14, ge=2, le=200)


class StochasticK(_NodeBase):
    type: Literal["stoch_k"] = "stoch_k"
    period: int = Field(default=14, ge=2, le=200)


class VWAP(_NodeBase):
    type: Literal["vwap"] = "vwap"
    period: int = Field(default=20, ge=2, le=400)


class MomentumK(_NodeBase):
    """k-bar log-return / k-bar realised vol — a normalised momentum score."""

    type: Literal["momentum_k"] = "momentum_k"
    k: int = Field(ge=1, le=400)


class ZScoreResidualOLS(_NodeBase):
    """Z-score of the rolling-OLS residual of close vs ``vs_symbol`` close.

    ``window`` is NOT currently consulted by ``compiler._lookback`` — the
    aux cross-symbol feed isn't wired yet (the compiled body unconditionally
    returns NAN), so the lookback is pinned to a minimal constant instead of
    ``window`` to avoid inflating a genome's warm-up requirement for a
    primitive that can never contribute a value. Restore
    ``compiler._lookback``'s ``ZScoreResidualOLS`` branch to ``node.window``
    when the aux feed lands and the body computes a real OLS residual.

    BACKWARDS-COMPATIBILITY WARNING: a genome's overall warm-up gate
    (``MIN_HISTORY``) is the max lookback over every node, so pinning this
    dead node's lookback to a minimal constant also lowers the warm-up gate
    of the WHOLE genome when this node was its deepest one. A composite
    genome pairing it with a faster live signal now begins submitting
    entries as soon as the live branch is warm instead of waiting up to
    ``window`` (400) bars — an intentional un-inflation (the dead branch
    never fired), but one that changes trade timing and will not
    bit-reproduce stored backtest artefacts for such genomes.
    """

    type: Literal["zscore_residual_ols"] = "zscore_residual_ols"
    window: int = Field(default=60, ge=10, le=400)
    vs_symbol: str


class Skew(_NodeBase):
    type: Literal["skew"] = "skew"
    window: int = Field(default=20, ge=5, le=200)


class VolRegimeState(_NodeBase):
    """Discrete regime label: 0 = low vol, 1 = mid, 2 = high (relative to lookback)."""

    type: Literal["vol_regime_state"] = "vol_regime_state"
    lookback: int = Field(default=60, ge=10, le=400)
    threshold: float = Field(default=1.0, gt=0)


# ---- Cross-asset (compile to NaN-returning helpers until aux feed lands) -----


class TermStructureSlope(_NodeBase):
    type: Literal["term_structure_slope"] = "term_structure_slope"
    front_symbol: str
    back_symbol: str
    window: int = Field(default=20, ge=5, le=200)


class FundingRateDeviation(_NodeBase):
    type: Literal["funding_rate_deviation"] = "funding_rate_deviation"
    symbol: str
    lookback: int = Field(default=24, ge=2, le=400)


# ---------------------------------------------------------------------------
# Numeric combinators.
# ---------------------------------------------------------------------------


class WeightedSum(_NodeBase):
    type: Literal["weighted_sum"] = "weighted_sum"
    children: List["NumNode"]
    weights: List[float]

    @model_validator(mode="after")
    def _check_lengths(self) -> "WeightedSum":
        if len(self.children) != len(self.weights):
            raise ValueError(
                f"weighted_sum: children ({len(self.children)}) and weights "
                f"({len(self.weights)}) must have equal length"
            )
        if not self.children:
            raise ValueError("weighted_sum must have at least one child")
        return self


class IfRegime(_NodeBase):
    """``gate`` boolean selects between two numeric branches."""

    type: Literal["if_regime"] = "if_regime"
    gate: "BoolNode"
    if_true: "NumNode"
    if_false: "NumNode"


# ---------------------------------------------------------------------------
# Boolean primitives & combinators.
# ---------------------------------------------------------------------------


class CompareGT(_NodeBase):
    type: Literal["gt"] = "gt"
    left: "NumNode"
    right: "NumNode"


class CompareLT(_NodeBase):
    type: Literal["lt"] = "lt"
    left: "NumNode"
    right: "NumNode"


class CrossOver(_NodeBase):
    """``fast`` crosses above ``slow`` between the previous and current bar."""

    type: Literal["crossover"] = "crossover"
    fast: "NumNode"
    slow: "NumNode"


class CrossUnder(_NodeBase):
    type: Literal["crossunder"] = "crossunder"
    fast: "NumNode"
    slow: "NumNode"


class ATRBreakout(_NodeBase):
    """Close exceeds the rolling ``k``-bar high by ``atr_mult`` × ATR(period)."""

    type: Literal["atr_breakout"] = "atr_breakout"
    k: int = Field(default=20, ge=2, le=400)
    atr_mult: float = Field(default=1.0, gt=0)
    atr_period: int = Field(default=14, ge=2, le=200)


class BoolAnd(_NodeBase):
    type: Literal["and"] = "and"
    children: List["BoolNode"]

    @model_validator(mode="after")
    def _check_children(self) -> "BoolAnd":
        if not self.children:
            raise ValueError("and: must have at least one child")
        return self


class BoolOr(_NodeBase):
    type: Literal["or"] = "or"
    children: List["BoolNode"]

    @model_validator(mode="after")
    def _check_children(self) -> "BoolOr":
        if not self.children:
            raise ValueError("or: must have at least one child")
        return self


class BoolNot(_NodeBase):
    type: Literal["not"] = "not"
    child: "BoolNode"


# ---------------------------------------------------------------------------
# Sizing.
# ---------------------------------------------------------------------------


class FixedQty(_NodeBase):
    type: Literal["fixed_qty"] = "fixed_qty"
    qty: float = Field(gt=0)


class PctOfEquity(_NodeBase):
    type: Literal["pct_of_equity"] = "pct_of_equity"
    pct: float = Field(gt=0, le=100)


class VolTargeted(_NodeBase):
    type: Literal["vol_targeted"] = "vol_targeted"
    target_annual_vol: float = Field(gt=0)
    lookback: int = Field(default=20, ge=5, le=400)


# ---------------------------------------------------------------------------
# Discriminated unions.
# ---------------------------------------------------------------------------

NumNode = Annotated[
    Union[
        Price,
        Const,
        SMA,
        EMA,
        RSI,
        MACDSignal,
        BollingerZ,
        ATR,
        ADX,
        StochasticK,
        VWAP,
        MomentumK,
        ZScoreResidualOLS,
        Skew,
        VolRegimeState,
        TermStructureSlope,
        FundingRateDeviation,
        WeightedSum,
        IfRegime,
    ],
    Field(discriminator="type"),
]

BoolNode = Annotated[
    Union[
        CompareGT,
        CompareLT,
        CrossOver,
        CrossUnder,
        ATRBreakout,
        BoolAnd,
        BoolOr,
        BoolNot,
    ],
    Field(discriminator="type"),
]

SizingNode = Annotated[
    Union[FixedQty, PctOfEquity, VolTargeted],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Top-level Genome.
# ---------------------------------------------------------------------------


class Genome(BaseModel):
    """One ideated strategy as a typed factor tree."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["genome"] = "genome"
    asset_class: AssetClass
    hypothesis: str = ""
    signal_definition: str = ""
    entry: BoolNode
    exit: BoolNode
    sizing: SizingNode
    risk_limits: RiskLimits = Field(default_factory=RiskLimits)
    speculative: bool = False
    metadata: Dict[str, str] = Field(default_factory=dict)


# Resolve forward references for nodes that point back into the unions.
WeightedSum.model_rebuild()
IfRegime.model_rebuild()
CompareGT.model_rebuild()
CompareLT.model_rebuild()
CrossOver.model_rebuild()
CrossUnder.model_rebuild()
BoolAnd.model_rebuild()
BoolOr.model_rebuild()
BoolNot.model_rebuild()
Genome.model_rebuild()


def parse_genome(payload: dict | str) -> Genome:
    """Parse a genome from a JSON string or already-decoded dict."""

    if isinstance(payload, (bytes, bytearray, str)):
        return Genome.model_validate_json(payload)
    return Genome.model_validate(payload)
