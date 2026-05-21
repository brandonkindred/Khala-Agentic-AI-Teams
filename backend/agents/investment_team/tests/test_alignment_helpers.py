"""Coverage for the format/coerce helpers in
``strategy_lab.agents.alignment``.
"""

from __future__ import annotations

import pytest

from investment_team.models import TradeRecord
from investment_team.strategy_lab.agents.alignment import (
    _coerce_report,
    _extract_json,
    _format_trades_section,
)


def _trade(n: int = 1, outcome: str = "win") -> TradeRecord:
    return TradeRecord(
        trade_num=n,
        symbol="AAA",
        side="long",
        entry_date="2024-01-01",
        exit_date="2024-01-05",
        entry_price=100.0,
        exit_price=101.0,
        shares=1.0,
        position_value=100.0,
        gross_pnl=1.0,
        net_pnl=1.0,
        return_pct=1.0,
        hold_days=4,
        cumulative_pnl=1.0,
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# _format_trades_section
# ---------------------------------------------------------------------------


def test_format_trades_section_no_trades() -> None:
    assert _format_trades_section([]) == "No trades produced by this backtest."


def test_format_trades_section_short_lists_all_trades() -> None:
    trades = [_trade(i + 1) for i in range(5)]
    out = _format_trades_section(trades, max_sample_rows=10)
    assert "Aggregate: 5 trades" in out
    # Every trade num appears.
    for i in range(5):
        assert f"#{i + 1}" in out


def test_format_trades_section_long_uses_head_tail_split() -> None:
    trades = [_trade(i + 1) for i in range(30)]
    out = _format_trades_section(trades, max_sample_rows=8)
    # Head + tail appear, middle is elided.
    assert "#1 " in out
    assert "#30" in out
    assert "additional trades not shown" in out


# ---------------------------------------------------------------------------
# _coerce_report
# ---------------------------------------------------------------------------


def test_coerce_report_aligned_clears_proposed_code() -> None:
    report = _coerce_report(
        {
            "aligned": True,
            "rationale": "trades match the spec",
            "issues": [],
            "proposed_code": "def x(): pass",  # ignored when aligned=True
            "predicted_aligned_after_fix": True,
            "changes_made": "tightened entry",
        },
        fallback_code="def fallback(): pass",
    )
    assert report.aligned is True
    assert report.proposed_code is None
    assert report.predicted_aligned_after_fix is False
    assert report.changes_made == ""


def test_coerce_report_misaligned_with_no_proposal_sets_predicted_false() -> None:
    report = _coerce_report(
        {
            "aligned": False,
            "rationale": "trades drifted",
            "issues": [{"rule_type": "entry_rules", "description": "..."}],
            "proposed_code": None,
            "predicted_aligned_after_fix": True,
        },
        fallback_code="def x(): pass",
    )
    assert report.aligned is False
    assert report.proposed_code is None
    # Auto-cleared because there's nothing to act on.
    assert report.predicted_aligned_after_fix is False


def test_coerce_report_normalises_bad_issue_to_warning() -> None:
    """A non-dict issue is skipped; a dict with an unknown severity defaults to warning."""
    report = _coerce_report(
        {
            "aligned": False,
            "rationale": "bad",
            "issues": [
                "junk-string",  # skipped
                {"rule_type": "exit_rules", "description": "x", "severity": "wild"},
            ],
            "proposed_code": "def x(): pass",
        },
        fallback_code="def y(): pass",
    )
    assert len(report.issues) == 1
    assert report.issues[0].severity == "warning"


def test_coerce_report_passes_through_proposed_code_when_misaligned() -> None:
    report = _coerce_report(
        {
            "aligned": False,
            "rationale": "x",
            "proposed_code": "def fix(): pass",
            "predicted_aligned_after_fix": True,
            "changes_made": "y",
        },
        fallback_code="def fallback(): pass",
    )
    assert report.aligned is False
    assert report.proposed_code == "def fix(): pass"
    assert report.predicted_aligned_after_fix is True
    assert report.changes_made == "y"


def test_coerce_report_blank_proposed_code_treated_as_none() -> None:
    report = _coerce_report(
        {
            "aligned": False,
            "rationale": "x",
            "proposed_code": "   ",
        },
        fallback_code="def fallback(): pass",
    )
    assert report.proposed_code is None


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


def test_extract_json_plain_object() -> None:
    data = _extract_json('preamble {"aligned": true, "issues": []} trailing')
    assert data == {"aligned": True, "issues": []}


def test_extract_json_handles_markdown_fence() -> None:
    text = "```json\n{\"aligned\": true}\n```"
    data = _extract_json(text)
    assert data == {"aligned": True}


def test_extract_json_handles_nested_braces() -> None:
    text = '{"a": {"b": 1}}'
    data = _extract_json(text)
    assert data == {"a": {"b": 1}}


def test_extract_json_raises_when_no_object() -> None:
    with pytest.raises(ValueError):
        _extract_json("no braces here")


def test_extract_json_raises_on_malformed_json() -> None:
    with pytest.raises(ValueError):
        _extract_json('{"missing_closing_quote: 1}')
