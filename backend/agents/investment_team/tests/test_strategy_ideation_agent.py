"""Coverage for ``investment_team.strategy_ideation_agent``.

Targets the trade-summary formatter, ideation, and the two-pass
analyze_result (draft → self-review). The single dependency on the LLM
is mocked via a callable that consumes a queue of canned responses.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from investment_team.models import (
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    StrategyLabRecord,
    StrategySpec,
    TradeRecord,
)
from investment_team.signal_intelligence_models import SignalIntelligenceBriefV1
from investment_team.strategy_ideation_agent import (
    StrategyIdeationAgent,
    _format_simulated_trades_summary,
)

# ---------------------------------------------------------------------------
# Trade summary formatter
# ---------------------------------------------------------------------------


def _trade(n: int, *, return_pct: float = 1.0, outcome: str = "win") -> TradeRecord:
    return TradeRecord(
        trade_num=n,
        symbol=f"SYM{n}",
        side="long",
        entry_date="2024-01-01",
        exit_date="2024-01-05",
        entry_price=100.0,
        exit_price=101.0 if return_pct >= 0 else 99.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=return_pct * 10,
        return_pct=return_pct,
        net_pnl=return_pct * 10,
        hold_days=4,
        cumulative_pnl=float(n * 10),
        outcome=outcome,
    )


def test_format_simulated_trades_summary_no_trades() -> None:
    assert _format_simulated_trades_summary([]) == "No simulated trades in ledger."


def test_format_simulated_trades_summary_small_sample_prints_all() -> None:
    trades = [_trade(i + 1) for i in range(5)]
    out = _format_simulated_trades_summary(trades)
    assert "5 simulated trades" in out
    for i in range(5):
        assert f"#{i + 1}" in out


def test_format_simulated_trades_summary_large_set_uses_head_tail_split() -> None:
    """When n > max_sample_rows the helper prints head + tail with a tail marker."""
    trades = [_trade(i + 1) for i in range(30)]
    out = _format_simulated_trades_summary(trades, max_sample_rows=6)
    # Head and tail should both appear; the middle should be elided.
    assert "#1 " in out
    assert "#30" in out
    # Some middle row must be missing.
    assert "#15" not in out
    assert "additional trades not shown" in out


def test_format_simulated_trades_summary_marks_best_and_worst() -> None:
    """Best and worst trades surface their trade_num + symbol in the header lines."""
    trades = [
        _trade(1, return_pct=-2.0, outcome="loss"),
        _trade(2, return_pct=10.0, outcome="win"),
        _trade(3, return_pct=1.0, outcome="win"),
    ]
    out = _format_simulated_trades_summary(trades)
    assert "best 10.00%" in out
    assert "worst -2.00%" in out


# ---------------------------------------------------------------------------
# ideate_strategy
# ---------------------------------------------------------------------------


class _StubLLM:
    """Callable LLM stand-in returning queued canned responses."""

    def __init__(self, responses: List[Any]) -> None:
        self._queue = list(responses)

    def __call__(self, prompt: str) -> str:
        if not self._queue:
            raise AssertionError("LLM called more times than queued")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return str(item)


def test_ideate_strategy_returns_dict_and_strips_rationale_and_sources() -> None:
    response = (
        '{"asset_class": "crypto", "hypothesis": "h", "signal_definition": "s", '
        '"entry_rules": ["e1"], "exit_rules": ["x1"], "sizing_rules": ["fixed"], '
        '"risk_limits": {"max_position_pct": 5}, "speculative": true, '
        '"rationale": "because", "signal_sources": ["price_action"]}'
    )
    agent = StrategyIdeationAgent(llm_client=_StubLLM([response]))
    data, rationale = agent.ideate_strategy()
    assert rationale == "because"
    # rationale + signal_sources stripped from returned dict.
    assert "rationale" not in data
    assert "signal_sources" not in data
    assert data["asset_class"] == "crypto"


def test_ideate_strategy_uses_excluded_asset_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    """``exclude_asset_classes`` must be appended to the ideation prompt mix hint."""
    captured: dict[str, str] = {}

    class _Recorder:
        def __call__(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return (
                '{"asset_class": "forex", "hypothesis": "h", "signal_definition": "s", '
                '"entry_rules": [], "exit_rules": [], "rationale": "r"}'
            )

    agent = StrategyIdeationAgent(llm_client=_Recorder())
    agent.ideate_strategy(exclude_asset_classes=["crypto", "options"])
    assert "HARD CONSTRAINT" in captured["prompt"]
    assert "crypto" in captured["prompt"]
    assert "options" in captured["prompt"]


def test_ideate_strategy_injects_precomputed_signal_brief() -> None:
    """A non-None precomputed_signal_brief surfaces inside guarded delimiters."""
    captured: dict[str, str] = {}

    class _Recorder:
        def __call__(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return (
                '{"asset_class": "stocks", "hypothesis": "h", "signal_definition": "s", '
                '"entry_rules": [], "exit_rules": [], "rationale": "r"}'
            )

    brief = SignalIntelligenceBriefV1(
        brief_version=1,
        macro_themes=["rates", "fx"],
        micro_themes=["sector-rotation"],
        high_value_signal_hypotheses=["momentum after Fed pivot"],
        trade_structures_benefiting=["long-equity short-bonds"],
        pairing_guidance="combine rate signals with equity vol",
        evidence_from_priors="none / first run",
        evidence_from_market_data="SPY, TLT",
        confidence="medium",
    )
    agent = StrategyIdeationAgent(llm_client=_Recorder())
    agent.ideate_strategy(precomputed_signal_brief=brief)
    assert "<signal_intelligence_brief>" in captured["prompt"]
    assert "momentum after Fed pivot" in captured["prompt"]


def test_ideate_strategy_empty_rationale_default() -> None:
    """Missing rationale in LLM JSON defaults to the canned fallback message."""
    response = (
        '{"asset_class": "stocks", "hypothesis": "h", "signal_definition": "s", '
        '"entry_rules": [], "exit_rules": []}'
    )
    agent = StrategyIdeationAgent(llm_client=_StubLLM([response]))
    _data, rationale = agent.ideate_strategy()
    assert rationale == "No rationale provided."


# ---------------------------------------------------------------------------
# analyze_result
# ---------------------------------------------------------------------------


def _build_record(*, winning: bool, with_trades: bool = True) -> StrategyLabRecord:
    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="sig",
        timeframe="1d",
    )
    config = BacktestConfig(start_date="2020-01-01", end_date="2024-12-31", initial_capital=100_000.0)
    result = BacktestResult(
        total_return_pct=50.0 if winning else -5.0,
        annualized_return_pct=20.0 if winning else -2.0,
        volatility_pct=10.0,
        sharpe_ratio=1.5 if winning else -0.5,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0 if winning else 40.0,
        profit_factor=2.0 if winning else 0.5,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    trades = [_trade(i + 1) for i in range(3)] if with_trades else []
    bt = BacktestRecord(
        backtest_id="bt-1",
        strategy_id="s",
        strategy=strategy,
        config=config,
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=result,
        trades=trades,
    )
    return StrategyLabRecord(
        lab_record_id="lab-1",
        strategy=strategy,
        backtest=bt,
        is_winning=winning,
        strategy_rationale="rat",
        analysis_narrative="prev",
        created_at="2024-01-01T01:00:00Z",
    )


def test_analyze_result_winning_two_pass_with_self_review() -> None:
    """Winning record → draft prompt then self-review prompt; output includes review note."""
    draft = '{"draft_narrative": "winning draft narrative"}'
    review = (
        '{"revised_narrative": "polished narrative", '
        '"verification_notes": "double-checked numbers"}'
    )
    agent = StrategyIdeationAgent(llm_client=_StubLLM([draft, review]))
    record = _build_record(winning=True)
    out = agent.analyze_result(record, "rationale")
    assert "polished narrative" in out
    assert "double-checked numbers" in out


def test_analyze_result_losing_uses_loss_template() -> None:
    """Losing record → loss template draft path; result still has narrative."""
    draft = '{"draft_narrative": "loss draft"}'
    review = '{"revised_narrative": "loss polished", "verification_notes": ""}'
    agent = StrategyIdeationAgent(llm_client=_StubLLM([draft, review]))
    record = _build_record(winning=False)
    out = agent.analyze_result(record, "rationale")
    # Empty verification → no [Self-review] suffix.
    assert out == "loss polished"


def test_analyze_result_draft_failure_falls_back_to_summary_string() -> None:
    """Draft LLM exception → falls back to a templated summary line."""
    review = '{"revised_narrative": "review polished", "verification_notes": ""}'
    agent = StrategyIdeationAgent(llm_client=_StubLLM([RuntimeError("draft 500"), review]))
    record = _build_record(winning=True)
    out = agent.analyze_result(record, "rationale")
    assert out == "review polished"


def test_analyze_result_self_review_failure_falls_back_to_draft() -> None:
    """Self-review LLM exception → fall back to the draft narrative."""
    draft = '{"draft_narrative": "draft only"}'
    agent = StrategyIdeationAgent(llm_client=_StubLLM([draft, RuntimeError("review 500")]))
    record = _build_record(winning=True)
    out = agent.analyze_result(record, "rationale")
    assert out == "draft only"


def test_analyze_result_both_failures_uses_templated_summary() -> None:
    agent = StrategyIdeationAgent(
        llm_client=_StubLLM([RuntimeError("draft"), RuntimeError("review")])
    )
    record = _build_record(winning=False)
    out = agent.analyze_result(record, "rationale")
    # The templated fallback includes the outcome label tokens.
    assert "annualized" in out


def test_analyze_result_empty_revised_falls_back_to_draft() -> None:
    """If the self-review returns an empty narrative, use the draft instead."""
    draft = '{"draft_narrative": "fallback draft"}'
    review = '{"revised_narrative": "   ", "verification_notes": ""}'
    agent = StrategyIdeationAgent(llm_client=_StubLLM([draft, review]))
    record = _build_record(winning=True)
    out = agent.analyze_result(record, "rationale")
    assert out == "fallback draft"
