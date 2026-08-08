"""Coverage for ``investment_team.paper_trading_agent``.

The agent's only LLM call is in ``analyze_divergence``; everything else
is deterministic. The tests:

* Stub the ``run_backtest`` helper to inject canned results / errors.
* Stub the LLM-backed divergence agent with a callable that returns
  pre-canned JSON.
* Exercise the success path, the look-ahead violation path, the
  service-level error path, the ``ValueError`` (no strategy_code)
  branch, the "zero trades" branch, the divergence-analysis fallback
  when the LLM raises, and the static ``compare_performance`` helper.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.models import (
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    PaperTradingStatus,
    PaperTradingVerdict,
    StrategySpec,
    TradeRecord,
)
from investment_team.paper_trading_agent import PaperTradingAgent, _format_trades_table
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    Predicate,
    SignalExitRule,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_strategy(*, code: str | None = "def x(): pass\n") -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-pt-1",
        authored_by="ideation",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=[
            EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=1.0))
        ],
        exit_rules=[SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=0.5))],
        strategy_code=code,
    )


def _make_backtest_record(strategy: StrategySpec, *, trade_count: int = 30) -> BacktestRecord:
    config = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-06-30",
        initial_capital=100_000.0,
    )
    result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.5,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    return BacktestRecord(
        backtest_id="bt-1",
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        config=config,
        submitted_by="test",
        submitted_at="2024-06-30T00:00:00Z",
        completed_at="2024-06-30T01:00:00Z",
        result=result,
        trades=[_trade(i + 1) for i in range(trade_count)],
        notes=[],
    )


def _trade(n: int) -> TradeRecord:
    return TradeRecord(
        trade_num=n,
        symbol="AAA",
        side="long",
        entry_date="2024-01-01",
        exit_date="2024-01-05",
        entry_price=100.0,
        exit_price=101.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=10.0,
        return_pct=1.0,
        net_pnl=10.0,
        hold_days=4,
        cumulative_pnl=float(n * 10),
        outcome="win" if n % 2 == 0 else "loss",
    )


def _bars() -> List[OHLCVBar]:
    return [
        OHLCVBar(date=f"2024-06-{i + 1:02d}", open=100, high=101, low=99, close=100, volume=1_000_000)
        for i in range(5)
    ]


class _FakeRunResult:
    """Mimic the namedtuple-like result of ``run_backtest``."""

    def __init__(
        self,
        *,
        trades: List[TradeRecord],
        result: BacktestResult | None,
        error: str | None = None,
        lookahead_violation: bool = False,
    ) -> None:
        self.trades = trades
        self.result = result
        self.service_result = _FakeServiceResult(error=error, lookahead_violation=lookahead_violation)


class _FakeServiceResult:
    def __init__(self, *, error: str | None, lookahead_violation: bool) -> None:
        self.error = error
        self.lookahead_violation = lookahead_violation


@pytest.fixture
def patched_run_backtest(monkeypatch: pytest.MonkeyPatch):
    """Patch the ``run_backtest`` import inside the agent's run_session.

    Returns a list[FakeRunResult] queue — agent calls pop the next entry.
    """
    queue: List[Any] = []

    def _fake_run_backtest(*, strategy, config, market_data):
        if not queue:
            raise AssertionError("run_backtest called more times than queued")
        next_item = queue.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item

    # The agent imports inside the method via:
    #   from .trading_service.modes.backtest import run_backtest
    import investment_team.trading_service.modes.backtest as backtest_mod

    monkeypatch.setattr(backtest_mod, "run_backtest", _fake_run_backtest)
    return queue


# ---------------------------------------------------------------------------
# _format_trades_table
# ---------------------------------------------------------------------------


def test_format_trades_table_empty() -> None:
    assert _format_trades_table([]) == "No trades."


def test_format_trades_table_populated() -> None:
    out = _format_trades_table([_trade(1), _trade(2)])
    lines = out.split("\n")
    assert lines[0].startswith("# |")
    assert "AAA" in out
    assert "1.00" in out or "+1.00%" in out


# ---------------------------------------------------------------------------
# compare_performance
# ---------------------------------------------------------------------------


def _result(*, win=60.0, ret=20.0, sharpe=1.5, dd=5.0, pf=2.0) -> BacktestResult:
    return BacktestResult(
        total_return_pct=ret,
        annualized_return_pct=ret,
        volatility_pct=10.0,
        sharpe_ratio=sharpe,
        max_drawdown_pct=dd,
        win_rate_pct=win,
        profit_factor=pf,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )


def test_compare_performance_aligned_when_metrics_match_and_sample_sufficient() -> None:
    bt = _result()
    pt = _result(win=58.0, ret=21.0, sharpe=1.55, dd=4.5, pf=2.1)
    cmp_ = PaperTradingAgent.compare_performance(pt, bt, paper_trade_count=30, backtest_trade_count=30)
    assert cmp_.overall_aligned is True
    assert cmp_.win_rate_aligned is True
    assert cmp_.return_aligned is True
    assert cmp_.sharpe_aligned is True
    assert cmp_.drawdown_aligned is True


def test_compare_performance_marks_insufficient_sample_as_unaligned() -> None:
    bt = _result()
    pt = _result()
    cmp_ = PaperTradingAgent.compare_performance(pt, bt, paper_trade_count=5, backtest_trade_count=30)
    assert cmp_.overall_aligned is False


def test_compare_performance_profit_factor_branches() -> None:
    """Both PF >= 1.0 → tolerance 0.5; otherwise tolerance 0.3."""
    bt = _result(pf=1.5)
    pt_high = _result(pf=1.9)  # diff = 0.4 ≤ 0.5
    pt_low = _result(pf=0.8)  # below 1.0 → diff = 0.7 > 0.3
    cmp_high = PaperTradingAgent.compare_performance(pt_high, bt, paper_trade_count=30)
    cmp_low = PaperTradingAgent.compare_performance(pt_low, bt, paper_trade_count=30)
    assert cmp_high.profit_factor_aligned is True
    assert cmp_low.profit_factor_aligned is False


def test_compare_performance_misaligned_when_return_diff_exceeds_threshold() -> None:
    bt = _result(ret=20.0)
    pt = _result(ret=10.0)  # 10pp delta > 2pp tolerance
    cmp_ = PaperTradingAgent.compare_performance(pt, bt, paper_trade_count=30)
    assert cmp_.return_aligned is False
    assert cmp_.overall_aligned is False


# ---------------------------------------------------------------------------
# run_session — full flows
# ---------------------------------------------------------------------------


class _StubLLM:
    """Callable that returns the queued response on each invocation."""

    def __init__(self, responses: list[str | Exception] | None = None) -> None:
        self._queue = list(responses or [])

    def __call__(self, prompt: str) -> str:  # pragma: no cover - never called when queue empty
        if not self._queue:
            raise AssertionError("LLM called more times than queued")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_run_session_zero_trades_marks_not_performant(patched_run_backtest) -> None:
    """No trades produced → NOT_PERFORMANT verdict and explanatory message."""
    patched_run_backtest.append(
        _FakeRunResult(trades=[], result=None, error=None, lookahead_violation=False)
    )

    agent = PaperTradingAgent(llm_client=_StubLLM([]))
    strategy = _make_strategy()
    backtest = _make_backtest_record(strategy, trade_count=5)
    session = agent.run_session(
        strategy=strategy,
        strategy_code=strategy.strategy_code or "",
        backtest_record=backtest,
        market_data={"AAA": _bars()},
        initial_capital=100_000.0,
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )

    assert session.status == PaperTradingStatus.COMPLETED
    assert session.verdict == PaperTradingVerdict.NOT_PERFORMANT
    assert "zero trades" in (session.divergence_analysis or "")


def test_run_session_session_id_has_sufficient_entropy(patched_run_backtest) -> None:
    """session_id must carry the full UUID hex, not a truncated 8-char prefix.

    A truncated `hex[:8]` id has only 32 bits of entropy, making collisions
    likely after ~65k sessions and risking a silent overwrite in
    `_paper_trading_sessions`. Require at least 16 hex characters (64 bits).
    """
    patched_run_backtest.append(
        _FakeRunResult(trades=[], result=None, error=None, lookahead_violation=False)
    )

    agent = PaperTradingAgent(llm_client=_StubLLM([]))
    strategy = _make_strategy()
    backtest = _make_backtest_record(strategy, trade_count=5)
    session = agent.run_session(
        strategy=strategy,
        strategy_code=strategy.strategy_code or "",
        backtest_record=backtest,
        market_data={"AAA": _bars()},
        initial_capital=100_000.0,
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )

    assert session.session_id.startswith("pt-")
    hex_suffix = session.session_id[len("pt-") :]
    assert len(hex_suffix) >= 16, (
        f"session_id hex suffix too short for adequate entropy: {session.session_id!r}"
    )


def test_run_session_ready_for_live_when_aligned(patched_run_backtest) -> None:
    """Aligned metrics + ≥30 trades → READY_FOR_LIVE."""
    pt_result = _result()
    pt_trades = [_trade(i + 1) for i in range(30)]
    patched_run_backtest.append(_FakeRunResult(trades=pt_trades, result=pt_result))

    agent = PaperTradingAgent(llm_client=_StubLLM([]))
    strategy = _make_strategy()
    backtest = _make_backtest_record(strategy, trade_count=30)

    session = agent.run_session(
        strategy=strategy,
        strategy_code=strategy.strategy_code or "",
        backtest_record=backtest,
        market_data={"AAA": _bars()},
    )

    assert session.status == PaperTradingStatus.COMPLETED
    assert session.verdict == PaperTradingVerdict.READY_FOR_LIVE
    assert session.comparison is not None
    assert session.comparison.overall_aligned is True


def test_run_session_calls_llm_when_not_aligned(patched_run_backtest) -> None:
    """Misaligned metrics → divergence analysis is triggered via the LLM."""
    pt_result = _result(ret=2.0)  # large delta → not aligned
    pt_trades = [_trade(i + 1) for i in range(30)]
    patched_run_backtest.append(_FakeRunResult(trades=pt_trades, result=pt_result))

    llm = _StubLLM(['{"analysis": "low return", "strategy_weaknesses": [], "improvement_suggestions": []}'])
    agent = PaperTradingAgent(llm_client=llm)
    strategy = _make_strategy()
    backtest = _make_backtest_record(strategy, trade_count=30)

    session = agent.run_session(
        strategy=strategy,
        strategy_code=strategy.strategy_code or "",
        backtest_record=backtest,
        market_data={"AAA": _bars()},
    )

    assert session.verdict == PaperTradingVerdict.NOT_PERFORMANT
    assert session.divergence_analysis == "low return"


def test_run_session_llm_failure_falls_back_to_summary(patched_run_backtest) -> None:
    """LLM exception → caught and replaced with a templated summary."""
    pt_result = _result(ret=2.0)
    pt_trades = [_trade(i + 1) for i in range(30)]
    patched_run_backtest.append(_FakeRunResult(trades=pt_trades, result=pt_result))

    llm = _StubLLM([RuntimeError("LLM 500")])
    agent = PaperTradingAgent(llm_client=llm)
    strategy = _make_strategy()
    backtest = _make_backtest_record(strategy, trade_count=30)

    session = agent.run_session(
        strategy=strategy,
        strategy_code=strategy.strategy_code or "",
        backtest_record=backtest,
        market_data={"AAA": _bars()},
    )

    assert session.verdict == PaperTradingVerdict.NOT_PERFORMANT
    assert "Paper trading did not align" in (session.divergence_analysis or "")
    assert "Automated analysis unavailable" in (session.divergence_analysis or "")


def test_run_session_marks_failed_on_lookahead_violation(patched_run_backtest) -> None:
    patched_run_backtest.append(
        _FakeRunResult(
            trades=[],
            result=None,
            error="bar.next_close access",
            lookahead_violation=True,
        )
    )
    agent = PaperTradingAgent(llm_client=_StubLLM([]))
    strategy = _make_strategy()
    backtest = _make_backtest_record(strategy)
    session = agent.run_session(
        strategy=strategy,
        strategy_code=strategy.strategy_code or "",
        backtest_record=backtest,
        market_data={"AAA": _bars()},
    )
    assert session.status == PaperTradingStatus.FAILED
    assert "future data" in (session.divergence_analysis or "")


def test_run_session_marks_failed_on_service_error(patched_run_backtest) -> None:
    partial_trades = [_trade(1)]
    patched_run_backtest.append(
        _FakeRunResult(
            trades=partial_trades,
            result=None,
            error="exploded at bar 10",
            lookahead_violation=False,
        )
    )
    agent = PaperTradingAgent(llm_client=_StubLLM([]))
    strategy = _make_strategy()
    backtest = _make_backtest_record(strategy)
    session = agent.run_session(
        strategy=strategy,
        strategy_code=strategy.strategy_code or "",
        backtest_record=backtest,
        market_data={"AAA": _bars()},
    )
    assert session.status == PaperTradingStatus.FAILED
    assert "execution failed" in (session.divergence_analysis or "")
    # Partial trades preserved for diagnosis.
    assert session.trades == partial_trades


def test_run_session_marks_failed_on_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ValueError`` from ``run_backtest`` (e.g. missing strategy_code) → FAILED."""

    def _raises_value_error(**kwargs):
        raise ValueError("strategy_code missing")

    import investment_team.trading_service.modes.backtest as backtest_mod

    monkeypatch.setattr(backtest_mod, "run_backtest", _raises_value_error)

    agent = PaperTradingAgent(llm_client=_StubLLM([]))
    strategy = _make_strategy(code=None)
    backtest = _make_backtest_record(strategy)
    session = agent.run_session(
        strategy=strategy,
        strategy_code="some-code",  # different from strategy.strategy_code (None)
        backtest_record=backtest,
        market_data={"AAA": _bars()},
    )
    assert session.status == PaperTradingStatus.FAILED
    assert "could not start" in (session.divergence_analysis or "")


