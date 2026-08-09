"""Coverage for ``_run_live_paper_trading_background`` in ``api.main``.

The worker:
* Resolves the trading universe via ``MarketDataService.resolve_strategy_symbols``.
* Builds a ``PaperTradeConfig`` + ``BacktestConfig``.
* Drives ``run_paper_trade`` to completion.
* Persists the resulting ``PaperTradingSession`` (status COMPLETED or
  FAILED depending on the ``terminated_reason`` / error).
* Handles top-level exceptions by marking the session FAILED.
* Always clears the stop-controller registry entry.

Tests stub ``MarketDataService``, ``run_paper_trade``, and the
``PaperTradeConfig``/``StopController`` imports so no real provider
fires.
"""

from __future__ import annotations

import types
from typing import Any, Dict

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
def api_state(monkeypatch: pytest.MonkeyPatch):
    """Replace the api.main persistent dicts so tests have clean state."""
    from investment_team.api import main as api_main

    for attr in (
        "_profiles", "_proposals", "_strategies", "_validations",
        "_backtests", "_strategy_lab_records", "_paper_trading_sessions",
        "_advisor_sessions",
    ):
        monkeypatch.setattr(api_main, attr, _InMemoryDict())
    monkeypatch.setattr(api_main, "_active_runs", {})
    monkeypatch.setattr(api_main, "_live_paper_stop_controllers", {})
    return api_main


def _winning_strategy():
    from investment_team.models import StrategySpec

    return StrategySpec(
        strategy_id="strat-w", authored_by="x", asset_class="equities",
        hypothesis="h", signal_definition="s", timeframe="1d",
        strategy_code="def x(): pass",
    )


def _seed_running_session(api_state, *, session_id: str = "pt-live"):
    from investment_team.models import (
        PaperTradingSession,
        PaperTradingStatus,
    )

    strategy = _winning_strategy()
    session = PaperTradingSession(
        session_id=session_id, lab_record_id="lab-w", strategy=strategy,
        status=PaperTradingStatus.OPENING, initial_capital=100_000.0,
        current_capital=100_000.0, symbols_traded=[],
        data_source="live", data_period_start="",
        data_period_end="", started_at="2024-06-01T00:00:00Z",
    )
    api_state._paper_trading_sessions[session_id] = session
    return strategy


class _FakeMarketService:
    def __init__(self, symbols=None) -> None:
        # Use ``is None`` so callers passing ``[]`` get an empty list back —
        # they're testing the "no symbols resolved" branch of the worker.
        self._symbols = ["AAA"] if symbols is None else symbols

    def resolve_strategy_symbols(self, strategy):
        return list(self._symbols)


class _FakeRunResult:
    def __init__(self, **kwargs: Any) -> None:
        self.trades = kwargs.get("trades", [])
        self.fill_count = kwargs.get("fill_count", 0)
        self.cutover_ts = kwargs.get("cutover_ts")
        self.provider_id = kwargs.get("provider_id", "binance")
        self.terminated_reason = kwargs.get("terminated_reason", "min_fills_reached")
        self.warnings = kwargs.get("warnings", [])
        self.error = kwargs.get("error")
        self.dataset_fingerprint = kwargs.get("dataset_fingerprint")


