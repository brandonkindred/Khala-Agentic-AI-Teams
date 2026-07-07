"""Tests for the investment team Temporal wiring.

Covers three things the runtime depends on:

1. **Bootstrap contract.** Importing ``investment_team.temporal`` must have NO
   side effects — in particular it must not call ``start_team_worker`` at import
   time. A module-level self-boot both races the first request (the worker
   connects its client asynchronously) and trips the temporalio workflow sandbox
   when it re-imports the module to register the workflows. Worker startup has
   two paths, both via
   ``investment_team.temporal.worker.start_investment_temporal_worker_thread``:
   the team_service entrypoint (before uvicorn accepts requests) and the app's
   ``on_startup`` lifespan backstop (covering standalone ``uvicorn ...:app`` runs
   and a wrapper start that silently failed).

2. **Activity wiring.** The two activities reconstruct their request models and
   delegate to the *existing* background workers (``_strategy_lab_worker`` /
   ``_run_backtest_background``).

3. **Dispatch branch.** ``POST /strategy-lab/run`` and ``POST /backtests`` route
   through a Temporal workflow when ``is_temporal_enabled()`` and fall back to a
   daemon thread otherwise.
"""

from __future__ import annotations

import importlib
import sys
import unittest.mock as mock

import pytest


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


# ---------------------------------------------------------------------------
# 1. Bootstrap contract
# ---------------------------------------------------------------------------


def test_workflows_and_activities_are_registered() -> None:
    from investment_team.temporal import (
        ACTIVITIES,
        TASK_QUEUE,
        WORKFLOW_ID_PREFIX,
        WORKFLOWS,
        InvestmentBacktestWorkflow,
        InvestmentStrategyLabWorkflow,
    )

    assert WORKFLOWS == [InvestmentStrategyLabWorkflow, InvestmentBacktestWorkflow]
    assert len(ACTIVITIES) == 2
    assert {a.__name__ for a in ACTIVITIES} == {
        "run_strategy_lab_activity",
        "run_backtest_activity",
    }
    assert TASK_QUEUE == "investment-queue"
    assert WORKFLOW_ID_PREFIX == "investment-"


def test_importing_temporal_package_does_not_call_start_team_worker() -> None:
    """Loading the package (or its submodules) must NOT spin up a worker."""
    import shared_temporal

    _purge("investment_team.temporal")
    with mock.patch.object(shared_temporal, "start_team_worker") as patched:
        importlib.import_module("investment_team.temporal")
        importlib.import_module("investment_team.temporal.workflows")
        importlib.import_module("investment_team.temporal.start_workflow")
        assert patched.call_count == 0, (
            "Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}). This races the first request "
            "and trips the temporalio sandbox os.getenv restriction when the "
            "workflow registers."
        )


def test_temporal_package_init_does_not_call_os_getenv() -> None:
    """The temporalio sandbox replays the package __init__ during workflow
    registration; any ``os.getenv`` there aborts registration."""
    import os

    _purge("investment_team.temporal")
    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("investment_team.temporal.workflows")
        spy.reset_mock()
        importlib.import_module("investment_team.temporal")
        assert spy.call_count == 0, (
            f"investment_team.temporal.__init__ called os.getenv {spy.call_count} "
            "time(s) at import — this trips the temporalio workflow sandbox."
        )


def test_worker_module_exposes_team_service_entrypoint() -> None:
    """team_service/entrypoint.py looks up ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE``. Keep the contract pinned so a rename can't
    silently break docker-compose."""
    from investment_team.temporal import worker

    fn = getattr(worker, "start_investment_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg "
        "start_investment_temporal_worker_thread() in investment_team.temporal.worker"
    )


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from investment_team.temporal.worker import start_investment_temporal_worker_thread

    assert start_investment_temporal_worker_thread() is False