def test_run_session_resolves_default_fees_from_asset_class(patched_run_backtest) -> None:
    """``transaction_cost_bps=None`` / ``slippage_bps=None`` → asset-class defaults."""
    pt_trades = [_trade(i + 1) for i in range(30)]
    patched_run_backtest.append(_FakeRunResult(trades=pt_trades, result=_result()))

    agent = PaperTradingAgent(llm_client=_StubLLM([]))
    strategy = _make_strategy()
    backtest = _make_backtest_record(strategy, trade_count=30)
    session = agent.run_session(
        strategy=strategy,
        strategy_code=strategy.strategy_code or "",
        backtest_record=backtest,
        market_data={"AAA": _bars()},
        transaction_cost_bps=None,
        slippage_bps=None,
    )
    # If we got here without exception, the fee defaults were resolved.
    assert session.status == PaperTradingStatus.COMPLETED


def test_run_session_with_empty_market_data_dict(patched_run_backtest) -> None:
    """Empty market_data → no all_dates → data_start/end remain empty strings."""
    pt_trades = [_trade(i + 1) for i in range(30)]
    patched_run_backtest.append(_FakeRunResult(trades=pt_trades, result=_result()))

    agent = PaperTradingAgent(llm_client=_StubLLM([]))
    strategy = _make_strategy()
    backtest = _make_backtest_record(strategy, trade_count=30)
    session = agent.run_session(
        strategy=strategy,
        strategy_code=strategy.strategy_code or "",
        backtest_record=backtest,
        market_data={},
    )
    assert session.data_period_start == ""
    assert session.data_period_end == ""