def _install_run_paper_trade(
    monkeypatch: pytest.MonkeyPatch,
    result: _FakeRunResult | Exception,
    *,
    captured: Dict[str, Any] | None = None,
):
    """Patch the ``run_paper_trade`` import inside the worker function.

    If ``captured`` is given, the call's ``backtest_config``/``paper_config``
    are stashed under ``captured["backtest_config"]``/``captured["paper_config"]``
    for assertions.
    """

    def _fake_run(*, strategy, backtest_config, paper_config, stop_controller):
        if captured is not None:
            captured["backtest_config"] = backtest_config
            captured["paper_config"] = paper_config
        if isinstance(result, Exception):
            raise result
        return result

    # Build a stub ``paper_trade`` module so the worker's
    # ``from investment_team.trading_service.modes.paper_trade import ...``
    # picks our stubs.
    import investment_team.trading_service.modes.paper_trade as ptm

    monkeypatch.setattr(ptm, "run_paper_trade", _fake_run)

    class _StopController:
        def __init__(self) -> None:
            self.stopped = False

        def request_stop(self):
            self.stopped = True

    class _PaperTradeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(ptm, "StopController", _StopController, raising=False)
    monkeypatch.setattr(ptm, "PaperTradeConfig", _PaperTradeConfig, raising=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_live_paper_background_marks_completed_on_clean_termination(
    monkeypatch: pytest.MonkeyPatch, api_state
) -> None:
    from investment_team.models import PaperTradingStatus

    strategy = _seed_running_session(api_state, session_id="pt-1")

    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService(["AAA"]))

    _install_run_paper_trade(monkeypatch, _FakeRunResult(
        trades=[], fill_count=5, provider_id="binance",
        terminated_reason="min_fills_reached", error=None,
    ))

    from investment_team.api.main import RunPaperTradingRequest, _run_live_paper_trading_background

    req = RunPaperTradingRequest(lab_record_id="lab-w")
    _run_live_paper_trading_background("pt-1", "lab-w", strategy, req)

    session = api_state._paper_trading_sessions.get("pt-1")
    assert session.status == PaperTradingStatus.COMPLETED
    assert session.data_source == "live:binance"
    # Stop-controller entry should have been cleared.
    assert "pt-1" not in api_state._live_paper_stop_controllers


def test_live_paper_background_falls_back_to_default_timeframe(
    monkeypatch: pytest.MonkeyPatch, api_state
) -> None:
    """Neither the request nor the strategy supplies a timeframe: the worker
    must fall back to ``api_main._DEFAULT_TIMEFRAME``, not a bare literal."""
    from investment_team.api import main as api_main

    _seed_running_session(api_state, session_id="pt-default-timeframe")
    # A strategy stand-in with no ``timeframe`` attribute — ``StrategySpec``
    # can't express this since ``timeframe`` is a required Literal field.
    strategy = types.SimpleNamespace(asset_class="equities")

    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService(["AAA"]))

    captured: Dict[str, Any] = {}
    _install_run_paper_trade(monkeypatch, _FakeRunResult(), captured=captured)

    req = api_main.RunPaperTradingRequest(lab_record_id="lab-w")
    api_main._run_live_paper_trading_background(
        "pt-default-timeframe", "lab-w", strategy, req
    )

    assert (
        captured["paper_config"].kwargs["strategy_timeframe"]
        == api_main._DEFAULT_TIMEFRAME
    )


def test_live_paper_background_marks_failed_on_lookahead_violation(
    monkeypatch: pytest.MonkeyPatch, api_state
) -> None:
    from investment_team.models import PaperTradingStatus

    strategy = _seed_running_session(api_state, session_id="pt-2")

    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService(["AAA"]))
    _install_run_paper_trade(monkeypatch, _FakeRunResult(
        terminated_reason="lookahead_violation", error="bar.next_close"
    ))

    from investment_team.api.main import RunPaperTradingRequest, _run_live_paper_trading_background

    req = RunPaperTradingRequest(lab_record_id="lab-w")
    _run_live_paper_trading_background("pt-2", "lab-w", strategy, req)

    session = api_state._paper_trading_sessions.get("pt-2")
    assert session.status == PaperTradingStatus.FAILED


def test_live_paper_background_handles_crash_and_clears_controller(
    monkeypatch: pytest.MonkeyPatch, api_state
) -> None:
    from investment_team.models import PaperTradingStatus

    strategy = _seed_running_session(api_state, session_id="pt-3")

    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService(["AAA"]))
    _install_run_paper_trade(monkeypatch, RuntimeError("provider exploded"))

    from investment_team.api.main import RunPaperTradingRequest, _run_live_paper_trading_background

    req = RunPaperTradingRequest(lab_record_id="lab-w")
    _run_live_paper_trading_background("pt-3", "lab-w", strategy, req)

    session = api_state._paper_trading_sessions.get("pt-3")
    assert session.status == PaperTradingStatus.FAILED
    assert "provider exploded" in (session.error or "")
    # Stop-controller entry should be cleared regardless of the error.
    assert "pt-3" not in api_state._live_paper_stop_controllers


