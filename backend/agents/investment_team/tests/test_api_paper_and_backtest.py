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
        "_profiles",
        "_proposals",
        "_strategies",
        "_validations",
        "_backtests",
        "_strategy_lab_records",
        "_paper_trading_sessions",
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
        strategy_id="strat-w",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code="def x(): pass",
    )
    cfg = BacktestConfig(start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0)
    result = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=20.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id="bt-w",
        strategy_id="strat-w",
        strategy=strat,
        config=cfg,
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=result,
        trades=[],
    )
    return StrategyLabRecord(
        lab_record_id="lab-w",
        strategy=strat,
        backtest=bt,
        is_winning=True,
        is_publishable=True,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
        strategy_code="def x(): pass",
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
        strategy_id="s-rb",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
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
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code="def x(): pass",
    )
    bt = BacktestRecord(
        backtest_id="bt-p",
        strategy_id="s",
        strategy=strategy,
        config=BacktestConfig(
            start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
        ),
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=BacktestResult(
            total_return_pct=10.0,
            annualized_return_pct=20.0,
            volatility_pct=10.0,
            sharpe_ratio=1.0,
            max_drawdown_pct=5.0,
            win_rate_pct=60.0,
            profit_factor=2.0,
            calmar_ratio=0.0,
            deflated_sharpe=0.0,
            sortino_ratio=0.0,
        ),
        trades=[],
    )

    # Pre-create a "running" session in the store.
    running = PaperTradingSession(
        session_id="pt-empty",
        lab_record_id="lab-w",
        strategy=strategy,
        status=PaperTradingStatus.RUNNING,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=[],
        data_source="yahoo_finance",
        data_period_start="",
        data_period_end="",
        started_at="2024-01-01T00:00:00Z",
    )
    api_main._paper_trading_sessions["pt-empty"] = running

    # Patch market service constructor so the worker uses our fake.
    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService({}))

    api_main._run_paper_trading_background(
        "pt-empty",
        "lab-w",
        strategy,
        "def x(): pass",
        bt,
        lookback_days=30,
        initial_capital=100_000.0,
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )

    # Worker updated the session to FAILED.
    updated = api_main._paper_trading_sessions.get("pt-empty")
    assert updated.status == PaperTradingStatus.FAILED
    assert "Failed to fetch market data" in (updated.divergence_analysis or "")
    assert "Failed to fetch market data" in (updated.error or "")


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
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code="def x(): pass",
    )
    bt = BacktestRecord(
        backtest_id="bt-p",
        strategy_id="s",
        strategy=strategy,
        config=BacktestConfig(
            start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
        ),
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=BacktestResult(
            total_return_pct=10.0,
            annualized_return_pct=20.0,
            volatility_pct=10.0,
            sharpe_ratio=1.0,
            max_drawdown_pct=5.0,
            win_rate_pct=60.0,
            profit_factor=2.0,
            calmar_ratio=0.0,
            deflated_sharpe=0.0,
            sortino_ratio=0.0,
        ),
        trades=[],
    )
    running = PaperTradingSession(
        session_id="pt-crash",
        lab_record_id="lab-w",
        strategy=strategy,
        status=PaperTradingStatus.RUNNING,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=[],
        data_source="yahoo_finance",
        data_period_start="",
        data_period_end="",
        started_at="2024-01-01T00:00:00Z",
    )
    api_main._paper_trading_sessions["pt-crash"] = running

    import investment_team.market_data_service as mds

    class _Broken:
        def resolve_strategy_symbols(self, s):
            raise RuntimeError("boom in resolve")

    monkeypatch.setattr(mds, "MarketDataService", lambda: _Broken())

    api_main._run_paper_trading_background(
        "pt-crash",
        "lab-w",
        strategy,
        "def x(): pass",
        bt,
        lookback_days=30,
        initial_capital=100_000.0,
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )
    updated = api_main._paper_trading_sessions.get("pt-crash")
    assert updated.status == PaperTradingStatus.FAILED
    assert "Paper trading crashed" in (updated.divergence_analysis or "")
    assert "Paper trading crashed" in (updated.error or "")


