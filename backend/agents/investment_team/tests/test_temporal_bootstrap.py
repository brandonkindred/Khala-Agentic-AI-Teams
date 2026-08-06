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

2. **Activity wiring.** The backtest activity reconstructs its request models
   and delegates to the *existing* ``_run_backtest_background`` worker. (The
   Strategy Lab batch run is no longer a coarse activity here — it is driven by
   the fine-grained ``StrategyLabBatchWorkflow`` in
   ``investment_team.strategy_lab.temporal`` on ``strategy-lab-queue``.)

3. **Dispatch branch.** ``POST /backtests`` routes through a Temporal workflow
   when ``is_temporal_enabled()`` and falls back to a daemon thread otherwise
   (target: ``temporal.start_workflow.start_backtest_workflow``).
   ``POST /strategy-lab/run`` is Temporal-only: it targets
   ``strategy_lab.temporal.start_workflow.start_strategy_lab_batch_workflow``
   and returns 503 (rolling the run to "failed") instead of falling back to a
   thread when Temporal is disabled or the dispatch fails.
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
        PaperTradingWorkflow,
    )

    # The coarse ``investment-queue`` serves the ad hoc single-backtest workflow
    # and the long-running paper-trading workflow; the Strategy Lab batch run
    # lives on ``strategy-lab-queue`` (``investment_team.strategy_lab.temporal``).
    assert WORKFLOWS == [InvestmentBacktestWorkflow, PaperTradingWorkflow]
    assert {a.__name__ for a in ACTIVITIES} == {
        "run_backtest_activity",
        "run_paper_trading_activity",
        "mark_paper_trading_stopped_activity",
    }
    assert TASK_QUEUE == "investment-queue"
    assert WORKFLOW_ID_PREFIX == "investment-"


def test_advisory_workflows_and_activities_are_registered() -> None:
    from investment_team.temporal import (
        ADVISORY_ACTIVITIES,
        ADVISORY_TASK_QUEUE,
        ADVISORY_WORKFLOW_ID_PREFIX,
        ADVISORY_WORKFLOWS,
    )

    # The interactive proposal/validation/promotion/memo/advisor workflows run on
    # their own queue so a multi-hour backtest activity can't head-of-line-block
    # a quick execute-and-wait call.
    assert len(ADVISORY_WORKFLOWS) == 9
    assert {a.__name__ for a in ADVISORY_ACTIVITIES} == {
        "create_proposal_activity",
        "validate_proposal_activity",
        "create_strategy_activity",
        "validate_strategy_activity",
        "promotion_decision_activity",
        "committee_memo_activity",
        "advisor_start_activity",
        "advisor_message_activity",
        "advisor_complete_activity",
    }
    assert ADVISORY_TASK_QUEUE == "investment-advisory-queue"
    assert ADVISORY_WORKFLOW_ID_PREFIX == "investment-adv-"


def test_importing_temporal_package_does_not_call_start_team_worker() -> None:
    """Loading the package (or its submodules) must NOT spin up a worker."""
    import shared.temporal

    _purge("investment_team.temporal")
    with mock.patch.object(shared.temporal, "start_team_worker") as patched:
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


def test_app_wires_startup_lifespan_backstop(monkeypatch) -> None:
    """The app's lifespan startup is the in-app worker backstop — the second
    start path alongside the team_service entrypoint. Keep it wired so a bare
    ``uvicorn ...:app`` run (or a swallowed entrypoint failure) still connects
    the worker client that Strategy Lab dispatch depends on.

    ``create_team_app`` wires ``on_startup`` inside an ``@asynccontextmanager``
    lifespan (it is *not* in ``app.router.on_startup``), so assert the wiring by
    actually driving the lifespan via the TestClient context manager and
    confirming the worker start fires — not merely that ``_startup`` exists."""
    from fastapi.testclient import TestClient

    from investment_team.api import main as api_main
    from investment_team.temporal import worker as worker_mod

    called = []
    monkeypatch.setattr(
        worker_mod,
        "start_investment_temporal_worker_thread",
        lambda: called.append(True) or True,
    )

    # Entering the TestClient context runs the app lifespan startup, which
    # invokes the registered on_startup hook (_startup) → the worker start.
    with TestClient(api_main.app):
        pass

    assert called == [True], "app lifespan startup did not invoke the worker backstop"


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


