"""Tests for the Temporal-only dispatch seams + start helpers.

The paper-trading and advisory endpoints are Temporal-only: the module-level
seams in ``investment_team.api.main`` require a worker and raise 503 otherwise,
and the ``start_workflow`` helpers translate a logical operation into a workflow
dispatch. The autouse ``_temporal_dispatch_inline`` fixture rebinds those seams
to run inline, so these tests capture the *real* seam functions at import time
(before the fixture patches the module attribute) to exercise the real logic.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

# Captured before the autouse fixture patches the module attributes.
from investment_team.api.main import (  # noqa: E402
    _execute_advisory as REAL_EXECUTE_ADVISORY,
)
from investment_team.api.main import (
    _signal_paper_trading_stop as REAL_SIGNAL_STOP,
)
from investment_team.api.main import (
    _start_paper_trading as REAL_START_PAPER,
)

# ---------------------------------------------------------------------------
# _require_temporal + seam 503 behavior
# ---------------------------------------------------------------------------


def test_require_temporal_raises_503_when_disabled(monkeypatch) -> None:
    import shared_temporal
    from investment_team.api import main as api_main

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: False)
    with pytest.raises(HTTPException) as ei:
        api_main._require_temporal()
    assert ei.value.status_code == 503


def test_require_temporal_ok_when_enabled(monkeypatch) -> None:
    import shared_temporal
    from investment_team.api import main as api_main

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    api_main._require_temporal()  # must not raise


def test_execute_advisory_503_when_disabled(monkeypatch) -> None:
    import shared_temporal

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: False)
    with pytest.raises(HTTPException) as ei:
        REAL_EXECUTE_ADVISORY("committee_memo", {}, key="k")
    assert ei.value.status_code == 503


def test_execute_advisory_delegates_when_enabled(monkeypatch) -> None:
    import shared_temporal
    from investment_team.temporal import start_workflow as sw

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    captured = {}

    def _fake(op, payload, *, key):
        captured.update(op=op, payload=payload, key=key)
        return {"r": 1}

    monkeypatch.setattr(sw, "execute_advisory_workflow", _fake)

    result = REAL_EXECUTE_ADVISORY("committee_memo", {"a": 1}, key="k")
    assert result == {"r": 1}
    assert captured == {"op": "committee_memo", "payload": {"a": 1}, "key": "k"}


def test_start_paper_trading_503_when_disabled(monkeypatch) -> None:
    import shared_temporal

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: False)
    with pytest.raises(HTTPException) as ei:
        REAL_START_PAPER("pt-1", {})
    assert ei.value.status_code == 503


def test_start_and_signal_delegate_when_enabled(monkeypatch) -> None:
    import shared_temporal
    from investment_team.temporal import start_workflow as sw

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    started, signalled = [], []
    monkeypatch.setattr(
        sw, "start_paper_trading_workflow", lambda sid, payload: started.append(sid)
    )
    monkeypatch.setattr(sw, "signal_paper_trading_stop", lambda sid: signalled.append(sid))

    REAL_START_PAPER("pt-1", {"session_id": "pt-1"})
    REAL_SIGNAL_STOP("pt-1")
    assert started == ["pt-1"] and signalled == ["pt-1"]


def test_route_returns_503_when_temporal_disabled(monkeypatch) -> None:
    """End-to-end: with the real seam restored and Temporal off, the route 503s."""
    from fastapi.testclient import TestClient

    import shared_temporal
    from investment_team.api import main as api_main

    # Override the autouse inline seam with the real one, then disable Temporal.
    monkeypatch.setattr(api_main, "_execute_advisory", REAL_EXECUTE_ADVISORY)
    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: False)

    client = TestClient(api_main.app)
    resp = client.post("/memos", json={"user_id": "u1", "recommendation": "r", "rationale": "x"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# start_workflow helpers
# ---------------------------------------------------------------------------


def test_execute_advisory_workflow_builds_id_and_dispatches(monkeypatch) -> None:
    import shared_temporal
    from investment_team.temporal import start_workflow as sw

    captured = {}

    def _fake_exec(run, payload, *, workflow_id, task_queue):
        captured.update(payload=payload, workflow_id=workflow_id, task_queue=task_queue)
        return {"ok": 1}

    monkeypatch.setattr(shared_temporal, "execute_workflow_sync", _fake_exec)

    result = sw.execute_advisory_workflow("promotion_decision", {"a": 1}, key="s1")
    assert result == {"ok": 1}
    assert captured["workflow_id"] == "investment-adv-promotion_decision-s1"
    assert captured["task_queue"] == "investment-advisory-queue"
    assert captured["payload"] == {"a": 1}


def test_execute_advisory_workflow_unknown_op_raises() -> None:
    from investment_team.temporal import start_workflow as sw

    with pytest.raises(ValueError, match="unknown advisory op"):
        sw.execute_advisory_workflow("nope", {}, key="k")


def test_start_paper_trading_workflow_builds_id(monkeypatch) -> None:
    import shared_temporal
    from investment_team.temporal import start_workflow as sw

    captured = {}
    monkeypatch.setattr(
        shared_temporal,
        "start_workflow_sync",
        lambda run, payload, *, workflow_id, task_queue: captured.update(
            workflow_id=workflow_id, task_queue=task_queue
        ),
    )
    sw.start_paper_trading_workflow("pt-1", {"session_id": "pt-1"})
    assert captured == {"workflow_id": "investment-pt-pt-1", "task_queue": "investment-queue"}


def test_signal_paper_trading_stop_sends_stop_signal(monkeypatch) -> None:
    import shared_temporal
    from investment_team.temporal import start_workflow as sw

    captured = {}
    monkeypatch.setattr(
        shared_temporal,
        "signal_workflow_sync",
        lambda wid, signal: captured.update(wid=wid, signal=signal),
    )
    sw.signal_paper_trading_stop("pt-1")
    assert captured == {"wid": "investment-pt-pt-1", "signal": "stop"}


# ---------------------------------------------------------------------------
# Worker boots all three queues
# ---------------------------------------------------------------------------


def test_worker_boots_all_three_queues(monkeypatch) -> None:
    from investment_team.strategy_lab.temporal import worker as sl_worker
    from investment_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)
    started = []
    monkeypatch.setattr(
        worker_mod,
        "start_team_worker",
        lambda team, wfs, acts, *, task_queue, **kw: started.append((team, task_queue)) or True,
    )
    monkeypatch.setattr(sl_worker, "start_strategy_lab_temporal_worker_thread", lambda: True)

    assert worker_mod.start_investment_temporal_worker_thread() is True
    teams = {t for t, _ in started}
    queues = {q for _, q in started}
    assert {"investment", "investment_advisory"} <= teams
    assert {"investment-queue", "investment-advisory-queue"} <= queues
