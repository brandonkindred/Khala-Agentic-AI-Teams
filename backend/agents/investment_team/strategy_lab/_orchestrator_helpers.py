"""Pure helpers + outcome dataclasses extracted from :mod:`orchestrator`.

These types and functions all live "below" :class:`StrategyLabOrchestrator`
in the dependency graph — they take primitive inputs (specs, bar lists,
metrics) and return fresh values. Hosting them in a sibling module keeps
``orchestrator.py`` focused on the coordinator's surface.

External callers (``zero_trade_repair.py``, the test suite,
``agents/refinement.py``'s docstring reference) historically imported
these names via ``investment_team.strategy_lab.orchestrator``. The
orchestrator re-exports them so existing import sites keep working.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..execution.metrics import build_equity_curve_from_trades
from ..execution.risk_filter import _RISK_LIMIT_TIGHTEN_DIRECTION, RiskLimits
from ..market_data_service import OHLCVBar
from ..models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    BacktestResult,
    StrategySpec,
    TradeRecord,
)
from ..trading_service.modes.sandbox_compat import StrategyRunResult, run_strategy_code
from .coverage_probe import run_coverage_stage, should_run_probes
from .quality_gates.models import QualityGateResult

logger = logging.getLogger(__name__)

# Cap on how many ``last_order_events`` entries the diagnostics block
# carries through to the refinement prompt. The diagnostics model already
# trims to 20; 10 is enough signal for the LLM to spot the failure pattern
# while keeping the JSON line under ~1 KB.
_DIAGNOSTICS_LAST_EVENTS_CAP = 10


# ──────────────────────────────────────────────────────────────────────────
# Outcome dataclasses returned by the orchestrator's phase methods.
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _MarketDataFetch:
    """Issue #525 — return envelope for ``_fetch_market_data``.

    Carries the OHLCV payload alongside the audit trail of the symbols the
    fetch was asked to retrieve and the symbols that actually returned
    usable bars. Both lists feed ``BacktestRecord`` so reviewers can see
    when a fetch silently dropped tickers without re-running the cycle.
    """

    data: Optional[Dict[str, List[OHLCVBar]]]
    requested_symbols: List[str]
    fetched_symbols: List[str]


@dataclass
class _VerificationOutcome:
    """Bundle of state mutated by ``_run_verification_phase``.

    The verification phase runs walk-forward (or its fallback anomaly
    recheck), exit-rule conformance, resolves ``is_winning``, and
    augments ``metrics.acceptance_reason`` with any veto causes.
    Returning a dataclass keeps the boundary explicit without forcing
    ``_run_design_attempt`` to learn the internal branches.
    """

    metrics: BacktestResult
    is_winning: bool
    upstream_admitted: bool
    acceptance_results: List[QualityGateResult]
    walk_forward_failed: bool
    exit_rule_conformance_passed: bool


@dataclass
class _AlignmentLoopOutcome:
    """Bundle of state mutated by ``_run_trade_alignment_loop``.

    The trade-alignment loop can replace the run's known-good
    ``spec`` / ``code`` / ``trades`` / ``metrics`` if it commits a fix,
    and tracks attempt strings + per-round reports the caller consumes.
    Returning a single dataclass keeps ``_run_design_attempt``'s
    unpacking explicit and small.
    """

    spec: StrategySpec
    code: str
    trades: List[TradeRecord]
    metrics: BacktestResult
    alignment_attempts: List[str] = field(default_factory=list)
    alignment_reports: List[Any] = field(default_factory=list)
    trades_aligned: bool = False

    @property
    def alignment_rounds(self) -> int:
        return len(self.alignment_attempts)


@dataclass
class _AlignmentRoundOutcome:
    """One iteration of ``_run_trade_alignment_loop``.

    Semantics:
    - ``terminate=True`` ⇒ caller breaks the loop. The spec/code/trades/
      metrics fields carry the pre-iteration state (either because the
      audit reported aligned, the proposal was rejected, or the round
      budget is spent).
    - ``terminate=False`` ⇒ caller continues. The spec/code/trades/
      metrics fields carry the just-committed proposal as the new
      known-good state.

    The helper mutates ``alignment_reports``, ``alignment_attempts``,
    and ``all_gate_results`` in place; callers observe those lists
    directly.
    """

    spec: StrategySpec
    code: str
    trades: List[TradeRecord]
    metrics: BacktestResult
    terminate: bool


@dataclass
class _AnomalyRecoveryOutcome:
    """Bundle of state returned by ``_handle_critical_anomalies``.

    The synthesis loop's evaluation phase delegates to that helper when
    the backtest produces critical anomaly gates. The helper either
    commits a zero-trade-repair proposal, applies a generic refinement,
    or exhausts the round budget — and the loop body needs to know which
    outcome happened so it can continue or break.

    Invariants on return:
    - ``exhausted=True`` ⇒ caller breaks the synthesis loop with
      ``max_rounds_exhausted=True``; the spec/code/trades/metrics fields
      carry the last failed-round values (callers should not commit them).
    - ``exhausted=False`` ⇒ caller continues to the next round; the
      spec/code/trades/metrics/exec_result fields carry the new known-good
      state (either ZTR-committed proposal or generic-refined source).
    """

    spec: StrategySpec
    code: str
    trades: List[TradeRecord]
    metrics: BacktestResult
    exec_result: StrategyRunResult
    exhausted: bool


@dataclass
class _SynthesisLoopOutcome:
    """Bundle of state mutated by ``_run_synthesis_loop``.

    The synthesis refinement loop iterates up to ``MAX_CODE_REFINEMENT_ROUNDS``
    rounds of (validate → fetch → execute → trade-collect → evaluate),
    refining ``spec``/``code`` between rounds and short-circuiting on
    fatal failures (market-data unavailable, target-symbol coverage,
    max-rounds exhaustion).

    Returning the full final state keeps the boundary explicit so
    ``_run_design_attempt`` doesn't need to inspect loop internals.

    Invariants on return:
    - ``execution_succeeded=True`` implies ``trades`` reflects a clean
      run with no critical anomalies and ``metrics`` was computed from
      those trades.
    - ``execution_succeeded=False`` implies the loop short-circuited or
      exhausted its rounds; ``trades``/``metrics`` may be empty defaults
      or carry the last failed round's partials.
    - ``market_data`` is ``None`` only when the first ``_fetch_market_data``
      call returned an empty payload — the loop breaks immediately in
      that case and downstream phases skip alignment.
    - ``max_rounds_exhausted`` is mutually exclusive with
      ``execution_succeeded=True``.
    """

    spec: StrategySpec
    code: str
    trades: List[TradeRecord]
    metrics: BacktestResult
    market_data: Optional[Dict[str, List[OHLCVBar]]]
    requested_symbols: List[str]
    fetched_symbols: List[str]
    execution_succeeded: bool
    max_rounds_exhausted: bool


# ──────────────────────────────────────────────────────────────────────────
# Pure helpers used by the orchestrator (and a few external callers).
# ──────────────────────────────────────────────────────────────────────────


def _maybe_attach_coverage_report(
    *,
    metrics: BacktestResult,
    spec: StrategySpec,
    market_data: Dict[str, List[OHLCVBar]],
    config: BacktestConfig,
    exec_result: StrategyRunResult,
) -> None:
    """Run the #451 coverage stage and stamp the report onto ``metrics``.

    The ``spec`` argument MUST carry the same ``strategy_code`` that was
    handed to ``run_strategy_code`` to produce ``exec_result``. The
    alignment and zero-trade-repair paths use a ``proposed_spec`` variant
    of the surrounding spec; pass that, not the loop-level ``spec``,
    otherwise the static probe will analyse stale source.

    No-ops when ``should_run_probes`` says the run isn't zero/low-trade —
    successful runs keep ``metrics.coverage_report = None`` and pay no
    probe cost.
    """
    if should_run_probes(exec_result.execution_diagnostics):
        metrics.coverage_report = run_coverage_stage(
            spec=spec,
            market_data=market_data,
            config=config,
            exec_result=exec_result,
            run_strategy_code_fn=run_strategy_code,
        )


def _format_execution_diagnostics(
    diagnostics: Optional[BacktestExecutionDiagnostics],
) -> str:
    """Render a compact JSON block of execution diagnostics for the
    refinement prompt (issue #414, part of #404).

    Returns an empty string when diagnostics is missing or the executor
    couldn't classify a zero-trade failure — healthy backtests must not
    bloat the prompt. When a ``zero_trade_category`` is present, returns a
    single line ``"Execution Diagnostics: {<json>}"`` whose JSON payload is
    stable-key-sorted and compact. ``last_order_events`` is capped to the
    most recent ``_DIAGNOSTICS_LAST_EVENTS_CAP`` entries.
    """
    if diagnostics is None or diagnostics.zero_trade_category is None:
        return ""

    payload = diagnostics.model_dump(mode="json", exclude_none=True)
    events = payload.get("last_order_events") or []
    if len(events) > _DIAGNOSTICS_LAST_EVENTS_CAP:
        payload["last_order_events"] = events[-_DIAGNOSTICS_LAST_EVENTS_CAP:]

    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return f"Execution Diagnostics: {encoded}"


def _apply_veto_to_acceptance_reason(
    metrics: BacktestResult,
    suffix: str,
    *,
    upstream_admitted: bool,
) -> Tuple[BacktestResult, bool]:
    """Stamp a publication veto's cause onto ``metrics.acceptance_reason``.

    Both the conformance veto (#527) and the alignment veto (#529)
    follow the same shape: replace a stale success-style upstream
    reason; append to a real upstream rejection. Returns the updated
    ``metrics`` and ``False`` for the new ``upstream_admitted``, so a
    subsequent veto on the same run appends to this one rather than
    overwriting it.

    The delimiter is ``" | "`` (not ``"; "`` which
    :func:`summarize_acceptance_reason` uses between failing gates)
    so downstream parsers can disambiguate the veto boundary from
    gate-internal boundaries.

    Whitespace-only upstream reasons (``None``, ``""``, ``"   "``)
    collapse to the suffix alone — never produces
    ``"   | <suffix>"`` with an empty left side.
    """
    prior = (metrics.acceptance_reason or "").strip()
    if prior and not upstream_admitted:
        combined = f"{prior} | {suffix}"
    else:
        combined = suffix
    return metrics.model_copy(update={"acceptance_reason": combined}), False


# ──────────────────────────────────────────────────────────────────────────
# Pure helpers (formerly @staticmethod on StrategyLabOrchestrator).
# ──────────────────────────────────────────────────────────────────────────


def _merge_risk_limits_tighten_only(
    current: RiskLimits, proposed: Any
) -> Tuple[RiskLimits, List[str], List[str]]:
    """Tighten-only merge of refinement-proposed risk limits (#543).

    Returns ``(merged_limits, loosened_fields, discarded_unknown_keys)``.

    - ``loosened_fields`` lists fields whose proposed value would loosen
      the limit (raise an "lower"-direction cap, lower a "higher"-direction
      floor, or transition ``target_annual_vol`` from ``None`` to a
      value — which fundamentally changes the sizing model and is
      treated as loosening).
    - ``discarded_unknown_keys`` lists fields the caller proposed that
      either aren't in the ``RiskLimits`` schema or are marked
      immutable in ``_RISK_LIMIT_TIGHTEN_DIRECTION`` (e.g.
      ``vol_lookback_days``).

    Callers raise ``SpecImplementabilityError`` when ``loosened_fields``
    is non-empty; unknown keys are warned but never trip.
    """
    loosened: List[str] = []
    unknown: List[str] = []
    if not isinstance(proposed, dict):
        return current, loosened, unknown

    merged_data = current.model_dump()
    for key, new_value in proposed.items():
        direction = _RISK_LIMIT_TIGHTEN_DIRECTION.get(key)
        if direction is None:
            # Either unknown to RiskLimits or explicitly immutable.
            unknown.append(key)
            continue

        current_value = merged_data.get(key)

        # Special-case ``target_annual_vol``: ``None`` means "no vol
        # target" (flat sizing). Switching to a value or vice-versa
        # changes the sizing model — treat any None↔value transition
        # as loosening.
        if key == "target_annual_vol":
            if current_value is None and new_value is not None:
                loosened.append(key)
                continue
            if current_value is not None and new_value is None:
                loosened.append(key)
                continue

        try:
            cmp_current = float(current_value) if current_value is not None else None
            cmp_new = float(new_value) if new_value is not None else None
        except (TypeError, ValueError):
            unknown.append(key)
            continue

        if cmp_current is None or cmp_new is None:
            # Already handled above; defensive.
            continue

        if direction == "lower":
            if cmp_new < cmp_current:
                merged_data[key] = new_value
            elif cmp_new > cmp_current:
                loosened.append(key)
            # equal: no-op
        elif direction == "higher":
            if cmp_new > cmp_current:
                merged_data[key] = new_value
            elif cmp_new < cmp_current:
                loosened.append(key)
            # equal: no-op

    try:
        merged = RiskLimits.model_validate(merged_data)
    except Exception:
        # Validation failed on the merged limits — bail out without
        # mutating; surface every proposed key as unknown so the caller
        # logs the full set and keeps the original limits.
        logger.warning(
            "Refined risk_limits failed pydantic validation; keeping current limits unchanged."
        )
        return current, loosened, sorted(set(unknown) | set(proposed.keys()))

    return merged, loosened, unknown


def _daily_returns_from_trades(
    trades: Sequence[TradeRecord],
    initial_capital: float,
    start_date: str,
    end_date: str,
) -> List[float]:
    """Daily log returns from the equity curve implied by the trades.

    Log basis matches :meth:`EquityCurve.daily_returns` and the rest of
    the metrics module, so OOS-Sharpe / DSR / bootstrap CIs computed
    downstream share the same return convention as the in-sample
    ``compute_performance_metrics`` Sharpe.

    If the equity curve crosses zero (portfolio ruin), the series is
    returned **empty** rather than zero-padding the ruin step. Zeroing
    a wipeout would convert it to a neutral day and let the OOS DSR /
    Sharpe CI / moments report misleadingly low risk; an empty series
    falls through every downstream consumer
    (:func:`summarize_return_moments`, :func:`compute_deflated_sharpe`,
    :func:`bootstrap_sharpe_ci`) as their well-defined "no data" path.
    """
    curve = build_equity_curve_from_trades(
        trades, initial_capital, start_date=start_date, end_date=end_date
    )
    if len(curve.equity) < 2:
        return []
    if any(v <= 0 for v in curve.equity):
        # Ruin: invalidate the whole series.
        return []
    out: List[float] = []
    for i in range(1, len(curve.equity)):
        out.append(math.log(curve.equity[i] / curve.equity[i - 1]))
    return out


def _equity_to_returns(equity: Sequence[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev <= 0:
            out.append(0.0)
        else:
            out.append((equity[i] - prev) / prev)
    return out


def _closes_to_equity(closes: Sequence[float], initial_capital: float) -> List[float]:
    if not closes or closes[0] <= 0:
        return []
    scale = initial_capital / closes[0]
    return [c * scale for c in closes]


def _parse_bar_date(d: str) -> Any:
    from datetime import date

    return date.fromisoformat(d[:10])


def _resolve_vix_provider() -> Optional[Callable[[Sequence[Any]], List[float]]]:
    """Return a VIX provider callable when ``STRATEGY_LAB_VIX_SOURCE`` is
    set, otherwise None so :func:`vix_quartile_subwindows` falls back to
    realized-vol on the benchmark series. Production deployments can
    wire in a Yahoo ``^VIX`` fetcher here without touching callers."""
    source = os.environ.get("STRATEGY_LAB_VIX_SOURCE", "").strip().lower()
    if not source:
        return None
    # Hook point for production providers; unset → realized-vol fallback.
    return None