def test_startup_calls_paper_trading_recovery(monkeypatch) -> None:
    """``_startup()`` must itself invoke orphaned-session recovery — it is no
    longer reachable via ``@app.on_event("startup")``, which a custom
    ``lifespan=`` (set by ``create_team_app``) makes FastAPI never invoke."""
    from investment_team.api import main as api_main
    from investment_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "start_investment_temporal_worker_thread", lambda: False)
    called = []
    monkeypatch.setattr(
        api_main, "_recover_orphaned_paper_trading_sessions", lambda: called.append(True)
    )

    api_main._startup()

    assert called == [True]


def test_app_lifespan_startup_invokes_paper_trading_recovery(monkeypatch) -> None:
    """End-to-end regression for the on_event/lifespan bug: driving the app's
    actual lifespan (not calling ``_startup()`` directly) must reach the
    paper-trading recovery pass. Before the fix this hook was registered via
    the now-dead ``@app.on_event("startup")`` decorator and never ran under
    ``create_team_app``'s custom ``lifespan=``."""
    from fastapi.testclient import TestClient

    from investment_team.api import main as api_main
    from investment_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "start_investment_temporal_worker_thread", lambda: False)
    called = []
    monkeypatch.setattr(
        api_main, "_recover_orphaned_paper_trading_sessions", lambda: called.append(True)
    )

    with TestClient(api_main.app):
        pass

    assert called == [True], "app lifespan startup did not invoke paper-trading recovery"


# ---------------------------------------------------------------------------
# 2. Activity wiring
# ---------------------------------------------------------------------------


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

    def _bg(*a):
        calls.append(a)
        return api_main._BT_JOB_STATUS_COMPLETED

    monkeypatch.setattr(api_main, "_run_backtest_background", _bg)

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
    monkeypatch.setattr(api_main, "_backtest_job_status", lambda jid: None)
    monkeypatch.setattr(
        api_main,
        "_run_backtest_background",
        lambda *a: api_main._BT_JOB_STATUS_FAILED,
    )

    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError, match="failed"):
        run_backtest_activity("job-x", {}, {}, "agent", [])


def test_run_backtest_activity_reports_cancelled_status(monkeypatch) -> None:
    """A user-cancelled backtest must be reported as ``cancelled``, not the
    default ``completed`` — the job store's actual terminal status wins."""
    from investment_team import models as inv_models
    from investment_team.api import main as api_main
    from investment_team.temporal.workflows import run_backtest_activity

    monkeypatch.setattr(inv_models, "StrategySpec", lambda **kw: object())
    monkeypatch.setattr(inv_models, "BacktestConfig", lambda **kw: object())
    monkeypatch.setattr(api_main, "_backtest_job_status", lambda jid: None)
    monkeypatch.setattr(
        api_main,
        "_run_backtest_background",
        lambda *a: api_main._BT_JOB_STATUS_CANCELLED,
    )

    result = run_backtest_activity("job-cancelled", {}, {}, "agent", [])

    assert result == {"job_id": "job-cancelled", "status": "cancelled"}


# ---------------------------------------------------------------------------
# 3. Dispatch branch
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client(monkeypatch):
    from fastapi.testclient import TestClient

    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_active_runs", {})
    monkeypatch.setattr(api_main, "_persist_run_state", lambda *a, **k: None)
    return TestClient(api_main.app)


def _enable_temporal(monkeypatch, starter_name, starter, module=None):
    import shared.temporal

    if module is None:
        from investment_team.temporal import start_workflow as module

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(module, starter_name, starter)


