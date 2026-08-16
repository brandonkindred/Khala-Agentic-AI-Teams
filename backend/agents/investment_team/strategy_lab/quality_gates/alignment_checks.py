"""Deterministic trade-alignment gate.

Runs inside the orchestrator's trade-alignment loop, replacing the LLM
audit's "read the ledger and write prose" inner step. Each executed
``TradeRecord`` is run through seven deterministic checks against the
structured :class:`StrategySpec`:

  1. Universe — ``trade.symbol in spec.target_symbols``
  2. Side — ``trade.side`` matches an entry rule's declared side
  3. Sizing — ``trade.position_value`` within ±1% of the sizing formula
  4. Stop-loss attribution — engine-attributed stop closes pass; a
     realized loss past the nominal floor on a non-attributed close is
     INFORMATIONAL only (a stop is a trigger, not a price cap — fills
     gap past the threshold and other rules may close first).
  5. Take-profit attribution — symmetric to #4: engine-attributed
     take-profit closes pass; a realized gain past the nominal ceiling
     on a non-attributed close is INFORMATIONAL only.
  6. Entry-signal correlation — predicate(s) evaluate ``True`` on the
     entry bar; near-misses (within
     ``STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT``) optionally route to a
     narrow LLM adjudicator.
  7. Signal-exit correlation — when the spec carries
     :class:`SignalExitRule` and the engine did not attribute the
     close to a structured exit, at least one signal-exit predicate
     must evaluate ``True`` at the exit bar.

Results are emitted as per-rule :class:`AlignmentFinding`s for the
record-level ``BacktestRecord.alignment_findings`` field and as
:class:`QualityGateResult` rows for ``quality_gate_results``. The gate
itself does no I/O — ``market_data`` is the same in-memory
``Dict[str, List[OHLCVBar]]`` the orchestrator already fetched.

The re-execution loop is preserved upstream. When this gate reports
``aligned=False``, the orchestrator hands findings to
:meth:`TradeAlignmentAgent.propose_code_fix`, which returns a narrow
code-only patch; the orchestrator then re-executes and re-checks on the
next iteration, up to ``MAX_ALIGNMENT_ROUNDS`` times.
"""

from __future__ import annotations

import logging
import math
import os
import time
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Optional, Protocol

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from shared.concurrency import parallel_map
from shared.env_config import env_int

from ..agents._llm_envelope import _FAILURE_FMT
from ..alignment_findings import (
    AlignmentFinding,
    NearMissVerdict,
    Severity,
    entry_rule_id,
    signal_exit_rule_id,
)
from ..executor.predicate_evaluator import (
    PandasHistoryView,
)
from ..executor.predicate_evaluator import (
    evaluate_tree as _evaluate_tree,
)
from ..executor.predicate_evaluator import (
    relative_miss as _relative_miss_shared,
)
from ..spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    VolatilityTargetSizing,
    format_predicate_tree,
)
from .models import GateResultsMixin, QualityGateResult

logger = logging.getLogger(__name__)

GATE = "trade_alignment"

# Sizing tolerance is a literal ±1% requirement from the issue.
_SIZING_TOL = 0.01

_DEFAULT_NEAR_MISS_PCT = 0.01

# The execution engine uses a ``signal-on-T / fill-on-T+1`` contract:
# the strategy sees bar T and submits an order; the engine fills on bar
# T+1; the resulting ``TradeRecord.entry_date`` / ``TradeRecord.exit_date``
# record the FILL bar (T+1), not the signal bar (T). Verified against
# ``backend/agents/investment_team/trading_service/engine/fill_simulator.py``
# where ``Position.entry_timestamp`` and the emitted ``TradeRecord`` are
# both stamped from the bar that fills the order. The alignment gate
# must therefore evaluate spec predicates at the SIGNAL bar (one before
# the recorded entry/exit date), or transient signals (cross events,
# threshold-touches) will be marked misaligned even when execution was
# correct.
_FILL_DELAY_BARS = 1


# ---------------------------------------------------------------------------
# Adjudicator protocol
# ---------------------------------------------------------------------------


class NearMissAdjudicator(Protocol):
    """Callable interface the deterministic gate uses to consult the LLM.

    Implemented by :class:`TradeAlignmentAgent.adjudicate_near_miss`.
    Lifted to a Protocol so the gate has no import dependency on the
    agent module (the agent imports the gate's models).
    """

    def __call__(
        self,
        *,
        rule_id: str,
        predicate_repr: str,
        computed_value: float,
        threshold: float,
        symbol: str,
        entry_date: str,
    ) -> NearMissVerdict: ...


# ---------------------------------------------------------------------------
# Tolerance helpers
# ---------------------------------------------------------------------------


def _near_miss_pct() -> float:
    """Resolve ``STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT``.

    Pre: env value, when set, parses as a finite non-negative float.
    Post: returns a non-negative float. ``0`` disables the LLM
    adjudicator (any predicate miss is a hard fail). Default ``0.01``
    = 1% relative tolerance.
    """
    raw = os.environ.get("STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT")
    if not raw:
        return _DEFAULT_NEAR_MISS_PCT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT=%r; using default %.4f",
            raw,
            _DEFAULT_NEAR_MISS_PCT,
        )
        return _DEFAULT_NEAR_MISS_PCT
    if not math.isfinite(value) or value < 0:
        logger.warning(
            "STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT=%r is out of range; using default %.4f",
            raw,
            _DEFAULT_NEAR_MISS_PCT,
        )
        return _DEFAULT_NEAR_MISS_PCT
    return value


_DEFAULT_ADJUDICATION_CONCURRENCY = 4


def _adjudication_concurrency() -> int:
    """Resolve ``STRATEGY_LAB_ALIGNMENT_ADJUDICATION_CONCURRENCY``.

    Pre: env value, when set, parses as an ``int``.
    Post: returns a positive integer — the maximum number of near-miss LLM
    adjudications run concurrently per gate ``check``. Default ``4``; sub-1
    overrides floor to ``1`` (fully serial); garbage values fall back to the
    default. The verdict applied to each trade is independent of how many
    run in parallel, so this knob trades cloud concurrency for wall time
    without changing the gate's output.
    """
    return env_int(
        "STRATEGY_LAB_ALIGNMENT_ADJUDICATION_CONCURRENCY",
        _DEFAULT_ADJUDICATION_CONCURRENCY,
        floor=1,
    )


