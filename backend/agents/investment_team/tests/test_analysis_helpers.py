"""Coverage for the disclaimer/fallback helpers in
``strategy_lab.agents.analysis``.

The orchestrator-driven ``AnalysisAgent.run`` exercises the LLM round-trip
elsewhere; this file targets the pure helpers that determine how
misalignment is surfaced.
"""

from __future__ import annotations

from typing import List

import pytest

from investment_team.models import BacktestResult, StrategySpec
from investment_team.strategy_lab.agents._parse_helpers import extract_json_object as _extract_json
from investment_team.strategy_lab.agents.alignment import (
    AlignmentIssue,
    TradeAlignmentReport,
)
from investment_team.strategy_lab.agents.analysis import (
    _MISALIGNED_DISCLAIMER,
    _ensure_misalignment_disclaimer,
    _fallback_narrative,
    _format_alignment_status_section,
    _format_simulated_trades_summary,
    format_misalignment_prefix,
)


def _result(*, ret: float = 12.0) -> BacktestResult:
    return BacktestResult(
        total_return_pct=ret,
        annualized_return_pct=ret,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=55.0,
        profit_factor=1.5,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="sig",
        timeframe="1d",
    )


def _aligned_report() -> TradeAlignmentReport:
    return TradeAlignmentReport(aligned=True, rationale="ok", issues=[])


def _misaligned_report(*, issues: List[AlignmentIssue] | None = None) -> TradeAlignmentReport:
    if issues is None:
        issues = [
            AlignmentIssue(
                rule_type="entry_rules",
                severity="critical",
                description="entries fired without the SMA crossover",
                affected_trades=[1, 2],
            )
        ]
    return TradeAlignmentReport(
        aligned=False,
        rationale="drifted",
        issues=issues,
        proposed_code="def fix(): pass",
        predicted_aligned_after_fix=True,
        changes_made="tightened",
    )


# ---------------------------------------------------------------------------
# _format_simulated_trades_summary
# ---------------------------------------------------------------------------


def test_format_simulated_trades_summary_no_trades() -> None:
    assert _format_simulated_trades_summary([]) == "No simulated trades in ledger."


def _trade(
    *,
    n: int,
    ret_pct: float,
    pnl: float,
    cum: float,
    hold: int = 5,
    sym: str = "AAA",
    outcome: str = "win",
) -> "object":
    """Build a small TradeRecord for summary tests."""
    from investment_team.models import TradeRecord

    return TradeRecord(
        trade_num=n,
        entry_date=f"2024-01-{n:02d}",
        exit_date=f"2024-01-{n + hold:02d}",
        symbol=sym,
        side="long",
        entry_price=100.0,
        exit_price=100.0 + ret_pct,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=pnl,
        net_pnl=pnl,
        return_pct=ret_pct,
        hold_days=hold,
        outcome=outcome,
        cumulative_pnl=cum,
    )


def test_format_simulated_trades_summary_small_ledger_lists_all_trades() -> None:
    """Pre: ledger smaller than ``max_sample_rows`` (default 14).
    Post: every trade appears in the sample; aggregate header lists
    total / wins / losses and best/worst trades.
    """
    trades = [
        _trade(n=1, ret_pct=3.0, pnl=30.0, cum=30.0, outcome="win"),
        _trade(n=2, ret_pct=-1.5, pnl=-15.0, cum=15.0, outcome="loss"),
        _trade(n=3, ret_pct=5.0, pnl=50.0, cum=65.0, sym="MSFT"),
    ]
    out = _format_simulated_trades_summary(trades)
    assert "Aggregate: 3 simulated trades" in out
    assert "2 wins / 1 losses" in out
    # Best trade is #3, worst is #2.
    assert "best 5.00% (trade #3 MSFT)" in out
    assert "worst -1.50% (trade #2 AAA)" in out
    # All trade numbers appear in the sample.
    for n in (1, 2, 3):
        assert f"#{n} " in out
    # No truncation suffix.
    assert "additional trades not shown" not in out


def test_format_simulated_trades_summary_large_ledger_truncates_with_head_tail() -> None:
    """Pre: ledger larger than ``max_sample_rows``; default is 14, so 20
    trades takes the head/tail path.
    Post: head=7, tail=7 (14 sample rows); 6 hidden; ellipsis line appears
    once with the correct count.
    """
    trades = [_trade(n=i + 1, ret_pct=float(i), pnl=float(i), cum=float(i)) for i in range(20)]
    out = _format_simulated_trades_summary(trades)
    # 7 head + 7 tail = 14 sample rows; 6 hidden
    assert "(6 additional trades not shown)" in out
    # Head includes #1..#7 and tail includes #14..#20 (numbers are 1-based).
    for n in list(range(1, 8)) + list(range(14, 21)):
        assert f"#{n} " in out
    # Middle ones (e.g. #10) are NOT in the sample.
    assert "#10 " not in out


def test_format_simulated_trades_summary_custom_max_sample_rows() -> None:
    """Pre: ``max_sample_rows=4`` and 6 trades.
    Post: head=2, tail=2; ellipsis reports the 2 hidden middles.
    """
    trades = [_trade(n=i + 1, ret_pct=float(i), pnl=float(i), cum=float(i)) for i in range(6)]
    out = _format_simulated_trades_summary(trades, max_sample_rows=4)
    assert "(2 additional trades not shown)" in out
    for n in (1, 2, 5, 6):
        assert f"#{n} " in out
    # Middles 3, 4 NOT in the sample.
    assert "#3 " not in out
    assert "#4 " not in out


