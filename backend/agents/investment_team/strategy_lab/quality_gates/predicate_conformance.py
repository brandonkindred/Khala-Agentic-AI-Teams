"""Pre-execution predicate conformance shadow check.

For every ``EntryRule`` and ``SignalExitRule`` whose predicate the LLM
translated into ``on_bar`` logic, the gate:

1. Generates ~50-80 synthetic bars that exercise both true and false
   predicate states (via :mod:`predicate_conformance_fixtures`).
2. Runs the strategy's ``on_bar`` in-process via a lightweight
   ``_ShadowContext`` that records every ``submit_order`` call with its
   bar index and side.
3. Runs the engine's ``evaluate_predicate()`` on the same bars.
4. Compares per-bar: any disagreement (strategy orders on a
   predicate-false bar, or misses a predicate-true bar when no
   position blocks the entry) is a conformance failure.

The gate only runs for ``requires_custom_code=True`` strategies — the
compiled path (engine-managed) emits zero ``submit_order`` calls and
has no inline predicate logic to drift.

Routing on failure: critical results join the synthesis loop's
``critical_failures`` collection and route through
``_refine_or_exhaust(failure_phase="validation", ...)``.  The
``rule_id`` and per-bar diff are surfaced in ``failure_details`` so the
refinement agent can target the exact branch.

Retry demotion: after ``attempt >= _code_conformance_retries()``,
criticals are demoted to warnings so the pipeline can proceed to
backtest rather than looping indefinitely on an edge case the LLM
cannot resolve.
"""

from __future__ import annotations

import builtins
import logging
import math
import statistics
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Optional, Type

from ..spec_dsl import EntryRule as _EntryRule
from ..spec_dsl import SignalExitRule as _SignalExitRule
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase
from .predicate_conformance_fixtures import ConformanceFixture, generate_conformance_fixtures

logger = logging.getLogger(__name__)

GATE: str = "predicate_conformance"


def _code_conformance_retries() -> int:
    """Resolved per call so tests can override via env or monkeypatch.

    Preconditions:
      Env value, when set, parses to ``int``.
    Postconditions:
      Returns a non-negative integer. Default 2; garbage values fall
      back to 2.
    """
    import os

    raw = os.environ.get("STRATEGY_LAB_CODE_CONFORMANCE_RETRIES", "2")
    try:
        return max(int(raw), 0)
    except ValueError:
        return 2


@dataclass
class _OrderRecord:
    """One ``submit_order`` call captured by the shadow context."""

    bar_index: int
    symbol: str
    side: str
    qty: float
    reason: str = ""


class _ShadowContext:
    """Lightweight mock of ``StrategyContext`` for in-process shadow execution.

    Records ``submit_order`` calls per bar without any fill simulation.
    Position state is tracked optimistically so the strategy's
    ``if position is None:`` guards evaluate correctly.

    Invariants:
      - ``_history[sym]`` never exceeds 500 bars (matching the real context).
      - ``orders`` is append-only during the shadow run.
    """

    def __init__(self, *, initial_capital: float = 100_000.0) -> None:
        self._history: Dict[str, list] = {}
        self._positions: Dict[str, _SimplePosition] = {}
        self._capital: float = initial_capital
        self._equity: float = initial_capital
        self._now: str = ""
        self._is_warmup: bool = False
        self._current_bar_index: int = -1
        self.orders: List[_OrderRecord] = []

    @property
    def capital(self) -> float:
        return self._capital

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def now(self) -> str:
        return self._now

    @property
    def is_warmup(self) -> bool:
        return self._is_warmup

    def position(self, symbol: str) -> Optional[_SimplePosition]:
        return self._positions.get(symbol)

    def history(self, symbol: str, n: int) -> list:
        bars = self._history.get(symbol, [])
        if n <= 0:
            return []
        return bars[-n:]

    def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_type: Any = None,
        limit_price: Any = None,
        stop_price: Any = None,
        trail_offset: Any = None,
        trail_offset_kind: str = "abs",
        tif: Any = None,
        reason: str = "",
        unfilled_policy: Any = None,
        twap_slices: Any = None,
        attached_stop_loss: Any = None,
        attached_take_profit: Any = None,
        parent_order_id: Any = None,
        oco_group_id: Any = None,
    ) -> str:
        self.orders.append(
            _OrderRecord(
                bar_index=self._current_bar_index,
                symbol=symbol,
                side=str(side),
                qty=qty,
                reason=reason,
            )
        )
        side_lower = str(side).lower()
        if side_lower in ("buy", "long"):
            self._positions[symbol] = _SimplePosition(
                symbol=symbol,
                side="long",
                entry_price=self._last_close(symbol),
            )
        elif side_lower in ("sell", "short"):
            if symbol in self._positions:
                del self._positions[symbol]
        return f"shadow_{self._current_bar_index}"

    def cancel(self, order_id: str) -> None:
        pass

    def _ingest_bar(self, bar: Any, index: int) -> None:
        self._current_bar_index = index
        self._history.setdefault(bar.symbol, []).append(bar)
        hist = self._history[bar.symbol]
        if len(hist) > 500:
            del hist[:-500]
        self._now = bar.timestamp

    def _last_close(self, symbol: str) -> float:
        hist = self._history.get(symbol, [])
        if hist:
            return hist[-1].close
        return 0.0


