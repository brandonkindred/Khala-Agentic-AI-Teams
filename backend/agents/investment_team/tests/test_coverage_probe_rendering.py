"""Unit tests for ``coverage_probe.format_coverage_report`` (#452).

The shared helper renders a compact JSON block of a
:class:`CoverageReport` for the refinement-prompt ``failure_details`` and
the zero-trade repair agent prompt. Same caps and same format string for
both consumers — tests live here so the contract is asserted in one
place.

Orchestrator-wiring tests (i.e. that each consumer actually *calls* the
helper or forwards the report) live in
``test_coverage_probe_orchestrator_stage.py``.
"""

from __future__ import annotations

import json

from investment_team.models import (
    CoverageCategory,
    CoverageReport,
    LikelyBlocker,
    SubconditionCoverage,
)
from investment_team.strategy_lab.coverage_probe import (
    COVERAGE_LIKELY_BLOCKERS_CAP,
    COVERAGE_SUBCONDITIONS_CAP,
    format_coverage_report,
)


def test_format_coverage_report_returns_empty_when_no_report() -> None:
    """No probe attached → no prompt bloat. Mirrors the
    ``_format_execution_diagnostics`` empty-on-None contract so callers can
    treat the line as additive.
    """
    assert format_coverage_report(None) == ""


def test_format_coverage_report_emits_single_compact_json_line() -> None:
    """Rendered block is exactly one line, ``Coverage Report: {<json>}``,
    with stable-sorted keys and compact separators — matches the existing
    ``Execution Diagnostics:`` line shape so the refinement prompt's
    parser-friendliness is unchanged.
    """
    report = CoverageReport(
        coverage_category=CoverageCategory.ENTRY_CONDITION_NEVER_TRUE,
        summary="RSI<25 never satisfied",
        bars_checked=250,
        symbols_checked=1,
        warmup_bars_required=14,
        entry_orders_emitted=0,
    )
    line = format_coverage_report(report)
    assert "\n" not in line
    assert line.startswith("Coverage Report: {")
    payload = json.loads(line[len("Coverage Report: ") :])
    assert payload["coverage_category"] == "ENTRY_CONDITION_NEVER_TRUE"
    assert payload["bars_checked"] == 250
    assert payload["warmup_bars_required"] == 14
    # Stable key ordering for diff-friendliness — sort_keys=True must hold.
    keys = list(payload.keys())
    assert keys == sorted(keys)
    # Compact separators (no whitespace after `:` or `,`).
    assert ", " not in line
    assert ": " not in line[len("Coverage Report: ") :]


def test_format_coverage_report_caps_likely_blockers_and_subconditions() -> None:
    """A pathological probe with many blockers/subconditions must not blow
    up ``failure_details``. The rendered JSON truncates to the module-level
    caps so the LLM context stays bounded.
    """
    blockers = [
        LikelyBlocker(reason=f"blocker_{i}", evidence=f"evidence {i}")
        for i in range(COVERAGE_LIKELY_BLOCKERS_CAP + 4)
    ]
    subconditions = [
        SubconditionCoverage(label=f"sub_{i}", hit_count=0, hit_rate=0.0)
        for i in range(COVERAGE_SUBCONDITIONS_CAP + 5)
    ]
    report = CoverageReport(
        coverage_category=CoverageCategory.CONJUNCTION_NEVER_TRUE,
        summary="too many conjuncts",
        likely_blockers=blockers,
        subconditions=subconditions,
    )
    line = format_coverage_report(report)
    payload = json.loads(line[len("Coverage Report: ") :])
    assert len(payload["likely_blockers"]) == COVERAGE_LIKELY_BLOCKERS_CAP
    assert len(payload["subconditions"]) == COVERAGE_SUBCONDITIONS_CAP
    # Head-trim keeps the earliest-emitted blockers, which the aggregator
    # produces in source order: static (structural causes) → indicator →
    # runtime. See ``coverage_probe.aggregator._dedup_blockers`` for the
    # stable-dedup contract that anchors this ordering.
    assert payload["likely_blockers"][0]["reason"] == "blocker_0"
    assert payload["subconditions"][0]["label"] == "sub_0"
