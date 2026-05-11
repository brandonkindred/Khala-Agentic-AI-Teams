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
from typing import Any, Callable, get_args

import pandas as pd

from investment_team.market_data_service import OHLCVBar
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
from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult

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

# Permanent invariant: every entry in ``_PROBE_TRIGGERING_CATEGORIES``
# must be a real ``ZeroTradeCategory`` literal. Renames in ``models.py``
# would otherwise drift past this set silently. Uses an explicit raise
# rather than ``assert`` so the guard survives ``python -O``.
if not _PROBE_TRIGGERING_CATEGORIES <= set(get_args(ZeroTradeCategory)):
    raise AssertionError(
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

# Permanent invariant: ``_CATEGORY_PRIORITY`` must list every value of
# ``CoverageCategory``. A missing value would otherwise trigger a
# ``KeyError`` from ``_CATEGORY_RANK`` deep inside the orchestrator at
# runtime. Survives ``python -O`` (see note above).
if set(_CATEGORY_PRIORITY) != set(CoverageCategory):
    raise AssertionError(
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

# Runtime-stage outcome tokens. Single source of truth — both
# ``_summary_line`` (which renders them) and ``_runtime_reexecute``
# (which produces them) read from here, so the vocabulary can't drift.
# A numeric token (``"0"``, ``"1"``, …) is also valid and reflects the
# count of ``runtime:`` blockers produced.
_RUNTIME_NOT_RUN = "n/a"
_RUNTIME_SKIPPED = "skipped"
_RUNTIME_FAILED = "failed"
_RUNTIME_NO_FRAME = "no_frame"

#: Type alias for market data as it appears at the probe stage. The
#: orchestrator hands us ``list[OHLCVBar]`` per symbol (production
#: contract from ``run_backtest``); the existing probe tests use
#: ``pd.DataFrame`` directly. Both shapes are accepted and the
#: indicator probe gets converted DataFrames internally.
SymbolBars = list[OHLCVBar] | pd.DataFrame

# Callable contract for the runtime re-execution. Kept as a module-level
# alias so tests can inject a fake.
RunStrategyCode = Callable[..., StrategyRunResult]


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def should_run_probes(
    diagnostics: BacktestExecutionDiagnostics | None,
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
    market_data: dict[str, SymbolBars],
    config: BacktestConfig,
    exec_result: StrategyRunResult,
    run_strategy_code_fn: RunStrategyCode,
) -> CoverageReport:
    """Run the coverage pipeline and return one ``CoverageReport``.

    The pipeline has five phases — two of which are early exits:

    1. Summarise market data (universe + longest-history bars).
    2. Static probe (#447). Exits with a finalised report if the static
       category is an absolute blocker (``WARMUP_EXCEEDS_HISTORY`` /
       ``TARGET_SYMBOL_MISSING``).
    3. Indicator probe (#448). Merged with the static report.
    4. Exit with the merged report if the merged category is conclusive.
    5. Runtime instrumentation (#449/#450). Augments the merged report
       with structured ``runtime:`` blockers and a runtime token in the
       summary.

    Callers should gate this with :func:`should_run_probes` so successful
    backtests don't pay the cost. The function itself is safe to call on
    any run — it just produces an ``UNKNOWN_LOW_COVERAGE`` report when no
    probe has anything to say.

    ``market_data`` accepts either ``list[OHLCVBar]`` (production shape
    from the orchestrator) or ``pd.DataFrame`` (existing probe-test
    fixtures). The indicator probe is fed pandas-converted data; the
    runtime stage forwards the original objects so the harness sees the
    same shape it always does.
    """
    exec_diag = exec_result.execution_diagnostics
    universe, available_bars = _summarize_market_data(market_data)

    static_report = run_static_probe(
        spec=spec,
        fetched_universe=universe,
        available_bars=available_bars,
    )

    # Stage 1 short-circuit: impossible warm-up or missing target symbol
    # makes the indicator probe pointless — the strategy can't trade.
    if static_report.coverage_category in _STATIC_SHORT_CIRCUIT_CATEGORIES:
        return _finalize_short_circuit(static_report, exec_diag=exec_diag)

    indicator_report = run_indicator_probe(
        strategy_code=spec.strategy_code or "",
        market_data=_to_dataframes(market_data),
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
        market_data=market_data,  # original shape — harness expects OHLCVBar lists
        config=config,
        run_strategy_code_fn=run_strategy_code_fn,
    )


def merge_reports(
    static_report: CoverageReport,
    indicator_report: CoverageReport,
    *,
    exec_diag: BacktestExecutionDiagnostics | None = None,
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
    * Subconditions: each ``SubconditionCoverage`` deep-copied from the
      indicator report so downstream consumers can mutate without
      affecting the source.
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
        subconditions=[s.model_copy() for s in indicator_report.subconditions],
        likely_blockers=_dedup_blockers(
            list(static_report.likely_blockers) + list(indicator_report.likely_blockers)
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────


def _summarize_market_data(
    market_data: dict[str, SymbolBars],
) -> tuple[list[str], int]:
    """Return ``(universe, longest_symbol_bars)`` in a single pass.

    ``universe`` is the list of symbols whose ``data`` has at least one
    bar (DataFrame or ``list[OHLCVBar]``). ``longest_symbol_bars`` is
    the max bar count across that filtered set — the static probe needs
    it to evaluate warm-up windows.
    """
    valid = [(sym, data) for sym, data in market_data.items() if _has_bars(data)]
    universe = [sym for sym, _ in valid]
    longest = max((len(data) for _, data in valid), default=0)
    return universe, longest


def _is_ohlcv_bar_list(data: object) -> bool:
    """True when ``data`` is a non-empty ``list[OHLCVBar]``.

    Single source of truth for "production-shape market-data entry" —
    both :func:`_has_bars` (used by :func:`_summarize_market_data`) and
    :func:`_to_dataframes` consult this predicate so they can't drift
    on what counts as a valid bar list.
    """
    return isinstance(data, list) and bool(data) and isinstance(data[0], OHLCVBar)


def _has_bars(data: object) -> bool:
    """True when ``data`` is a non-empty DataFrame or OHLCVBar list."""
    if isinstance(data, pd.DataFrame):
        return len(data) > 0
    return _is_ohlcv_bar_list(data)


_OHLCV_FIELDS: tuple[str, ...] = ("date", "open", "high", "low", "close", "volume")


def _to_dataframes(market_data: dict[str, SymbolBars]) -> dict[str, pd.DataFrame]:
    """Convert OHLCV-bar lists to pandas DataFrames for the indicator probe.

    The orchestrator hands us ``list[OHLCVBar]`` per symbol (production
    shape) but the indicator probe is written against ``pd.DataFrame``.
    Already-DataFrame entries are passed through. Empty / malformed
    entries are dropped (with a debug log) so the probe's
    ``isinstance(df, pd.DataFrame)`` filter stays valid.
    """
    out: dict[str, pd.DataFrame] = {}
    for sym, data in market_data.items():
        if isinstance(data, pd.DataFrame):
            out[sym] = data
            continue
        if _is_ohlcv_bar_list(data):
            out[sym] = _ohlcv_list_to_dataframe(data)
            continue
        logger.debug(
            "coverage_probe: dropping market_data entry for %r — "
            "unexpected shape %s (expected pd.DataFrame or non-empty list[OHLCVBar])",
            sym,
            type(data).__name__,
        )
    return out


def _ohlcv_list_to_dataframe(bars: list[OHLCVBar]) -> pd.DataFrame:
    """Build a DataFrame from a list of ``OHLCVBar`` via attribute access.

    Uses ``from_records`` with explicit columns rather than
    ``[bar.model_dump() for bar in bars]`` to skip the per-bar pydantic
    dict construction — meaningfully faster for the multi-thousand-bar
    history typical of strategy backtests.
    """
    df = pd.DataFrame.from_records(
        ((b.date, b.open, b.high, b.low, b.close, b.volume) for b in bars),
        columns=_OHLCV_FIELDS,
    )
    # Index on the date column when every value parses; ``errors="raise"``
    # lets pandas short-circuit on the first bad value, so we don't pay
    # the full conversion cost just to throw it away. Any parse failure
    # keeps the integer index — a half-parsed DatetimeIndex would leave
    # NaT rows that downstream renderers spell out as the literal "NaT".
    try:
        df.index = pd.to_datetime(df["date"], errors="raise")
    except (ValueError, TypeError):
        pass
    return df


def _entry_orders_emitted(exec_diag: BacktestExecutionDiagnostics | None) -> int:
    return exec_diag.orders_accepted if exec_diag is not None else 0


def _category_rank(cat: CoverageCategory) -> int:
    """Lookup priority rank for a coverage category (lower = wins)."""
    return _CATEGORY_RANK[cat]


def _pick_category(
    static_cat: CoverageCategory, indicator_cat: CoverageCategory
) -> CoverageCategory:
    return min(static_cat, indicator_cat, key=_category_rank)


def _dedup_blockers(blockers: list[LikelyBlocker]) -> list[LikelyBlocker]:
    """Stable dedup of likely blockers on ``(reason, evidence, hit_rate)``."""
    seen: set[tuple[str, str, float | None]] = set()
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
    indicator_cat: CoverageCategory | None,
    runtime: str = _RUNTIME_NOT_RUN,
) -> str:
    """Render the prompt-facing summary line.

    ``indicator_cat=None`` means the indicator probe was deliberately
    skipped (static short-circuited); render as ``SKIPPED`` rather than
    lying with a default-valued ``UNKNOWN_LOW_COVERAGE``.

    ``runtime`` is a short token from the runtime stage. See the
    ``_RUNTIME_*`` constants at the top of the module for the
    vocabulary; numeric strings (``"0"``, ``"1"``, …) are also valid
    and reflect the produced ``runtime:`` blocker count.
    """
    indicator = "SKIPPED" if indicator_cat is None else indicator_cat.value
    return f"static={static_cat.value}; indicator={indicator}; runtime_events={runtime}"


def _finalize_short_circuit(
    report: CoverageReport,
    *,
    exec_diag: BacktestExecutionDiagnostics | None,
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


# Runtime-stage outcome helpers. Each builds a ``(blockers, token)``
# pair where ``token`` is the runtime-events render in the summary.


def _skip(reason: str, evidence: str) -> tuple[list[LikelyBlocker], str]:
    return [LikelyBlocker(reason=reason, evidence=evidence)], _RUNTIME_SKIPPED


def _fail(error_type: str) -> tuple[list[LikelyBlocker], str]:
    return (
        [LikelyBlocker(reason="runtime_probe_failed", evidence=error_type[:160])],
        _RUNTIME_FAILED,
    )


def _no_frame() -> tuple[list[LikelyBlocker], str]:
    return (
        [
            LikelyBlocker(
                reason="runtime_probe_no_frame",
                evidence="strategy completed without emitting probe_events",
            )
        ],
        _RUNTIME_NO_FRAME,
    )


def _no_hits(rule_count: int) -> tuple[list[LikelyBlocker], str]:
    return (
        [
            LikelyBlocker(
                reason="runtime_probe_no_hits",
                evidence=f"rules_instrumented={rule_count}; hits=0",
            )
        ],
        "0",
    )


def _augment_with_runtime(
    merged: CoverageReport,
    *,
    static_cat: CoverageCategory,
    indicator_cat: CoverageCategory,
    spec: StrategySpec,
    market_data: dict[str, SymbolBars],
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
    market_data: dict[str, SymbolBars],
    config: BacktestConfig,
    run_strategy_code_fn: RunStrategyCode,
) -> tuple[list[LikelyBlocker], str]:
    """Instrument the strategy and re-run with ``coverage_probe_mode=True``.

    Returns ``(blockers, runtime_token)``. Tokens are documented in the
    module-level ``_RUNTIME_*`` constants; numeric strings reflect the
    produced ``runtime:`` blocker count.

    This function orchestrates only — actual outcome classification
    lives in :func:`_interpret_probe_exec`.
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
        # ``logger.warning`` is the middle ground between ``exception``
        # (ERROR, page-worthy) and ``debug`` (silent in prod). The probe
        # is best-effort, so a sandbox crash isn't a fault — but a spike
        # in crashes is worth seeing in steady-state logs. ``exc_info``
        # preserves the traceback for triage.
        logger.warning("coverage_probe runtime re-execution raised", exc_info=True)
        return _fail(f"{type(exc).__name__}: {exc}")

    return _interpret_probe_exec(probe_exec, rule_index.rules)


def _interpret_probe_exec(
    probe_exec: StrategyRunResult,
    rule_labels: dict[str, str],
) -> tuple[list[LikelyBlocker], str]:
    """Map a successful sandbox re-execution to ``(blockers, token)``.

    Split out from :func:`_runtime_reexecute` so the orchestration
    (set up + run subprocess + handle exception) reads separately from
    outcome interpretation. Each branch corresponds to one observable
    state of the probe envelope.
    """
    if not probe_exec.success:
        return _fail(probe_exec.error_type or "unknown error")

    if not probe_exec.probe_events:
        # Subprocess ran cleanly but the harness didn't flush a frame.
        # Distinct from a hard failure — surface separately so #452 can
        # decide whether to retry vs. treat the strategy as opaque.
        return _no_frame()

    events = probe_exec.probe_events.get("events") or []
    if not events:
        # The runtime probe ran and observed zero predicate firings —
        # the strongest possible evidence of ENTRY_CONDITION_NEVER_TRUE.
        return _no_hits(len(rule_labels))

    blockers = _runtime_events_to_blockers(events, rule_labels)
    # The token reflects the number of blockers actually produced — if
    # the harness ever emits events with empty/None ``rule_id``s they
    # get filtered out, and the count stays truthful.
    return blockers, str(len(blockers))


def _runtime_events_to_blockers(
    events: list[dict[str, Any]], rule_labels: dict[str, str]
) -> list[LikelyBlocker]:
    """Render runtime probe events as structured ``LikelyBlocker`` rows.

    The pipeline is filter → sort → map:

    1. Filter events without a valid ``rule_id`` (None / empty string).
       Doing this first means the produced-blocker count stays truthful
       and the sort key receives only valid strings.
    2. Sort the survivors by their numeric rule index so the prompt
       reads ``r0, r1, r2, r10`` rather than the lexicographic mess.
    3. Map each survivor to a ``LikelyBlocker`` carrying the rule label
       and a compact evidence string.

    #451's contract is "incorporate probe_events"; full prompt
    formatting belongs to #452. Hit-rate is left as ``None`` (the
    runtime collector caps per-rule events rather than counting bars,
    so a true rate isn't computable here).
    """
    valid = [(rid, ev) for ev in events if (rid := _normalized_rule_id(ev.get("rule_id")))]
    valid.sort(key=lambda pair: _rule_sort_key(pair[0]))
    return [_event_to_blocker(rule_id, ev, rule_labels) for rule_id, ev in valid]


def _normalized_rule_id(raw: Any) -> str:
    """Return the canonical string rule_id, or an empty string to skip."""
    if raw is None:
        return ""  # ``str(None) == "None"`` would otherwise sneak through
    return str(raw)


def _event_to_blocker(
    rule_id: str, event: dict[str, Any], rule_labels: dict[str, str]
) -> LikelyBlocker:
    label = rule_labels.get(rule_id, rule_id)
    evidence_parts = [
        f"{tag}={value}"
        for tag, value in (
            ("hits", event.get("hit_count")),
            ("first", event.get("first_true_bar")),
            ("last", event.get("last_true_bar")),
        )
        if value is not None
    ]
    return LikelyBlocker(reason=f"runtime: {label}", evidence=" ".join(evidence_parts))


def _rule_sort_key(rule_id: str) -> tuple[int, int, str]:
    """Numeric-aware sort key for valid ``rule_id`` strings.

    Rules are emitted as ``r0``, ``r1``, …, ``r10``. Lexicographic sort
    would order them ``r0, r1, r10, r2`` — numeric sort is more
    intuitive in the prompt. Strings that don't match the ``rN`` shape
    fall back to lexicographic order, sorted after the numeric ones.

    Callers must filter out empty / ``None`` rule_ids before invoking
    this function (see :func:`_runtime_events_to_blockers`).
    """
    if rule_id.startswith("r") and rule_id[1:].isdigit():
        return (0, int(rule_id[1:]), "")
    return (1, 0, rule_id)


__all__ = [
    "LOW_TRADE_THRESHOLD",
    "SymbolBars",
    "merge_reports",
    "run_coverage_stage",
    "should_run_probes",
]
