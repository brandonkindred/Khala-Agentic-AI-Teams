"""Strategy Lab deterministic rule-coverage probes (#406)."""

import json
from typing import Optional

from investment_team.models import CoverageReport, RuleIndex

from .aggregator import (
    LOW_TRADE_THRESHOLD,
    merge_reports,
    run_coverage_stage,
    should_run_probes,
)
from .indicator_probe import run_indicator_probe
from .runtime_instrument import instrument_strategy_code
from .static_probe import run_static_probe

# Caps on the rendered coverage block (issue #452). Keep the JSON line
# bounded so a pathological probe output cannot blow up the refinement
# prompt or the persisted ``QualityGateResult.details``.
COVERAGE_LIKELY_BLOCKERS_CAP = 6
COVERAGE_SUBCONDITIONS_CAP = 8


def format_coverage_report(report: Optional[CoverageReport]) -> str:
    """Render a compact JSON block of the rule-coverage probe verdict for
    the refinement and zero-trade-repair prompts (issue #452, part of #406).

    Returns:
        ``""`` when ``report is None`` (successful runs and runs where
        ``should_run_probes`` short-circuited keep ``metrics.coverage_report``
        as ``None``), otherwise a single line ``"Coverage Report: {<json>}"``
        whose payload is stable-key-sorted and compact (matches the
        ``Execution Diagnostics: {...}`` line style).

    The ``likely_blockers`` and ``subconditions`` lists are head-trimmed to
    :data:`COVERAGE_LIKELY_BLOCKERS_CAP` and :data:`COVERAGE_SUBCONDITIONS_CAP`
    entries respectively. Head-trim is meaningful here because the
    aggregator emits blockers in source order — static (structural
    causes: missing symbol, warmup-exceeds-history, etc.) first, then
    indicator-probe blockers, then runtime blockers — with stable dedup
    (see ``coverage_probe.aggregator._dedup_blockers``). Earlier entries
    therefore correspond to the more fundamental failure mode.
    """
    if report is None:
        return ""

    payload = report.model_dump(mode="json", exclude_none=True)
    blockers = payload.get("likely_blockers") or []
    if len(blockers) > COVERAGE_LIKELY_BLOCKERS_CAP:
        payload["likely_blockers"] = blockers[:COVERAGE_LIKELY_BLOCKERS_CAP]
    subconditions = payload.get("subconditions") or []
    if len(subconditions) > COVERAGE_SUBCONDITIONS_CAP:
        payload["subconditions"] = subconditions[:COVERAGE_SUBCONDITIONS_CAP]

    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return f"Coverage Report: {encoded}"


__all__ = [
    "COVERAGE_LIKELY_BLOCKERS_CAP",
    "COVERAGE_SUBCONDITIONS_CAP",
    "LOW_TRADE_THRESHOLD",
    "RuleIndex",
    "format_coverage_report",
    "instrument_strategy_code",
    "merge_reports",
    "run_coverage_stage",
    "run_indicator_probe",
    "run_static_probe",
    "should_run_probes",
]
