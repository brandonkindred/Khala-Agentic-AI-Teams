"""Orchestrator-level regression guard for issue #532 (Codex review on PR #584).

Two failure modes are pinned here:

1. ``_resolve_alignment_report_for_analysis`` — when the LLM alignment audit
   returns ``aligned=True`` but the deterministic ``ExitRuleConformanceGate``
   then vetoes publication, a synthetic misaligned report must be substituted
   so the analysis prompts can't narrate "audit clean" over the veto.

2. ``StrategyLabOrchestrator._run_analysis_phase`` — when
   ``analysis_agent.run`` itself raises (before its in-agent
   ``_fallback_narrative`` can run, e.g. prompt-file IO or model factory
   failure), the orchestrator-level auto-summary must still prepend the
   disclaimer + alignment issues on misaligned runs.
"""

from __future__ import annotations

from typing import Any

from investment_team.models import BacktestResult, StrategySpec, TradeRecord
from investment_team.strategy_lab.agents.alignment import (
    AlignmentIssue,
    TradeAlignmentReport,
)
from investment_team.strategy_lab.orchestrator import (
    StrategyLabOrchestrator,
    _resolve_alignment_report_for_analysis,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    Predicate,
    StopLossRule,
)

# ---------------------------------------------------------------------------
# _resolve_alignment_report_for_analysis
# ---------------------------------------------------------------------------


def _clean_report() -> TradeAlignmentReport:
    return TradeAlignmentReport(aligned=True, rationale="audit clean")


def _misaligned_report() -> TradeAlignmentReport:
    return TradeAlignmentReport(
        aligned=False,
        rationale="audit caught a stop-loss skip",
        issues=[
            AlignmentIssue(
                rule_type="exit_rules",
                severity="critical",
                description="stop-loss did not fire on trade #1",
                affected_trades=[1],
            )
        ],
    )


def test_resolve_returns_none_when_no_reports() -> None:
    """Strategies whose alignment loop never ran (e.g. execution failed
    before alignment) get a ``None`` report — the analysis prompt then
    renders an empty Alignment status section, byte-identical to legacy
    behaviour."""
    assert _resolve_alignment_report_for_analysis([], exit_rule_conformance_passed=True) is None
    assert _resolve_alignment_report_for_analysis([], exit_rule_conformance_passed=False) is None


def test_resolve_passes_through_clean_report_when_conformance_passes() -> None:
    """The common happy path: clean LLM audit + clean conformance check
    means the analysis prompts see ``aligned=True``."""
    clean = _clean_report()
    out = _resolve_alignment_report_for_analysis([clean], exit_rule_conformance_passed=True)
    assert out is clean


def test_resolve_passes_through_misaligned_report_regardless_of_conformance() -> None:
    """A misaligned LLM audit must reach the analysis prompts unchanged
    — the conformance flag is only consulted to *escalate* a clean audit
    that the gate then rejected, never to soften a misaligned one."""
    misaligned = _misaligned_report()
    out_pass = _resolve_alignment_report_for_analysis(
        [misaligned], exit_rule_conformance_passed=True
    )
    out_fail = _resolve_alignment_report_for_analysis(
        [misaligned], exit_rule_conformance_passed=False
    )
    assert out_pass is misaligned
    assert out_fail is misaligned


def test_resolve_overrides_clean_audit_when_conformance_vetoes() -> None:
    """The bug Codex flagged (PR #584): clean LLM audit + failing
    ExitRuleConformanceGate must NOT narrate audit-clean. A synthetic
    misaligned report carrying the conformance veto as a critical
    exit_rules issue is substituted instead."""
    out = _resolve_alignment_report_for_analysis(
        [_clean_report()], exit_rule_conformance_passed=False
    )
    assert out is not None
    assert out.aligned is False
    assert "ExitRuleConformanceGate" in out.rationale
    assert len(out.issues) == 1
    issue = out.issues[0]
    assert issue.rule_type == "exit_rules"
    assert issue.severity == "critical"
    assert "ExitRuleConformanceGate failed" in issue.description


def test_resolve_picks_latest_when_multiple_reports() -> None:
    """The alignment loop appends a report per round; only the latest is
    consulted (matches the verification phase's ``alignment_reports[-1]``
    convention)."""
    first = _misaligned_report()
    second = _clean_report()
    out = _resolve_alignment_report_for_analysis([first, second], exit_rule_conformance_passed=True)
    assert out is second


