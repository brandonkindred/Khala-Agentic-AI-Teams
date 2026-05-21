"""Coverage for paper-trading and backtest workers in ``api.main``.

Targets:

* ``POST /backtests`` route (creates a background job).
* ``_run_paper_trading_background`` happy + market-data-unavailable +
  crash branches.
* ``_run_live_paper_trading_background`` happy + crash branches +
  terminal-reason → FAILED mapping (when INVESTMENT_LIVE_PAPER_ENABLED=true).
* ``stop_live_paper_trading`` 404 + happy paths.
* ``run_paper_trading`` live-mode concurrency guard (409).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest


class _InMemoryDict:
    def __init__(self) -> None:
        self._d: Dict[str, Any] = {}

    def __setitem__(self, k, v):
        self._d[k] = v

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)

    def __contains__(self, k):
        return k in self._d

    def __delitem__(self, k):
        self._d.pop(k, None)

    def pop(self, k, *args):
        if args:
            return self._d.pop(k, args[0])
        return self._d.pop(k)

    def values(self):
        return list(self._d.values())


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from investment_team.api import main as api_main

    for attr in (
        "_profiles", "_proposals", "_strategies", "_validations",
        "_backtests", "_strategy_lab_records", "_paper_trading_sessions",
        "_advisor_sessions",
    ):
        monkeypatch.setattr(api_main, attr, _InMemoryDict())
    monkeypatch.setattr(api_main, "_active_runs", {})

    return TestClient(api_main.app)


def _winning_record():
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    strat = StrategySpec(
        strategy_id="strat-w", authored_by="x", asset_class="equities",
        hypothesis="h", signal_definition="s", timeframe="1d",
        strategy_code="def x(): pass",
    )
    cfg = BacktestConfig(start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0)
    result = BacktestResult(
        total_return_pct=10.0, annualized_return_pct=20.0, volatility_pct=10.0,
        sharpe_ratio=1.0, max_drawdown_pct=5.0, win_rate_pct=60.0, profit_factor=2.0,
        calmar_ratio=0.0, deflated_sharpe=0.0, sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt-w", strategy_id="strat-w", strategy=strat, config=cfg,
        submitted_by="x", submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z", result=result, trades=[],
    )
    return StrategyLabRecord(
        lab_record_id="lab-w", strategy=strat, backtest=bt, is_winning=True,
        strategy_rationale="r", analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z", strategy_code="def x(): pass",
    )


# ---------------------------------------------------------------------------
# POST /backtests route
# ---------------------------------------------------------------------------


def test_run_backtest_route_returns_pending_job(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import StrategySpec

    strategy = StrategySpec(
        strategy_id="s-rb", authored_by="x", asset_class="equities",
        hypothesis="h", signal_definition="s", timeframe="1d",
    )
    api_main._strategies["s-rb"] = strategy

    monkeypatch.setattr(api_main, "_bt_create_job", lambda jid, **k: None)
    # Stub the worker so the daemon thread doesn't run real work.
    monkeypatch.setattr(api_main, "_run_backtest_background", lambda *a, **k: None)

    resp = api_client.post(
        "/backtests",
        json={
            "strategy_id": "s-rb",
            "submitted_by": "tester",
            "start_date": "2024-01-01",
            "end_date": "2024-02-01",
            "initial_capital": 100_000.0,
            "notes": [],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["job_id"]


def test_run_backtest_route_404_when_strategy_missing(api_client) -> None:
    resp = api_client.post(
        "/backtests",
        json={
            "strategy_id": "nope",
            "submitted_by": "tester",
            "start_date": "2024-01-01",
            "end_date": "2024-02-01",
            "initial_capital": 100_000.0,
            "notes": [],
        },
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# _run_paper_trading_background
# ---------------------------------------------------------------------------


class _FakeMarketService:
    """Stand-in for ``MarketDataService`` (resolve_strategy_symbols + fetch_multi_symbol)."""

    def __init__(self, market_data: Optional[Dict[str, list]] = None) -> None:
        self._market_data = market_data or {}

    def resolve_strategy_symbols(self, strategy):
        return list(self._market_data.keys()) or ["AAA"]

    def fetch_multi_symbol(self, symbols, asset_class, lookback_days):
        return dict(self._market_data)


def test_run_paper_trading_background_marks_failed_on_empty_market_data(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Empty market_data → session ends in FAILED with a 'Failed to fetch' message."""
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        PaperTradingSession,
        PaperTradingStatus,
        StrategySpec,
    )

    strategy = StrategySpec(
        strategy_id="s", authored_by="x", asset_class="equities",
        hypothesis="h", signal_definition="s", timeframe="1d",
        strategy_code="def x(): pass",
    )
    bt = BacktestRecord(
        backtest_id="bt-p", strategy_id="s", strategy=strategy,
        config=BacktestConfig(start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0),
        submitted_by="x", submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=BacktestResult(
            total_return_pct=10.0, annualized_return_pct=20.0, volatility_pct=10.0,
            sharpe_ratio=1.0, max_drawdown_pct=5.0, win_rate_pct=60.0, profit_factor=2.0,
            calmar_ratio=0.0, deflated_sharpe=0.0, sortino_ratio=0.0,
        ),
        trades=[],
    )

    # Pre-create a "running" session in the store.
    running = PaperTradingSession(
        session_id="pt-empty", lab_record_id="lab-w", strategy=strategy,
        status=PaperTradingStatus.RUNNING, initial_capital=100_000.0,
        current_capital=100_000.0, symbols_traded=[],
        data_source="yahoo_finance",
        data_period_start="", data_period_end="",
        started_at="2024-01-01T00:00:00Z",
    )
    api_main._paper_trading_sessions["pt-empty"] = running

    # Patch market service constructor so the worker uses our fake.
    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService({}))

    api_main._run_paper_trading_background(
        "pt-empty", "lab-w", strategy, "def x(): pass", bt,
        lookback_days=30, initial_capital=100_000.0,
        transaction_cost_bps=5.0, slippage_bps=2.0,
    )

    # Worker updated the session to FAILED.
    updated = api_main._paper_trading_sessions.get("pt-empty")
    assert updated.status == PaperTradingStatus.FAILED
    assert "Failed to fetch market data" in (updated.divergence_analysis or "")


