"""Regression tests for ``_run_real_data_backtest``.

The Investment Strategy Lab regressed in the past by evaluating backtests
with one LLM call per bar via ``BacktestingAgent`` — even for strategies
that already had a Strategy-Lab-generated Python script attached as
``strategy.strategy_code``.  These tests lock in the correct behaviour:

* When ``strategy_code`` is present, the sandbox path
  (``SandboxRunner.run`` + ``build_trade_records`` + ``compute_metrics``)
  is taken and the LLM-per-bar agent is never instantiated.
* When ``strategy_code`` is absent, we fall back to the legacy
  ``BacktestingAgent``.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.models import (
    BacktestConfig,
    BacktestResult,
    StrategySpec,
    TradeRecord,
)


def _sample_strategy(*, with_code: bool) -> StrategySpec:
    code = "def run_strategy(data, config):\n    return []\n"
    return StrategySpec(
        strategy_id="strat-test-1",
        authored_by="ideation",
        asset_class="equity",
        hypothesis="h",
        signal_definition="s",
        entry_rules=["a > b"],
        exit_rules=["b > a"],
        strategy_code=code if with_code else None,
    )


def _sample_config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-02-01",
        initial_capital=100_000.0,
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


def _sample_bars() -> List[OHLCVBar]:
    return [
        OHLCVBar(
            date=f"2024-01-{i + 1:02d}",
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=1_000_000,
        )
        for i in range(5)
    ]


class _FakeMarketDataService:
    """Stand-in for ``MarketDataService`` that returns canned market data."""

    def __init__(self, market_data: Dict[str, List[OHLCVBar]]) -> None:
        self._market_data = market_data

    def get_symbols_for_strategy(self, strategy: StrategySpec) -> List[str]:
        return list(self._market_data.keys())

    def fetch_multi_symbol_range(
        self, symbols: List[str], asset_class: str, start: str, end: str
    ) -> Dict[str, List[OHLCVBar]]:
        return {s: self._market_data[s] for s in symbols if s in self._market_data}


def _install_fake_market_service(monkeypatch, market_data: Dict[str, List[OHLCVBar]]) -> None:
    # ``_run_real_data_backtest`` does a lazy ``from investment_team.market_data_service
    # import MarketDataService``.  ``pythonpath = agents`` in pytest.ini means the
    # ``investment_team`` and ``agents.investment_team`` module paths resolve to
    # *different* entries in ``sys.modules``; we patch the path the function
    # actually imports from.
    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketDataService(market_data))


def test_run_real_data_backtest_uses_sandbox_when_strategy_code_present(monkeypatch) -> None:
    """Strategies with generated code should execute via SandboxRunner, NOT per-bar LLM."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab.executor import sandbox_runner as sr_mod

    market_data = {"AAA": _sample_bars(), "BBB": _sample_bars()}
    _install_fake_market_service(monkeypatch, market_data)

    sandbox_calls: List[Dict[str, Any]] = []
    stub_raw_trade = {
        "symbol": "AAA",
        "side": "long",
        "entry_date": "2024-01-02",
        "entry_price": 101.5,
        "exit_date": "2024-01-04",
        "exit_price": 103.5,
        "shares": 10,
    }

    class _StubSandbox:
        def run(self, code: str, md: Dict[str, List[OHLCVBar]], cfg: BacktestConfig):
            sandbox_calls.append({"code": code, "symbols": list(md.keys()), "cfg": cfg})
            return sr_mod.CodeExecutionResult(
                success=True,
                raw_trades=[stub_raw_trade],
                stdout="",
                stderr="",
                execution_time_seconds=0.01,
            )

    # Patch SandboxRunner at its source module AND on the executor package
    # (the function under test uses ``from investment_team.strategy_lab.executor
    # import SandboxRunner``, which resolves against the package namespace).
    monkeypatch.setattr(sr_mod, "SandboxRunner", _StubSandbox)
    from investment_team.strategy_lab import executor as executor_pkg

    monkeypatch.setattr(executor_pkg, "SandboxRunner", _StubSandbox)

    # Guard: if the sandbox path is taken, BacktestingAgent must NEVER be constructed.
    import investment_team.backtesting_agent as bt_mod

    def _must_not_instantiate(*args, **kwargs):
        raise AssertionError(
            "BacktestingAgent was instantiated even though strategy_code is present — "
            "regression: sandbox execution was bypassed."
        )

    monkeypatch.setattr(bt_mod, "BacktestingAgent", _must_not_instantiate)

    strategy = _sample_strategy(with_code=True)
    config = _sample_config()

    result, trades = api_main._run_real_data_backtest(strategy, config)

    # Sandbox was called exactly once with the generated code and our canned data
    assert len(sandbox_calls) == 1
    assert sandbox_calls[0]["code"] == strategy.strategy_code
    assert set(sandbox_calls[0]["symbols"]) == {"AAA", "BBB"}

    # Result & trades come from the stubbed raw_trade
    assert isinstance(result, BacktestResult)
    assert len(trades) == 1
    assert isinstance(trades[0], TradeRecord)
    assert trades[0].symbol == "AAA"
    assert trades[0].side == "long"