def test_format_simulated_trades_summary_records_final_cumulative_pnl() -> None:
    """Pre: ledger of two trades ending at cum=42.5.
    Post: the ending cumulative P&L appears in the aggregate section.
    """
    trades = [
        _trade(n=1, ret_pct=2.0, pnl=20.0, cum=20.0),
        _trade(n=2, ret_pct=2.25, pnl=22.5, cum=42.5),
    ]
    out = _format_simulated_trades_summary(trades)
    assert "ending cumulative P&L = 42.50" in out


# ---------------------------------------------------------------------------
# _format_alignment_status_section
# ---------------------------------------------------------------------------


def test_format_alignment_status_section_none_returns_empty() -> None:
    assert _format_alignment_status_section(None) == ""


def test_format_alignment_status_section_aligned_returns_clean_marker() -> None:
    out = _format_alignment_status_section(_aligned_report())
    assert "alignment audit clean" in out


def test_format_alignment_status_section_misaligned_renders_issues() -> None:
    out = _format_alignment_status_section(_misaligned_report())
    assert "TRADES DID NOT IMPLEMENT THE SPEC" in out
    assert _MISALIGNED_DISCLAIMER in out
    assert "entries fired without the SMA crossover" in out
    assert "Audit rationale" in out


def test_format_alignment_status_section_misaligned_without_issues_shows_marker() -> None:
    report = TradeAlignmentReport(
        aligned=False,
        rationale="",
        issues=[],
    )
    out = _format_alignment_status_section(report)
    assert "audit returned aligned=False with no enumerated issues" in out


# ---------------------------------------------------------------------------
# format_misalignment_prefix
# ---------------------------------------------------------------------------


def test_format_misalignment_prefix_returns_empty_when_aligned() -> None:
    assert format_misalignment_prefix(None) == ""
    assert format_misalignment_prefix(_aligned_report()) == ""


def test_format_misalignment_prefix_includes_disclaimer_and_issues() -> None:
    out = format_misalignment_prefix(_misaligned_report())
    assert _MISALIGNED_DISCLAIMER in out
    assert "entries fired" in out


# ---------------------------------------------------------------------------
# _ensure_misalignment_disclaimer
# ---------------------------------------------------------------------------


def test_ensure_misalignment_disclaimer_aligned_no_change() -> None:
    out = _ensure_misalignment_disclaimer("the narrative", _aligned_report())
    assert out == "the narrative"


def test_ensure_misalignment_disclaimer_none_no_change() -> None:
    out = _ensure_misalignment_disclaimer("the narrative", None)
    assert out == "the narrative"


def test_ensure_misalignment_disclaimer_prepends_when_missing() -> None:
    """Narrative missing the disclaimer → full prefix is prepended."""
    out = _ensure_misalignment_disclaimer("Strategy ran fine.", _misaligned_report())
    assert out.startswith(_MISALIGNED_DISCLAIMER)
    # Original narrative is preserved.
    assert "Strategy ran fine." in out


def test_ensure_misalignment_disclaimer_appends_missing_issues() -> None:
    """When the disclaimer opens the narrative but issues are dropped, append them."""
    narrative_with_disclaimer = f"{_MISALIGNED_DISCLAIMER} Body text without issue."
    out = _ensure_misalignment_disclaimer(narrative_with_disclaimer, _misaligned_report())
    # The narrative starts with the disclaimer, so prefix is NOT prepended.
    assert out.startswith(_MISALIGNED_DISCLAIMER)
    assert "Alignment issues (deterministically appended)" in out
    assert "entries fired" in out


# ---------------------------------------------------------------------------
# _fallback_narrative
# ---------------------------------------------------------------------------


def test_fallback_narrative_includes_metric_summary() -> None:
    out = _fallback_narrative(_spec(), _result(ret=20.0), is_winning=True)
    assert "winning" in out
    assert "annualized return 20.0%" in out


def test_fallback_narrative_misaligned_prepends_prefix() -> None:
    out = _fallback_narrative(
        _spec(), _result(ret=-2.0), is_winning=False, alignment_report=_misaligned_report()
    )
    assert _MISALIGNED_DISCLAIMER in out
    assert "losing" in out


def test_fallback_narrative_aligned_does_not_prepend() -> None:
    out = _fallback_narrative(
        _spec(), _result(), is_winning=True, alignment_report=_aligned_report()
    )
    assert not out.startswith(_MISALIGNED_DISCLAIMER)


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


def test_extract_json_handles_fence_and_braces() -> None:
    text = '```json\n{"k": 1}\n```'
    assert _extract_json(text) == {"k": 1}


def test_extract_json_no_object_raises() -> None:
    with pytest.raises(ValueError):
        _extract_json("plain text")


def test_extract_json_brace_inside_string_value() -> None:
    text = '{"draft_narrative": "returns beat the {benchmark} index", "confidence": 0.8}'
    assert _extract_json(text) == {
        "draft_narrative": "returns beat the {benchmark} index",
        "confidence": 0.8,
    }
