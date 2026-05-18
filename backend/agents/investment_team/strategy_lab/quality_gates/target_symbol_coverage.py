"""Issue #526 — assert the backtest universe matches the requested symbols.

Without this gate, a hypothesis naming ``QQQ`` can silently be backtested on
the asset-class default universe (TSLA/AAPL/...) and the analysis agent
will confidently write about the wrong tickers. The gate fires twice per
backtest round:

* ``check_fetch`` — right after market data is fetched; verifies that every
  entry in ``spec.target_symbols`` was actually fetched. When
  ``spec.target_symbols`` is empty but the hypothesis text mentions a
  known ticker, emits a warning recommending the operator set it
  explicitly.
* ``check_trades`` — right after the backtest produces a trade ledger;
  verifies that every executed trade's symbol is inside
  ``spec.target_symbols``.

Critical failures from this gate cause the orchestrator to fail the run
closed (``is_winning`` stays False) without attempting code refinement —
a target-symbol mismatch is a spec/data issue, not something the LLM can
fix by rewriting strategy code.
"""

from __future__ import annotations

import re
from typing import ClassVar, List

from ...models import StrategySpec, TradeRecord
from ...symbols import (
    COMMODITY_SYMBOLS,
    CRYPTO_SYMBOLS,
    FOREX_SYMBOLS,
    FOREX_SYMBOLS_BARE,
    FUTURES_SYMBOLS,
    FUTURES_SYMBOLS_BARE,
    OTHER_SYMBOLS,
    STOCK_SYMBOLS,
)
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "target_symbol_coverage"

_KNOWN_TICKERS: frozenset[str] = frozenset(
    s.upper()
    for s in (
        *STOCK_SYMBOLS,
        *CRYPTO_SYMBOLS,
        *COMMODITY_SYMBOLS,
        *FOREX_SYMBOLS,
        *FOREX_SYMBOLS_BARE,
        *FUTURES_SYMBOLS,
        *FUTURES_SYMBOLS_BARE,
        *OTHER_SYMBOLS,
    )
)

# Uppercase tokens 2-6 chars; conservative enough to skip common English words
# while still catching tickers embedded in prose like "buy QQQ when ..." and
# 6-char forex bare names like EURUSD/USDJPY in FOREX_SYMBOLS_BARE.
_TICKER_RE = re.compile(r"\b([A-Z]{2,6})\b")


class TargetSymbolCoverageGate(GateResultsMixin):
    """Critical gate enforcing that the realized universe matches intent."""

    GATE: ClassVar[str] = GATE

    def check_fetch(
        self,
        spec: StrategySpec,
        requested_symbols: List[str],
        fetched_symbols: List[str],
        *,
        phase: StrategyLabPhase = "synthesis",
    ) -> List[QualityGateResult]:
        self._set_phase(phase)
        results: List[QualityGateResult] = []
        fetched_upper = {s.strip().upper() for s in fetched_symbols if s and s.strip()}

        if spec.target_symbols:
            target_upper = {s.strip().upper() for s in spec.target_symbols if s and s.strip()}
            missing = sorted(target_upper - fetched_upper)
            if missing:
                results.append(
                    self._critical("target_symbols missing from fetched market data: "
                            f"{missing}. Requested={sorted({s.upper() for s in requested_symbols})}, "
                            f"fetched={sorted(fetched_upper)}.")
                )
            else:
                results.append(
                    self._info(f"All {len(target_upper)} target_symbols present in fetched data.")
                )
        else:
            mentioned = sorted(self._tickers_in_hypothesis(spec.hypothesis or ""))
            if mentioned:
                results.append(
                    self._warning(f"Hypothesis mentions known ticker(s) {mentioned} but "
                            "spec.target_symbols is empty; the backtest is using the "
                            "asset-class default universe. Set spec.target_symbols "
                            "explicitly to guarantee the run trades the intended tickers.")
                )
            else:
                results.append(
                    self._info("spec.target_symbols empty; no specific tickers referenced in hypothesis.")
                )

        return results

    def check_trades(
        self,
        spec: StrategySpec,
        trades: List[TradeRecord],
        *,
        phase: StrategyLabPhase = "synthesis",
    ) -> List[QualityGateResult]:
        self._set_phase(phase)
        if not spec.target_symbols:
            return [
                self._info("spec.target_symbols empty; trade-symbol coverage check skipped.")
            ]

        target_upper = {s.strip().upper() for s in spec.target_symbols if s and s.strip()}
        offending = sorted({t.symbol.strip().upper() for t in trades if t.symbol} - target_upper)
        if offending:
            return [
                self._critical("Trade ledger contains symbols outside spec.target_symbols: "
                        f"{offending}. target_symbols={sorted(target_upper)}.")
            ]

        return [
            self._info(f"All {len(trades)} trades within target_symbols.")
        ]

    @staticmethod
    def _tickers_in_hypothesis(text: str) -> set[str]:
        return {tok for tok in _TICKER_RE.findall(text or "") if tok in _KNOWN_TICKERS}