@dataclass
class _SimplePosition:
    """Minimal stand-in for ``_PositionSnapshot``."""

    symbol: str
    side: str = "long"
    entry_price: float = 0.0
    qty: float = 1.0


class PredicateConformanceGate(GateResultsMixin):
    """Shadow check that compares per-bar ``submit_order`` decisions against
    the engine's ``evaluate_predicate()`` verdicts on synthetic fixtures.

    Contract:
      Pre: ``code`` is non-empty Python source that passed
      ``CodeSafetyChecker``; ``spec`` has ``entry_rules`` / ``exit_rules``.
      Post: returned list has one ``QualityGateResult`` per fixture.
      ``rule_id`` is set on every result.
    """

    GATE: ClassVar[str] = GATE

    def check(
        self,
        code: str,
        spec: Any,
        *,
        phase: StrategyLabPhase = "synthesis",
        attempt: int = 0,
    ) -> List[QualityGateResult]:
        """Run the predicate conformance shadow check.

        Preconditions:
          ``code`` is Python source; ``spec`` is a ``StrategySpec``.
          ``attempt`` is the zero-based retry counter from the orchestrator.
        Postconditions:
          One ``QualityGateResult`` per fixture. After ``attempt >=
          _code_conformance_retries()``, criticals are demoted to warnings.
        """
        with self._using_phase(phase):
            if not code or not code.strip():
                return [self._critical("Predicate conformance gate received empty strategy_code.")]

            if not getattr(spec, "requires_custom_code", False):
                return [
                    self._info("Skipped: engine-managed strategy has no inline predicate logic.")
                ]

            entry_rules = getattr(spec, "entry_rules", []) or []
            exit_rules = getattr(spec, "exit_rules", []) or []
            has_predicates = any(isinstance(r, _EntryRule) for r in entry_rules) or any(
                isinstance(r, _SignalExitRule) for r in exit_rules
            )
            if not has_predicates:
                return [self._info("No predicate-bearing rules to check.")]

            strategy_cls = _exec_strategy(code)
            if strategy_cls is None:
                return [
                    self._critical(
                        "Could not extract Strategy subclass from code.",
                    )
                ]

            fixtures = generate_conformance_fixtures(spec)
            if not fixtures:
                return [self._info("No conformance fixtures generated.")]

            max_retries = _code_conformance_retries()
            demote = attempt >= max_retries
            results: List[QualityGateResult] = []

            for fixture in fixtures:
                result = self._check_fixture(strategy_cls, fixture, demote=demote)
                results.append(result)

            return results

    def _check_fixture(
        self,
        strategy_cls: Type,
        fixture: ConformanceFixture,
        *,
        demote: bool = False,
    ) -> QualityGateResult:
        if not fixture.synthesizable:
            return self._warning(
                f"Fixture unsynthesizable: {fixture.unsynthesizable_reason}",
                rule_id=fixture.rule_id,
            )

        ctx = _ShadowContext()
        try:
            strategy = strategy_cls()
        except Exception as exc:
            return self._critical(
                f"Strategy instantiation failed: {exc}",
                rule_id=fixture.rule_id,
            )

        for i, bar in enumerate(fixture.bars):
            shadow_bar = _to_shadow_bar(bar, fixture.symbol)
            ctx._ingest_bar(shadow_bar, i)
            try:
                strategy.on_bar(ctx, shadow_bar)
            except Exception:
                pass

        order_bars = {o.bar_index for o in ctx.orders}

        false_positives: List[int] = []
        false_negatives: List[int] = []
        position_open = False

        for i, verdict in enumerate(fixture.expected_verdicts):
            if verdict is None:
                continue

            has_order = i in order_bars

            if fixture.rule_kind == "entry":
                if verdict and not position_open and not has_order:
                    false_negatives.append(i)
                elif not verdict and has_order and not position_open:
                    false_positives.append(i)

                if has_order and not position_open:
                    position_open = True
                for o in ctx.orders:
                    if o.bar_index == i and str(o.side).lower() in ("sell", "short"):
                        position_open = False
            else:
                if verdict and position_open and not has_order:
                    false_negatives.append(i)
                elif not verdict and has_order and position_open:
                    false_positives.append(i)

                if has_order and position_open:
                    position_open = False

        if not false_positives and not false_negatives:
            return self._info(
                f"Predicate conformance OK ({len(fixture.bars)} bars checked).",
                rule_id=fixture.rule_id,
            )

        parts = [f"rule_id={fixture.rule_id}: predicate conformance failed."]
        if false_positives:
            parts.append(
                f"  False positives (order on predicate-false bar): bars {false_positives[:10]}"
            )
        if false_negatives:
            parts.append(
                f"  False negatives (no order on predicate-true bar): bars {false_negatives[:10]}"
            )
        detail = "\n".join(parts)

        if demote:
            return self._warning(detail, rule_id=fixture.rule_id)
        return self._critical(detail, rule_id=fixture.rule_id)