def test_run_real_data_backtest_falls_back_to_llm_when_no_strategy_code(monkeypatch) -> None:
    """Legacy strategies without ``strategy_code`` should still go through BacktestingAgent."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab.executor import sandbox_runner as sr_mod

    market_data = {"AAA": _sample_bars()}
    _install_fake_market_service(monkeypatch, market_data)

    # Guard: the sandbox must NOT be invoked when there's no generated code.
    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("SandboxRunner was called for a strategy with no strategy_code")

    class _ForbiddenSandbox:
        def run(self, *a, **kw):
            _must_not_be_called()

    monkeypatch.setattr(sr_mod, "SandboxRunner", _ForbiddenSandbox)
    from investment_team.strategy_lab import executor as executor_pkg

    monkeypatch.setattr(executor_pkg, "SandboxRunner", _ForbiddenSandbox)

    # Stub BacktestingAgent to return a fixed (result, trades) pair and record the call.
    import investment_team.backtesting_agent as bt_mod

    fake_result = BacktestResult(
        total_return_pct=0.0,
        annualized_return_pct=0.0,
        volatility_pct=0.0,
        sharpe_ratio=0.0,
        max_drawdown_pct=0.0,
        win_rate_pct=0.0,
        profit_factor=0.0,
    )
    agent_calls: List[Dict[str, Any]] = []

    class _StubAgent:
        def run_backtest(self, strategy, config, md):
            agent_calls.append({"strategy_id": strategy.strategy_id})
            return fake_result, []

    monkeypatch.setattr(bt_mod, "BacktestingAgent", lambda: _StubAgent())

    strategy = _sample_strategy(with_code=False)
    config = _sample_config()

    result, trades = api_main._run_real_data_backtest(strategy, config)

    assert len(agent_calls) == 1
    assert agent_calls[0]["strategy_id"] == strategy.strategy_id
    assert result is fake_result
    assert trades == []


def test_run_real_data_backtest_raises_when_sandbox_execution_fails(monkeypatch) -> None:
    """A failed sandbox run should surface as HTTP 422, not a silent LLM fallback."""
    from fastapi import HTTPException

    from investment_team.api import main as api_main
    from investment_team.strategy_lab.executor import sandbox_runner as sr_mod

    market_data = {"AAA": _sample_bars()}
    _install_fake_market_service(monkeypatch, market_data)

    class _FailingSandbox:
        def run(self, code, md, cfg):
            return sr_mod.CodeExecutionResult(
                success=False,
                raw_trades=[],
                stdout="",
                stderr="ValueError: bad indicator",
                execution_time_seconds=0.0,
                error_type="runtime_error",
            )

    monkeypatch.setattr(sr_mod, "SandboxRunner", _FailingSandbox)
    from investment_team.strategy_lab import executor as executor_pkg

    monkeypatch.setattr(executor_pkg, "SandboxRunner", _FailingSandbox)

    import investment_team.backtesting_agent as bt_mod

    def _must_not_instantiate(*args, **kwargs):
        raise AssertionError("Fell back to BacktestingAgent after sandbox failure")

    monkeypatch.setattr(bt_mod, "BacktestingAgent", _must_not_instantiate)

    strategy = _sample_strategy(with_code=True)
    config = _sample_config()

    with pytest.raises(HTTPException) as excinfo:
        api_main._run_real_data_backtest(strategy, config)

    assert excinfo.value.status_code == 422
    assert "runtime_error" in excinfo.value.detail


@pytest.mark.parametrize(
    ("bad_trade", "expected_err"),
    [
        # None for a numeric field → TypeError inside float(None)
        (
            {
                "symbol": "AAA",
                "side": "long",
                "entry_date": "2024-01-02",
                "entry_price": None,
                "exit_date": "2024-01-04",
                "exit_price": 103.5,
                "shares": 10,
            },
            TypeError,
        ),
        # Invalid side → ValueError raised explicitly by build_trade_records
        (
            {
                "symbol": "AAA",
                "side": "sideways",
                "entry_date": "2024-01-02",
                "entry_price": 101.5,
                "exit_date": "2024-01-04",
                "exit_price": 103.5,
                "shares": 10,
            },
            ValueError,
        ),
    ],
    ids=["typeerror_null_price", "valueerror_bad_side"],
)
def test_run_real_data_backtest_returns_422_for_invalid_trade_output(
    monkeypatch, bad_trade, expected_err
) -> None:
    """Malformed sandbox trade output should surface as HTTP 422, not 500.

    ``build_trade_records`` can raise either ``ValueError`` (explicit, for
    bad ``side``) or ``TypeError`` (from ``float(None)`` during numeric
    coercion); both are user-facing output-shape errors and must be
    handled uniformly.
    """
    from fastapi import HTTPException

    from investment_team.api import main as api_main
    from investment_team.strategy_lab.executor import sandbox_runner as sr_mod

    # Sanity check: build_trade_records really does raise the expected
    # error on this input, so the test stays meaningful if the builder
    # evolves.
    from investment_team.strategy_lab.executor.trade_builder import build_trade_records

    with pytest.raises(expected_err):
        build_trade_records([bad_trade], _sample_config())

    market_data = {"AAA": _sample_bars()}
    _install_fake_market_service(monkeypatch, market_data)

    class _StubSandbox:
        def run(self, code, md, cfg):
            return sr_mod.CodeExecutionResult(
                success=True,
                raw_trades=[bad_trade],
                stdout="",
                stderr="",
                execution_time_seconds=0.0,
            )

    monkeypatch.setattr(sr_mod, "SandboxRunner", _StubSandbox)
    from investment_team.strategy_lab import executor as executor_pkg

    monkeypatch.setattr(executor_pkg, "SandboxRunner", _StubSandbox)

    strategy = _sample_strategy(with_code=True)
    config = _sample_config()

    with pytest.raises(HTTPException) as excinfo:
        api_main._run_real_data_backtest(strategy, config)

    assert excinfo.value.status_code == 422
    assert "Invalid trade output" in excinfo.value.detail