def test_strategy_lab_dispatch_uses_temporal_when_enabled(monkeypatch, api_client) -> None:
    from investment_team.api import main as api_main
    from investment_team.strategy_lab.temporal import start_workflow as sl_sw

    started = []
    _enable_temporal(
        monkeypatch,
        "start_strategy_lab_batch_workflow",
        lambda run_id, request: started.append(run_id),
        module=sl_sw,
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


def test_strategy_lab_dispatch_returns_503_when_temporal_disabled(monkeypatch, api_client) -> None:
    """Strategy Lab dispatch is Temporal-only: a disabled Temporal 503s and
    rolls the freshly-registered run to "failed" instead of spawning a thread."""
    import shared.temporal
    from investment_team.api import main as api_main

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: False)

    resp = api_client.post(
        "/strategy-lab/run",
        json={"batch_size": 2, "batch_count": 1, "max_parallel": 1, "paper_trading_enabled": False},
    )

    assert resp.status_code == 503
    (run_state,) = api_main._active_runs.values()
    assert run_state["status"] == "failed"


def test_fail_strategy_lab_run_schedules_active_runs_cleanup(monkeypatch) -> None:
    """A dispatch failure (e.g. a Temporal outage) must not leak its
    _active_runs entry forever — repeated requests during an outage would
    otherwise grow it unboundedly until a process restart.
    _fail_strategy_lab_run schedules a 900s delayed cleanup for this."""
    from investment_team.api import main as api_main

    run_id = "run-cleanup-me"
    monkeypatch.setattr(api_main, "_active_runs", {run_id: {"run_id": run_id, "status": "running"}})
    monkeypatch.setattr(api_main, "_persist_run_state", lambda *a, **k: None)

    captured = {}

    class _FakeTimer:
        def __init__(self, delay, callback):
            captured["delay"] = delay
            captured["callback"] = callback
            self.daemon = None

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(api_main.threading, "Timer", _FakeTimer)

    api_main._fail_strategy_lab_run(run_id, "boom")

    assert api_main._active_runs[run_id]["status"] == "failed"
    assert captured["delay"] == 900.0
    assert captured["started"] is True

    # Firing the captured callback (simulating the timer elapsing) pops the entry.
    captured["callback"]()
    assert run_id not in api_main._active_runs


def test_fail_strategy_lab_run_persists_outside_the_lock(monkeypatch) -> None:
    """_persist_run_state performs a synchronous job-service RPC and must not
    run while _lock (the process-wide lock shared by run-status queries,
    dispatch, and reconciliation) is held -- otherwise that I/O blocks every
    other thread needing _lock for its duration.

    Also confirms the persisted payload is a snapshot: mutating the live
    _active_runs entry after _fail_strategy_lab_run returns must not affect
    what was captured for persistence.
    """
    from investment_team.api import main as api_main

    run_id = "run-persist-unlocked"
    state = {"run_id": run_id, "status": "running"}
    monkeypatch.setattr(api_main, "_active_runs", {run_id: state})
    monkeypatch.setattr(api_main.threading, "Timer", lambda *a, **k: mock.Mock())

    observed = {}

    def _fake_persist(rid, persisted_state, **kwargs):
        observed["run_id"] = rid
        observed["state"] = dict(persisted_state)
        observed["lock_held"] = api_main._lock.locked()

    monkeypatch.setattr(api_main, "_persist_run_state", _fake_persist)

    api_main._fail_strategy_lab_run(run_id, "boom")

    assert observed["run_id"] == run_id
    assert observed["lock_held"] is False
    assert observed["state"]["status"] == "failed"
    assert observed["state"]["error"] == "boom"

    # The live entry is mutated in place (by design -- see the function's
    # Postconditions), but the persisted snapshot was captured by value and
    # must not change if the live entry is mutated afterward.
    state["error"] = "mutated after the fact"
    assert observed["state"]["error"] == "boom"