def test_live_paper_background_unparseable_record_does_not_escape_crash_handler(
    monkeypatch: pytest.MonkeyPatch, api_state
) -> None:
    """The crash handler's own ``PaperTradingSession.parse_persisted(raw)``
    call can itself raise (e.g. a corrupt persisted record) — that secondary
    exception must not escape the worker in place of the original crash,
    contradicting the documented ``Raises: None`` contract. The record is
    left as-is (logged, not updated) rather than propagating."""
    # A raw dict missing required fields -> parse_persisted raises when the
    # crash handler tries to re-parse it.
    api_state._paper_trading_sessions["pt-crash-corrupt"] = {"session_id": "pt-crash-corrupt"}
    strategy = _winning_strategy()

    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService(["AAA"]))
    _install_run_paper_trade(monkeypatch, RuntimeError("provider exploded"))

    from investment_team.api.main import RunPaperTradingRequest, _run_live_paper_trading_background

    req = RunPaperTradingRequest(lab_record_id="lab-w")

    # Must not raise.
    _run_live_paper_trading_background("pt-crash-corrupt", "lab-w", strategy, req)

    # Left as-is: the corrupt record could not be re-parsed, so it was logged
    # rather than overwritten with a FAILED status.
    assert api_state._paper_trading_sessions.get("pt-crash-corrupt") == {
        "session_id": "pt-crash-corrupt"
    }
    assert "pt-crash-corrupt" not in api_state._live_paper_stop_controllers


def test_live_paper_background_raises_when_no_symbols(
    monkeypatch: pytest.MonkeyPatch, api_state
) -> None:
    """Empty symbol list → RuntimeError, caught + persisted as FAILED."""
    from investment_team.models import PaperTradingStatus

    strategy = _seed_running_session(api_state, session_id="pt-4")

    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService([]))
    # run_paper_trade must NOT be reached, but install a stub so the import works.
    _install_run_paper_trade(monkeypatch, _FakeRunResult())

    from investment_team.api.main import RunPaperTradingRequest, _run_live_paper_trading_background

    req = RunPaperTradingRequest(lab_record_id="lab-w")
    _run_live_paper_trading_background("pt-4", "lab-w", strategy, req)

    session = api_state._paper_trading_sessions.get("pt-4")
    assert session.status == PaperTradingStatus.FAILED


def test_live_paper_background_logs_when_session_removed_before_success_write(
    monkeypatch: pytest.MonkeyPatch, api_state, caplog: pytest.LogCaptureFixture
) -> None:
    """If the session is removed from ``_paper_trading_sessions`` (e.g.
    deleted) while this worker is still running, the success-path persist
    must not silently discard the completed run's results — it should log a
    warning identifying the session before returning."""
    strategy = _winning_strategy()

    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService(["AAA"]))
    _install_run_paper_trade(monkeypatch, _FakeRunResult(
        trades=[], fill_count=5, provider_id="binance",
        terminated_reason="min_fills_reached", error=None,
    ))

    from investment_team.api.main import RunPaperTradingRequest, _run_live_paper_trading_background

    req = RunPaperTradingRequest(lab_record_id="lab-w")
    with caplog.at_level("WARNING"):
        _run_live_paper_trading_background("pt-removed", "lab-w", strategy, req)

    # No session entry was (re)created for a session that no longer exists.
    assert "pt-removed" not in api_state._paper_trading_sessions
    assert "pt-removed" not in api_state._live_paper_stop_controllers
    assert any(
        "pt-removed" in record.message and "discarding" in record.message.lower()
        for record in caplog.records
    )


def test_live_paper_background_does_not_clobber_already_terminal_session_on_success(
    monkeypatch: pytest.MonkeyPatch, api_state
) -> None:
    """If the session was already declared FAILED (e.g. by
    ``run_paper_trading``'s dispatch-failure path, ``_fail_paper_trading_session``)
    while this worker was an orphaned Temporal workflow still running
    server-side, the worker's own late-arriving success write must not
    clobber that FAILED status."""
    from investment_team.models import PaperTradingSession, PaperTradingStatus

    strategy = _winning_strategy()
    failed_session = PaperTradingSession(
        session_id="pt-orphan-success",
        lab_record_id="lab-w",
        strategy=strategy,
        status=PaperTradingStatus.FAILED,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=[],
        data_source="live",
        data_period_start="",
        data_period_end="",
        started_at="2024-06-01T00:00:00Z",
        completed_at="2024-06-01T00:05:00Z",
        error="Failed to start the paper-trading workflow (Temporal unavailable).",
    )
    api_state._paper_trading_sessions["pt-orphan-success"] = failed_session

    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService(["AAA"]))
    _install_run_paper_trade(
        monkeypatch,
        _FakeRunResult(
            trades=[],
            fill_count=5,
            provider_id="binance",
            terminated_reason="min_fills_reached",
            error=None,
        ),
    )

    from investment_team.api.main import RunPaperTradingRequest, _run_live_paper_trading_background

    req = RunPaperTradingRequest(lab_record_id="lab-w")
    _run_live_paper_trading_background("pt-orphan-success", "lab-w", strategy, req)

    session = api_state._paper_trading_sessions.get("pt-orphan-success")
    assert session.status == PaperTradingStatus.FAILED
    assert session.error == "Failed to start the paper-trading workflow (Temporal unavailable)."
    assert session.completed_at == "2024-06-01T00:05:00Z"
    # The orphaned run's data must not have been persisted onto the session.
    assert session.fill_count == 0