@dataclass
class _PendingNearMiss:
    """A deferred near-miss adjudication awaiting the LLM verdict.

    ``f_slot`` / ``g_slot`` are the reserved indices in the ``findings`` /
    ``gate_results`` lists (placeholder ``None``s) that the resolved finding
    and its gate row are written back into, so concurrent adjudication
    preserves the exact serial ordering of the output lists.
    """

    trade: Any
    evaluation: Dict[str, Any]
    f_slot: int
    g_slot: int


def _entry_equity_by_trade(trades: List[Any], initial_capital: float) -> Dict[int, float]:
    """Equity *realized* at each trade's entry, for sizing check #3.

    Each trade's baseline is ``initial_capital + sum(prior.net_pnl for prior
    in trades if prior.exit_date <= trade.entry_date)`` excluding the trade
    itself. A naive left-fold over net_pnl in entry-date order would leak
    future PnL from still-open overlapping trades into earlier entries (trade
    A entering before B but exiting after B's entry must not contribute its
    PnL to B's baseline), so the sum is gated on each prior's *exit* date.

    Implemented as a sorted-exit-date prefix sum + binary search rather than
    the naive per-trade rescan over all priors: one ascending exit-date
    timeline carries a cumulative-PnL prefix, and each trade's baseline is the
    prefix value at ``bisect_right(exit_dates, entry_date)`` — exactly the
    priors with ``exit_date <= entry_date``. O(n log n) overall vs. the prior
    O(n²), and value-identical to it (modulo float summation order — a prefix
    sum and a per-trade re-sum accumulate rounding differently).

    Preconditions:
      - Every ``trade`` exposes ``trade_num``, ``entry_date``, ``exit_date``,
        ``net_pnl``; ``entry_date``/``exit_date`` are mutually comparable.
      - ``trade_num`` is unique across ``trades``.
    Postconditions:
      - Returns ``{trade_num: initial_capital + realized_prior_pnl}`` for every
        trade. Pure: no side effects.
    """
    initial = float(initial_capital)
    exit_events = sorted(
        ((t.exit_date, float(t.net_pnl)) for t in trades),
        key=lambda event: event[0],
    )
    exit_dates = [event[0] for event in exit_events]
    prefix_pnl = [0.0]
    for _date, pnl in exit_events:
        prefix_pnl.append(prefix_pnl[-1] + pnl)

    equity: Dict[int, float] = {}
    for trade in trades:
        realized_pnl = prefix_pnl[bisect_right(exit_dates, trade.entry_date)]
        # A trade is never its own prior unless it exits on or before its own
        # entry (degenerate same-bar fill); subtract its PnL in that case to
        # mirror the original ``prior.trade_num != trade.trade_num`` guard.
        if trade.exit_date <= trade.entry_date:
            realized_pnl -= float(trade.net_pnl)
        equity[trade.trade_num] = initial + realized_pnl
    return equity


def _relative_miss(computed: float, threshold: float) -> float:
    """Delegate to the shared predicate evaluator."""
    return _relative_miss_shared(computed, threshold)


# ---------------------------------------------------------------------------
# Indicator evaluation
# ---------------------------------------------------------------------------


