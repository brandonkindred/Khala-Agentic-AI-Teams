"""Default benchmark symbols per asset class for performance attribution.

Rationale (see issue #174 plan):

- **US equities** → ``SPY`` (total-market proxy).
- **Crypto** → ``BTC-USD`` (industry-standard beta reference).
- **Forex** → ``DX-Y.NYB`` (ICE Dollar Index) for USD-quoted pairs; crosses
  currently fall back to DXY (a trade-weighted basket overlay can slot in here
  later without changing callers).
- **Futures** — routed by contract family:
    - equity-index (ES, NQ, …) → ``SPY``
    - rates/bonds (ZN, ZB, ZF) → ``AGG``
    - energy/metals/ags and broad commodities → ``DBC``
    - unknown/broad multi-asset → ``SPY`` (conservative default)
- **Commodities** → ``DBC``.

Callers can always override via ``StrategySpec.audit.calc_artifacts`` or an
explicit ``benchmark_symbol`` on the request/config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from ..models import StrategySpec


DEFAULT_BENCHMARK_BY_ASSET_CLASS: dict[str, str] = {
    "stocks": "SPY",
    "options": "SPY",
    "crypto": "BTC-USD",
    "forex": "DX-Y.NYB",
    "commodities": "DBC",
    "futures": "SPY",
}


_FUTURES_FAMILY_BENCHMARK: dict[str, str] = {
    # equity-index futures
    "ES": "SPY",
    "NQ": "SPY",
    "YM": "SPY",
    "RTY": "SPY",
    # rates/bonds
    "ZN": "AGG",
    "ZB": "AGG",
    "ZF": "AGG",
    "ZT": "AGG",
    "UB": "AGG",
    # energy
    "CL": "DBC",
    "NG": "DBC",
    "HO": "DBC",
    "RB": "DBC",
    # metals
    "GC": "DBC",
    "SI": "DBC",
    "HG": "DBC",
    "PL": "DBC",
    "PA": "DBC",
    # ags
    "ZC": "DBC",
    "ZS": "DBC",
    "ZW": "DBC",
    "CT": "DBC",
}


def benchmark_for_strategy(
    strategy: "StrategySpec",
    *,
    primary_symbol: Optional[str] = None,
) -> str:
    """Return the best default benchmark symbol for a given strategy.

    For futures strategies, ``primary_symbol`` (when provided) is used to
    route to the right family benchmark (equity index → SPY, rates → AGG,
    commodities → DBC). Unknown families fall back to SPY.
    """
    asset = (strategy.asset_class or "").lower().strip()
    default = DEFAULT_BENCHMARK_BY_ASSET_CLASS.get(asset, "SPY")

    if asset == "futures" and primary_symbol:
        sym = primary_symbol.upper()
        root = sym.removesuffix("=F")[:2]
        return _FUTURES_FAMILY_BENCHMARK.get(root, default)

    return default


# ---------------------------------------------------------------------------
# 60/40 benchmark composition (issue #247 step 6)
# ---------------------------------------------------------------------------


def build_weighted_blend_equity(
    equity_stock: Sequence[float],
    equity_bond: Sequence[float],
    *,
    weights: Tuple[float, float] = (0.6, 0.4),
    initial_capital: float = 100_000.0,
) -> List[float]:
    """Compound a daily-aligned weighted-blend equity curve.

    Blends per-period returns from two source curves by ``weights`` (must sum
    to 1) and compounds from ``initial_capital``. Input curves must be
    1-to-1 aligned by date. Returns a list of the same length as the shorter
    input. ``build_60_40_equity`` below is the named convenience wrapper.
    """
    w_s, w_b = weights
    if abs(w_s + w_b - 1.0) > 1e-9:
        raise ValueError(f"weights must sum to 1, got {weights}")
    n = min(len(equity_stock), len(equity_bond))
    if n == 0:
        return []
    out: List[float] = [initial_capital]
    for i in range(1, n):
        s_prev = equity_stock[i - 1]
        b_prev = equity_bond[i - 1]
        r_s = (equity_stock[i] - s_prev) / s_prev if s_prev > 0 else 0.0
        r_b = (equity_bond[i] - b_prev) / b_prev if b_prev > 0 else 0.0
        out.append(out[-1] * (1.0 + w_s * r_s + w_b * r_b))
    return out


def build_60_40_equity(
    equity_spy: Sequence[float],
    equity_agg: Sequence[float],
    *,
    initial_capital: float = 100_000.0,
) -> List[float]:
    """Synthesize a 60 SPY / 40 AGG daily equity curve.

    Convenience wrapper around :func:`build_weighted_blend_equity`. Per the
    discussion in issue #247, this is the default benchmark composition for
    the regime-conditional acceptance gate; callers may override via
    ``BacktestConfig.benchmark_composition``.
    """
    return build_weighted_blend_equity(
        equity_spy, equity_agg, weights=(0.6, 0.4), initial_capital=initial_capital
    )
