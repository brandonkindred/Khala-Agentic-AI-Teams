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
    import shared.temporal
    from investment_team.api import main as api_main

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: False)
    with pytest.raises(HTTPException) as ei:
        api_main._require_temporal()
    assert ei.value.status_code == 503


def test_require_temporal_ok_when_enabled(monkeypatch) -> None:
    import shared.temporal
    from investment_team.api import main as api_main

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    api_main._require_temporal()  # must not raise


def test_require_temporal_maps_enablement_check_error_to_503(monkeypatch) -> None:
    """``is_temporal_enabled()`` raising (e.g. a misconfigured Temporal
    client) must map to the same documented 503, not propagate as an
    unhandled 500."""
    import shared.temporal
    from investment_team.api import main as api_main

    def _boom() -> bool:
        raise RuntimeError("Temporal client misconfigured")

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", _boom)
    with pytest.raises(HTTPException) as ei:
        api_main._require_temporal()
    assert ei.value.status_code == 503


def test_execute_advisory_503_when_disabled(monkeypatch) -> None:
    import shared.temporal

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: False)
    with pytest.raises(HTTPException) as ei:
        REAL_EXECUTE_ADVISORY("committee_memo", {}, key="k")
    assert ei.value.status_code == 503


def test_execute_advisory_delegates_when_enabled(monkeypatch) -> None:
    import shared.temporal
    from investment_team.temporal import start_workflow as sw

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    captured = {}

    def _fake(op, payload, *, key):
        captured.update(op=op, payload=payload, key=key)
        return {"r": 1}

    monkeypatch.setattr(sw, "execute_advisory_workflow", _fake)

    result = REAL_EXECUTE_ADVISORY("committee_memo", {"a": 1}, key="k")
    assert result == {"r": 1}
    assert captured == {"op": "committee_memo", "payload": {"a": 1}, "key": "k"}


def test_start_paper_trading_503_when_disabled(monkeypatch) -> None:
    import shared.temporal

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: False)
    with pytest.raises(HTTPException) as ei:
        REAL_START_PAPER("pt-1", {})
    assert ei.value.status_code == 503


def test_start_paper_trading_delegates_when_enabled(monkeypatch) -> None:
    import shared.temporal
    from investment_team.temporal import start_workflow as sw

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    started = []
    monkeypatch.setattr(
        sw, "start_paper_trading_workflow", lambda sid, payload: started.append(sid)
    )

    REAL_START_PAPER("pt-1", {"session_id": "pt-1"})
    assert started == ["pt-1"]


def test_signal_paper_trading_stop_delegates_when_enabled(monkeypatch) -> None:
    import shared.temporal
    from investment_team.temporal import start_workflow as sw

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    signalled = []
    monkeypatch.setattr(sw, "signal_paper_trading_stop", lambda sid: signalled.append(sid))

    REAL_SIGNAL_STOP("pt-1")
    assert signalled == ["pt-1"]