def test_run_paper_trading_background_crashes_into_failed(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """An exception inside the worker is caught and persisted as FAILED."""
    from investment_team.api import main as api_main
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        PaperTradingSession,
        PaperTradingStatus,
        StrategySpec,
    )

    strategy = StrategySpec(
        strategy_id="s", authored_by="x", asset_class="equities",
        hypothesis="h", signal_definition="s", timeframe="1d",
        strategy_code="def x(): pass",
    )
    bt = BacktestRecord(
        backtest_id="bt-p", strategy_id="s", strategy=strategy,
        config=BacktestConfig(start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0),
        submitted_by="x", submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=BacktestResult(
            total_return_pct=10.0, annualized_return_pct=20.0, volatility_pct=10.0,
            sharpe_ratio=1.0, max_drawdown_pct=5.0, win_rate_pct=60.0, profit_factor=2.0,
            calmar_ratio=0.0, deflated_sharpe=0.0, sortino_ratio=0.0,
        ),
        trades=[],
    )
    running = PaperTradingSession(
        session_id="pt-crash", lab_record_id="lab-w", strategy=strategy,
        status=PaperTradingStatus.RUNNING, initial_capital=100_000.0,
        current_capital=100_000.0, symbols_traded=[],
        data_source="yahoo_finance", data_period_start="", data_period_end="",
        started_at="2024-01-01T00:00:00Z",
    )
    api_main._paper_trading_sessions["pt-crash"] = running

    import investment_team.market_data_service as mds

    class _Broken:
        def resolve_strategy_symbols(self, s):
            raise RuntimeError("boom in resolve")

    monkeypatch.setattr(mds, "MarketDataService", lambda: _Broken())

    api_main._run_paper_trading_background(
        "pt-crash", "lab-w", strategy, "def x(): pass", bt,
        lookback_days=30, initial_capital=100_000.0,
        transaction_cost_bps=5.0, slippage_bps=2.0,
    )
    updated = api_main._paper_trading_sessions.get("pt-crash")
    assert updated.status == PaperTradingStatus.FAILED
    assert "Paper trading crashed" in (updated.divergence_analysis or "")


