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

from typing import TYPE_CHECKING, Optional

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
        root = primary_symbol.upper().rstrip("=F")[:2]
        return _FUTURES_FAMILY_BENCHMARK.get(root, default)

    return default