def test_fail_strategy_lab_run_cleanup_is_noop_after_resume_supersedes_it(
    monkeypatch,
) -> None:
    """If the run gets resumed (a new state object installed at run_id)
    before the delayed cleanup fires, the stale timer must not tear down the
    resumed run's live tracking entry or its event-bus subscribers —
    otherwise a second run could start concurrently with the still-executing
    resumed workflow."""
    from investment_team.api import main as api_main

    run_id = "run-resumed-before-cleanup"
    active_runs = {run_id: {"run_id": run_id, "status": "running"}}
    monkeypatch.setattr(api_main, "_active_runs", active_runs)
    monkeypatch.setattr(api_main, "_persist_run_state", lambda *a, **k: None)

    captured = {}

    class _FakeTimer:
        def __init__(self, delay, callback):
            captured["callback"] = callback
            self.daemon = None

        def start(self):
            pass

    monkeypatch.setattr(api_main.threading, "Timer", _FakeTimer)

    api_main._fail_strategy_lab_run(run_id, "boom")
    assert active_runs[run_id]["status"] == "failed"

    # Simulate a resume: a brand-new state object replaces the failed one
    # (mirrors resume_strategy_lab_run, which never mutates in place).
    resumed_state = {"run_id": run_id, "status": "running"}
    active_runs[run_id] = resumed_state

    # The old timer fires; it must leave the resumed entry alone.
    captured["callback"]()

    assert active_runs[run_id] is resumed_state
    assert active_runs[run_id]["status"] == "running"


def test_fail_strategy_lab_run_cleanup_survives_cleanup_job_failure(monkeypatch) -> None:
    """The delayed cleanup callback runs on threading.Timer's own daemon
    thread, well after _fail_strategy_lab_run's own try/except has exited —
    that outer handler can't catch anything the callback raises. If
    cleanup_job() raises, the callback must log and swallow it rather than
    let the exception escape unhandled on the timer thread."""
    from investment_team.api import job_event_bus
    from investment_team.api import main as api_main

    run_id = "run-cleanup-job-boom"
    monkeypatch.setattr(api_main, "_active_runs", {run_id: {"run_id": run_id, "status": "running"}})
    monkeypatch.setattr(api_main, "_persist_run_state", lambda *a, **k: None)

    def _boom(_job_id):
        raise RuntimeError("event bus is on fire")

    monkeypatch.setattr(job_event_bus, "cleanup_job", _boom)

    captured = {}

    class _FakeTimer:
        def __init__(self, delay, callback):
            captured["callback"] = callback
            self.daemon = None

        def start(self):
            pass

    monkeypatch.setattr(api_main.threading, "Timer", _FakeTimer)

    api_main._fail_strategy_lab_run(run_id, "boom")

    # The callback must not raise despite cleanup_job() blowing up, and the
    # _active_runs entry must still be popped (that happens before the
    # cleanup_job() call).
    captured["callback"]()
    assert run_id not in api_main._active_runs


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
    import shared.temporal
    from investment_team.api import main as api_main

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)

    def _boom() -> None:
        raise RuntimeError("Temporal client not available")

    # A dispatch failure must be swallowed to False (never raise), so the caller
    # can fall back to its thread path.
    assert api_main._dispatch_via_temporal(_boom) is False


def test_dispatch_via_temporal_false_when_disabled(monkeypatch) -> None:
    import shared.temporal
    from investment_team.api import main as api_main

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: False)
    called = []
    assert api_main._dispatch_via_temporal(lambda: called.append(1)) is False
    assert called == []  # starter not invoked when Temporal is disabled


def test_dispatch_via_temporal_downgrades_enablement_check_error_to_false(monkeypatch) -> None:
    """``is_temporal_enabled()`` raising (not just the import failing) must
    also be swallowed to False -- regression test for the gap where only
    ``ImportError`` from the import statement was caught, leaving an
    exception from the enablement check itself free to propagate and break
    this function's documented "Never raises" contract."""
    import shared.temporal
    from investment_team.api import main as api_main

    def _boom() -> bool:
        raise RuntimeError("enablement check backend unavailable")

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", _boom)
    called = []
    assert api_main._dispatch_via_temporal(lambda: called.append(1)) is False
    assert called == []  # starter not invoked when the enablement check itself fails