def test_live_paper_background_does_not_clobber_already_terminal_session_on_crash(
    monkeypatch: pytest.MonkeyPatch, api_state
) -> None:
    """Same guard on the crash-handler's terminal write."""
    from investment_team.models import PaperTradingSession, PaperTradingStatus

    strategy = _winning_strategy()
    failed_session = PaperTradingSession(
        session_id="pt-orphan-crash",
        lab_record_id="lab-w",
        strategy=strategy,
        status=PaperTradingStatus.FAILED,
        initial_capital=100_000.0,
        current_capital=100_000.0,
        symbols_traded=[],
        data_source="live",
        data_period_start="",
        data_period_end="",
        started_at="2024-06-01T00:00:00Z",
        completed_at="2024-06-01T00:05:00Z",
        error="Failed to start the paper-trading workflow (Temporal unavailable).",
    )
    api_state._paper_trading_sessions["pt-orphan-crash"] = failed_session

    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService(["AAA"]))
    _install_run_paper_trade(monkeypatch, RuntimeError("provider exploded"))

    from investment_team.api.main import RunPaperTradingRequest, _run_live_paper_trading_background

    req = RunPaperTradingRequest(lab_record_id="lab-w")
    _run_live_paper_trading_background("pt-orphan-crash", "lab-w", strategy, req)

    session = api_state._paper_trading_sessions.get("pt-orphan-crash")
    assert session.status == PaperTradingStatus.FAILED
    assert session.error == "Failed to start the paper-trading workflow (Temporal unavailable)."
    assert session.completed_at == "2024-06-01T00:05:00Z"


def test_live_paper_background_bt_config_start_end_date_match(
    monkeypatch: pytest.MonkeyPatch, api_state
) -> None:
    """``start_date``/``end_date`` must come from one captured "today", not
    two separate ``datetime.now()`` calls — two calls straddling midnight
    would otherwise produce a config spanning two days for what's meant to
    be a single live trading day."""
    from datetime import datetime, timezone

    from investment_team.api import main as api_main

    strategy = _seed_running_session(api_state, session_id="pt-midnight")

    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "MarketDataService", lambda: _FakeMarketService(["AAA"]))

    class _SequentialNow:
        """Returns a fixed sequence of timestamps straddling midnight, then
        holds at the last one for any further calls (e.g. completed_at)."""

        _timestamps = [
            datetime(2024, 1, 1, 23, 59, 59, 999999, tzinfo=timezone.utc),
            datetime(2024, 1, 2, 0, 0, 0, 1, tzinfo=timezone.utc),
        ]
        _calls = 0

        @classmethod
        def now(cls, tz=None):
            i = min(cls._calls, len(cls._timestamps) - 1)
            cls._calls += 1
            return cls._timestamps[i]

    monkeypatch.setattr(api_main, "datetime", _SequentialNow)

    captured: Dict[str, Any] = {}
    _install_run_paper_trade(monkeypatch, _FakeRunResult(), captured=captured)

    from investment_team.api.main import RunPaperTradingRequest, _run_live_paper_trading_background

    req = RunPaperTradingRequest(lab_record_id="lab-w")
    _run_live_paper_trading_background("pt-midnight", "lab-w", strategy, req)

    bt_config = captured["backtest_config"]
    assert bt_config.start_date == bt_config.end_date == "2024-01-01"