def test_run_paper_trading_background_import_failure_marks_failed(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """An ImportError raised by the function-scoped imports (e.g. a missing
    dependency or circular import in ``market_data_service``) is caught by the
    same handler as any other in-worker exception, so the session still ends
    in FAILED instead of leaving the background thread to crash silently.
    """
    import sys

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
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code="def x(): pass",
    )
    bt = BacktestRecord(
        backtest_id="bt-p",
        strategy_id="s",
        strategy=strategy,
        config=BacktestConfig(
            start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
        ),
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=BacktestResult(
            total_return_pct=10.0,
            annualized_return_pct=20.0,
            volatility_pct=10.0,
            sharpe_ratio=1.0,
            max_drawdown_pct=5.0,
            win_rate_pct=60.0,
            profit_factor=2.0,
            calmar_ratio=0.0,
            deflated_sharpe=0.0,
            sortino_ratio=0.0,
        ),
        trades=[],
    )
    running = PaperTradingSession(
        session_id="pt-import-fail",
        lab_record_id="lab-w",
        strategy=strategy,
        status=PaperTradingStatus.RUNNING,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=[],
        data_source="yahoo_finance",
        data_period_start="",
        data_period_end="",
        started_at="2024-01-01T00:00:00Z",
    )
    api_main._paper_trading_sessions["pt-import-fail"] = running

    # Force `from investment_team.market_data_service import MarketDataService`
    # to raise ModuleNotFoundError, simulating a missing dependency / circular
    # import at import time (not a runtime failure inside the try body).
    monkeypatch.setitem(sys.modules, "investment_team.market_data_service", None)

    api_main._run_paper_trading_background(
        "pt-import-fail",
        "lab-w",
        strategy,
        "def x(): pass",
        bt,
        lookback_days=30,
        initial_capital=100_000.0,
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )

    updated = api_main._paper_trading_sessions.get("pt-import-fail")
    assert updated.status == PaperTradingStatus.FAILED
    assert updated.completed_at
    assert "Paper trading crashed" in (updated.divergence_analysis or "")
    assert "Paper trading crashed" in (updated.error or "")


# ---------------------------------------------------------------------------
# stop_live_paper_trading
# ---------------------------------------------------------------------------