def test_strategy_lab_dispatch_returns_503_on_dispatch_failure(monkeypatch, api_client) -> None:
    """A RuntimeError from the starter must not 500 or leave a stuck 'running'
    entry — Temporal-only dispatch rolls the run to 'failed' and 503s (no
    thread fallback)."""
    import shared.temporal
    from investment_team.api import main as api_main
    from investment_team.strategy_lab.temporal import start_workflow as sl_sw

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)

    def _boom(run_id, request):
        raise RuntimeError("Temporal client not available")

    monkeypatch.setattr(sl_sw, "start_strategy_lab_batch_workflow", _boom)

    resp = api_client.post(
        "/strategy-lab/run",
        json={"batch_size": 1, "batch_count": 1, "max_parallel": 1, "paper_trading_enabled": False},
    )

    assert resp.status_code == 503
    (run_state,) = api_main._active_runs.values()
    assert run_state["status"] == "failed"


def test_strategy_lab_dispatch_503_survives_workflow_already_started_error_import_failure(
    monkeypatch, api_client
) -> None:
    """If importing ``WorkflowAlreadyStartedError`` itself fails (e.g. a
    broken ``temporalio`` install), that ImportError must not mask the
    dispatch failure it's meant to help classify -- the endpoint still 503s
    instead of surfacing an unhandled ImportError.

    Regression test for the bug where this import lived inside the dispatch
    `except Exception` handler: an ImportError raised there would propagate
    straight out of the except block, in place of the intended 503.
    """
    import sys

    import shared.temporal
    from investment_team.strategy_lab.temporal import start_workflow as sl_sw

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    # Force `from temporalio.exceptions import WorkflowAlreadyStartedError` to
    # raise ImportError, regardless of whether the real module is already
    # cached -- the standard `sys.modules[name] = None` technique.
    monkeypatch.setitem(sys.modules, "temporalio.exceptions", None)

    def _boom(run_id, request):
        raise RuntimeError("Temporal client not available")

    monkeypatch.setattr(sl_sw, "start_strategy_lab_batch_workflow", _boom)

    resp = api_client.post(
        "/strategy-lab/run",
        json={"batch_size": 1, "batch_count": 1, "max_parallel": 1, "paper_trading_enabled": False},
    )

    assert resp.status_code == 503


def test_strategy_lab_dispatch_treats_already_started_workflow_as_success(
    monkeypatch, api_client
) -> None:
    """A collision with an already-running workflow (e.g. resume issued after
    an API-process restart, while the durable workflow itself survived) must
    be treated as a successful dispatch, not a failure — marking the run
    "failed" here would be observed by that still-running workflow as an
    external stop signal and abort a healthy run."""
    from temporalio.exceptions import WorkflowAlreadyStartedError

    import shared.temporal
    from investment_team.api import main as api_main
    from investment_team.strategy_lab.temporal import start_workflow as sl_sw

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)

    def _already_started(run_id, request):
        raise WorkflowAlreadyStartedError(
            workflow_id=f"strategy-lab-{run_id}", run_id="prior-run", workflow_type="X"
        )

    monkeypatch.setattr(sl_sw, "start_strategy_lab_batch_workflow", _already_started)

    resp = api_client.post(
        "/strategy-lab/run",
        json={"batch_size": 1, "batch_count": 1, "max_parallel": 1, "paper_trading_enabled": False},
    )

    assert resp.status_code == 200
    (run_state,) = api_main._active_runs.values()
    assert run_state["status"] == "running"