def _bars_to_frame(bars: List[Any]) -> pd.DataFrame:
    """Convert ``List[OHLCVBar]`` to a ``DataFrame`` indexed by date.

    Returns a DataFrame with columns ``open/high/low/close/volume`` and
    a string-typed date index. Order is preserved from the input list.
    """
    rows = [
        {
            "date": b.date,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in bars
    ]
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.set_index("date")
    return df


def _format_predicate(p: Predicate) -> str:
    """Render a predicate as ``lhs op rhs`` for human-readable details.

    Used as the leaf renderer passed to the shared
    :func:`spec_dsl.format_predicate_tree`, so an ``all_of`` / ``any_of`` ``when``
    reads back as ``(lhs op rhs and …)`` in alignment findings without this module
    re-implementing the tree-walk.
    """
    return f"{p.lhs!r} {p.op} {p.rhs!r}"


# ---------------------------------------------------------------------------
# Sizing helpers
# ---------------------------------------------------------------------------


def _expected_position_value(
    sizing: Any,
    *,
    equity_at_entry: float,
    entry_price: float,
) -> Optional[float]:
    """Compute the spec-implied position-value (USD) at trade entry.

    Pre: ``equity_at_entry > 0`` and ``entry_price > 0``.
    Post: returns the expected position value, or ``None`` when the
    sizing variant is not numerically resolvable from trade-level data
    (e.g. :class:`VolatilityTargetSizing`, which needs realized
    portfolio volatility the gate doesn't compute).
    """
    if isinstance(sizing, FixedFractionSizing):
        return float(sizing.fraction) * float(equity_at_entry)
    if isinstance(sizing, FixedNotionalSizing):
        return float(sizing.notional_usd)
    if isinstance(sizing, VolatilityTargetSizing):
        # Vol-target sizing requires the realized volatility stream the
        # engine computed at entry time. The alignment gate doesn't have
        # that signal, so this variant is not numerically checkable
        # here — emit a soft skip rather than a false positive.
        return None
    return None


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class DeterministicAlignmentChecker(GateResultsMixin):
    """Run the seven deterministic alignment checks over a trade ledger.

    Contract:
      Pre: every input is a settled artifact from the synthesis loop —
        ``spec`` is the immutable post-ideation spec, ``trades`` is the
        completed ledger, ``market_data`` is the OHLCV dictionary the
        orchestrator passed to the sandbox.
      Post: :meth:`check` returns an :class:`AlignmentCheckResult` whose
        ``aligned`` flag is the conjunction of every critical-severity
        finding's ``passed`` value, and whose ``findings`` /
        ``gate_results`` lists are aligned 1:1 by index.
      Invariants: no I/O; no LLM call unless a near-miss adjudicator is
        supplied AND a check #6 predicate misses within the near-miss
        tolerance.
    """

    GATE: ClassVar[str] = GATE

    def check(
        self,
        *,
        spec: Any,
        trades: List[Any],
        market_data: Dict[str, List[Any]],
        initial_capital: float,
        near_miss_adjudicator: Optional[NearMissAdjudicator] = None,
    ) -> "AlignmentCheckResult":
        """Run every check across every trade.

        Pre: ``initial_capital > 0``; ``market_data`` is keyed by symbol
        and contains the bars used by the backtest that produced
        ``trades``. ``near_miss_adjudicator`` is consulted only when
        :func:`_near_miss_pct` is non-zero and a check #6 predicate misses
        within tolerance.
        Post: returned :class:`AlignmentCheckResult` carries one or more
        :class:`AlignmentFinding`s per trade and matching
        :class:`QualityGateResult` rows. ``aligned`` is ``True`` iff
        every ``severity="critical"`` finding ``passed``.
        """
        assert initial_capital > 0, "initial_capital must be positive"

        with self._using_phase("verification"):
            findings: List[AlignmentFinding] = []
            gate_results: List[QualityGateResult] = []

            # Pre-compute the per-symbol OHLCV frame once per gate run —
            # repeated indicator evaluation across trades on the same
            # symbol then reuses the same DataFrame.
            frames: Dict[str, pd.DataFrame] = {
                symbol: _bars_to_frame(bars) for symbol, bars in market_data.items()
            }
            indicator_caches: Dict[str, Dict[str, pd.Series]] = {
                symbol: {} for symbol in market_data
            }

            # Entry-equity baseline for sizing check #3 — see
            # :func:`_entry_equity_by_trade` for the contract.
            entry_equity_by_trade = _entry_equity_by_trade(trades, initial_capital)

            if not trades:
                # No trades to check — the orchestrator will treat this
                # as aligned vacuously; the upstream "zero trades"
                # critical path is already covered by other gates.
                info = self._info(
                    "No trades to align: deterministic alignment is "
                    "vacuously satisfied. Zero-trade handling is owned "
                    "by upstream gates.",
                    rule_id=None,
                )
                gate_results.append(info)
                return AlignmentCheckResult(
                    aligned=True,
                    findings=findings,
                    gate_results=gate_results,
                    rationale="No trades produced — alignment vacuously satisfied.",
                )

            near_miss_pct = _near_miss_pct()

            # Near-miss LLM adjudications are collected here during the trade
            # loop and dispatched concurrently afterwards (one bounded thread
            # pool) instead of blocking the loop one trade at a time.
            pending_near_misses: List[_PendingNearMiss] = []

            for trade in trades:
                # The seven checks. Each yields zero-or-more findings
                # (and matching gate_result rows). Critical failures
                # drive ``aligned=False``.
                self._check_universe(spec, trade, findings, gate_results)
                self._check_side(spec, trade, findings, gate_results)
                self._check_sizing(
                    spec,
                    trade,
                    findings,
                    gate_results,
                    entry_equity=entry_equity_by_trade[trade.trade_num],
                )
                self._check_stop_loss(spec, trade, findings, gate_results)
                self._check_take_profit(spec, trade, findings, gate_results)
                self._check_entry_signal(
                    spec,
                    trade,
                    frames,
                    indicator_caches,
                    near_miss_pct,
                    near_miss_adjudicator,
                    findings,
                    gate_results,
                    pending_near_misses,
                )
                self._check_signal_exit(
                    spec,
                    trade,
                    frames,
                    indicator_caches,
                    findings,
                    gate_results,
                )

            # Fill in every deferred near-miss verdict (concurrently) before
            # the aligned/critical roll-up reads the findings list.
            self._resolve_pending_near_misses(
                pending_near_misses, near_miss_adjudicator, findings, gate_results
            )

            aligned = all(f.passed for f in findings if f.severity == "critical")
            rationale = (
                "All deterministic alignment checks passed."
                if aligned
                else "One or more critical alignment findings."
            )
            return AlignmentCheckResult(
                aligned=aligned,
                findings=findings,
                gate_results=gate_results,
                rationale=rationale,
            )

    # ------------------------------------------------------------------
    # Check 1 — universe
    # ------------------------------------------------------------------
    def _check_universe(
        self,
        spec: Any,
        trade: Any,
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        target = list(getattr(spec, "target_symbols", []) or [])
        if not target:
            # Spec didn't pin a universe — alignment defers to the
            # asset-class fallback; no finding emitted.
            return
        passed = trade.symbol in target
        details = (
            f"Trade #{trade.trade_num} symbol {trade.symbol!r} ∈ target_symbols={target}."
            if passed
            else (
                f"Trade #{trade.trade_num} symbol {trade.symbol!r} is not in "
                f"spec.target_symbols={target}."
            )
        )
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="universe",
            check_name="universe",
            passed=passed,
            severity="info" if passed else "critical",
            details=details,
        )
        self._record(finding, findings, gate_results)

    # ------------------------------------------------------------------
    # Check 2 — side matches an entry rule's declared side
    # ------------------------------------------------------------------
    def _check_side(
        self,
        spec: Any,
        trade: Any,
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        entry_rules = [
            r for r in (getattr(spec, "entry_rules", []) or []) if isinstance(r, EntryRule)
        ]
        if not entry_rules:
            return
        allowed_sides = sorted({r.side for r in entry_rules})
        passed = trade.side in allowed_sides
        details = (
            f"Trade #{trade.trade_num} side {trade.side!r} matches an entry "
            f"rule (allowed sides: {allowed_sides})."
            if passed
            else (
                f"Trade #{trade.trade_num} side {trade.side!r} is not declared "
                f"by any entry rule (allowed sides: {allowed_sides})."
            )
        )
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="entry:side",
            check_name="side",
            passed=passed,
            severity="info" if passed else "critical",
            details=details,
        )
        self._record(finding, findings, gate_results)

    # ------------------------------------------------------------------
    # Check 3 — sizing within ±1%
    # ------------------------------------------------------------------
    def _check_sizing(
        self,
        spec: Any,
        trade: Any,
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
        *,
        entry_equity: float,
    ) -> None:
        sizing = getattr(spec, "sizing", None)
        if sizing is None:
            return

        expected = _expected_position_value(
            sizing,
            equity_at_entry=entry_equity,
            entry_price=float(trade.entry_price),
        )
        if expected is None:
            # Vol-target / unknown sizing — emit info skip so the row
            # exists in the audit ledger but doesn't gate the verdict.
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="sizing",
                check_name="sizing",
                passed=True,
                severity="info",
                details=(
                    f"Sizing variant {type(sizing).__name__} is not numerically "
                    "checkable at trade-level; deferring to engine-side enforcement."
                ),
            )
            self._record(finding, findings, gate_results)
            return

        actual = float(trade.position_value)
        rel_miss = _relative_miss(actual, expected)
        within_tol = rel_miss <= _SIZING_TOL

        if within_tol:
            severity: Severity = "info"
            passed = True
            details = (
                f"Trade #{trade.trade_num} position_value=${actual:,.2f} within "
                f"±{_SIZING_TOL * 100:.1f}% of expected ${expected:,.2f}."
            )
        else:
            # ``TradeRecord`` documents ``participation_clipped`` /
            # ``partial_fill_count`` as ``None`` when the engine hasn't
            # annotated the trade — distinct from "annotated as no
            # caveat". Treating unknown as known-false would mark every
            # legacy / pre-annotation trade with sizing drift as
            # critical and trigger needless alignment-fix iterations.
            # Three-way classification:
            #   - explicit caveat present (clipped=True or partial>0)
            #     → info (engine-attested explanation)
            #   - explicit no-caveat (clipped=False AND partial==0)
            #     → critical (engine attested execution was clean,
            #     so the drift is a real misalignment)
            #   - unknown (either field is None) → warning (flag the
            #     drift but don't claim it's definitively misaligned)
            explicit_caveat = bool(trade.participation_clipped) or (
                trade.partial_fill_count is not None and trade.partial_fill_count > 0
            )
            explicit_no_caveat = (
                trade.participation_clipped is False and trade.partial_fill_count == 0
            )
            if explicit_caveat:
                severity = "info"
                passed = True
                caveat_msg = ""
                if trade.participation_clipped:
                    caveat_msg = " (participation_clipped=True)"
                elif trade.partial_fill_count is not None and trade.partial_fill_count > 0:
                    caveat_msg = f" (partial_fill_count={trade.partial_fill_count})"
            elif explicit_no_caveat:
                severity = "critical"
                passed = False
                caveat_msg = " (no execution caveat reported)"
            else:
                # Unknown execution metadata — downgrade to warning.
                severity = "warning"
                passed = False
                caveat_msg = " (execution metadata not annotated; unknown)"
            details = (
                f"Trade #{trade.trade_num} position_value=${actual:,.2f} differs "
                f"from expected ${expected:,.2f} by "
                f"{rel_miss * 100:.2f}% (tolerance ±{_SIZING_TOL * 100:.1f}%)"
                f"{caveat_msg}."
            )

        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="sizing",
            check_name="sizing",
            passed=passed,
            severity=severity,
            details=details,
            computed_value=actual,
            expected_value=expected,
        )
        self._record(finding, findings, gate_results)

    # ------------------------------------------------------------------
    # Check 4 — stop-loss compliance
    # ------------------------------------------------------------------
    def _check_stop_loss(
        self,
        spec: Any,
        trade: Any,
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        stop_rules = [
            r for r in (getattr(spec, "exit_rules", []) or []) if isinstance(r, StopLossRule)
        ]
        if not stop_rules:
            return

        # Trailing-high / trailing-low stops are path-dependent: the
        # effective floor moves with the running peak/trough between
        # entry and exit, so it cannot be validated from
        # ``trade.return_pct`` alone. Emit an info-severity skip for
        # each trailing rule so the audit ledger is self-describing,
        # and only run the entry-price floor check against rules whose
        # ``basis == "entry_price"``.
        entry_basis_rules = [r for r in stop_rules if r.basis == "entry_price"]
        trailing_rules = [r for r in stop_rules if r.basis != "entry_price"]
        for tr in trailing_rules:
            self._record(
                AlignmentFinding(
                    trade_num=trade.trade_num,
                    rule_id=f"exit:stop_loss:{tr.basis}",
                    check_name="stop_loss",
                    passed=True,
                    severity="info",
                    details=(
                        f"Trade #{trade.trade_num} stop_loss basis={tr.basis!r} is "
                        "path-dependent (depends on running peak/trough); deferring "
                        "to engine-side enforcement."
                    ),
                ),
                findings,
                gate_results,
            )

        if not entry_basis_rules:
            return

        # Tightest entry-price stop wins — if the spec has multiple
        # entry-basis stops, the floor is the smallest ``pct``.
        tightest = min(r.pct for r in entry_basis_rules)
        # Engine-attributed stop-loss exit is the strongest signal that
        # the engine honoured the rule, regardless of what
        # ``return_pct`` rounded to.
        if trade.exit_reason == "engine_exit:stop_loss":
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="exit:stop_loss",
                check_name="stop_loss",
                passed=True,
                severity="info",
                details=(f"Trade #{trade.trade_num} closed by engine stop-loss attribution."),
                computed_value=float(trade.return_pct),
                expected_value=-tightest * 100.0,
            )
            self._record(finding, findings, gate_results)
            return

        # No engine stop-loss attribution. A stop-loss is a TRIGGER that
        # submits a sell order — it is NOT a hard cap on realized loss.
        # Prices move tick-to-tick, gap between bars, and carry a bid/ask
        # spread, so the fill price (and therefore ``return_pct``)
        # routinely lands past the nominal floor. A non-attributed close
        # below the floor means one of:
        #   - the strategy (or another exit rule) closed the position; a
        #     market exit can fill on a next-bar gap-down open beneath the
        #     floor before the engine evaluates that bar; or
        #   - another rule (e.g. a signal exit) fired first on the same bar.
        # Either way the position WAS exited to limit further loss, which
        # is the stop's entire purpose. Treating ``return_pct < floor`` as
        # a hard-limit breach is a category error — a stop cannot bound
        # realized loss tick-for-tick. The genuine "stop never fired"
        # enforcement leak is owned by ``ExitRuleConformanceGate``, which
        # has the per-symbol firing telemetry this per-trade view lacks.
        # Emit an INFORMATIONAL (passing) row so the audit ledger is
        # self-describing without a false-positive critical.
        return_pct = float(trade.return_pct)
        floor_pct = -tightest * 100.0
        reason = trade.exit_reason or "strategy/other exit"
        if return_pct < floor_pct:
            details = (
                f"Trade #{trade.trade_num} return_pct={return_pct:.2f}% is past the "
                f"nominal stop-loss floor {floor_pct:.2f}%; closed via {reason!r} "
                "(not engine_exit:stop_loss). A stop-loss is a trigger, not a price "
                "guarantee — the fill can land past the threshold on a gap/spread, or "
                "another exit closed the position first. The position was still "
                "exited to limit further loss; this is expected market behaviour, not "
                "a spec misalignment. Engine-side stop firing is verified "
                "deterministically by the exit-rule conformance gate."
            )
        else:
            details = (
                f"Trade #{trade.trade_num} return_pct={return_pct:.2f}% is within the "
                f"nominal stop-loss floor {floor_pct:.2f}%; closed via {reason!r}."
            )
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="exit:stop_loss",
            check_name="stop_loss",
            passed=True,
            severity="info",
            details=details,
            computed_value=return_pct,
            expected_value=floor_pct,
        )
        self._record(finding, findings, gate_results)

    # ------------------------------------------------------------------
    # Check 5 — take-profit compliance
    # ------------------------------------------------------------------
    def _check_take_profit(
        self,
        spec: Any,
        trade: Any,
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        tp_rules = [
            r for r in (getattr(spec, "exit_rules", []) or []) if isinstance(r, TakeProfitRule)
        ]
        if not tp_rules:
            return
        # Tightest take-profit wins for ceiling computation. A trade
        # closed at a higher return than spec's tightest TP either was
        # engine-fired with attribution, or the engine missed the rule.
        tightest = min(r.pct for r in tp_rules)
        if trade.exit_reason == "engine_exit:take_profit":
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="exit:take_profit",
                check_name="take_profit",
                passed=True,
                severity="info",
                details=(f"Trade #{trade.trade_num} closed by engine take-profit attribution."),
                computed_value=float(trade.return_pct),
                expected_value=tightest * 100.0,
            )
            self._record(finding, findings, gate_results)
            return

        # No engine take-profit attribution. Symmetric to the stop-loss
        # check: a take-profit is a TRIGGER, not a price cap. A position
        # can fill past the nominal ceiling on a gap-up (the engine
        # detects the trigger on bar N and fills bar N+1's open), or
        # another exit rule may have closed the position first. A
        # non-attributed close above the ceiling is therefore expected
        # market behaviour, not a misalignment — emit an INFORMATIONAL
        # (passing) row rather than a false-positive critical.
        return_pct = float(trade.return_pct)
        ceiling_pct = tightest * 100.0
        reason = trade.exit_reason or "strategy/other exit"
        if return_pct > ceiling_pct:
            details = (
                f"Trade #{trade.trade_num} return_pct={return_pct:.2f}% is past the "
                f"nominal take-profit ceiling {ceiling_pct:.2f}%; closed via {reason!r} "
                "(not engine_exit:take_profit). A take-profit is a trigger, not a price "
                "guarantee — the fill can land past the threshold on a gap-up, or "
                "another exit closed the position first. Engine-side take-profit firing "
                "is verified deterministically by the exit-rule conformance gate."
            )
        else:
            details = (
                f"Trade #{trade.trade_num} return_pct={return_pct:.2f}% is within the "
                f"nominal take-profit ceiling {ceiling_pct:.2f}%; closed via {reason!r}."
            )
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="exit:take_profit",
            check_name="take_profit",
            passed=True,
            severity="info",
            details=details,
            computed_value=return_pct,
            expected_value=ceiling_pct,
        )
        self._record(finding, findings, gate_results)

    # ------------------------------------------------------------------
    # Check 6 — entry-signal correlation (with near-miss adjudication)
    # ------------------------------------------------------------------
    def _check_entry_signal(
        self,
        spec: Any,
        trade: Any,
        frames: Dict[str, pd.DataFrame],
        indicator_caches: Dict[str, Dict[str, pd.Series]],
        near_miss_pct: float,
        adjudicator: Optional[NearMissAdjudicator],
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
        pending: Optional[List[_PendingNearMiss]] = None,
    ) -> None:
        entry_rules = [
            r for r in (getattr(spec, "entry_rules", []) or []) if isinstance(r, EntryRule)
        ]
        if not entry_rules:
            return

        # Restrict to entry rules whose side matches the trade's side,
        # preserving each rule's original index in ``spec.entry_rules``
        # so ``rule_id`` values like ``entry[2]`` map back to the spec
        # the operator actually wrote. Renumbering relative to the
        # filtered subset would silently misattribute findings on the
        # second side when a spec mixes long/short rules.
        matching = [(orig_idx, r) for orig_idx, r in enumerate(entry_rules) if r.side == trade.side]
        if not matching:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="entry:side_mismatch",
                check_name="entry_signal",
                passed=False,
                severity="info",
                details=(
                    f"Trade #{trade.trade_num}: no entry rule with side={trade.side!r}; "
                    "check #2 (side) carries the critical finding."
                ),
            )
            self._record(finding, findings, gate_results)
            return

        df = frames.get(trade.symbol)
        if df is None or df.empty:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="entry:bars_missing",
                check_name="entry_signal",
                passed=False,
                severity="critical",
                details=(
                    f"Trade #{trade.trade_num}: no market_data bars for symbol "
                    f"{trade.symbol!r}. Cannot reproduce entry signal."
                ),
            )
            self._record(finding, findings, gate_results)
            return

        matching_positions = np.where(df.index.to_numpy() == trade.entry_date)[0]
        if matching_positions.size == 0:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="entry:bar_missing",
                check_name="entry_signal",
                passed=False,
                severity="critical",
                details=(
                    f"Trade #{trade.trade_num}: entry_date {trade.entry_date!r} is "
                    f"not present in market_data for {trade.symbol!r}."
                ),
            )
            self._record(finding, findings, gate_results)
            return
        # Duplicate dates are vanishingly rare in real backtest data —
        # if they exist, the first occurrence is the one the engine
        # would have fired on.
        fill_idx = int(matching_positions[0])

        # Shift to the SIGNAL bar (engine fills the next bar after the
        # signal fires; see ``_FILL_DELAY_BARS`` constant). When the
        # fill bar is the first bar in market_data we have no signal
        # bar to verify — fall closed (warmup-style) rather than mark
        # the trade as critical for lack of data.
        signal_idx = fill_idx - _FILL_DELAY_BARS
        if signal_idx < 0:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="entry:signal_bar_missing",
                check_name="entry_signal",
                passed=False,
                severity="critical",
                details=(
                    f"Trade #{trade.trade_num}: fill bar {trade.entry_date!r} "
                    "is the first bar in market_data; the signal bar (one "
                    "before) is not present, cannot reproduce entry signal."
                ),
            )
            self._record(finding, findings, gate_results)
            return

        cache = indicator_caches.setdefault(trade.symbol, {})

        # Track the closest near-miss across all matching rules. We
        # want to consult the adjudicator at most once per trade —
        # picking the tightest (smallest relative miss) gives the LLM
        # the most-likely-legitimate candidate to rule on.
        rule_evaluations: List[
            Dict[str, Any]
        ] = []  # one entry per matching rule, with eval outcome.

        any_satisfied = False
        for orig_idx, rule in matching:
            outcome = self._evaluate_entry_rule_predicate(
                rule, df, signal_idx, cache, rule_idx=orig_idx
            )
            rule_evaluations.append(outcome)
            if outcome["status"] == "satisfied":
                any_satisfied = True

        if any_satisfied:
            # At least one matching entry predicate fires — the entry is
            # aligned. Emit an info finding for EVERY matching rule whose
            # predicate is satisfied at the signal bar, not just the
            # first: two entry rules can legitimately overlap (both
            # satisfied on the same bar), and crediting only one would
            # under-report the other across the whole ledger for any
            # rule-level consumer (e.g. RuleFiringRateGate's custom-code
            # correlation signal), which would then misread a rule that
            # only ever co-fires with another as dead code.
            for satisfied in (o for o in rule_evaluations if o["status"] == "satisfied"):
                # ``lhs`` / ``rhs`` are populated for a leaf predicate and
                # ``None`` for a combinator (``all_of`` / ``any_of`` — no
                # single scalar pair); render the scalar tail only when
                # both are available.
                scalar_tail = (
                    f" → lhs={satisfied['lhs']:.6g}, rhs={satisfied['rhs']:.6g}."
                    if satisfied["lhs"] is not None and satisfied["rhs"] is not None
                    else "."
                )
                finding = AlignmentFinding(
                    trade_num=trade.trade_num,
                    rule_id=satisfied["rule_id"],
                    check_name="entry_signal",
                    passed=True,
                    severity="info",
                    details=(
                        f"Trade #{trade.trade_num} entry satisfied by "
                        f"{satisfied['rule_id']}: {satisfied['predicate_repr']}{scalar_tail}"
                    ),
                    computed_value=satisfied.get("lhs"),
                    expected_value=satisfied.get("rhs"),
                )
                self._record(finding, findings, gate_results)
            return

        # No matching rule was satisfied. Find the tightest near-miss
        # among the evaluations; if it's within tolerance and the
        # adjudicator is wired up, consult the LLM. Otherwise emit
        # critical.
        near_miss_candidates = [
            o for o in rule_evaluations if o["status"] == "miss" and o["rel_miss"] is not None
        ]
        if near_miss_candidates and near_miss_pct > 0 and adjudicator is not None:
            tightest = min(near_miss_candidates, key=lambda o: o["rel_miss"])
            if tightest["rel_miss"] <= near_miss_pct:
                if pending is not None:
                    # Defer the LLM call: reserve the finding/gate slots now
                    # (so output order matches the serial path) and let
                    # ``_resolve_pending_near_misses`` adjudicate concurrently.
                    f_slot = len(findings)
                    g_slot = len(gate_results)
                    findings.append(None)  # type: ignore[arg-type]
                    gate_results.append(None)  # type: ignore[arg-type]
                    pending.append(
                        _PendingNearMiss(
                            trade=trade,
                            evaluation=tightest,
                            f_slot=f_slot,
                            g_slot=g_slot,
                        )
                    )
                    return
                # Serial fallback (no collector supplied).
                verdict = self._consult_near_miss(adjudicator, tightest, trade)
                finding = self._build_near_miss_finding(trade, tightest, verdict)
                self._record(finding, findings, gate_results)
                return

        # Hard miss — emit critical citing the first matching rule that
        # produced a real evaluation (priority: misses with values,
        # then warmup misses).
        primary = next(
            (o for o in rule_evaluations if o["status"] == "miss"),
            rule_evaluations[0],
        )
        if primary["status"] == "warmup":
            details = (
                f"Trade #{trade.trade_num} entry on {trade.entry_date} hit a "
                f"warmup-NaN indicator value for {primary['rule_id']}: "
                f"{primary['predicate_repr']}. The engine fired before its "
                "indicator was ready."
            )
        else:
            # ``lhs`` / ``rhs`` are populated for a leaf predicate and ``None``
            # for a combinator (``all_of`` / ``any_of`` — no single scalar
            # pair); render the scalar tail only when both are available.
            scalar_tail = (
                f" (lhs={primary['lhs']:.6g}, rhs={primary['rhs']:.6g})."
                if primary["lhs"] is not None and primary["rhs"] is not None
                else "."
            )
            details = (
                f"Trade #{trade.trade_num} entry predicate not satisfied for "
                f"{primary['rule_id']}: {primary['predicate_repr']}{scalar_tail}"
            )
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id=primary["rule_id"],
            check_name="entry_signal",
            passed=False,
            severity="critical",
            details=details,
            computed_value=primary.get("lhs"),
            expected_value=primary.get("rhs"),
        )
        self._record(finding, findings, gate_results)

    # ------------------------------------------------------------------
    # Check 7 — signal-exit correlation
    # ------------------------------------------------------------------
    def _check_signal_exit(
        self,
        spec: Any,
        trade: Any,
        frames: Dict[str, pd.DataFrame],
        indicator_caches: Dict[str, Dict[str, pd.Series]],
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        """Validate that strategy-emitted closes match a SignalExitRule.

        Pre: ``frames`` is the per-symbol OHLCV map; ``indicator_caches``
        is the per-trade cache the entry-signal check populates.
        Post: emits zero-or-more findings:
          - no SignalExitRule in spec → no-op (other exit checks cover
            stop_loss / take_profit)
          - engine-attributed close (``exit_reason`` starts with
            ``engine_exit:``) → info skip (signal-exit check N/A)
          - strategy-emitted close (or unknown attribution) without
            any SignalExitRule predicate firing at the exit bar →
            critical
          - every SignalExitRule whose predicate fires at the exit bar →
            one info pass each
        """
        # Preserve each rule's original index in ``spec.exit_rules`` (not
        # a renumbered index into this filtered subset) so ``rule_id``
        # values like ``exit:signal_exit[N]`` line up with the engine's
        # own ``engine_exit:signal_exit[N]`` stamp (``service.py``, which
        # indexes off the unfiltered ``exit_rules`` list) and with
        # ``RuleFiringRateGate``'s custom-code correlation, which counts
        # hits by that same absolute index. A spec that mixes exit-rule
        # kinds (e.g. ``[StopLossRule, SignalExitRule]``) would otherwise
        # renumber ``SignalExitRule`` to index 0 here while every other
        # consumer expects index 1, misattributing the finding to the
        # wrong spec rule.
        signal_exit_rules = [
            (orig_idx, r)
            for orig_idx, r in enumerate(getattr(spec, "exit_rules", []) or [])
            if isinstance(r, SignalExitRule)
        ]
        if not signal_exit_rules:
            return

        exit_reason = getattr(trade, "exit_reason", None) or ""
        if exit_reason.startswith("engine_exit:"):
            # Engine attribution already covered by the matching
            # structured-exit check (stop_loss / take_profit).
            # Emit a single info row so the audit ledger
            # records that the signal-exit check was reached and
            # deliberately skipped.
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="exit:signal_exit",
                check_name="signal_exit",
                passed=True,
                severity="info",
                details=(
                    f"Trade #{trade.trade_num} closed via engine "
                    f"attribution {exit_reason!r}; signal-exit check N/A."
                ),
            )
            self._record(finding, findings, gate_results)
            return

        df = frames.get(trade.symbol)
        if df is None or df.empty:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="exit:signal_exit:bars_missing",
                check_name="signal_exit",
                passed=False,
                severity="critical",
                details=(
                    f"Trade #{trade.trade_num}: no market_data bars for "
                    f"{trade.symbol!r}. Cannot reproduce signal exit."
                ),
            )
            self._record(finding, findings, gate_results)
            return

        matching_positions = np.where(df.index.to_numpy() == trade.exit_date)[0]
        if matching_positions.size == 0:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="exit:signal_exit:bar_missing",
                check_name="signal_exit",
                passed=False,
                severity="critical",
                details=(
                    f"Trade #{trade.trade_num}: exit_date {trade.exit_date!r} "
                    f"is not present in market_data for {trade.symbol!r}."
                ),
            )
            self._record(finding, findings, gate_results)
            return

        fill_idx = int(matching_positions[0])

        # Shift to the SIGNAL bar — same engine contract as the entry
        # check (``_FILL_DELAY_BARS``). ``trade.exit_date`` is the fill
        # bar where the close completed; the strategy's signal-exit
        # predicate fired one bar earlier.
        signal_idx = fill_idx - _FILL_DELAY_BARS
        if signal_idx < 0:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="exit:signal_exit:signal_bar_missing",
                check_name="signal_exit",
                passed=False,
                severity="critical",
                details=(
                    f"Trade #{trade.trade_num}: fill bar {trade.exit_date!r} "
                    "is the first bar in market_data; the signal bar (one "
                    "before) is not present, cannot reproduce signal exit."
                ),
            )
            self._record(finding, findings, gate_results)
            return

        cache = indicator_caches.setdefault(trade.symbol, {})
        # One view, reused across every rule and the four resolves a cross
        # predicate needs (lhs/rhs at i and i-1), so its per-column / per-indicator
        # numpy arrays are built once instead of per resolve.
        view = PandasHistoryView(df, cache)

        # Evaluate every signal-exit rule at the signal bar and emit an
        # info finding for EACH one satisfied, not just the first: two
        # signal-exit rules can legitimately overlap (both satisfied on
        # the same bar), and crediting only one — as an earlier version
        # of this check did — would under-report the other across the
        # whole ledger to a rule-level consumer (e.g. RuleFiringRateGate's
        # custom-code correlation), misreading it as dead code even
        # though its predicate held on every trade. Mirrors the
        # entry-signal check's identical fix above. The shared
        # ``evaluate_tree`` handles leaf predicates, ``all_of`` / ``any_of``
        # combinators, cross-op previous-bar state, and warmup uniformly,
        # so this loop is agnostic to each rule's ``when`` shape.
        any_satisfied = False
        for rule_idx, rule in signal_exit_rules:
            result = _evaluate_tree(rule.when, view, signal_idx)
            if result.status != "satisfied":
                continue  # miss or warmup — try next rule
            any_satisfied = True

            # ``lhs`` / ``rhs`` are populated for a leaf predicate and ``None``
            # for a combinator (no single pair of scalars); render the scalar
            # tail only when both are available.
            scalar_tail = (
                f" → lhs={result.lhs:.6g}, rhs={result.rhs:.6g}."
                if result.lhs is not None and result.rhs is not None
                else "."
            )
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id=signal_exit_rule_id(rule_idx),
                check_name="signal_exit",
                passed=True,
                severity="info",
                details=(
                    f"Trade #{trade.trade_num} signal-exit satisfied by "
                    f"exit[{rule_idx}]: {format_predicate_tree(rule.when, leaf_formatter=_format_predicate)}{scalar_tail}"
                ),
                computed_value=result.lhs,
                expected_value=result.rhs,
            )
            self._record(finding, findings, gate_results)
        if any_satisfied:
            return

        # No SignalExitRule fired at the exit bar, but the engine did
        # not attribute the close to a structured rule — the strategy
        # closed without a matching signal.
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="exit:signal_exit",
            check_name="signal_exit",
            passed=False,
            severity="critical",
            details=(
                f"Trade #{trade.trade_num} exited on {trade.exit_date} without "
                "engine attribution, but no SignalExitRule predicate fires at "
                "the exit bar."
            ),
        )
        self._record(finding, findings, gate_results)

    def _evaluate_entry_rule_predicate(
        self,
        rule: EntryRule,
        df: pd.DataFrame,
        entry_idx: int,
        cache: Dict[str, pd.Series],
        *,
        rule_idx: int,
    ) -> Dict[str, Any]:
        """Evaluate one entry rule's predicate at the SIGNAL bar.

        Pre: the caller has already shifted from the fill bar (where
        ``TradeRecord.entry_date`` points) back to the signal bar
        (``signal_idx = fill_idx - _FILL_DELAY_BARS``); the
        ``entry_idx`` parameter here is the resolved signal bar.

        Returns a dict with:
          - ``status``: ``"satisfied"`` | ``"miss"`` | ``"warmup"``
          - ``rule_id``: :func:`entry_rule_id` of ``rule_idx``
          - ``predicate_repr``: rendered predicate string
          - ``lhs`` / ``rhs``: resolved scalars (``None`` for warmup)
          - ``rel_miss``: relative miss when ``status == "miss"`` and
            ``rhs`` is a numeric anchor; ``None`` otherwise.
        """
        rule_id = entry_rule_id(rule_idx)
        predicate_repr = format_predicate_tree(rule.when, leaf_formatter=_format_predicate)
        # One view, reused across every resolve a cross predicate needs (lhs/rhs
        # at i and i-1) so its cached numpy arrays are shared. The shared
        # ``evaluate_tree`` owns leaf evaluation, cross-op previous-bar handling,
        # warmup, AND ``all_of`` / ``any_of`` composition uniformly. ``lhs`` /
        # ``rhs`` / ``rel_miss`` come straight off its result: populated for a
        # leaf predicate, ``None`` for a combinator (no single scalar pair) — so
        # a multi-confirmation rule never enters the near-miss path, which is
        # filtered on ``rel_miss is not None``.
        view = PandasHistoryView(df, cache)
        result = _evaluate_tree(rule.when, view, entry_idx)
        return {
            "status": result.status,
            "rule_id": rule_id,
            "predicate_repr": predicate_repr,
            "lhs": result.lhs,
            "rhs": result.rhs,
            "rel_miss": result.rel_miss,
        }

    def _build_near_miss_finding(
        self,
        trade: Any,
        evaluation: Dict[str, Any],
        verdict: NearMissVerdict,
    ) -> AlignmentFinding:
        """Construct the entry-signal finding for an adjudicated near-miss.

        Shared by the serial path and the concurrent
        :meth:`_resolve_pending_near_misses` path so both emit byte-identical
        findings for the same ``(trade, evaluation, verdict)``.
        """
        return AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id=evaluation["rule_id"],
            check_name="entry_signal",
            passed=verdict.legitimate,
            severity="info" if verdict.legitimate else "critical",
            details=(
                f"Trade #{trade.trade_num} entry near-miss "
                f"({evaluation['rel_miss'] * 100:.2f}% relative) on "
                f"{evaluation['rule_id']}: {evaluation['predicate_repr']}. "
                f"Adjudicator: {verdict.rationale!s}"
            ),
            computed_value=evaluation["lhs"],
            expected_value=evaluation["rhs"],
        )

    def _resolve_pending_near_misses(
        self,
        pending: List[_PendingNearMiss],
        adjudicator: Optional[NearMissAdjudicator],
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        """Adjudicate all deferred near-misses and fill their reserved slots.

        Pre: each ``pending`` entry's ``f_slot``/``g_slot`` index a ``None``
        placeholder reserved during the trade loop; ``adjudicator`` is the
        same callable that drove deferral (non-``None`` whenever ``pending``
        is non-empty).
        Post: every reserved slot holds the finding/gate row produced from
        that trade's verdict — identical to what the serial path would have
        emitted, independent of completion order. Adjudications run through
        :func:`shared.concurrency.parallel_map` (``STRATEGY_LAB_ALIGNMENT_
        ADJUDICATION_CONCURRENCY`` workers) since the underlying adjudicator
        is synchronous; ``propagate_context`` keeps LLM attribution/request-id
        contextvars visible inside each worker.
        """
        if not pending:
            return
        assert adjudicator is not None, "pending near-misses require an adjudicator"

        verdicts = parallel_map(
            pending,
            lambda item: self._consult_near_miss(adjudicator, item.evaluation, item.trade),
            max_workers=_adjudication_concurrency(),
            preserve_order=True,
            skip_none=False,
            propagate_context=True,
        )

        for item, verdict in zip(pending, verdicts):
            finding = self._build_near_miss_finding(item.trade, item.evaluation, verdict)
            findings[item.f_slot] = finding
            gate_results[item.g_slot] = self._emit_for_finding(finding)

    def _consult_near_miss(
        self,
        adjudicator: NearMissAdjudicator,
        evaluation: Dict[str, Any],
        trade: Any,
    ) -> NearMissVerdict:
        """Invoke the LLM near-miss adjudicator with a single tight prompt.

        Failures route to a deterministic fail-closed verdict
        (``legitimate=False``) rather than propagating — the orchestrator
        is then free to treat the trade as misaligned and rebuild via
        ``propose_code_fix`` on the next iteration.
        """
        t0 = time.monotonic()
        try:
            return adjudicator(
                rule_id=evaluation["rule_id"],
                predicate_repr=evaluation["predicate_repr"],
                computed_value=float(evaluation["lhs"]),
                threshold=float(evaluation["rhs"]),
                symbol=trade.symbol,
                entry_date=trade.entry_date,
            )
        except Exception as exc:
            # Safety-critical fail-closed: a stuck or erroring adjudicator must
            # never legitimise a missed predicate. Reuse the envelope's canonical
            # ``_FAILURE_FMT`` so this fail-closed site emits the identical
            # five-field schema (agent/phase/attempt/latency_ms/error_class) —
            # on-call greps one format across the whole lab, and ``phase`` marks
            # this as the near-miss guard. This is a single terminal attempt
            # (no envelope retry loop), so it reports ``attempt=1/1``.
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                _FAILURE_FMT,
                "alignment",
                "alignment_near_miss",
                1,
                1,
                latency_ms,
                type(exc).__name__,
            )
            return NearMissVerdict(
                legitimate=False,
                rationale=f"adjudicator error: {type(exc).__name__}",
            )

    # ------------------------------------------------------------------
    # Per-finding recording helper
    # ------------------------------------------------------------------
    def _record(
        self,
        finding: AlignmentFinding,
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        """Append a finding and its gate-result row, keeping the lists 1:1.

        Collapses the ``findings.append(finding); gate_results.append(
        self._emit_for_finding(finding))`` idiom every check repeated inline.

        Preconditions:
          - ``_using_phase`` is active (asserted transitively by
            :meth:`_emit_for_finding`).
          - ``findings`` and ``gate_results`` are the same-length output
            lists being built for this ``check()`` call.
        Postconditions:
          - ``finding`` is appended to ``findings`` and its translated
            :class:`QualityGateResult` to ``gate_results``; both lists grow by
            exactly one and remain index-aligned.
        """
        findings.append(finding)
        gate_results.append(self._emit_for_finding(finding))

    # ------------------------------------------------------------------
    # Per-finding gate-row helper
    # ------------------------------------------------------------------
    def _emit_for_finding(self, finding: AlignmentFinding) -> QualityGateResult:
        """Translate one :class:`AlignmentFinding` into a
        :class:`QualityGateResult` row for the gate-result stream.

        The per-finding row uses ``gate_name="alignment_finding"`` —
        deliberately distinct from the gate's ``GATE="trade_alignment"``
        cycle-level aggregate. A misaligned cycle with N trades × 7
        checks can emit dozens of per-finding rows, and
        :class:`ConvergenceTracker.record` increments
        ``_failure_modes[gate_name]`` per failed row. Sharing the
        ``trade_alignment`` name would inflate the cycle-level failure
        count by the per-finding fan-out and prematurely trip
        ``get_failure_directives(min_occurrences=3)`` after a single
        bad cycle. The orchestrator builds a separate aggregate
        ``trade_alignment`` row per cycle that the tracker keys on
        instead.
        """
        # ``_using_phase`` is active because callers wrap the public
        # ``check()`` body in ``with self._using_phase("verification")``;
        # mirror its assertion here for the same fail-fast contract.
        assert self._phase is not None, (
            f"{type(self).__name__}._emit_for_finding must be inside `with self._using_phase(...)`"
        )
        details = f"[{finding.check_name}] {finding.details}"
        return QualityGateResult(
            gate_name="alignment_finding",
            phase=self._phase,
            passed=finding.passed,
            severity=finding.severity,
            details=details,
            rule_id=finding.rule_id,
        )


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


class AlignmentCheckResult(BaseModel):
    """Aggregated output of :meth:`DeterministicAlignmentChecker.check`."""

    aligned: bool
    findings: List[AlignmentFinding] = Field(default_factory=list)
    gate_results: List[QualityGateResult] = Field(default_factory=list)
    rationale: str = ""