# ---------------------------------------------------------------------------
# Strategy class extraction
# ---------------------------------------------------------------------------


def _exec_strategy(code: str) -> Optional[Type]:
    """``exec()`` the strategy code and extract the Strategy subclass.

    Preconditions:
      ``code`` has passed ``CodeSafetyChecker``.
    Postconditions:
      Returns the Strategy subclass, or ``None`` on failure.
    """
    _BLOCKED = frozenset({"exec", "eval", "compile", "__import__", "open", "input", "breakpoint"})
    safe_builtins = {k: v for k, v in vars(builtins).items() if k not in _BLOCKED}
    namespace: Dict[str, Any] = {
        "__builtins__": safe_builtins,
        "math": math,
        "statistics": statistics,
    }

    try:
        exec(code, namespace)  # noqa: S102
    except Exception:
        return None

    for obj in namespace.values():
        if isinstance(obj, type) and obj.__name__ != "Strategy" and _has_on_bar(obj):
            return obj

    return None


def _has_on_bar(cls: type) -> bool:
    return callable(getattr(cls, "on_bar", None))


def _to_shadow_bar(ohlcv: Any, symbol: str) -> _ShadowBar:
    """Convert an ``OHLCVBar`` to a bar object the strategy's ``on_bar`` can consume."""
    return _ShadowBar(
        symbol=symbol,
        timestamp=ohlcv.date,
        timeframe="1d",
        open=ohlcv.open,
        high=ohlcv.high,
        low=ohlcv.low,
        close=ohlcv.close,
        volume=ohlcv.volume,
    )


class _ShadowBar:
    """Lightweight bar matching the ``Bar`` protocol the strategy reads."""

    __slots__ = ("symbol", "timestamp", "timeframe", "open", "high", "low", "close", "volume")

    def __init__(
        self,
        *,
        symbol: str,
        timestamp: str,
        timeframe: str,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        self.symbol = symbol
        self.timestamp = timestamp
        self.timeframe = timeframe
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


__all__ = ["GATE", "PredicateConformanceGate"]