def test_strategy_lab_restart_returns_409_on_workflow_already_started(
    monkeypatch, api_client
) -> None:
    """Restart's collision differs from resume's: a WorkflowAlreadyStartedError
    means an old, un-reset execution is still running (not that the intended
    restart is already in flight), so it must 409 rather than silently
    succeed (which would misreport a from-scratch restart) or mark the run
    failed (which would abort a still-healthy old execution).

    Termination is mocked as already-succeeded here so this test exercises
    the residual dispatch-collision path (a second restart/resume racing in
    right after the terminate-and-wait step confirms the old execution
    closed) rather than the terminate-first 409/503 paths, which have their
    own dedicated tests."""
    from temporalio.exceptions import WorkflowAlreadyStartedError

    import shared.temporal
    from investment_team.api import main as api_main
    from investment_team.strategy_lab.temporal import start_workflow as sl_sw

    run_id = "run-restart-collide"
    persisted = {
        "run_id": run_id,
        "status": "cancelled",
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }
    monkeypatch.setattr(api_main, "_get_run_state", lambda rid: persisted)
    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(shared.temporal, "terminate_and_await_workflow_sync", lambda *a, **k: None)

    persisted_calls = []
    monkeypatch.setattr(
        api_main,
        "_persist_run_state",
        lambda rid, state, **k: persisted_calls.append(state),
    )

    def _already_started(rid, request):
        raise WorkflowAlreadyStartedError(
            workflow_id=f"strategy-lab-{rid}", run_id="prior-run", workflow_type="X"
        )

    monkeypatch.setattr(sl_sw, "start_strategy_lab_batch_workflow", _already_started)

    resp = api_client.post(f"/strategy-lab/runs/{run_id}/restart")

    assert resp.status_code == 409
    (run_state,) = api_main._active_runs.values()
    assert run_state["status"] != "failed"
    # The optimistic reset (status "running", contiguous_cycles=0) must be
    # rolled back to the pre-restart snapshot, not left wedged as "running"
    # (which would block every future run/resume/restart via
    # _ensure_no_active_run()).
    assert run_state is persisted
    assert run_state["status"] == "cancelled"
    assert persisted_calls[-1] is persisted


def test_strategy_lab_restart_returns_409_when_termination_times_out(
    monkeypatch, api_client
) -> None:
    """restart resolves any prior execution BEFORE writing any state, so a
    termination that can't be confirmed within budget must 409 without ever
    touching _active_runs or the persisted store — a stronger guarantee than
    the post-hoc rollback the residual dispatch-collision path needs."""
    import shared.temporal
    from investment_team.api import main as api_main

    run_id = "run-restart-term-timeout"
    persisted = {
        "run_id": run_id,
        "status": "cancelled",
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }
    monkeypatch.setattr(api_main, "_get_run_state", lambda rid: persisted)
    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)

    def _times_out(workflow_id, **kwargs):
        raise TimeoutError("termination not confirmed")

    monkeypatch.setattr(shared.temporal, "terminate_and_await_workflow_sync", _times_out)

    persist_calls = []
    monkeypatch.setattr(
        api_main, "_persist_run_state", lambda rid, state, **k: persist_calls.append(state)
    )

    resp = api_client.post(f"/strategy-lab/runs/{run_id}/restart")

    assert resp.status_code == 409
    assert api_main._active_runs == {}  # never written
    assert persist_calls == []  # never written


def test_strategy_lab_restart_returns_503_when_termination_fails(monkeypatch, api_client) -> None:
    """A non-timeout failure resolving the prior execution (e.g. the worker
    client never connects) surfaces as 503, distinct from the 409 'retry
    shortly' case — nothing is written here either."""
    import shared.temporal
    from investment_team.api import main as api_main

    run_id = "run-restart-term-error"
    persisted = {
        "run_id": run_id,
        "status": "cancelled",
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }
    monkeypatch.setattr(api_main, "_get_run_state", lambda rid: persisted)
    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)

    def _boom(workflow_id, **kwargs):
        raise RuntimeError("Temporal client not available")

    monkeypatch.setattr(shared.temporal, "terminate_and_await_workflow_sync", _boom)

    persist_calls = []
    monkeypatch.setattr(
        api_main, "_persist_run_state", lambda rid, state, **k: persist_calls.append(state)
    )

    resp = api_client.post(f"/strategy-lab/runs/{run_id}/restart")

    assert resp.status_code == 503
    assert api_main._active_runs == {}  # never written
    assert persist_calls == []


