"""Unit tests for the attempt-scoped :class:`BacktestCache`.

The cache memoizes ``run_strategy_code`` on ``(code, market_data, config)``
so identical re-executions (alignment re-checks, determinism re-checks,
audit re-backtests) short-circuit. These tests pin the key sensitivity
(code / market-data / config), the hit/miss accounting, and the runner
override that keeps test monkeypatches of ``orchestrator.run_strategy_code``
in effect.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.models import BacktestConfig
from investment_team.strategy_lab.backtest_cache import BacktestCache
from investment_team.tests.test_alignment_checks import _spec
from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult

_CODE = "class S:\n    pass\n"


def _config(**overrides: Any) -> BacktestConfig:
    base = dict(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )
    base.update(overrides)
    return BacktestConfig(**base)


def _market_data(close: float = 100.0) -> Dict[str, List[OHLCVBar]]:
    return {
        "AAPL": [
            OHLCVBar(
                date="2023-01-02",
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=1_000_000,
            )
        ]
    }


def _counting_runner():
    """A ``run_strategy_code``-shaped stub that records each call."""
    calls: List[Dict[str, Any]] = []

    def _runner(code, market_data, config, *, strategy=None) -> StrategyRunResult:
        calls.append({"code": code, "strategy": strategy})
        return StrategyRunResult(success=True, trades=[], stdout=f"run#{len(calls)}")

    return calls, _runner


def test_identical_inputs_hit_after_first_run() -> None:
    cache = BacktestCache()
    calls, runner = _counting_runner()
    md = _market_data()
    cfg = _config()

    first, hit1 = cache.get_or_run(_CODE, md, cfg, strategy=None, runner=runner)
    second, hit2 = cache.get_or_run(_CODE, md, cfg, strategy=None, runner=runner)

    assert hit1 is False and hit2 is True
    assert len(calls) == 1  # runner invoked exactly once
    assert second is first  # the stored object is returned verbatim
    assert cache.hits == 1 and cache.misses == 1


def test_different_code_is_a_miss() -> None:
    cache = BacktestCache()
    calls, runner = _counting_runner()
    md, cfg = _market_data(), _config()

    cache.get_or_run(_CODE, md, cfg, strategy=None, runner=runner)
    _, hit = cache.get_or_run(_CODE + "# changed\n", md, cfg, strategy=None, runner=runner)

    assert hit is False
    assert len(calls) == 2
    assert cache.misses == 2 and cache.hits == 0


def test_different_market_data_content_is_a_miss() -> None:
    cache = BacktestCache()
    calls, runner = _counting_runner()
    cfg = _config()

    cache.get_or_run(_CODE, _market_data(close=100.0), cfg, strategy=None, runner=runner)
    _, hit = cache.get_or_run(_CODE, _market_data(close=200.0), cfg, strategy=None, runner=runner)

    assert hit is False
    assert len(calls) == 2


def test_same_content_different_object_still_hits() -> None:
    """Equal-by-value market data fingerprints identically even as a fresh
    dict object, so the second call hits without re-running."""
    cache = BacktestCache()
    calls, runner = _counting_runner()
    cfg = _config()

    cache.get_or_run(_CODE, _market_data(close=123.0), cfg, strategy=None, runner=runner)
    _, hit = cache.get_or_run(_CODE, _market_data(close=123.0), cfg, strategy=None, runner=runner)

    assert hit is True
    assert len(calls) == 1


def test_different_cost_assumptions_is_a_miss() -> None:
    cache = BacktestCache()
    calls, runner = _counting_runner()
    md = _market_data()

    cache.get_or_run(_CODE, md, _config(slippage_bps=2.0), strategy=None, runner=runner)
    _, hit = cache.get_or_run(_CODE, md, _config(slippage_bps=9.0), strategy=None, runner=runner)

    assert hit is False
    assert len(calls) == 2


def test_runner_receives_strategy_argument() -> None:
    cache = BacktestCache()
    calls, runner = _counting_runner()
    sentinel = object()

    cache.get_or_run(_CODE, _market_data(), _config(), strategy=sentinel, runner=runner)

    assert calls[0]["strategy"] is sentinel


def test_different_strategy_spec_is_a_miss() -> None:
    """Same code + market data + config but a different spec must NOT alias:
    ``run_strategy_code(..., strategy=spec)`` feeds risk_limits / rules /
    sizing / target_symbols into the engine, so a spec change (e.g. a
    risk-only refinement that leaves the source unchanged) has to re-execute.
    """
    cache = BacktestCache()
    calls, runner = _counting_runner()
    md, cfg = _market_data(), _config()
    spec_a = _spec(target_symbols=["AAPL"])
    spec_b = _spec(target_symbols=["MSFT"])

    _, hit_a = cache.get_or_run(_CODE, md, cfg, strategy=spec_a, runner=runner)
    _, hit_a_again = cache.get_or_run(_CODE, md, cfg, strategy=spec_a, runner=runner)
    _, hit_b = cache.get_or_run(_CODE, md, cfg, strategy=spec_b, runner=runner)

    assert hit_a is False  # first run for spec_a
    assert hit_a_again is True  # identical spec -> hit
    assert hit_b is False  # different spec -> miss, re-executed
    assert len(calls) == 2


def test_wall_clock_dependent_code_is_never_cached() -> None:
    """Code that branches on wall-clock state (datetime.now/today, time.*) is
    nondeterministic across runs, so it must re-execute every time rather than
    freezing the first ledger."""
    cache = BacktestCache()
    calls, runner = _counting_runner()
    md, cfg = _market_data(), _config()
    code = (
        "import datetime\n"
        "class S:\n"
        "    def on_bar(self, ctx, bar):\n"
        "        if datetime.datetime.now().hour > 12:\n"
        "            pass\n"
    )

    _, hit1 = cache.get_or_run(code, md, cfg, strategy=None, runner=runner)
    _, hit2 = cache.get_or_run(code, md, cfg, strategy=None, runner=runner)

    assert hit1 is False and hit2 is False  # never served from cache
    assert len(calls) == 2  # re-executed each time
    assert cache.misses == 2 and cache.hits == 0


def test_default_runner_is_module_run_strategy_code(monkeypatch) -> None:
    """When ``runner`` is omitted, the cache falls back to the module-level
    ``run_strategy_code`` import."""
    import investment_team.strategy_lab.backtest_cache as bc_module

    calls: List[str] = []

    def _fake(code, market_data, config, *, strategy=None) -> StrategyRunResult:
        calls.append(code)
        return StrategyRunResult(success=True, trades=[])

    monkeypatch.setattr(bc_module, "run_strategy_code", _fake)
    cache = BacktestCache()
    _, hit = cache.get_or_run(_CODE, _market_data(), _config(), strategy=None)
    assert hit is False
    assert calls == [_CODE]


def test_empty_code_violates_precondition() -> None:
    cache = BacktestCache()
    _, runner = _counting_runner()
    with pytest.raises(AssertionError):
        cache.get_or_run("", _market_data(), _config(), strategy=None, runner=runner)