def test_app_wires_startup_lifespan_backstop() -> None:
    """The app's ``on_startup`` hook is the in-app worker backstop — the second
    start path alongside the team_service entrypoint. Keep it wired so a bare
    ``uvicorn ...:app`` run (or a swallowed entrypoint failure) still connects
    the worker client that Strategy Lab dispatch depends on."""
    from investment_team.api import main as api_main

    assert callable(getattr(api_main, "_startup", None)), (
        "investment_team.api.main._startup lifespan backstop is missing"
    )


def test_startup_backstop_starts_worker(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal import worker as worker_mod

    called = []
    monkeypatch.setattr(
        worker_mod,
        "start_investment_temporal_worker_thread",
        lambda: called.append(True) or True,
    )

    api_main._startup()

    assert called == [True]


def test_startup_backstop_swallows_worker_error(monkeypatch) -> None:
    """A raising worker start must NOT abort app boot — the backstop logs and
    returns (it runs as an ``on_startup`` hook)."""
    from investment_team.api import main as api_main
    from investment_team.temporal import worker as worker_mod

    def _boom() -> bool:
        raise RuntimeError("worker connect failed")

    monkeypatch.setattr(worker_mod, "start_investment_temporal_worker_thread", _boom)

    api_main._startup()  # must not raise


# ---------------------------------------------------------------------------
# 2. Activity wiring
# ---------------------------------------------------------------------------


def test_run_strategy_lab_activity_reconstructs_request_and_runs_worker(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.workflows import run_strategy_lab_activity

    sentinel_request = object()
    built = {}

    def _fake_request(**kwargs):
        built.update(kwargs)
        return sentinel_request

    calls = []
    monkeypatch.setattr(api_main, "RunStrategyLabRequest", _fake_request)
    monkeypatch.setattr(
        api_main,
        "_strategy_lab_worker",
        lambda run_id, req, start_cycle_offset=0: calls.append((run_id, req, start_cycle_offset)),
    )
    # No durable state for this run_id → offset 0, no failure.
    monkeypatch.setattr(api_main, "_rehydrate_active_run_offset", lambda rid: 0)
    monkeypatch.setattr(api_main, "_strategy_lab_run_failure", lambda rid: None)

    result = run_strategy_lab_activity("run-abc", {"batch_size": 3})

    assert built == {"batch_size": 3}
    assert calls == [("run-abc", sentinel_request, 0)]
    assert result == {"run_id": "run-abc", "status": "completed"}


def test_run_strategy_lab_activity_resumes_from_offset_and_raises_on_failure(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.workflows import run_strategy_lab_activity

    monkeypatch.setattr(api_main, "RunStrategyLabRequest", lambda **kw: object())
    monkeypatch.setattr(api_main, "_rehydrate_active_run_offset", lambda rid: 7)
    seen = {}
    monkeypatch.setattr(
        api_main,
        "_strategy_lab_worker",
        lambda run_id, req, start_cycle_offset=0: seen.update(offset=start_cycle_offset),
    )
    # Worker ended in a hard-failed state → activity must raise so Temporal sees it.
    monkeypatch.setattr(api_main, "_strategy_lab_run_failure", lambda rid: "boom")

    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError, match="boom"):
        run_strategy_lab_activity("run-z", {})

    assert seen == {"offset": 7}  # resumed from the persisted offset


def test_run_backtest_activity_reconstructs_models_and_runs_worker(monkeypatch) -> None:
    from investment_team import models as inv_models
    from investment_team.api import main as api_main
    from investment_team.temporal.workflows import run_backtest_activity

    strat_sentinel = object()
    cfg_sentinel = object()
    monkeypatch.setattr(inv_models, "StrategySpec", lambda **kw: strat_sentinel)
    monkeypatch.setattr(inv_models, "BacktestConfig", lambda **kw: cfg_sentinel)
    # Job is not yet complete/failed → run, then report completed.
    monkeypatch.setattr(api_main, "_backtest_job_status", lambda jid: None)

    calls = []
    monkeypatch.setattr(
        api_main,
        "_run_backtest_background",
        lambda *a: calls.append(a),
    )

    result = run_backtest_activity(
        "job-1", {"strategy_id": "s"}, {"start_date": "2024-01-01"}, "agent-x", ["note"]
    )

    assert calls == [("job-1", strat_sentinel, cfg_sentinel, "agent-x", ["note"])]
    assert result == {"job_id": "job-1", "status": "completed"}


def test_run_backtest_activity_is_idempotent_when_already_completed(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.temporal.workflows import run_backtest_activity

    monkeypatch.setattr(
        api_main, "_backtest_job_status", lambda jid: api_main._BT_JOB_STATUS_COMPLETED
    )
    bg = mock.Mock()
    monkeypatch.setattr(api_main, "_run_backtest_background", bg)

    result = run_backtest_activity("job-done", {}, {}, "agent", [])

    bg.assert_not_called()  # already completed → no recompute, no duplicate record
    assert result == {"job_id": "job-done", "status": "completed"}


def test_run_backtest_activity_raises_when_job_failed(monkeypatch) -> None:
    from investment_team import models as inv_models
    from investment_team.api import main as api_main
    from investment_team.temporal.workflows import run_backtest_activity

    monkeypatch.setattr(inv_models, "StrategySpec", lambda **kw: object())
    monkeypatch.setattr(inv_models, "BacktestConfig", lambda **kw: object())
    # Not completed at entry, failed after the worker runs.
    statuses = iter([None, api_main._BT_JOB_STATUS_FAILED])
    monkeypatch.setattr(api_main, "_backtest_job_status", lambda jid: next(statuses))
    monkeypatch.setattr(api_main, "_run_backtest_background", lambda *a: None)

    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError, match="failed"):
        run_backtest_activity("job-x", {}, {}, "agent", [])


# ---------------------------------------------------------------------------
# 3. Dispatch branch
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client(monkeypatch):
    from fastapi.testclient import TestClient

    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_active_runs", {})
    monkeypatch.setattr(api_main, "_persist_run_state", lambda *a, **k: None)
    monkeypatch.setattr(api_main, "_strategy_lab_worker", lambda *a, **k: None)
    return TestClient(api_main.app)


def _enable_temporal(monkeypatch, starter_name, starter):
    import shared_temporal
    from investment_team.temporal import start_workflow as sw

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(sw, starter_name, starter)


def test_strategy_lab_dispatch_uses_temporal_when_enabled(monkeypatch, api_client) -> None:
    from investment_team.api import main as api_main

    started = []
    _enable_temporal(
        monkeypatch,
        "start_strategy_lab_workflow",
        lambda run_id, request: started.append(run_id),
    )
    thread_ctor = mock.Mock()
    monkeypatch.setattr(api_main.threading, "Thread", thread_ctor)

    resp = api_client.post(
        "/strategy-lab/run",
        json={"batch_size": 2, "batch_count": 1, "max_parallel": 1, "paper_trading_enabled": False},
    )

    assert resp.status_code == 200
    assert len(started) == 1 and started[0].startswith("run-")
    thread_ctor.assert_not_called()


def test_strategy_lab_dispatch_uses_thread_when_disabled(monkeypatch, api_client) -> None:
    import shared_temporal
    from investment_team.api import main as api_main

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: False)
    thread_ctor = mock.Mock()
    monkeypatch.setattr(api_main.threading, "Thread", thread_ctor)

    resp = api_client.post(
        "/strategy-lab/run",
        json={"batch_size": 2, "batch_count": 1, "max_parallel": 1, "paper_trading_enabled": False},
    )

    assert resp.status_code == 200
    thread_ctor.assert_called_once()


def test_backtest_dispatch_uses_temporal_when_enabled(monkeypatch, api_client) -> None:
    from investment_team.api import main as api_main
    from investment_team.models import StrategySpec

    strat = StrategySpec.model_construct(strategy_id="strat-x")
    monkeypatch.setattr(api_main, "_strategies", {"strat-x": {"strategy_id": "strat-x"}})
    monkeypatch.setattr(api_main.StrategySpec, "parse_persisted", staticmethod(lambda s: strat))
    monkeypatch.setattr(api_main, "_bt_create_job", lambda *a, **k: None)
    bg = mock.Mock()
    monkeypatch.setattr(api_main, "_run_backtest_background", bg)

    started = []
    _enable_temporal(
        monkeypatch,
        "start_backtest_workflow",
        lambda job_id, strategy, config, submitted_by, notes: started.append(job_id),
    )
    thread_ctor = mock.Mock()
    monkeypatch.setattr(api_main.threading, "Thread", thread_ctor)

    resp = api_client.post(
        "/backtests",
        json={
            "strategy_id": "strat-x",
            "submitted_by": "agent-1",
            "start_date": "2024-01-01",
            "end_date": "2024-02-01",
        },
    )

    assert resp.status_code == 200
    assert len(started) == 1
    thread_ctor.assert_not_called()
    bg.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Graceful degradation on dispatch failure + durability helpers
# ---------------------------------------------------------------------------


def test_dispatch_via_temporal_downgrades_starter_error_to_false(monkeypatch) -> None:
    import shared_temporal
    from investment_team.api import main as api_main

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)

    def _boom() -> None:
        raise RuntimeError("Temporal client not available")

    # A dispatch failure must be swallowed to False (never raise), so the caller
    # can fall back to its thread path.
    assert api_main._dispatch_via_temporal(_boom) is False