def test_backtest_dispatch_falls_back_to_thread_on_dispatch_failure(
    monkeypatch, api_client
) -> None:
    """Finding 5 regression: a starter RuntimeError falls back to the thread so
    the created job still runs (not orphaned at 'pending')."""
    import shared.temporal
    from investment_team.api import main as api_main
    from investment_team.models import StrategySpec
    from investment_team.temporal import start_workflow as sw

    strat = StrategySpec.model_construct(strategy_id="strat-x")
    monkeypatch.setattr(api_main, "_strategies", {"strat-x": {"strategy_id": "strat-x"}})
    monkeypatch.setattr(api_main.StrategySpec, "parse_persisted", staticmethod(lambda s: strat))
    monkeypatch.setattr(api_main, "_bt_create_job", lambda *a, **k: None)
    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)

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
    thread_ctor.return_value.start.assert_called_once()
    _, kwargs = thread_ctor.call_args
    assert kwargs["target"] is api_main._run_backtest_background
    args = kwargs["args"]
    assert args[0] == resp.json()["job_id"]
    assert args[1] is strat
    assert args[3] == "agent-1"


def test_rehydrate_active_run_offset_repopulates_from_job_store(monkeypatch) -> None:
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "active_runs", {})
    monkeypatch.setattr(
        run_state,
        "load_run_from_job_service",
        lambda rid: {"run_id": rid, "status": "running", "contiguous_cycles": 4},
    )

    offset = run_state.rehydrate_active_run_offset("run-k")

    assert offset == 4
    # The in-memory entry is rehydrated so _update_run can persist progress.
    assert run_state.active_runs["run-k"]["contiguous_cycles"] == 4


def test_rehydrate_active_run_offset_defaults_to_zero(monkeypatch) -> None:
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "active_runs", {})
    monkeypatch.setattr(run_state, "load_run_from_job_service", lambda rid: None)

    assert run_state.rehydrate_active_run_offset("missing") == 0


def test_rehydrate_active_run_offset_propagates_job_service_error(monkeypatch) -> None:
    """A genuine job-service failure during resume must fail closed, not
    silently resolve the offset to 0 as if the run never existed -- that
    would replay every already-completed cycle."""
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "active_runs", {})

    def _broken(rid):
        raise RuntimeError("backend down")

    monkeypatch.setattr(run_state, "load_run_from_job_service", _broken)

    with pytest.raises(RuntimeError, match="backend down"):
        run_state.rehydrate_active_run_offset("run-broken")


def test_get_resume_seed_counters_propagates_job_service_error(monkeypatch) -> None:
    """Mirrors the offset case: a job-service failure must not be seeded as
    zero/empty resume counters."""
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "active_runs", {})

    def _broken(rid):
        raise RuntimeError("backend down")

    monkeypatch.setattr(run_state, "load_run_from_job_service", _broken)

    with pytest.raises(RuntimeError, match="backend down"):
        run_state.get_resume_seed_counters("run-broken")


def test_get_resume_seed_counters_defaults_for_unknown_run(monkeypatch) -> None:
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "active_runs", {})
    monkeypatch.setattr(run_state, "load_run_from_job_service", lambda rid: None)

    assert run_state.get_resume_seed_counters("missing") == {
        "skipped_cycles": 0,
        "errored_cycles": 0,
        "errored_details": [],
        "tracker_merge_error_count": 0,
        "completed_record_ids": [],
    }


