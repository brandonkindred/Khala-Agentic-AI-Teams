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
from typing import Any, Callable, Optional, get_args

import pandas as pd

from investment_team.models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    CoverageCategory,
    CoverageReport,
    LikelyBlocker,
    StrategySpec,
    ZeroTradeCategory,
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

# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

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

# Import-time check: every entry in ``_PROBE_TRIGGERING_CATEGORIES`` must
# be a real ``ZeroTradeCategory`` literal. Renames in ``models.py`` would
# otherwise drift past this set silently.
assert _PROBE_TRIGGERING_CATEGORIES <= set(get_args(ZeroTradeCategory)), (
    "_PROBE_TRIGGERING_CATEGORIES contains values not in ZeroTradeCategory; "
    f"orphans: {_PROBE_TRIGGERING_CATEGORIES - set(get_args(ZeroTradeCategory))}"
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
# ``KeyError`` from ``_CATEGORY_RANK`` deep inside the orchestrator at
# runtime. Catching it here keeps the failure local to this module.
assert set(_CATEGORY_PRIORITY) == set(CoverageCategory), (
    "Aggregator _CATEGORY_PRIORITY must list every CoverageCategory value; "
    f"missing: {set(CoverageCategory) - set(_CATEGORY_PRIORITY)}"
)

#: O(1) priority lookup; lower rank = higher priority.
_CATEGORY_RANK: dict[CoverageCategory, int] = {cat: i for i, cat in enumerate(_CATEGORY_PRIORITY)}

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


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


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
    market_data: dict[str, pd.DataFrame],
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
    exec_diag = exec_result.execution_diagnostics
    available_bars = _longest_symbol_bars(market_data)

    static_report = run_static_probe(
        spec=spec,
        fetched_universe=_fetched_universe(market_data),
        available_bars=available_bars,
    )

    # Stage 1 short-circuit: impossible warm-up or missing target symbol
    # makes the indicator probe pointless — the strategy can't trade.
    if static_report.coverage_category in _STATIC_SHORT_CIRCUIT_CATEGORIES:
        return _finalize_short_circuit(static_report, exec_diag=exec_diag)

    indicator_report = run_indicator_probe(
        strategy_code=spec.strategy_code or "",
        market_data=market_data,
        warmup_bars_required=static_report.warmup_bars_required,
    )

    merged = merge_reports(static_report, indicator_report, exec_diag=exec_diag)

    # Stage 3: only re-execute with instrumentation when the deterministic
    # probes can't classify the failure.
    if merged.coverage_category is not CoverageCategory.UNKNOWN_LOW_COVERAGE:
        return merged

    return _augment_with_runtime(
        merged,
        static_cat=static_report.coverage_category,
        indicator_cat=indicator_report.coverage_category,
        spec=spec,
        market_data=market_data,
        config=config,
        run_strategy_code_fn=run_strategy_code_fn,
    )


def merge_reports(
    static_report: CoverageReport,
    indicator_report: CoverageReport,
    *,
    exec_diag: Optional[BacktestExecutionDiagnostics] = None,
) -> CoverageReport:
    """Merge a static and indicator report into one ``CoverageReport``.

    Aggregation rules:

    * Category: highest-priority of the two per ``_CATEGORY_PRIORITY``.
    * ``warmup_bars_required``: per-field max (both probes agree on this
      unit — bars-per-symbol).
    * ``bars_checked`` / ``symbols_checked``: taken from the indicator
      report. The two probes use different denominators (static = longest
      single-symbol history; indicator = sum across symbols), so a
      ``max(...)`` across them would conflate units. The indicator
      probe's value reflects what was actually examined for hit-rate
      computation, which is what downstream consumers want.
    * Subconditions: indicator report's list verbatim (static never
      produces any).
    * Blockers: static then indicator, deduplicated on
      ``(reason, evidence, hit_rate)`` preserving first-seen order.
    * Summary: deterministic templated string.
    """
    return CoverageReport(
        coverage_category=_pick_category(
            static_report.coverage_category, indicator_report.coverage_category
        ),
        summary=_summary_line(
            static_cat=static_report.coverage_category,
            indicator_cat=indicator_report.coverage_category,
        ),
        symbols_checked=indicator_report.symbols_checked,
        bars_checked=indicator_report.bars_checked,
        warmup_bars_required=max(
            static_report.warmup_bars_required,
            indicator_report.warmup_bars_required,
        ),
        entry_orders_emitted=_entry_orders_emitted(exec_diag),
        subconditions=list(indicator_report.subconditions),
        likely_blockers=_dedup_blockers(
            list(static_report.likely_blockers) + list(indicator_report.likely_blockers)
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────


def _fetched_universe(market_data: dict[str, pd.DataFrame]) -> list[str]:
    return [sym for sym, df in market_data.items() if isinstance(df, pd.DataFrame)]


def _longest_symbol_bars(market_data: dict[str, pd.DataFrame]) -> int:
    return max(
        (len(df) for df in market_data.values() if isinstance(df, pd.DataFrame)),
        default=0,
    )


def _entry_orders_emitted(exec_diag: Optional[BacktestExecutionDiagnostics]) -> int:
    return exec_diag.orders_accepted if exec_diag is not None else 0


def _pick_category(
    static_cat: CoverageCategory, indicator_cat: CoverageCategory
) -> CoverageCategory:
    return min(static_cat, indicator_cat, key=_CATEGORY_RANK.__getitem__)


def _dedup_blockers(blockers: list[LikelyBlocker]) -> list[LikelyBlocker]:
    """Stable dedup of likely blockers on ``(reason, evidence, hit_rate)``."""
    seen: set[tuple[str, str, Optional[float]]] = set()
    out: list[LikelyBlocker] = []
    for b in blockers:
        key = (b.reason, b.evidence, b.hit_rate)
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


def _summary_line(
    *,
    static_cat: CoverageCategory,
    indicator_cat: Optional[CoverageCategory],
    runtime: str = "n/a",
) -> str:
    """Render the prompt-facing summary line.

    ``indicator_cat=None`` means the indicator probe was deliberately
    skipped (static short-circuited); render as ``SKIPPED`` rather than
    lying with a default-valued ``UNKNOWN_LOW_COVERAGE``.

    ``runtime`` is a free-form short token: ``"n/a"`` (didn't run),
    ``"0"`` / ``"5"`` (event count), ``"failed"`` / ``"no_frame"`` /
    ``"skipped"`` (negative outcomes).
    """
    indicator = "SKIPPED" if indicator_cat is None else indicator_cat.value
    return f"static={static_cat.value}; indicator={indicator}; runtime_events={runtime}"


def _finalize_short_circuit(
    report: CoverageReport,
    *,
    exec_diag: Optional[BacktestExecutionDiagnostics],
) -> CoverageReport:
    """Stamp orchestrator-only fields onto a stand-alone static report."""
    return report.model_copy(
        update={
            "entry_orders_emitted": _entry_orders_emitted(exec_diag),
            "summary": _summary_line(
                static_cat=report.coverage_category,
                indicator_cat=None,  # indicator probe was skipped
            ),
        }
    )


def _skip(reason: str, evidence: str) -> tuple[list[LikelyBlocker], str]:
    """Build a (blockers, runtime-token) pair for an unmet runtime precondition."""
    return [LikelyBlocker(reason=reason, evidence=evidence)], "skipped"


def _fail(error_type: str) -> tuple[list[LikelyBlocker], str]:
    """Build a (blockers, runtime-token) pair for a hard runtime failure."""
    return (
        [LikelyBlocker(reason="runtime_probe_failed", evidence=error_type[:160])],
        "failed",
    )


def _augment_with_runtime(
    merged: CoverageReport,
    *,
    static_cat: CoverageCategory,
    indicator_cat: CoverageCategory,
    spec: StrategySpec,
    market_data: dict[str, pd.DataFrame],
    config: BacktestConfig,
    run_strategy_code_fn: RunStrategyCode,
) -> CoverageReport:
    """Run the instrumented re-execution and fold its evidence into ``merged``.

    Always updates the merged report's summary with the runtime outcome so
    the prompt-facing string distinguishes "didn't run" from "ran, 0 hits".
    Never changes ``coverage_category`` — that's #452's call once it has
    the structured runtime evidence in hand.
    """
    runtime_blockers, runtime_token = _runtime_reexecute(
        spec=spec,
        market_data=market_data,
        config=config,
        run_strategy_code_fn=run_strategy_code_fn,
    )
    return merged.model_copy(
        update={
            "likely_blockers": _dedup_blockers(list(merged.likely_blockers) + runtime_blockers),
            "summary": _summary_line(
                static_cat=static_cat,
                indicator_cat=indicator_cat,
                runtime=runtime_token,
            ),
        }
    )


def _runtime_reexecute(
    *,
    spec: StrategySpec,
    market_data: dict[str, pd.DataFrame],
    config: BacktestConfig,
    run_strategy_code_fn: RunStrategyCode,
) -> tuple[list[LikelyBlocker], str]:
    """Instrument the strategy and re-run with ``coverage_probe_mode=True``.

    Returns ``(blockers, runtime_token)``. The token is one of:

    * ``"<int>"`` — the number of probe events the harness emitted.
      ``"0"`` is a legitimate, informative outcome ("predicates never
      fired across the run").
    * ``"skipped"`` — preconditions weren't met (no source, no
      instrumentable predicates).
    * ``"failed"`` — the subprocess crashed.
    * ``"no_frame"`` — subprocess ran cleanly but the harness produced
      no ``probe_events`` frame (likely a soft failure inside ``on_bar``
      before ``end``).

    The blockers list is structured material for #452 to render into the
    refinement prompt; the runtime stage does not change the merged
    category — it only enriches the evidence.
    """
    code = spec.strategy_code or ""
    if not code:
        return _skip("runtime_probe_skipped", "spec.strategy_code is empty")

    instrumented_code, rule_index = instrument_strategy_code(code)
    if not rule_index.rules:
        # Nothing was instrumented (no on_bar, malformed source, or
        # already-instrumented). Record as evidence and bail.
        return _skip(
            "runtime_probe_skipped",
            "no instrumentable predicates in on_bar",
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
        logger.exception("coverage_probe runtime re-execution raised")
        return _fail(f"{type(exc).__name__}: {str(exc)[:120]}")

    if not probe_exec.success:
        return _fail(probe_exec.error_type or "unknown error")

    if not probe_exec.probe_events:
        # Subprocess ran cleanly but the harness didn't flush a frame.
        # Distinct from a hard failure — surface separately so #452 can
        # decide whether to retry vs. treat the strategy as opaque.
        return (
            [
                LikelyBlocker(
                    reason="runtime_probe_no_frame",
                    evidence="strategy completed without emitting probe_events",
                )
            ],
            "no_frame",
        )

    events = probe_exec.probe_events.get("events") or []
    if not events:
        # The runtime probe ran and observed zero predicate firings —
        # the strongest possible evidence of ENTRY_CONDITION_NEVER_TRUE.
        # #452 will fold this into the prompt; for now we record it as
        # a single high-signal blocker.
        return (
            [
                LikelyBlocker(
                    reason="runtime_probe_no_hits",
                    evidence=f"rules_instrumented={len(rule_index.rules)}; hits=0",
                )
            ],
            "0",
        )

    blockers = _runtime_events_to_blockers(events, rule_index.rules)
    return blockers, str(len(events))


def _runtime_events_to_blockers(
    events: list[dict[str, Any]], rule_labels: dict[str, str]
) -> list[LikelyBlocker]:
    """Render runtime probe events as structured ``LikelyBlocker`` rows.

    The renderer is intentionally minimal — #451's contract is "incorporate
    probe_events"; full prompt formatting belongs to #452. Each rule is
    one row, sorted by ``rule_id`` for determinism, hit-rate left as
    ``None`` (the runtime collector caps per-rule events rather than
    counting bars, so a true rate isn't computable here).
    """
    out: list[LikelyBlocker] = []
    for ev in sorted(events, key=lambda e: _rule_sort_key(e.get("rule_id"))):
        rule_id = str(ev.get("rule_id", ""))
        if not rule_id:
            continue
        label = rule_labels.get(rule_id, rule_id)
        evidence_parts = [
            f"{tag}={value}"
            for tag, value in (
                ("hits", ev.get("hit_count")),
                ("first", ev.get("first_true_bar")),
                ("last", ev.get("last_true_bar")),
            )
            if value is not None
        ]
        out.append(
            LikelyBlocker(
                reason=f"runtime: {label}",
                evidence=" ".join(evidence_parts),
            )
        )
    return out


def _rule_sort_key(rule_id: Any) -> tuple[int, int, str]:
    """Numeric-aware sort key for ``rule_id`` strings.

    Rules are emitted as ``r0``, ``r1``, …, ``r10``. Lexicographic sort
    would order them ``r0, r1, r10, r2`` — numeric sort is more
    intuitive in the prompt. Strings that don't match the ``rN`` shape
    fall back to lexicographic order, sorted after the numeric ones.
    """
    raw = str(rule_id) if rule_id is not None else ""
    if raw.startswith("r") and raw[1:].isdigit():
        return (0, int(raw[1:]), "")
    return (1, 0, raw)


__all__ = [
    "LOW_TRADE_THRESHOLD",
    "merge_reports",
    "run_coverage_stage",
    "should_run_probes",
]
