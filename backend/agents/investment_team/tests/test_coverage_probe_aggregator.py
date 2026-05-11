"""Unit tests for the coverage-probe aggregator module (#451).

Pure, fixture-free tests of the building blocks: ``should_run_probes``
(the gate), ``merge_reports`` (priority + dedup + numeric merge),
``_dedup_blockers`` (hit-rate-aware dedup), and the module-level
priority invariant. Tests that exercise the full pipeline
(``run_coverage_stage``) live in ``test_coverage_probe_stage.py``;
orchestrator-wiring tests live in ``test_coverage_probe_orchestrator_stage.py``.
"""

from __future__ import annotations

from investment_team.models import CoverageCategory, LikelyBlocker
from investment_team.strategy_lab.coverage_probe import (
    LOW_TRADE_THRESHOLD,
    merge_reports,
    should_run_probes,
)
from investment_team.strategy_lab.coverage_probe import aggregator as agg_mod

from ._coverage_probe_test_helpers import make_diag, make_report

# ─────────────────────────────────────────────────────────────────────
# should_run_probes
# ─────────────────────────────────────────────────────────────────────


def test_should_run_probes_returns_false_when_diagnostics_missing() -> None:
    assert should_run_probes(None) is False


def test_should_run_probes_returns_false_for_healthy_run() -> None:
    assert should_run_probes(make_diag(closed=10)) is False


def test_should_run_probes_triggers_on_no_orders_emitted() -> None:
    assert should_run_probes(make_diag(category="NO_ORDERS_EMITTED", closed=0)) is True


def test_should_run_probes_triggers_on_only_warmup_orders() -> None:
    assert should_run_probes(make_diag(category="ONLY_WARMUP_ORDERS", closed=0)) is True


def test_should_run_probes_triggers_on_unknown_zero_trade_path() -> None:
    assert should_run_probes(make_diag(category="UNKNOWN_ZERO_TRADE_PATH", closed=0)) is True


def test_should_run_probes_triggers_on_low_closed_trades() -> None:
    assert should_run_probes(make_diag(closed=LOW_TRADE_THRESHOLD - 1)) is True


def test_should_run_probes_does_not_trigger_for_other_lifecycle_failures() -> None:
    # ORDERS_REJECTED already has a structured envelope (#404); coverage
    # probes can't add anything, so the stage stays off.
    assert should_run_probes(make_diag(category="ORDERS_REJECTED", closed=10)) is False


# ─────────────────────────────────────────────────────────────────────
# merge_reports — category priority
# ─────────────────────────────────────────────────────────────────────


def test_merge_warmup_exceeds_history_beats_indicator_restrictive() -> None:
    merged = merge_reports(
        make_report(CoverageCategory.WARMUP_EXCEEDS_HISTORY, warmup=200, bars=120),
        make_report(CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE),
    )
    assert merged.coverage_category is CoverageCategory.WARMUP_EXCEEDS_HISTORY


def test_merge_target_symbol_missing_beats_conjunction_never_true() -> None:
    merged = merge_reports(
        make_report(CoverageCategory.TARGET_SYMBOL_MISSING),
        make_report(CoverageCategory.CONJUNCTION_NEVER_TRUE),
    )
    assert merged.coverage_category is CoverageCategory.TARGET_SYMBOL_MISSING


def test_merge_indicator_restrictive_beats_unknown_static() -> None:
    merged = merge_reports(
        make_report(CoverageCategory.UNKNOWN_LOW_COVERAGE),
        make_report(CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE),
    )
    assert merged.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_merge_both_ok_returns_ok() -> None:
    merged = merge_reports(
        make_report(CoverageCategory.COVERAGE_OK),
        make_report(CoverageCategory.COVERAGE_OK),
    )
    assert merged.coverage_category is CoverageCategory.COVERAGE_OK


# ─────────────────────────────────────────────────────────────────────
# merge_reports — numeric + structural behaviour
# ─────────────────────────────────────────────────────────────────────