# ---------------------------------------------------------------------------
# stop_live_paper_trading
# ---------------------------------------------------------------------------


def test_stop_live_paper_trading_404_when_session_missing(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    resp = api_client.post("/strategy-lab/paper-trade/no-session/stop")
    assert resp.status_code == 404


def test_stop_live_paper_trading_happy_path(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Stop endpoint must invoke the StopController and stamp the session."""
    from investment_team.api import main as api_main
    from investment_team.models import (
        PaperTradingSession,
        PaperTradingStatus,
        StrategySpec,
    )

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")

    strategy = StrategySpec(
        strategy_id="s", authored_by="x", asset_class="equities",
        hypothesis="h", signal_definition="s", timeframe="1d",
        strategy_code="def x(): pass",
    )
    session = PaperTradingSession(
        session_id="pt-live", lab_record_id="lab-w", strategy=strategy,
        status=PaperTradingStatus.LIVE, initial_capital=100_000.0,
        current_capital=100_000.0, symbols_traded=["AAA"],
        data_source="live:binance", data_period_start="2024-01-01",
        data_period_end="2024-06-01", started_at="2024-06-01T00:00:00Z",
    )
    api_main._paper_trading_sessions["pt-live"] = session

    class _Controller:
        def __init__(self):
            self.stopped = False

        def request_stop(self):
            self.stopped = True

    ctrl = _Controller()
    api_main._live_paper_stop_controllers["pt-live"] = ctrl

    resp = api_client.post("/strategy-lab/paper-trade/pt-live/stop")
    assert resp.status_code == 200
    assert ctrl.stopped is True
    # Session was updated with a user_stop_requested_at timestamp.
    updated = api_main._paper_trading_sessions.get("pt-live")
    assert updated.user_stop_requested_at is not None


# ---------------------------------------------------------------------------
# run_paper_trading concurrency guard (live mode)
# ---------------------------------------------------------------------------


def test_run_paper_trading_409_when_live_session_already_active(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import (
        PaperTradingSession,
        PaperTradingStatus,
    )

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    record = _winning_record()
    api_main._strategy_lab_records["lab-w"] = record

    # Stub the live-mode worker so the thread doesn't run.
    monkeypatch.setattr(api_main, "_run_live_paper_trading_background", lambda *a, **k: None)

    # Pre-seed an active session for the same strategy_id.
    active = PaperTradingSession(
        session_id="pt-existing", lab_record_id="lab-w", strategy=record.strategy,
        status=PaperTradingStatus.LIVE, initial_capital=100_000.0,
        current_capital=100_000.0, symbols_traded=["AAA"],
        data_source="live:binance", data_period_start="2024-01-01",
        data_period_end="2024-06-01", started_at="2024-06-01T00:00:00Z",
    )
    api_main._paper_trading_sessions["pt-existing"] = active

    resp = api_client.post(
        "/strategy-lab/paper-trade",
        json={"lab_record_id": "lab-w"},
    )
    assert resp.status_code == 409
    assert "already has an" in resp.json()["detail"]


def test_run_paper_trading_live_mode_kicks_off_thread(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """When live mode is on and no conflict, the route should return ``opening`` status."""
    from investment_team.api import main as api_main

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    record = _winning_record()
    api_main._strategy_lab_records["lab-w"] = record

    started: List[bool] = []
    monkeypatch.setattr(
        api_main, "_run_live_paper_trading_background", lambda *a, **k: started.append(True)
    )

    resp = api_client.post(
        "/strategy-lab/paper-trade",
        json={"lab_record_id": "lab-w"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["status"] == "opening"
    # The background thread should run (poll briefly).
    import time

    for _ in range(20):
        if started:
            break
        time.sleep(0.05)
    assert started == [True]


# ---------------------------------------------------------------------------
# _run_paper_trading_step (pure helper — strategy lab cycle path)
# ---------------------------------------------------------------------------


def _step_strategy_and_record():
    """Return a (StrategySpec, BacktestRecord) pair suitable for paper-trading-step tests."""
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategySpec,
    )

    strategy = StrategySpec(
        strategy_id="s-step", authored_by="x", asset_class="equities",
        hypothesis="h", signal_definition="s", timeframe="1d",
        strategy_code="def x(): pass",
    )
    bt = BacktestRecord(
        backtest_id="bt-step", strategy_id="s-step", strategy=strategy,
        config=BacktestConfig(
            start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
        ),
        submitted_by="x", submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=BacktestResult(
            total_return_pct=10.0, annualized_return_pct=20.0, volatility_pct=10.0,
            sharpe_ratio=1.0, max_drawdown_pct=5.0, win_rate_pct=60.0,
            profit_factor=2.0, calmar_ratio=0.0, deflated_sharpe=0.0,
            sortino_ratio=0.0,
        ),
        trades=[],
    )
    return strategy, bt


def test_run_paper_trading_step_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: market_service returns data → agent.run_session() result is forwarded verbatim."""
    from investment_team.api import main as api_main
    from investment_team.models import (
        PaperTradingSession,
        PaperTradingStatus,
        PaperTradingVerdict,
    )

    strategy, bt = _step_strategy_and_record()
    market_data_payload = {"AAA": [{"close": 1.0}], "BBB": [{"close": 2.0}]}

    fake_market = _FakeMarketService(market_data_payload)
    captured_fetch: Dict[str, Any] = {}

    def _fetch(symbols, asset_class, lookback_days):
        captured_fetch["symbols"] = list(symbols)
        captured_fetch["asset_class"] = asset_class
        captured_fetch["lookback_days"] = lookback_days
        return dict(market_data_payload)

    fake_market.fetch_multi_symbol = _fetch  # type: ignore[assignment]

    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: fake_market)

    expected_session = PaperTradingSession(
        session_id="pt-step-1", lab_record_id="", strategy=strategy,
        status=PaperTradingStatus.COMPLETED, initial_capital=100_000.0,
        current_capital=110_000.0, symbols_traded=["AAA", "BBB"],
        data_source="fake", data_period_start="2024-01-01",
        data_period_end="2024-06-01", started_at="2024-06-01T00:00:00Z",
        verdict=PaperTradingVerdict.READY_FOR_LIVE,
    )

    captured_run: Dict[str, Any] = {}

    class _FakeAgent:
        def run_session(self, **kwargs):
            captured_run.update(kwargs)
            return expected_session

    import investment_team.paper_trading_agent as pta

    monkeypatch.setattr(pta, "PaperTradingAgent", lambda: _FakeAgent())

    session = api_main._run_paper_trading_step(
        strategy=strategy,
        strategy_code="def x(): pass",
        backtest_record=bt,
        initial_capital=100_000.0,
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
        lookback_days=180,
    )

    assert session is expected_session
    # Helper forwarded the lookback + symbols through to fetch_multi_symbol.
    assert captured_fetch["lookback_days"] == 180
    assert captured_fetch["asset_class"] == strategy.asset_class
    assert set(captured_fetch["symbols"]) == {"AAA", "BBB"}
    # Helper forwarded execution assumptions and market_data through to the agent.
    assert captured_run["initial_capital"] == 100_000.0
    assert captured_run["transaction_cost_bps"] == 5.0
    assert captured_run["slippage_bps"] == 2.0
    assert captured_run["strategy_code"] == "def x(): pass"
    assert captured_run["market_data"] == market_data_payload


def test_run_paper_trading_step_raises_on_empty_market_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty market_data → _PaperTradingDataUnavailable, agent never constructed."""
    from investment_team.api import main as api_main

    strategy, bt = _step_strategy_and_record()
    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService({}))

    # Tripwire: if the helper ever reaches PaperTradingAgent on an empty-data
    # input, the test fails loudly instead of silently passing.
    import investment_team.paper_trading_agent as pta

    def _should_not_construct():
        raise AssertionError("PaperTradingAgent must not be constructed when market_data is empty")

    monkeypatch.setattr(pta, "PaperTradingAgent", _should_not_construct)

    with pytest.raises(api_main._PaperTradingDataUnavailable) as exc_info:
        api_main._run_paper_trading_step(
            strategy=strategy,
            strategy_code="def x(): pass",
            backtest_record=bt,
            initial_capital=100_000.0,
            transaction_cost_bps=5.0,
            slippage_bps=2.0,
            lookback_days=30,
        )
    assert "Failed to fetch market data" in str(exc_info.value)


def test_run_paper_trading_step_propagates_agent_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-data errors (e.g. sandbox crash) propagate out so the cycle records them as failed."""
    from investment_team.api import main as api_main

    strategy, bt = _step_strategy_and_record()
    import investment_team.market_data_service as mds

    monkeypatch.setattr(
        mds, "MarketDataService", lambda: _FakeMarketService({"AAA": [{"close": 1.0}]})
    )

    class _BoomAgent:
        def run_session(self, **kwargs):
            raise RuntimeError("sandbox exploded")

    import investment_team.paper_trading_agent as pta

    monkeypatch.setattr(pta, "PaperTradingAgent", lambda: _BoomAgent())

    with pytest.raises(RuntimeError, match="sandbox exploded"):
        api_main._run_paper_trading_step(
            strategy=strategy,
            strategy_code="def x(): pass",
            backtest_record=bt,
            initial_capital=100_000.0,
            transaction_cost_bps=5.0,
            slippage_bps=2.0,
            lookback_days=30,
        )


# ---------------------------------------------------------------------------
# _run_paper_trading_background — happy path (agent construction + persist)
# ---------------------------------------------------------------------------


def test_run_paper_trading_background_happy_path(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Happy path: market data + agent.run_session() complete → session persisted with session_id/lab_record_id preserved."""
    from investment_team.api import main as api_main
    from investment_team.models import (
        PaperTradingSession,
        PaperTradingStatus,
        PaperTradingVerdict,
    )

    strategy, bt = _step_strategy_and_record()
    running = PaperTradingSession(
        session_id="pt-ok", lab_record_id="lab-ok", strategy=strategy,
        status=PaperTradingStatus.RUNNING, initial_capital=100_000.0,
        current_capital=100_000.0, symbols_traded=[],
        data_source="yahoo_finance", data_period_start="", data_period_end="",
        started_at="2024-01-01T00:00:00Z",
    )
    api_main._paper_trading_sessions["pt-ok"] = running

    import investment_team.market_data_service as mds

    monkeypatch.setattr(
        mds, "MarketDataService", lambda: _FakeMarketService({"AAA": [{"close": 1.0}]})
    )

    # Returned session has a different (placeholder) session_id/lab_record_id
    # so the test can verify the worker overrides them with the caller's IDs.
    returned = PaperTradingSession(
        session_id="placeholder", lab_record_id="placeholder",
        strategy=strategy, status=PaperTradingStatus.COMPLETED,
        initial_capital=100_000.0, current_capital=120_000.0,
        symbols_traded=["AAA"], data_source="fake",
        data_period_start="2024-01-01", data_period_end="2024-06-01",
        started_at="2024-06-01T00:00:00Z",
        verdict=PaperTradingVerdict.READY_FOR_LIVE,
    )

    class _FakeAgent:
        def run_session(self, **kwargs):
            return returned

    import investment_team.paper_trading_agent as pta

    monkeypatch.setattr(pta, "PaperTradingAgent", lambda: _FakeAgent())

    api_main._run_paper_trading_background(
        "pt-ok", "lab-ok", strategy, "def x(): pass", bt,
        lookback_days=30, initial_capital=100_000.0,
        transaction_cost_bps=5.0, slippage_bps=2.0,
    )

    persisted = api_main._paper_trading_sessions.get("pt-ok")
    assert persisted is not None
    assert persisted.status == PaperTradingStatus.COMPLETED
    assert persisted.verdict == PaperTradingVerdict.READY_FOR_LIVE
    # Worker overrode the placeholder IDs with the caller's IDs.
    assert persisted.session_id == "pt-ok"
    assert persisted.lab_record_id == "lab-ok"