def test_dispatch_via_temporal_false_when_disabled(monkeypatch) -> None:
    import shared_temporal
    from investment_team.api import main as api_main

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: False)
    called = []
    assert api_main._dispatch_via_temporal(lambda: called.append(1)) is False
    assert called == []  # starter not invoked when Temporal is disabled


def test_strategy_lab_dispatch_falls_back_to_thread_on_dispatch_failure(
    monkeypatch, api_client
) -> None:
    """Finding 1 regression: a RuntimeError from the starter must NOT 500 or
    leave a stuck 'running' entry — it falls back to the daemon thread."""
    import shared_temporal
    from investment_team.api import main as api_main
    from investment_team.temporal import start_workflow as sw

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)

    def _boom(run_id, request):
        raise RuntimeError("Temporal client not available")

    monkeypatch.setattr(sw, "start_strategy_lab_workflow", _boom)
    thread_ctor = mock.Mock()
    monkeypatch.setattr(api_main.threading, "Thread", thread_ctor)

    resp = api_client.post(
        "/strategy-lab/run",
        json={"batch_size": 1, "batch_count": 1, "max_parallel": 1, "paper_trading_enabled": False},
    )

    assert resp.status_code == 200  # not a 500
    thread_ctor.assert_called_once()  # fell back to the thread path


