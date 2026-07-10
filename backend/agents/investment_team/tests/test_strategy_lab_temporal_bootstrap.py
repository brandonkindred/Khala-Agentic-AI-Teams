"""Bootstrap tests for the ``strategy_lab.temporal`` Pattern-A package.

Covers the package exports, the worker boot helper, and the sync dispatch helper
— all thin wrappers with lazy imports, mocked at the ``shared_temporal`` /
``api.main`` boundary so no live Temporal server or job service is needed.
"""

from __future__ import annotations

import sys


def test_package_exports():
    import investment_team.strategy_lab.temporal as pkg
    from investment_team.strategy_lab.temporal import workflows as wf

    assert pkg.TASK_QUEUE == "strategy-lab-queue"
    assert pkg.WORKFLOW_ID_PREFIX == "strategy-lab-"
    assert pkg.WORKFLOWS == [wf.StrategyLabCycleWorkflow, wf.StrategyLabBatchWorkflow]
    assert pkg.ACTIVITIES is wf.ACTIVITIES
    assert pkg.StrategyLabBatchWorkflow is wf.StrategyLabBatchWorkflow


def test_importing_package_starts_no_worker(monkeypatch):
    """Importing the package must have zero side effects (no worker boot) so the
    temporalio sandbox can safely re-import it.

    The re-import is done in isolation and the original module objects are
    restored afterward, so this test never pollutes the class identities other
    tests (and temporalio's ``@workflow.defn`` registry) depend on.
    """
    import shared_temporal

    started: list = []
    monkeypatch.setattr(shared_temporal, "start_team_worker", lambda *a, **k: started.append(a))

    prefix = "investment_team.strategy_lab.temporal"
    saved = {n: m for n, m in sys.modules.items() if n == prefix or n.startswith(prefix + ".")}
    for name in saved:
        del sys.modules[name]
    try:
        import investment_team.strategy_lab.temporal  # noqa: F401

        assert started == []
    finally:
        for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
            del sys.modules[name]
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# worker.py
# ---------------------------------------------------------------------------


def test_worker_returns_false_when_temporal_disabled(monkeypatch):
    import shared_temporal
    from investment_team.strategy_lab.temporal.worker import (
        start_strategy_lab_temporal_worker_thread,
    )

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: False)
    called: list = []
    monkeypatch.setattr(shared_temporal, "start_team_worker", lambda *a, **k: called.append(a))
    assert start_strategy_lab_temporal_worker_thread() is False
    assert called == []


def test_worker_starts_on_strategy_lab_queue_when_enabled(monkeypatch):
    import shared_temporal
    from investment_team.strategy_lab.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
    from investment_team.strategy_lab.temporal.worker import (
        start_strategy_lab_temporal_worker_thread,
    )

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue, max_concurrent_activities):
        captured.update(
            team=team,
            workflows=workflows,
            activities=activities,
            task_queue=task_queue,
            max_concurrent_activities=max_concurrent_activities,
        )
        return True

    monkeypatch.setattr(shared_temporal, "start_team_worker", _fake_start)
    monkeypatch.setenv("STRATEGY_LAB_MAX_CONCURRENT_ACTIVITIES", "12")

    assert start_strategy_lab_temporal_worker_thread() is True
    assert captured["team"] == "investment_strategy_lab"
    assert captured["task_queue"] == TASK_QUEUE == "strategy-lab-queue"
    assert captured["workflows"] is WORKFLOWS
    assert captured["activities"] is ACTIVITIES
    assert captured["max_concurrent_activities"] == 12


def test_worker_max_concurrent_activities_defaults_and_clamps(monkeypatch):
    from investment_team.strategy_lab.temporal.worker import _max_concurrent_activities

    monkeypatch.delenv("STRATEGY_LAB_MAX_CONCURRENT_ACTIVITIES", raising=False)
    assert _max_concurrent_activities() == 8
    monkeypatch.setenv("STRATEGY_LAB_MAX_CONCURRENT_ACTIVITIES", "garbage")
    assert _max_concurrent_activities() == 8
    monkeypatch.setenv("STRATEGY_LAB_MAX_CONCURRENT_ACTIVITIES", "-3")
    assert _max_concurrent_activities() == 1