def test_stop_live_paper_trading_404_when_session_missing(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    resp = api_client.post("/strategy-lab/paper-trade/no-session/stop")
    assert resp.status_code == 404


def test_stop_live_paper_trading_happy_path(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    """Stop endpoint must invoke the StopController and stamp the session."""
    from investment_team.api import main as api_main
    from investment_team.models import (
        PaperTradingSession,
        PaperTradingStatus,
        StrategySpec,
    )

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code="def x(): pass",
    )
    session = PaperTradingSession(
        session_id="pt-live",
        lab_record_id="lab-w",
        strategy=strategy,
        status=PaperTradingStatus.LIVE,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=["AAA"],
        data_source="live:binance",
        data_period_start="2024-01-01",
        data_period_end="2024-06-01",
        started_at="2024-06-01T00:00:00Z",
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


def test_stop_does_not_resurrect_session_deleted_during_signal(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """If the session is deleted (e.g. its lab record was deleted) in the
    window between the pre-signal read and the post-signal re-read, the stop
    route must not write the stale pre-signal snapshot back into the store —
    that would resurrect a session that should have stayed deleted."""
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingStatus, StrategySpec

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code="def x(): pass",
    )
    api_main._paper_trading_sessions["pt-deleted"] = _live_session(
        "pt-deleted", strategy, PaperTradingStatus.LIVE
    )

    def _delete_during_signal(session_id):
        # Simulate a concurrent delete (e.g. DELETE /strategy-lab/records/{id})
        # racing with the in-flight stop signal.
        del api_main._paper_trading_sessions[session_id]

    monkeypatch.setattr(api_main, "_signal_paper_trading_stop", _delete_during_signal)

    resp = api_client.post("/strategy-lab/paper-trade/pt-deleted/stop")

    assert resp.status_code == 404
    assert "pt-deleted" not in api_main._paper_trading_sessions


def _live_session(session_id, strategy, status):
    from investment_team.models import PaperTradingSession

    return PaperTradingSession(
        session_id=session_id,
        lab_record_id="lab-w",
        strategy=strategy,
        status=status,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=["AAA"],
        data_source="live:binance",
        data_period_start="2024-01-01",
        data_period_end="2024-06-01",
        started_at="2024-06-01T00:00:00Z",
    )


def test_stop_idempotent_for_terminal_session_does_not_signal(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A terminal session has a closed workflow — /stop must not signal it (which
    would 500) and must return the session unchanged."""
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingStatus, StrategySpec

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code="def x(): pass",
    )
    api_main._paper_trading_sessions["pt-done"] = _live_session(
        "pt-done", strategy, PaperTradingStatus.COMPLETED
    )
    signalled: List[str] = []
    monkeypatch.setattr(api_main, "_signal_paper_trading_stop", lambda sid: signalled.append(sid))

    resp = api_client.post("/strategy-lab/paper-trade/pt-done/stop")

    assert resp.status_code == 200
    assert signalled == []  # terminal session → no signal sent


def test_stop_swallows_closed_workflow_rpc_error(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A race where the workflow closes before the signal lands (a real
    temporalio RPCError with status=NOT_FOUND) must not 500 the idempotent stop
    route — it's treated as an already-finished session."""
    from temporalio.service import RPCError, RPCStatusCode

    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingStatus, StrategySpec

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code="def x(): pass",
    )
    api_main._paper_trading_sessions["pt-race"] = _live_session(
        "pt-race", strategy, PaperTradingStatus.LIVE
    )

    def _boom(session_id):
        raise RPCError("workflow execution already completed", RPCStatusCode.NOT_FOUND, b"")

    monkeypatch.setattr(api_main, "_signal_paper_trading_stop", _boom)

    resp = api_client.post("/strategy-lab/paper-trade/pt-race/stop")

    assert resp.status_code == 200  # not a 500 (nor a 502)
    assert "already finished" in resp.json()["message"]


def test_stop_surfaces_genuine_signal_delivery_failure(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A genuine delivery failure (client not connected, RPC timeout, any other
    RPC error) must NOT be swallowed as a false success on a live-trading kill
    switch — it must surface as a real error."""
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingStatus, StrategySpec

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code="def x(): pass",
    )
    api_main._paper_trading_sessions["pt-race"] = _live_session(
        "pt-race", strategy, PaperTradingStatus.LIVE
    )

    def _boom(session_id):
        raise RuntimeError("Temporal client not available; is the team's worker running?")

    monkeypatch.setattr(api_main, "_signal_paper_trading_stop", _boom)

    resp = api_client.post("/strategy-lab/paper-trade/pt-race/stop")

    assert resp.status_code == 502
    assert "pt-race" in resp.json()["detail"]


def test_run_paper_trading_marks_failed_when_dispatch_raises_http(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A 503 from dispatch must roll the just-created session forward to FAILED so
    it can't orphan the live concurrency guard."""
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingSession, PaperTradingStatus

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    api_main._strategy_lab_records["lab-w"] = _winning_record()

    def _boom(session_id, payload):
        raise api_main.HTTPException(status_code=503, detail="Temporal unavailable")

    monkeypatch.setattr(api_main, "_start_paper_trading", _boom)

    resp = api_client.post("/strategy-lab/paper-trade", json={"lab_record_id": "lab-w"})

    assert resp.status_code == 503
    sessions = [
        PaperTradingSession.parse_persisted(s) for s in api_main._paper_trading_sessions.values()
    ]
    assert sessions and all(s.status == PaperTradingStatus.FAILED for s in sessions)


def test_run_paper_trading_wraps_runtime_dispatch_error_as_503(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingSession, PaperTradingStatus

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    api_main._strategy_lab_records["lab-w"] = _winning_record()

    def _boom(session_id, payload):
        raise RuntimeError("worker client not connected")

    monkeypatch.setattr(api_main, "_start_paper_trading", _boom)

    resp = api_client.post("/strategy-lab/paper-trade", json={"lab_record_id": "lab-w"})

    assert resp.status_code == 503
    sessions = [
        PaperTradingSession.parse_persisted(s) for s in api_main._paper_trading_sessions.values()
    ]
    assert sessions and all(s.status == PaperTradingStatus.FAILED for s in sessions)


def test_run_paper_trading_dispatch_failure_attempts_best_effort_stop_signal(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A dispatch-ack timeout doesn't prove the workflow never started (the sync
    bridge's wait only bounds our own wait) — the route must best-effort signal
    the deterministic workflow id to stop it if it did start server-side, so it
    can't be orphaned unstoppable."""
    from investment_team.api import main as api_main

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    api_main._strategy_lab_records["lab-w"] = _winning_record()

    def _boom(session_id, payload):
        raise RuntimeError("ack timeout")

    monkeypatch.setattr(api_main, "_start_paper_trading", _boom)
    signalled: List[str] = []
    monkeypatch.setattr(api_main, "_signal_paper_trading_stop", lambda sid: signalled.append(sid))

    resp = api_client.post("/strategy-lab/paper-trade", json={"lab_record_id": "lab-w"})

    assert resp.status_code == 503
    assert len(signalled) == 1  # best-effort stop attempted for the session_id minted


def test_run_paper_trading_dispatch_failure_swallows_signal_error(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """The best-effort stop-signal attempt must itself never break the
    dispatch-failure response — a workflow that never started has nothing to
    signal, so a signal failure here is expected and harmless."""
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingSession, PaperTradingStatus

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    api_main._strategy_lab_records["lab-w"] = _winning_record()

    monkeypatch.setattr(
        api_main, "_start_paper_trading", lambda sid, payload: (_ for _ in ()).throw(RuntimeError())
    )

    def _signal_boom(session_id):
        raise RuntimeError("Temporal client not available")

    monkeypatch.setattr(api_main, "_signal_paper_trading_stop", _signal_boom)

    resp = api_client.post("/strategy-lab/paper-trade", json={"lab_record_id": "lab-w"})

    assert resp.status_code == 503  # the original dispatch failure, not the signal failure
    sessions = [
        PaperTradingSession.parse_persisted(s) for s in api_main._paper_trading_sessions.values()
    ]
    assert sessions and all(s.status == PaperTradingStatus.FAILED for s in sessions)


def test_max_hours_rejects_absurd_values(api_client) -> None:
    """An unbounded max_hours would overflow timedelta construction inside
    workflow code; the field must reject values above the documented cap."""
    from investment_team.api import main as api_main

    api_main._strategy_lab_records["lab-w"] = _winning_record()

    resp = api_client.post(
        "/strategy-lab/paper-trade",
        json={"lab_record_id": "lab-w", "max_hours": 1e12},
    )

    assert resp.status_code == 422


def test_fail_paper_trading_session_is_idempotent_on_completed(monkeypatch) -> None:
    """Must not clobber a session that already reached a real terminal outcome
    concurrently (e.g. the workflow actually completed while the dispatch-error
    handler was deciding to mark it failed)."""
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingSession, PaperTradingStatus, StrategySpec

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code="def x(): pass",
    )
    completed = _live_session("pt-done", strategy, PaperTradingStatus.COMPLETED)
    completed.error = None
    store = {"pt-done": completed}
    monkeypatch.setattr(api_main, "_paper_trading_sessions", store)

    api_main._fail_paper_trading_session("pt-done", "should not apply")

    result = PaperTradingSession.parse_persisted(store["pt-done"])
    assert result.status == PaperTradingStatus.COMPLETED
    assert result.error is None


def test_fail_paper_trading_session_marks_active_session_failed(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingSession, PaperTradingStatus, StrategySpec

    strategy = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code="def x(): pass",
    )
    live = _live_session("pt-live2", strategy, PaperTradingStatus.LIVE)
    store = {"pt-live2": live}
    monkeypatch.setattr(api_main, "_paper_trading_sessions", store)

    api_main._fail_paper_trading_session("pt-live2", "dispatch failed")

    result = PaperTradingSession.parse_persisted(store["pt-live2"])
    assert result.status == PaperTradingStatus.FAILED
    assert result.error == "dispatch failed"


def test_fail_paper_trading_session_handles_unparseable_data(monkeypatch) -> None:
    """A malformed persisted record must not raise — the documented "Never
    raises" postcondition must actually hold."""
    from investment_team.api import main as api_main

    store = {"pt-bad": {"not": "a valid session shape"}}
    monkeypatch.setattr(api_main, "_paper_trading_sessions", store)

    api_main._fail_paper_trading_session("pt-bad", "irrelevant")  # must not raise


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
        session_id="pt-existing",
        lab_record_id="lab-w",
        strategy=record.strategy,
        status=PaperTradingStatus.LIVE,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=["AAA"],
        data_source="live:binance",
        data_period_start="2024-01-01",
        data_period_end="2024-06-01",
        started_at="2024-06-01T00:00:00Z",
    )
    api_main._paper_trading_sessions["pt-existing"] = active

    resp = api_client.post(
        "/strategy-lab/paper-trade",
        json={"lab_record_id": "lab-w"},
    )
    assert resp.status_code == 409
    assert "already has an" in resp.json()["detail"]


def test_run_paper_trading_skips_unparseable_session_in_guard(
    monkeypatch: pytest.MonkeyPatch, api_client, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt/unparseable record in ``_paper_trading_sessions`` must not
    turn the concurrency guard into a 500 for unrelated strategies — it
    should be logged and skipped, same as
    ``_recover_orphaned_paper_trading_sessions``."""
    import logging

    from investment_team.api import main as api_main

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    record = _winning_record()
    api_main._strategy_lab_records["lab-w"] = record
    monkeypatch.setattr(api_main, "_run_live_paper_trading_background", lambda *a, **k: None)

    # Corrupt record: fails PaperTradingSession.parse_persisted, unrelated to
    # this request's strategy_id either way.
    api_main._paper_trading_sessions["pt-bad"] = {"not": "a-paper-trading-session"}

    with caplog.at_level(logging.WARNING, logger="investment_team.api.main"):
        resp = api_client.post(
            "/strategy-lab/paper-trade",
            json={"lab_record_id": "lab-w"},
        )

    assert resp.status_code == 200
    assert resp.json()["session"]["status"] == "opening"

    warnings = [
        r for r in caplog.records if "Skipping unparseable paper-trading session" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    # The corrupt row is left untouched, not overwritten/removed.
    assert api_main._paper_trading_sessions["pt-bad"] == {"not": "a-paper-trading-session"}


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


def test_run_paper_trading_concurrent_starts_same_strategy_only_one_wins(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Regression test for a race in the live-mode concurrency guard: the
    scan, session construction, and dict insertion must be atomic under
    ``_lock`` so two concurrent starts for the same strategy can't both
    pass the guard.

    The vulnerable gap in the pre-fix code sat between the scan's ``with
    _lock:`` block and the later ``with _lock:`` that inserted the built
    session — ``PaperTradingSession(...)`` construction ran unlocked in
    between. This test widens exactly that gap by delaying the *first*
    ``PaperTradingSession(...)`` construction: on the buggy code the delayed
    thread has already released the scan lock by the time it starts
    constructing, so the second racer's scan runs concurrently and also
    finds no conflict (both succeed, two active sessions). On the fixed
    code the lock is held continuously across scan + construction + insert,
    so the second racer blocks until the first has already inserted and
    correctly gets 409.
    """
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingSession

    monkeypatch.setenv("INVESTMENT_LIVE_PAPER_ENABLED", "true")
    record = _winning_record()
    api_main._strategy_lab_records["lab-w"] = record
    monkeypatch.setattr(api_main, "_run_live_paper_trading_background", lambda *a, **k: None)

    real_session_cls = api_main.PaperTradingSession
    call_count = {"n": 0}
    count_lock = threading.Lock()

    class _SlowPaperTradingSession(real_session_cls):
        def __init__(self, *args, **kwargs):
            with count_lock:
                call_count["n"] += 1
                is_first = call_count["n"] == 1
            if is_first:
                time.sleep(0.2)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(api_main, "PaperTradingSession", _SlowPaperTradingSession)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                api_client.post, "/strategy-lab/paper-trade", json={"lab_record_id": "lab-w"}
            )
            for _ in range(2)
        ]
        results = [f.result(timeout=5) for f in futures]

    assert sorted(r.status_code for r in results) == [200, 409]
    active = [
        s
        for s in api_main._paper_trading_sessions.values()
        if PaperTradingSession.parse_persisted(s).status in api_main._ACTIVE_PT_STATES
    ]
    assert len(active) == 1


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
        strategy_id="s-step",
        authored_by="x",
        asset_class="equities",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        strategy_code="def x(): pass",
    )
    bt = BacktestRecord(
        backtest_id="bt-step",
        strategy_id="s-step",
        strategy=strategy,
        config=BacktestConfig(
            start_date="2024-01-01", end_date="2024-02-01", initial_capital=100_000.0
        ),
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=BacktestResult(
            total_return_pct=10.0,
            annualized_return_pct=20.0,
            volatility_pct=10.0,
            sharpe_ratio=1.0,
            max_drawdown_pct=5.0,
            win_rate_pct=60.0,
            profit_factor=2.0,
            calmar_ratio=0.0,
            deflated_sharpe=0.0,
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
        session_id="pt-step-1",
        lab_record_id="",
        strategy=strategy,
        status=PaperTradingStatus.COMPLETED,
        initial_capital=100_000.0,
        current_capital=110_000.0,
        symbols_traded=["AAA", "BBB"],
        data_source="fake",
        data_period_start="2024-01-01",
        data_period_end="2024-06-01",
        started_at="2024-06-01T00:00:00Z",
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
        session_id="pt-ok",
        lab_record_id="lab-ok",
        strategy=strategy,
        status=PaperTradingStatus.RUNNING,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=[],
        data_source="yahoo_finance",
        data_period_start="",
        data_period_end="",
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
        session_id="placeholder",
        lab_record_id="placeholder",
        strategy=strategy,
        status=PaperTradingStatus.COMPLETED,
        initial_capital=100_000.0,
        current_capital=120_000.0,
        symbols_traded=["AAA"],
        data_source="fake",
        data_period_start="2024-01-01",
        data_period_end="2024-06-01",
        started_at="2024-06-01T00:00:00Z",
        completed_at="2024-06-01T00:05:00Z",
        verdict=PaperTradingVerdict.READY_FOR_LIVE,
    )

    class _FakeAgent:
        def run_session(self, **kwargs):
            return returned

    import investment_team.paper_trading_agent as pta

    monkeypatch.setattr(pta, "PaperTradingAgent", lambda: _FakeAgent())

    api_main._run_paper_trading_background(
        "pt-ok",
        "lab-ok",
        strategy,
        "def x(): pass",
        bt,
        lookback_days=30,
        initial_capital=100_000.0,
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )

    persisted = api_main._paper_trading_sessions.get("pt-ok")
    assert persisted is not None
    assert persisted.status == PaperTradingStatus.COMPLETED
    assert persisted.verdict == PaperTradingVerdict.READY_FOR_LIVE
    # Worker overrode the placeholder IDs with the caller's IDs.
    assert persisted.session_id == "pt-ok"
    assert persisted.lab_record_id == "lab-ok"


def test_run_paper_trading_background_guards_non_terminal_agent_result(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A ``run_session`` result that violates its documented terminal-state
    contract (status still RUNNING, no ``completed_at``) must not be persisted
    verbatim — the worker's postcondition guard should route it into the same
    FAILED handling as any other in-worker crash.
    """
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingSession, PaperTradingStatus

    strategy, bt = _step_strategy_and_record()
    running = PaperTradingSession(
        session_id="pt-nonterminal",
        lab_record_id="lab-nonterminal",
        strategy=strategy,
        status=PaperTradingStatus.RUNNING,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=[],
        data_source="yahoo_finance",
        data_period_start="",
        data_period_end="",
        started_at="2024-01-01T00:00:00Z",
    )
    api_main._paper_trading_sessions["pt-nonterminal"] = running

    import investment_team.market_data_service as mds

    monkeypatch.setattr(
        mds, "MarketDataService", lambda: _FakeMarketService({"AAA": [{"close": 1.0}]})
    )

    # A misbehaving agent that returns a non-terminal session (no completed_at).
    non_terminal = PaperTradingSession(
        session_id="placeholder",
        lab_record_id="placeholder",
        strategy=strategy,
        status=PaperTradingStatus.RUNNING,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=["AAA"],
        data_source="fake",
        started_at="2024-06-01T00:00:00Z",
    )

    class _FakeAgent:
        def run_session(self, **kwargs):
            return non_terminal

    import investment_team.paper_trading_agent as pta

    monkeypatch.setattr(pta, "PaperTradingAgent", lambda: _FakeAgent())

    api_main._run_paper_trading_background(
        "pt-nonterminal",
        "lab-nonterminal",
        strategy,
        "def x(): pass",
        bt,
        lookback_days=30,
        initial_capital=100_000.0,
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )

    persisted = api_main._paper_trading_sessions.get("pt-nonterminal")
    assert persisted is not None
    assert persisted.status == PaperTradingStatus.FAILED
    assert persisted.completed_at
    assert "non-terminal status" in (persisted.error or "")