def test_merge_dedups_blockers_and_preserves_order() -> None:
    static = make_report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        blockers=[
            LikelyBlocker(reason="first", evidence="static-evidence"),
            LikelyBlocker(reason="dup", evidence="x"),
        ],
    )
    indicator = make_report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        blockers=[
            LikelyBlocker(reason="dup", evidence="x"),
            LikelyBlocker(reason="second", evidence="indicator-evidence"),
        ],
    )
    merged = merge_reports(static, indicator)
    assert [b.reason for b in merged.likely_blockers] == ["first", "dup", "second"]


def test_merge_warmup_takes_max_across_reports() -> None:
    merged = merge_reports(
        make_report(CoverageCategory.UNKNOWN_LOW_COVERAGE, warmup=80),
        make_report(CoverageCategory.UNKNOWN_LOW_COVERAGE, warmup=120),
    )
    # Warmup is a per-symbol bars count; both probes use the same unit
    # so max() is safe.
    assert merged.warmup_bars_required == 120


def test_merge_bars_and_symbols_take_indicator_values() -> None:
    # bars_checked / symbols_checked are reported in different units by
    # the two probes (static = longest single-symbol history; indicator =
    # sum across symbols). merge_reports trusts the indicator probe's
    # values because they reflect what was actually examined for hit-rate
    # computation.
    static = make_report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        bars=250,  # longest single symbol
        symbols=1,
    )
    indicator = make_report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        bars=200,  # sum across symbols actually examined
        symbols=3,
    )
    merged = merge_reports(static, indicator)
    assert merged.bars_checked == 200
    assert merged.symbols_checked == 3


def test_merge_uses_exec_diag_for_entry_orders_emitted() -> None:
    merged = merge_reports(
        make_report(CoverageCategory.COVERAGE_OK),
        make_report(CoverageCategory.COVERAGE_OK),
        exec_diag=make_diag(orders_accepted=7),
    )
    assert merged.entry_orders_emitted == 7


def test_merge_is_deterministic_across_calls() -> None:
    static = make_report(
        CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE,
        blockers=[LikelyBlocker(reason="r1", evidence="e1")],
    )
    indicator = make_report(
        CoverageCategory.CONJUNCTION_NEVER_TRUE,
        blockers=[LikelyBlocker(reason="r2", evidence="e2")],
    )
    assert (
        merge_reports(static, indicator).model_dump()
        == merge_reports(static, indicator).model_dump()
    )


# ─────────────────────────────────────────────────────────────────────
# B3 — dedup respects hit_rate
# ─────────────────────────────────────────────────────────────────────


def test_dedup_keeps_blockers_with_distinct_hit_rates() -> None:
    """Two blockers with identical ``(reason, evidence)`` but distinct
    ``hit_rate`` values carry different information. The dedup key
    includes ``hit_rate`` so neither is dropped."""
    static = make_report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        blockers=[LikelyBlocker(reason="r", evidence="e", hit_rate=0.0)],
    )
    indicator = make_report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        blockers=[
            LikelyBlocker(reason="r", evidence="e", hit_rate=0.0),  # exact dup → drop
            LikelyBlocker(reason="r", evidence="e", hit_rate=0.25),  # distinct → keep
        ],
    )
    merged = merge_reports(static, indicator)
    assert [b.hit_rate for b in merged.likely_blockers if b.reason == "r"] == [0.0, 0.25]


# ─────────────────────────────────────────────────────────────────────
# Module-level exhaustiveness invariants
# ─────────────────────────────────────────────────────────────────────


def test_category_priority_covers_every_coverage_category() -> None:
    # The aggregator module enforces this at import time with an explicit
    # raise, but the explicit test pins the contract so a future enum
    # addition fails here with a clear name rather than as an opaque
    # KeyError from _CATEGORY_RANK deep inside the orchestrator.
    assert set(agg_mod._CATEGORY_PRIORITY) == set(CoverageCategory)
