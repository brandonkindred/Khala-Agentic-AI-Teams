"""Coverage-probe orchestrator stage (#451).

Runs the static (#447) and indicator (#448) probes after a backtest, and
optionally re-executes with runtime instrumentation (#449/#450) when the
deterministic probes can't classify the failure. Aggregates the outputs
into a single :class:`CoverageReport` that the orchestrator attaches to
:class:`BacktestResult.coverage_report`.

Successful backtests pay zero probe cost: ``should_run_probes`` returns
``False`` and the orchestrator skips the entire stage before any probe
import is touched.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from investment_team.models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    CoverageCategory,
    CoverageReport,
    LikelyBlocker,
    StrategySpec,
)
from investment_team.strategy_lab.coverage_probe.indicator_probe import run_indicator_probe
from investment_team.strategy_lab.coverage_probe.runtime_instrument import (
    instrument_strategy_code,
)
from investment_team.strategy_lab.coverage_probe.static_probe import run_static_probe
from investment_team.trading_service.modes.sandbox_compat import (
    StrategyRunResult,
    run_strategy_code,
)

logger = logging.getLogger(__name__)

#: Minimum closed-trade count below which the probe stage runs even when
#: ``zero_trade_category`` is not set. Three is enough to distinguish a
#: clearly-thin run from a strategy that just had one or two real signals.
LOW_TRADE_THRESHOLD = 3

#: ``zero_trade_category`` values that always trigger the probe stage. The
#: other categories (``ORDERS_REJECTED``, ``ORDERS_UNFILLED``,
#: ``ENTRY_WITH_NO_EXIT``) describe order-lifecycle failures that the
#: existing #404 envelope already pinpoints — coverage probes would add
#: no signal there.
_PROBE_TRIGGERING_CATEGORIES = frozenset(
    {
        "NO_ORDERS_EMITTED",
        "ONLY_WARMUP_ORDERS",
        "UNKNOWN_ZERO_TRADE_PATH",
    }
)

#: Aggregation priority. The static probe's impossible-configuration
#: categories outrank everything because they describe a strategy that
#: cannot trade regardless of the data. Restrictive filters then
#: outrank a conjunction-empty result (one always-zero leg is a stronger
#: signal than a never-true AND). ``UNKNOWN_LOW_COVERAGE`` is the
#: weakest non-OK category and ``COVERAGE_OK`` ranks last.
_CATEGORY_PRIORITY: tuple[CoverageCategory, ...] = (
    CoverageCategory.WARMUP_EXCEEDS_HISTORY,
    CoverageCategory.TARGET_SYMBOL_MISSING,
    CoverageCategory.INSUFFICIENT_BARS,
    CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE,
    CoverageCategory.CONJUNCTION_NEVER_TRUE,
    CoverageCategory.ENTRY_CONDITION_NEVER_TRUE,
    CoverageCategory.UNKNOWN_LOW_COVERAGE,
    CoverageCategory.COVERAGE_OK,
)

# Import-time exhaustiveness check. A future ``CoverageCategory`` value
# not added to ``_CATEGORY_PRIORITY`` would otherwise trigger a
# ``ValueError`` from ``tuple.index`` deep inside the orchestrator at
# runtime. Catching it here keeps the failure local to this module.
assert set(_CATEGORY_PRIORITY) == set(CoverageCategory), (
    "Aggregator _CATEGORY_PRIORITY must list every CoverageCategory value; "
    f"missing: {set(CoverageCategory) - set(_CATEGORY_PRIORITY)}"
)

#: Static-probe categories that make running the indicator probe pointless.
#: A required warm-up window longer than history, or a target symbol that
#: isn't in the fetched universe, are absolute blockers: even if the
#: indicators look fine on paper they cannot fire in this run.
_STATIC_SHORT_CIRCUIT_CATEGORIES = frozenset(
    {
        CoverageCategory.WARMUP_EXCEEDS_HISTORY,
        CoverageCategory.TARGET_SYMBOL_MISSING,
    }
)


# Callable contract for the runtime re-execution. Kept as a module-level
# alias so tests can inject a fake without monkeypatching
# ``run_strategy_code`` globally.
RunStrategyCode = Callable[..., StrategyRunResult]


def should_run_probes(
    diagnostics: Optional[BacktestExecutionDiagnostics],
) -> bool:
    """Gate for the coverage-probe stage.

    Returns ``True`` iff the diagnostics envelope says the run is
    zero/low-trade in a way the probes can speak to. Successful runs and
    runs whose failures are already classified by the #404 lifecycle
    diagnostics (rejected/unfilled orders, entry-with-no-exit) bypass the
    stage entirely.
    """
    if diagnostics is None:
        return False
    if diagnostics.zero_trade_category in _PROBE_TRIGGERING_CATEGORIES:
        return True
    if diagnostics.closed_trades < LOW_TRADE_THRESHOLD:
        return True
    return False


def run_coverage_stage(
    *,
    spec: StrategySpec,
    market_data: Dict[str, pd.DataFrame],
    config: BacktestConfig,
    exec_result: StrategyRunResult,
    run_strategy_code_fn: RunStrategyCode = run_strategy_code,
) -> CoverageReport:
    """Execute the three-stage coverage pipeline and return one report.

    Callers should gate this with :func:`should_run_probes` so successful
    backtests don't pay the cost. The function itself is safe to call on
    any run — it just produces an ``UNKNOWN_LOW_COVERAGE`` report when no
    probe has anything to say.
    """
    fetched_universe: List[str] = [
        sym for sym, df in market_data.items() if isinstance(df, pd.DataFrame)
    ]
    available_bars = max(
        (len(df) for df in market_data.values() if isinstance(df, pd.DataFrame)),
        default=0,
    )

    static_report = run_static_probe(
        spec=spec,
        fetched_universe=fetched_universe,
        available_bars=available_bars,
    )

    # Stage 1 short-circuit: impossible warm-up or missing target symbol
    # makes the indicator probe pointless — the strategy can't trade.
    if static_report.coverage_category in _STATIC_SHORT_CIRCUIT_CATEGORIES:
        return _annotate(
            static_report,
            exec_diag=exec_result.execution_diagnostics,
            indicator_category=None,
            runtime_event_count=None,
        )

    indicator_report = run_indicator_probe(
        strategy_code=spec.strategy_code or "",
        market_data=market_data,
        warmup_bars_required=static_report.warmup_bars_required,
    )

    merged = merge_reports(
        static_report,
        indicator_report,
        exec_diag=exec_result.execution_diagnostics,
    )

    # Stage 3: only re-execute with instrumentation when the deterministic
    # probes can't classify the failure.
    if merged.coverage_category is not CoverageCategory.UNKNOWN_LOW_COVERAGE:
        return merged

    runtime_blockers, event_count = _runtime_reexecute(
        spec=spec,
        market_data=market_data,
        config=config,
        run_strategy_code_fn=run_strategy_code_fn,
    )
    if runtime_blockers:
        merged = merged.model_copy(
            update={
                "likely_blockers": _dedup_blockers(list(merged.likely_blockers) + runtime_blockers),
                "summary": _summary_line(
                    static_cat=static_report.coverage_category,
                    indicator_cat=indicator_report.coverage_category,
                    runtime_event_count=event_count,
                ),
            }
        )
    return merged


def merge_reports(
    static_report: CoverageReport,
    indicator_report: CoverageReport,
    *,
    exec_diag: Optional[BacktestExecutionDiagnostics] = None,
) -> CoverageReport:
    """Merge a static and indicator report into one ``CoverageReport``.

    Aggregation rules:

    * Category: highest-priority of the two per ``_CATEGORY_PRIORITY``.
    * Numeric fields: per-field max across the two reports.
    * Subconditions: indicator report's list verbatim (static never
      produces any).
    * Blockers: static then indicator, deduplicated on
      ``(reason, evidence)`` preserving first-seen order.
    * Summary: deterministic templated string.
    """
    category = _pick_category(static_report.coverage_category, indicator_report.coverage_category)
    blockers = _dedup_blockers(
        list(static_report.likely_blockers) + list(indicator_report.likely_blockers)
    )
    entry_orders_emitted = exec_diag.orders_accepted if exec_diag is not None else 0
    return CoverageReport(
        coverage_category=category,
        summary=_summary_line(
            static_cat=static_report.coverage_category,
            indicator_cat=indicator_report.coverage_category,
            runtime_event_count=None,
        ),
        symbols_checked=max(static_report.symbols_checked, indicator_report.symbols_checked),
        bars_checked=max(static_report.bars_checked, indicator_report.bars_checked),
        warmup_bars_required=max(
            static_report.warmup_bars_required,
            indicator_report.warmup_bars_required,
        ),
        entry_orders_emitted=entry_orders_emitted,
        subconditions=list(indicator_report.subconditions),
        likely_blockers=blockers,
    )


# ─────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────


def _pick_category(
    static_cat: CoverageCategory, indicator_cat: CoverageCategory
) -> CoverageCategory:
    static_rank = _CATEGORY_PRIORITY.index(static_cat)
    indicator_rank = _CATEGORY_PRIORITY.index(indicator_cat)
    return static_cat if static_rank <= indicator_rank else indicator_cat


def _dedup_blockers(blockers: List[LikelyBlocker]) -> List[LikelyBlocker]:
    """Stable dedup of likely blockers on ``(reason, evidence)``."""
    seen: set[tuple[str, str]] = set()
    out: List[LikelyBlocker] = []
    for b in blockers:
        key = (b.reason, b.evidence)
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


def _summary_line(
    *,
    static_cat: CoverageCategory,
    indicator_cat: CoverageCategory,
    runtime_event_count: Optional[int],
) -> str:
    runtime = "n/a" if runtime_event_count is None else str(runtime_event_count)
    return f"static={static_cat.value}; indicator={indicator_cat.value}; runtime_events={runtime}"


def _annotate(
    report: CoverageReport,
    *,
    exec_diag: Optional[BacktestExecutionDiagnostics],
    indicator_category: Optional[CoverageCategory],
    runtime_event_count: Optional[int],
) -> CoverageReport:
    """Stamp orchestrator-only fields on a stand-alone probe report."""
    indicator_cat = indicator_category or CoverageCategory.UNKNOWN_LOW_COVERAGE
    return report.model_copy(
        update={
            "entry_orders_emitted": (exec_diag.orders_accepted if exec_diag is not None else 0),
            "summary": _summary_line(
                static_cat=report.coverage_category,
                indicator_cat=indicator_cat,
                runtime_event_count=runtime_event_count,
            ),
        }
    )


def _runtime_reexecute(
    *,
    spec: StrategySpec,
    market_data: Dict[str, pd.DataFrame],
    config: BacktestConfig,
    run_strategy_code_fn: RunStrategyCode,
) -> tuple[List[LikelyBlocker], Optional[int]]:
    """Instrument the strategy and re-run with ``coverage_probe_mode=True``.

    Returns ``(blockers, event_count)``. ``event_count`` is ``None`` when
    the instrumentation or re-execution didn't produce a usable event
    list (failure paths). The returned blockers are structured material
    for #452 to render into the refinement prompt; the runtime stage
    does not change the merged category — it only enriches the evidence.
    """
    code = spec.strategy_code or ""
    if not code:
        return (
            [
                LikelyBlocker(
                    reason="runtime_probe_skipped",
                    evidence="spec.strategy_code is empty",
                )
            ],
            None,
        )

    instrumented_code, rule_index = instrument_strategy_code(code)
    if not rule_index.rules:
        # Nothing was instrumented (no on_bar, malformed source, or
        # already-instrumented). Record as evidence and bail.
        return (
            [
                LikelyBlocker(
                    reason="runtime_probe_skipped",
                    evidence="no instrumentable predicates in on_bar",
                )
            ],
            None,
        )

    instrumented_spec = spec.model_copy(update={"strategy_code": instrumented_code})

    try:
        probe_exec = run_strategy_code_fn(
            instrumented_code,
            market_data,
            config,
            strategy=instrumented_spec,
            coverage_probe_mode=True,
        )
    except Exception as exc:  # noqa: BLE001 — probe is best-effort
        logger.debug("coverage_probe runtime re-execution raised: %s", exc)
        return (
            [
                LikelyBlocker(
                    reason="runtime_probe_failed",
                    evidence=f"{type(exc).__name__}: {str(exc)[:160]}",
                )
            ],
            None,
        )

    if not probe_exec.success or not probe_exec.probe_events:
        return (
            [
                LikelyBlocker(
                    reason="runtime_probe_failed",
                    evidence=(probe_exec.error_type or "no probe_events frame")[:160],
                )
            ],
            None,
        )

    events = probe_exec.probe_events.get("events") or []
    blockers = _runtime_events_to_blockers(events, rule_index.rules)
    return blockers, len(events)


def _runtime_events_to_blockers(
    events: List[Dict[str, Any]], rule_labels: Dict[str, str]
) -> List[LikelyBlocker]:
    """Render runtime probe events as structured ``LikelyBlocker`` rows.

    The renderer is intentionally minimal — #451's contract is "incorporate
    probe_events"; full prompt formatting belongs to #452. Each rule is
    one row, sorted by ``rule_id`` for determinism, hit-rate left as
    ``None`` (the runtime collector caps per-rule events rather than
    counting bars, so a true rate isn't computable here).
    """
    out: List[LikelyBlocker] = []
    for ev in sorted(events, key=lambda e: str(e.get("rule_id", ""))):
        rule_id = str(ev.get("rule_id", ""))
        if not rule_id:
            continue
        label = rule_labels.get(rule_id, rule_id)
        hit_count = ev.get("hit_count")
        first = ev.get("first_true_bar")
        last = ev.get("last_true_bar")
        evidence_parts: List[str] = []
        if hit_count is not None:
            evidence_parts.append(f"hits={hit_count}")
        if first is not None:
            evidence_parts.append(f"first={first}")
        if last is not None:
            evidence_parts.append(f"last={last}")
        out.append(
            LikelyBlocker(
                reason=f"runtime: {label}",
                evidence=" ".join(evidence_parts),
            )
        )
    return out


__all__ = [
    "LOW_TRADE_THRESHOLD",
    "merge_reports",
    "run_coverage_stage",
    "should_run_probes",
]