def test_get_resume_seed_counters_reads_persisted_values(monkeypatch) -> None:
    from investment_team.strategy_lab import run_state

    details = [{"cycle_index": 1, "error": "boom"}]
    record_ids = ["r1", "r2"]
    monkeypatch.setattr(
        run_state,
        "active_runs",
        {
            "run-c": {
                "run_id": "run-c",
                "status": "interrupted",
                "skipped_cycles": 3,
                "errored_cycles": 2,
                "errored_details": details,
                "tracker_merge_error_count": 1,
                "completed_record_ids": record_ids,
            }
        },
    )

    seeded = run_state.get_resume_seed_counters("run-c")

    assert seeded == {
        "skipped_cycles": 3,
        "errored_cycles": 2,
        "errored_details": details,
        "tracker_merge_error_count": 1,
        "completed_record_ids": record_ids,
    }
    # A fresh list, not an alias — the caller mustn't be able to mutate the
    # live in-memory run state through the returned dict.
    assert seeded["errored_details"] is not details
    assert seeded["completed_record_ids"] is not record_ids


def test_get_resume_seed_counters_defensive_against_malformed_values(monkeypatch) -> None:
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(
        run_state,
        "active_runs",
        {
            "run-d": {
                "run_id": "run-d",
                "status": "failed",
                "skipped_cycles": "not-a-number",
                "errored_cycles": None,
                "errored_details": "not-a-list",
                "tracker_merge_error_count": -5,
                "completed_record_ids": "not-a-list-either",
            }
        },
    )

    seeded = run_state.get_resume_seed_counters("run-d")

    assert seeded["skipped_cycles"] == 0
    assert seeded["errored_cycles"] == 0
    assert seeded["errored_details"] == []
    assert seeded["tracker_merge_error_count"] == 0  # clamped, matches rehydrate's max(0, ...)
    assert seeded["completed_record_ids"] == []


def test_strategy_lab_run_failure_reports_only_hard_failure(monkeypatch) -> None:
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "active_runs", {"r": {"status": "failed", "error": "kaboom"}})
    assert api_main._strategy_lab_run_failure("r") == "kaboom"

    monkeypatch.setattr(run_state, "active_runs", {"r": {"status": "completed_with_errors"}})
    assert api_main._strategy_lab_run_failure("r") is None


def test_resume_dispatches_via_temporal_when_enabled(monkeypatch, api_client) -> None:
    """Finding 6: resume must also route through Temporal when enabled."""
    import shared.temporal
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state
    from investment_team.strategy_lab.temporal import start_workflow as sl_sw

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
    # ``resume`` looks the run up via ``_get_run_state``, whose durable fallback
    # is ``run_state.load_run_from_job_service`` — patch that (not the api.main alias).
    monkeypatch.setattr(run_state, "load_run_from_job_service", lambda rid: dict(state))
    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    started = []
    monkeypatch.setattr(
        sl_sw, "start_strategy_lab_batch_workflow", lambda rid, req: started.append(rid)
    )
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
    import shared.temporal
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state
    from investment_team.strategy_lab.temporal import start_workflow as sl_sw

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
    # ``restart`` looks the run up via ``_get_run_state``, whose durable fallback
    # is ``run_state.load_run_from_job_service`` — patch that (not the api.main alias).
    monkeypatch.setattr(run_state, "load_run_from_job_service", lambda rid: dict(state))
    persisted = {}
    monkeypatch.setattr(api_main, "_persist_run_state", lambda rid, s, **k: persisted.update(s))
    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(shared.temporal, "terminate_and_await_workflow_sync", lambda *a, **k: None)
    started = []
    monkeypatch.setattr(
        sl_sw, "start_strategy_lab_batch_workflow", lambda rid, req: started.append(rid)
    )
    thread_ctor = mock.Mock()
    monkeypatch.setattr(api_main.threading, "Thread", thread_ctor)

    resp = api_client.post("/strategy-lab/runs/run-x/restart")

    assert resp.status_code == 200
    assert started == ["run-x"]
    thread_ctor.assert_not_called()
    assert persisted["contiguous_cycles"] == 0  # reset so the activity restarts from 0