def test_route_returns_503_when_temporal_disabled(monkeypatch) -> None:
    """End-to-end: with the real seam restored and Temporal off, the route 503s."""
    from fastapi.testclient import TestClient

    import shared.temporal
    from investment_team.api import main as api_main

    # Override the autouse inline seam with the real one, then disable Temporal.
    monkeypatch.setattr(api_main, "_execute_advisory", REAL_EXECUTE_ADVISORY)
    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: False)

    client = TestClient(api_main.app)
    resp = client.post("/memos", json={"user_id": "u1", "recommendation": "r", "rationale": "x"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# start_workflow helpers
# ---------------------------------------------------------------------------


def test_execute_advisory_workflow_builds_id_and_dispatches(monkeypatch) -> None:
    import shared.temporal
    from investment_team.temporal import start_workflow as sw

    captured = {}

    def _fake_exec(run, payload, *, workflow_id, task_queue, execute_timeout_s=None):
        captured.update(
            payload=payload,
            workflow_id=workflow_id,
            task_queue=task_queue,
            execute_timeout_s=execute_timeout_s,
        )
        return {"ok": 1}

    monkeypatch.setattr(shared.temporal, "execute_workflow_sync", _fake_exec)

    result = sw.execute_advisory_workflow("promotion_decision", {"a": 1}, key="s1")
    assert result == {"ok": 1}
    # A random suffix is appended so two calls for the same (op, key) never
    # collide on a live workflow id (see test below).
    assert captured["workflow_id"].startswith("investment-adv-promotion_decision-s1-")
    assert captured["task_queue"] == "investment-advisory-queue"
    assert captured["payload"] == {"a": 1}
    assert captured["execute_timeout_s"] == pytest.approx(180.0)


def test_execute_advisory_workflow_mints_unique_id_per_call(monkeypatch) -> None:
    """Two calls for the same (op, key) — e.g. two chat messages in the same
    advisor session — must not collide on a live Temporal workflow id."""
    import shared.temporal
    from investment_team.temporal import start_workflow as sw

    ids = []

    def _fake_exec(run, payload, *, workflow_id, task_queue, execute_timeout_s=None):
        ids.append(workflow_id)
        return {"ok": 1}

    monkeypatch.setattr(shared.temporal, "execute_workflow_sync", _fake_exec)

    sw.execute_advisory_workflow("advisor_message", {}, key="session-1")
    sw.execute_advisory_workflow("advisor_message", {}, key="session-1")

    assert len(ids) == 2
    assert ids[0] != ids[1]


def test_execute_advisory_workflow_truncates_long_key(monkeypatch) -> None:
    """An unbounded caller-supplied key (e.g. a long client-provided user_id)
    must not push the workflow id past Temporal's server-side length limit."""
    import shared.temporal
    from investment_team.temporal import start_workflow as sw

    captured = {}

    def _fake_exec(run, payload, *, workflow_id, task_queue, execute_timeout_s=None):
        captured["workflow_id"] = workflow_id
        return {"ok": 1}

    monkeypatch.setattr(shared.temporal, "execute_workflow_sync", _fake_exec)

    huge_key = "u" * 5_000
    sw.execute_advisory_workflow("advisor_start", {}, key=huge_key)

    assert len(captured["workflow_id"]) < 300
    assert captured["workflow_id"].startswith(
        f"investment-adv-advisor_start-{huge_key[: sw._ADVISORY_KEY_MAX_LEN]}-"
    )


def test_execute_advisory_workflow_empty_key_still_dispatches(monkeypatch) -> None:
    """An empty-string key (a valid, if degenerate, caller-supplied label —
    see the precondition docstring) still produces a well-formed, dispatchable
    workflow id rather than a malformed one. ``None`` is not a valid ``key``
    (every call site's Pydantic request field is a required, non-optional
    ``str``), so it is out of this function's precondition and not exercised
    here."""
    import shared.temporal
    from investment_team.temporal import start_workflow as sw

    captured = {}

    def _fake_exec(run, payload, *, workflow_id, task_queue, execute_timeout_s=None):
        captured["workflow_id"] = workflow_id
        return {"ok": 1}

    monkeypatch.setattr(shared.temporal, "execute_workflow_sync", _fake_exec)

    sw.execute_advisory_workflow("advisor_start", {}, key="")

    assert captured["workflow_id"].startswith("investment-adv-advisor_start--")


def test_execute_advisory_workflow_unknown_op_raises() -> None:
    from investment_team.temporal import start_workflow as sw

    with pytest.raises(ValueError, match="unknown advisory op"):
        sw.execute_advisory_workflow("nope", {}, key="k")


def test_start_paper_trading_workflow_builds_id(monkeypatch) -> None:
    import shared.temporal
    from investment_team.temporal import start_workflow as sw

    captured = {}
    monkeypatch.setattr(
        shared.temporal,
        "start_workflow_sync",
        lambda run, payload, *, workflow_id, task_queue: captured.update(
            workflow_id=workflow_id, task_queue=task_queue
        ),
    )
    sw.start_paper_trading_workflow("pt-1", {"session_id": "pt-1"})
    assert captured == {"workflow_id": "investment-pt-pt-1", "task_queue": "investment-queue"}


def test_signal_paper_trading_stop_sends_stop_signal(monkeypatch) -> None:
    import shared.temporal
    from investment_team.temporal import start_workflow as sw

    captured = {}
    monkeypatch.setattr(
        shared.temporal,
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


def test_investment_queue_worker_uses_tuned_concurrency(monkeypatch) -> None:
    """The investment-queue worker (backtest + paper-trading) must not default
    to the shared framework's 4-thread cap — a paper-trading session can hold a
    slot for hours."""
    from investment_team.strategy_lab.temporal import worker as sl_worker
    from investment_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(sl_worker, "start_strategy_lab_temporal_worker_thread", lambda: True)
    calls = {}

    def _fake_start(team, wfs, acts, *, task_queue, max_concurrent_activities=4):
        calls[team] = max_concurrent_activities
        return True

    monkeypatch.setattr(worker_mod, "start_team_worker", _fake_start)

    worker_mod.start_investment_temporal_worker_thread()

    assert calls["investment"] > 4


@pytest.mark.parametrize(
    "env_value, expected",
    [
        (None, 8),
        ("16", 16),
        ("0", 1),
        ("-5", 1),
        ("not-a-number", 8),
    ],
)
def test_max_concurrent_activities_env_parsing(monkeypatch, env_value, expected) -> None:
    from investment_team.temporal import worker as worker_mod

    if env_value is None:
        monkeypatch.delenv("INVESTMENT_MAX_CONCURRENT_ACTIVITIES", raising=False)
    else:
        monkeypatch.setenv("INVESTMENT_MAX_CONCURRENT_ACTIVITIES", env_value)

    assert worker_mod._max_concurrent_activities() == expected


# ---------------------------------------------------------------------------
# _execute_advisory / _translate_advisory_failure — error → HTTPException mapping
# ---------------------------------------------------------------------------


def test_execute_advisory_translates_application_error_by_type(monkeypatch) -> None:
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import ActivityError, ApplicationError

    import shared.temporal

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)

    def _raise(*a, **kw):
        app_err = ApplicationError("Proposal prop-1 not found", type="NotFound", non_retryable=True)
        act_err = ActivityError(
            "activity failed",
            scheduled_event_id=1,
            started_event_id=1,
            identity="",
            activity_type="",
            activity_id="",
            retry_state=None,
        )
        act_err.__cause__ = app_err
        raise WorkflowFailureError(cause=act_err)

    monkeypatch.setattr("investment_team.temporal.start_workflow.execute_advisory_workflow", _raise)

    with pytest.raises(HTTPException) as ei:
        REAL_EXECUTE_ADVISORY("validate_proposal", {}, key="prop-1")
    assert ei.value.status_code == 404
    assert "not found" in ei.value.detail


@pytest.mark.parametrize(
    "app_error_type, expected_status",
    [
        ("NotFound", 404),
        ("MissingFields", 400),
        ("NoValidation", 400),
        ("ValueError", 400),
        ("SomethingUnmapped", 500),
    ],
)
def test_translate_advisory_failure_maps_application_error_types(
    app_error_type, expected_status
) -> None:
    from temporalio.exceptions import ApplicationError

    from investment_team.api import main as api_main

    result = api_main._translate_advisory_failure(
        ApplicationError("boom", type=app_error_type, non_retryable=True)
    )
    assert result.status_code == expected_status
    assert result.detail == "boom"


def test_translate_advisory_failure_maps_workflow_already_started(monkeypatch) -> None:
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from investment_team.api import main as api_main

    err = WorkflowAlreadyStartedError("wf-1", "SomeWorkflow", run_id="r1")
    result = api_main._translate_advisory_failure(err)
    assert result.status_code == 409


def test_translate_advisory_failure_defaults_to_502_for_unknown_error() -> None:
    from investment_team.api import main as api_main

    result = api_main._translate_advisory_failure(RuntimeError("client not connected"))
    assert result.status_code == 502


def test_execute_advisory_maps_client_not_ready_runtime_error_to_503(monkeypatch) -> None:
    """``shared.temporal._await_client`` raises a bare RuntimeError when
    TEMPORAL_ADDRESS is set but no worker client ever became ready — this must
    surface as the same 503 ``_require_temporal`` raises up front, not the
    generic 502 ``_translate_advisory_failure`` defaults to for an unrecognized
    error."""
    import shared.temporal

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)

    def _raise(*a, **kw):
        raise RuntimeError("Temporal client not available; is the team's worker running?")

    monkeypatch.setattr("investment_team.temporal.start_workflow.execute_advisory_workflow", _raise)

    with pytest.raises(HTTPException) as ei:
        REAL_EXECUTE_ADVISORY("committee_memo", {}, key="k")
    assert ei.value.status_code == 503


def test_execute_advisory_passes_through_503_when_disabled_without_translation(
    monkeypatch,
) -> None:
    """_require_temporal's HTTPException(503) must not be re-wrapped by the
    generic translator."""
    import shared.temporal

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: False)
    with pytest.raises(HTTPException) as ei:
        REAL_EXECUTE_ADVISORY("committee_memo", {}, key="k")
    assert ei.value.status_code == 503