def test_backtest_dispatch_falls_back_to_thread_on_dispatch_failure(
    monkeypatch, api_client
) -> None:
    """Finding 5 regression: a starter RuntimeError falls back to the thread so
    the created job still runs (not orphaned at 'pending')."""
    import shared_temporal
    from investment_team.api import main as api_main
    from investment_team.models import StrategySpec
    from investment_team.temporal import start_workflow as sw

    strat = StrategySpec.model_construct(strategy_id="strat-x")
    monkeypatch.setattr(api_main, "_strategies", {"strat-x": {"strategy_id": "strat-x"}})
    monkeypatch.setattr(api_main.StrategySpec, "parse_persisted", staticmethod(lambda s: strat))
    monkeypatch.setattr(api_main, "_bt_create_job", lambda *a, **k: None)
    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)

    def _boom(*a):
        raise RuntimeError("Temporal client not available")

    monkeypatch.setattr(sw, "start_backtest_workflow", _boom)
    thread_ctor = mock.Mock()
    monkeypatch.setattr(api_main.threading, "Thread", thread_ctor)

    resp = api_client.post(
        "/backtests",
        json={
            "strategy_id": "strat-x",
            "submitted_by": "agent-1",
            "start_date": "2024-01-01",
            "end_date": "2024-02-01",
        },
    )

    assert resp.status_code == 200  # not a 500
    thread_ctor.assert_called_once()  # fell back to the thread path


