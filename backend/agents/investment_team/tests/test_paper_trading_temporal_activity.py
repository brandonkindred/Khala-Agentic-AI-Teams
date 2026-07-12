"""Unit tests for ``investment_team.temporal.paper_trading.run_paper_trading_activity``.

The activity must reuse the existing background workers verbatim (legacy +
live), re-read the lab record by id, and — on Temporal cancellation — trip the
session's ``StopController`` via its heartbeat. No live Temporal server is
required: ``temporalio.activity.heartbeat`` / ``is_cancelled`` are patched, and
the reused background workers are stubbed.
"""

from __future__ import annotations

import time

import pytest


class _Rec:
    """Minimal stand-in for a reconstructed ``StrategyLabRecord``."""

    strategy = object()
    backtest = object()
    strategy_code = "print('code')"


def _seed_record(monkeypatch, records=None, sessions=None):
    from investment_team import models as inv_models
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main, "_strategy_lab_records", dict(records or {"lab-1": {"id": "lab-1"}})
    )
    monkeypatch.setattr(api_main, "_paper_trading_sessions", dict(sessions or {}))
    monkeypatch.setattr(
        inv_models.StrategyLabRecord, "parse_persisted", staticmethod(lambda raw: _Rec())
    )


def test_legacy_path_reuses_background_worker(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.paper_trading import run_paper_trading_activity

    _seed_record(monkeypatch)
    calls = []
    monkeypatch.setattr(api_main, "_run_paper_trading_background", lambda *a: calls.append(a))
    monkeypatch.setattr(
        api_main, "_run_live_paper_trading_background", lambda *a: calls.append(("LIVE",) + a)
    )

    result = run_paper_trading_activity(
        {
            "session_id": "pt-1",
            "lab_record_id": "lab-1",
            "use_live": False,
            "request": {
                "lookback_days": 30,
                "initial_capital": 1000.0,
                "transaction_cost_bps": 5.0,
                "slippage_bps": 2.0,
            },
        }
    )

    assert len(calls) == 1
    # (session_id, lab_record_id, strategy, strategy_code, backtest, lookback, capital, tx, slip)
    assert calls[0][0] == "pt-1" and calls[0][1] == "lab-1"
    assert calls[0][3] == _Rec.strategy_code
    assert result == {"session_id": "pt-1", "status": "unknown"}


def test_live_path_reuses_live_background_worker(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.paper_trading import run_paper_trading_activity

    _seed_record(monkeypatch)
    calls = []
    monkeypatch.setattr(api_main, "_run_live_paper_trading_background", lambda *a: calls.append(a))

    result = run_paper_trading_activity(
        {
            "session_id": "pt-2",
            "lab_record_id": "lab-1",
            "use_live": True,
            "request": {"lab_record_id": "lab-1", "initial_capital": 5000.0},
        }
    )

    assert len(calls) == 1 and calls[0][0] == "pt-2" and calls[0][1] == "lab-1"
    assert result["session_id"] == "pt-2"


def test_missing_lab_record_raises_application_error(monkeypatch) -> None:
    from temporalio.exceptions import ApplicationError

    from investment_team.api import main as api_main
    from investment_team.temporal.paper_trading import run_paper_trading_activity

    monkeypatch.setattr(api_main, "_strategy_lab_records", {})
    with pytest.raises(ApplicationError, match="not found"):
        run_paper_trading_activity(
            {"session_id": "pt-x", "lab_record_id": "missing", "use_live": False, "request": {}}
        )


def test_cancellation_trips_stop_controller(monkeypatch) -> None:
    """On Temporal cancellation the heartbeat trips the live session's StopController."""
    from temporalio import activity as tl_activity

    from investment_team.api import main as api_main
    from investment_team.temporal import paper_trading as pt
    from investment_team.trading_service.modes.paper_trade import StopController

    _seed_record(monkeypatch)
    # Fast heartbeat + simulate an in-flight cancellation.
    monkeypatch.setattr(pt, "_HEARTBEAT_INTERVAL_S", 0.01)
    monkeypatch.setattr(tl_activity, "heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(tl_activity, "is_cancelled", lambda: True)

    controller = StopController()
    monkeypatch.setattr(api_main, "_live_paper_stop_controllers", {"pt-3": controller})

    def _fake_live(session_id, lab_id, strategy, request):
        # Block until the heartbeat trips the controller (bounded).
        for _ in range(200):
            if controller.is_stopped():
                return
            time.sleep(0.01)

    monkeypatch.setattr(api_main, "_run_live_paper_trading_background", _fake_live)

    run_result = pt.run_paper_trading_activity(
        {
            "session_id": "pt-3",
            "lab_record_id": "lab-1",
            "use_live": True,
            "request": {"lab_record_id": "lab-1"},
        }
    )

    assert controller.is_stopped()
    assert run_result["session_id"] == "pt-3"


class _StubSession:
    """Mutable stand-in for a PaperTradingSession with a real enum ``status``."""

    def __init__(self, status) -> None:
        self.status = status
        self.terminated_reason = None
        self.error = None
        self.completed_at = None


def test_mark_stopped_activity_marks_active_session_failed(monkeypatch) -> None:
    from investment_team import models as inv_models
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingStatus
    from investment_team.temporal.paper_trading import mark_paper_trading_stopped_activity

    stub = _StubSession(PaperTradingStatus.OPENING)
    monkeypatch.setattr(api_main, "_paper_trading_sessions", {"pt-9": {"status": "opening"}})
    monkeypatch.setattr(
        inv_models.PaperTradingSession, "parse_persisted", staticmethod(lambda raw: stub)
    )

    result = mark_paper_trading_stopped_activity("pt-9")

    assert result == {"session_id": "pt-9", "status": "failed"}
    assert stub.status == PaperTradingStatus.FAILED
    assert stub.terminated_reason == "user_stop"
    assert stub.completed_at is not None


def test_mark_stopped_activity_is_noop_on_terminal_session(monkeypatch) -> None:
    from investment_team import models as inv_models
    from investment_team.api import main as api_main
    from investment_team.models import PaperTradingStatus
    from investment_team.temporal.paper_trading import mark_paper_trading_stopped_activity

    stub = _StubSession(PaperTradingStatus.COMPLETED)
    monkeypatch.setattr(api_main, "_paper_trading_sessions", {"pt-10": {"status": "completed"}})
    monkeypatch.setattr(
        inv_models.PaperTradingSession, "parse_persisted", staticmethod(lambda raw: stub)
    )

    result = mark_paper_trading_stopped_activity("pt-10")

    assert result == {"session_id": "pt-10", "status": "completed"}
    assert stub.terminated_reason is None  # left untouched


def test_mark_stopped_activity_missing_session(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.paper_trading import mark_paper_trading_stopped_activity

    monkeypatch.setattr(api_main, "_paper_trading_sessions", {})
    assert mark_paper_trading_stopped_activity("nope") == {
        "session_id": "nope",
        "status": "unknown",
    }


def test_status_reflects_persisted_session(monkeypatch) -> None:
    from investment_team import models as inv_models
    from investment_team.api import main as api_main
    from investment_team.temporal.paper_trading import run_paper_trading_activity

    _seed_record(monkeypatch, sessions={"pt-4": {"status": "completed"}})
    monkeypatch.setattr(api_main, "_run_paper_trading_background", lambda *a: None)

    class _Sess:
        class status:
            value = "completed"

    monkeypatch.setattr(
        inv_models.PaperTradingSession, "parse_persisted", staticmethod(lambda raw: _Sess())
    )

    result = run_paper_trading_activity(
        {"session_id": "pt-4", "lab_record_id": "lab-1", "use_live": False, "request": {}}
    )
    assert result == {"session_id": "pt-4", "status": "completed"}