# ---------------------------------------------------------------------------
# start_workflow.py
# ---------------------------------------------------------------------------


class _FakeRequest:
    start_date = "2023-01-01"
    end_date = "2023-12-31"
    initial_capital = 100000.0
    benchmark_symbol = "SPY"
    transaction_cost_bps = 5.0
    slippage_bps = 2.0
    batch_size = 3
    batch_count = 2
    max_parallel = 5
    allowed_asset_classes = None
    paper_trading_enabled = True
    paper_trading_lookback_days = 200


def test_build_batch_input_maps_request(monkeypatch):
    from investment_team.api import main as api_main
    from investment_team.strategy_lab.temporal.start_workflow import (
        build_strategy_lab_batch_input,
    )

    monkeypatch.setattr(api_main, "_clamp_max_parallel", lambda n: min(n, 4))
    monkeypatch.setattr(api_main, "_rehydrate_active_run_offset", lambda run_id: 6)

    bi = build_strategy_lab_batch_input("run-9", _FakeRequest())
    assert bi["run_id"] == "run-9"
    assert bi["batch_size"] == 3
    assert bi["batch_count"] == 2
    assert bi["max_parallel"] == 4  # clamped
    assert bi["benchmark_symbol"] == "SPY"
    assert bi["exclude_asset_classes"] is None
    assert bi["paper_trading_enabled"] is True
    assert bi["paper_trading_lookback_days"] == 200
    assert bi["start_cycle_offset"] == 6
    # config is a JSON-shaped BacktestConfig dump with the cost-stress sweep on.
    assert bi["config"]["cost_stress"] is True
    assert bi["config"]["start_date"] == "2023-01-01"


def test_build_batch_input_translates_allowed_asset_classes(monkeypatch):
    from investment_team import strategy_lab_context
    from investment_team.api import main as api_main
    from investment_team.strategy_lab.temporal.start_workflow import (
        build_strategy_lab_batch_input,
    )

    monkeypatch.setattr(api_main, "_clamp_max_parallel", lambda n: n)
    monkeypatch.setattr(api_main, "_rehydrate_active_run_offset", lambda run_id: 0)
    # ``excluded_for_allowed`` is imported from its home module, not laundered
    # through api.main, so patch it at the source.
    monkeypatch.setattr(
        strategy_lab_context, "excluded_for_allowed", lambda allowed: ["crypto", "forex"]
    )

    req = _FakeRequest()
    req.allowed_asset_classes = ["stocks"]
    bi = build_strategy_lab_batch_input("run-1", req)
    assert bi["exclude_asset_classes"] == ["crypto", "forex"]


def test_start_batch_workflow_dispatches(monkeypatch):
    import shared_temporal
    from investment_team.strategy_lab.temporal import start_workflow as sw

    captured: dict = {}

    def _fake_start_sync(workflow_run, batch_input, *, workflow_id, task_queue):
        captured.update(
            workflow_run=workflow_run,
            batch_input=batch_input,
            workflow_id=workflow_id,
            task_queue=task_queue,
        )

    monkeypatch.setattr(shared_temporal, "start_workflow_sync", _fake_start_sync)
    monkeypatch.setattr(
        sw, "build_strategy_lab_batch_input", lambda run_id, request: {"rid": run_id}
    )

    sw.start_strategy_lab_batch_workflow("run-7", _FakeRequest())
    assert captured["workflow_id"] == "strategy-lab-run-7"
    assert captured["task_queue"] == "strategy-lab-queue"
    assert captured["batch_input"] == {"rid": "run-7"}