def test_rehydrate_active_run_offset_repopulates_from_job_store(monkeypatch) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_active_runs", {})
    monkeypatch.setattr(
        api_main,
        "_load_run_from_job_service",
        lambda rid: {"run_id": rid, "status": "running", "contiguous_cycles": 4},
    )

    offset = api_main._rehydrate_active_run_offset("run-k")

    assert offset == 4
    # The in-memory entry is rehydrated so _update_run can persist progress.
    assert api_main._active_runs["run-k"]["contiguous_cycles"] == 4


def test_rehydrate_active_run_offset_defaults_to_zero(monkeypatch) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_active_runs", {})
    monkeypatch.setattr(api_main, "_load_run_from_job_service", lambda rid: None)

    assert api_main._rehydrate_active_run_offset("missing") == 0


def test_strategy_lab_run_failure_reports_only_hard_failure(monkeypatch) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_active_runs", {"r": {"status": "failed", "error": "kaboom"}})
    assert api_main._strategy_lab_run_failure("r") == "kaboom"

    monkeypatch.setattr(api_main, "_active_runs", {"r": {"status": "completed_with_errors"}})
    assert api_main._strategy_lab_run_failure("r") is None


def test_resume_dispatches_via_temporal_when_enabled(monkeypatch, api_client) -> None:
    """Finding 6: resume must also route through Temporal when enabled."""
    import shared_temporal
    from investment_team.api import main as api_main
    from investment_team.temporal import start_workflow as sw

    state = {
        "run_id": "run-r",
        "status": "interrupted",
        "request_payload": {
            "batch_size": 2,
            "batch_count": 2,
            "max_parallel": 1,
            "paper_trading_enabled": False,
        },
        "completed_cycles": 2,
        "contiguous_cycles": 2,
    }
    monkeypatch.setattr(api_main, "_load_run_from_job_service", lambda rid: dict(state))
    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    started = []
    monkeypatch.setattr(sw, "start_strategy_lab_workflow", lambda rid, req: started.append(rid))
    thread_ctor = mock.Mock()
    monkeypatch.setattr(api_main.threading, "Thread", thread_ctor)

    resp = api_client.post("/strategy-lab/runs/run-r/resume")

    assert resp.status_code == 200
    assert started == ["run-r"]
    thread_ctor.assert_not_called()


def test_restart_dispatches_via_temporal_and_resets_offset(monkeypatch, api_client) -> None:
    """Finding 6 + restart offset reset: restart routes through Temporal and the
    persisted state it writes must carry contiguous_cycles=0 so the activity
    re-runs from scratch."""
    import shared_temporal
    from investment_team.api import main as api_main
    from investment_team.temporal import start_workflow as sw

    state = {
        "run_id": "run-x",
        "status": "completed",
        "request_payload": {
            "batch_size": 2,
            "batch_count": 1,
            "max_parallel": 1,
            "paper_trading_enabled": False,
        },
        "contiguous_cycles": 2,
    }
    monkeypatch.setattr(api_main, "_load_run_from_job_service", lambda rid: dict(state))
    persisted = {}
    monkeypatch.setattr(api_main, "_persist_run_state", lambda rid, s, **k: persisted.update(s))
    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    started = []
    monkeypatch.setattr(sw, "start_strategy_lab_workflow", lambda rid, req: started.append(rid))
    thread_ctor = mock.Mock()
    monkeypatch.setattr(api_main.threading, "Thread", thread_ctor)

    resp = api_client.post("/strategy-lab/runs/run-x/restart")

    assert resp.status_code == 200
    assert started == ["run-x"]
    thread_ctor.assert_not_called()
    assert persisted["contiguous_cycles"] == 0  # reset so the activity restarts from 0