# ---------------------------------------------------------------------------
# StrategyLabOrchestrator._run_analysis_phase exception handler
# ---------------------------------------------------------------------------


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-test",
        authored_by="test-suite",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs="bar.close", op=">", rhs=0),
            )
        ],
        exit_rules=[StopLossRule(pct=0.03)],
        risk_limits={},
        speculative=False,
    )


def _metrics() -> BacktestResult:
    return BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=15.0,
        volatility_pct=8.0,
        sharpe_ratio=1.4,
        max_drawdown_pct=4.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )


def _stub_trade() -> TradeRecord:
    return TradeRecord(
        trade_num=1,
        entry_date="2024-01-01",
        exit_date="2024-01-05",
        symbol="AAA",
        side="long",
        entry_price=100.0,
        exit_price=102.0,
        shares=100.0,
        position_value=10000.0,
        gross_pnl=200.0,
        net_pnl=200.0,
        return_pct=2.0,
        hold_days=4,
        outcome="win",
        cumulative_pnl=200.0,
    )


class _RaisingAnalysisAgent:
    """Stand-in for ``AnalysisAgent`` that always raises during ``run`` so
    the orchestrator-level ``except Exception`` arm is exercised."""

    def run(self, *_: Any, **__: Any) -> str:
        raise RuntimeError("simulated catastrophic agent failure")


def _make_orchestrator() -> StrategyLabOrchestrator:
    """Build an orchestrator with all collaborators stubbed out. The
    analysis-phase fallback exercised here only reads ``self.analysis_agent``
    — every other collaborator is irrelevant, so MagicMock keeps the wiring
    cheap."""
    orch = StrategyLabOrchestrator.__new__(StrategyLabOrchestrator)
    orch.analysis_agent = _RaisingAnalysisAgent()
    return orch


def _emit(*_args: Any, **_kwargs: Any) -> None:
    pass


def test_analysis_phase_fallback_injects_disclaimer_on_misaligned_run() -> None:
    """Codex follow-up (PR #584): when ``analysis_agent.run`` raises
    before its internal ``_fallback_narrative`` runs (e.g. prompt-file
    IO or model factory failure), the orchestrator-level fallback must
    still surface the disclaimer + audit issues on ``aligned=False`` runs.
    Otherwise a transient outage publishes a confident auto-summary on a
    misaligned run."""
    orch = _make_orchestrator()
    misaligned = _misaligned_report()

    narrative = orch._run_analysis_phase(
        spec=_spec(),
        metrics=_metrics(),
        trades=[_stub_trade()],
        rationale="rationale",
        is_winning=False,
        execution_succeeded=True,
        refinement_attempts=[],
        all_gate_results=[],
        alignment_report=misaligned,
        emit=_emit,
    )

    assert (
        "The executed trades did not faithfully implement the specification; "
        "interpretation is preliminary." in narrative
    )
    for issue in misaligned.issues:
        assert issue.description in narrative
    assert "Detailed narrative generation failed" in narrative


def test_analysis_phase_fallback_unchanged_on_aligned_run() -> None:
    """Aligned runs (or runs with no alignment report) must keep the
    legacy auto-summary text — no disclaimer is injected to keep clean
    runs byte-identical to pre-#532 behaviour."""
    orch = _make_orchestrator()

    narrative = orch._run_analysis_phase(
        spec=_spec(),
        metrics=_metrics(),
        trades=[_stub_trade()],
        rationale="rationale",
        is_winning=True,
        execution_succeeded=True,
        refinement_attempts=[],
        all_gate_results=[],
        alignment_report=TradeAlignmentReport(aligned=True),
        emit=_emit,
    )

    assert "did not faithfully implement the specification" not in narrative
    assert "Alignment issues:" not in narrative
    assert "Detailed narrative generation failed" in narrative


def test_analysis_phase_fallback_unchanged_with_no_alignment_report() -> None:
    """Legacy callers that pass ``alignment_report=None`` (or pipeline
    paths where the alignment loop never ran) keep the original
    auto-summary text — back-compat guard."""
    orch = _make_orchestrator()

    narrative = orch._run_analysis_phase(
        spec=_spec(),
        metrics=_metrics(),
        trades=[_stub_trade()],
        rationale="rationale",
        is_winning=True,
        execution_succeeded=True,
        refinement_attempts=[],
        all_gate_results=[],
        alignment_report=None,
        emit=_emit,
    )

    assert "did not faithfully implement the specification" not in narrative
    assert "Detailed narrative generation failed" in narrative
