"""Regression tests for the sales_team Temporal bootstrap.

Guards two failure modes the wiring was designed to avoid:

1. **Self-bootstrap at import time.** The package ``__init__`` used to call
   ``shared.temporal.start_team_worker(...)`` at module load. The worker
   thread connects the Temporal client asynchronously, so the first
   ``start_sales_workflow`` call could lose the race and raise
   ``RuntimeError: Temporal client not available``. Boot is now the
   team_service entrypoint's job (or the API lifespan as a backstop).

2. **Workflow sandbox blocks ``os.getenv``.** The temporalio sandbox
   re-imports the workflow module to load ``SalesWorkflow``. The previous
   ``__init__.py`` called ``is_temporal_enabled()`` — which calls
   ``os.getenv("TEMPORAL_ADDRESS")`` — at module level, and the sandbox
   would abort with ``__call__ on os.getenv restricted``. Both the package
   ``__init__`` and the dedicated ``workflows`` module must import clean.
"""

from __future__ import annotations

import importlib
import sys
import unittest.mock as mock


def _purge(prefix: str) -> None:
    """Drop ``prefix`` and its submodules from ``sys.modules`` so the next
    ``import_module`` re-executes them from scratch.

    Side-effect warning: this mutates the process-wide module cache. These
    tests deliberately re-import ``sales_team.temporal`` to observe its
    import-time behavior, which is only meaningful on a fresh import. Any test
    that holds a reference to a purged module object would see a *different*
    object after a later re-import — but nothing here does: every test in this
    file re-imports the names it needs inside its own body, so the purge is
    self-contained and order-independent. This mirrors the identical helper in
    ``market_research_team``/``investment_team``/``user_agent_founder``'s
    bootstrap tests.
    """
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


def test_importing_temporal_package_does_not_call_start_team_worker():
    """Loading the package must NOT spin up a worker thread."""
    import shared.temporal

    _purge("sales_team.temporal")
    with mock.patch.object(shared.temporal, "start_team_worker") as patched:
        importlib.import_module("sales_team.temporal")
        importlib.import_module("sales_team.temporal.workflows")
        importlib.import_module("sales_team.temporal.start_workflow")
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}). This causes a race on the "
            f"first request and a temporalio sandbox os.getenv violation when "
            f"the workflow registers."
        )


def test_workflow_registers_in_temporalio_sandbox():
    """Ground truth for sandbox safety: ``SalesWorkflow`` (which now imports the
    sales models + activities under ``workflow.unsafe.imports_passed_through()``)
    must import and instantiate inside the real ``SandboxedWorkflowRunner`` with
    ``shared.temporal``'s passthrough restrictions, without a
    ``RestrictedWorkflowAccessError``.

    This supersedes the old "zero ``os.getenv`` at import" spy: the workflow now
    legitimately imports pydantic models under passthrough (pydantic reads
    ``PYDANTIC_DISABLE_PLUGINS`` while building model classes), which the sandbox
    explicitly permits. Registering the workflow for real is a stronger check
    than the proxy — it exercises exactly what the worker does at boot.
    """
    import asyncio

    import temporalio.workflow as _wf

    from shared.temporal.worker import _build_workflow_runner

    _purge("sales_team.temporal")
    from sales_team.temporal import WORKFLOWS

    async def _prepare() -> None:
        runner = _build_workflow_runner()
        for wfc in WORKFLOWS:  # SalesWorkflow + DeepResearchWorkflow
            runner.prepare_workflow(_wf._Definition.must_from_class(wfc))

    # No RestrictedWorkflowAccessError => the modules (and their passthrough
    # imports) loaded cleanly in the sandbox.
    asyncio.run(_prepare())


def test_activities_list_exposes_every_stage_activity():
    """The worker registers the whole ``ACTIVITIES`` list; guard that all twelve
    fine-grained activities are exported so a rename can't silently drop one from
    the worker's registration."""
    from sales_team.temporal import ACTIVITIES

    names = {getattr(a, "__name__", None) for a in ACTIVITIES}
    assert names == {
        # main pipeline
        "prepare_sales_pipeline_activity",
        "prospect_activity",
        "load_dossiers_activity",
        "outreach_one_activity",
        "qualify_one_activity",
        "nurture_one_activity",
        "discovery_one_activity",
        "proposal_one_activity",
        "close_one_activity",
        "coach_activity",
        "report_progress_activity",
        "mark_failed_activity",
        "finalize_sales_pipeline_activity",
        # deep research
        "prepare_deep_research_activity",
        "companies_activity",
        "map_company_one_activity",
        "rank_activity",
        "build_dossier_one_activity",
        "finalize_deep_research_activity",
    }


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py looks up ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE``. Keep that contract pinned so a rename
    can't silently break docker-compose.
    """
    from sales_team.temporal import worker

    fn = getattr(worker, "start_sales_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg "
        "start_sales_temporal_worker_thread() in sales_team.temporal.worker"
    )


def test_max_concurrent_activities_env_parsing(monkeypatch):
    """The fan-out ceiling is env-tunable (shared env_int parser): a valid value
    is honoured, unset/garbage falls back to the default, and a non-positive
    value is clamped up to the floor of 1."""
    from sales_team.temporal import worker as worker_mod

    monkeypatch.setenv("SALES_TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "16")
    assert worker_mod._max_concurrent_activities() == 16
    monkeypatch.setenv("SALES_TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "nan")
    assert worker_mod._max_concurrent_activities() == 8  # garbage → default
    monkeypatch.setenv("SALES_TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "0")
    assert worker_mod._max_concurrent_activities() == 1  # clamped to floor
    monkeypatch.delenv("SALES_TEMPORAL_MAX_CONCURRENT_ACTIVITIES", raising=False)
    assert worker_mod._max_concurrent_activities() == 8


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    """Standalone uvicorn dev path: with TEMPORAL_ADDRESS unset, the backstop
    must return False instead of raising."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from sales_team.temporal.worker import start_sales_temporal_worker_thread

    assert start_sales_temporal_worker_thread() is False


def test_worker_start_delegates_to_start_team_worker(monkeypatch):
    """When enabled, the no-arg func delegates to ``start_team_worker`` with
    the team's own task queue and returns its result."""
    from sales_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
    from sales_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)
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

    monkeypatch.setattr(worker_mod, "start_team_worker", _fake_start)

    assert worker_mod.start_sales_temporal_worker_thread() is True
    assert captured == {
        "team": "sales",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
        # Fan-out is now Temporal-managed; the ceiling defaults to the old
        # thread-pool width so throughput is preserved.
        "max_concurrent_activities": 8,
    }
    assert TASK_QUEUE == "sales-queue"


def test_start_sales_workflow_delegates_to_shared_bridge(monkeypatch):
    """The team wrapper forwards to ``shared.temporal.start_workflow_sync`` with
    the sales workflow id + task queue."""
    from sales_team.temporal import SalesWorkflow
    from sales_team.temporal import start_workflow as sw

    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue, **_kw):
        captured.update(
            workflow_run=workflow_run, args=args, workflow_id=workflow_id, task_queue=task_queue
        )

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    sw.start_sales_workflow("job-abc", {"product_name": "x"})

    assert captured["workflow_run"] is SalesWorkflow.run
    assert captured["args"] == ("job-abc", {"product_name": "x"})
    assert captured["workflow_id"] == "sales-job-abc"
    assert captured["task_queue"] == "sales-queue"


def test_start_deep_research_workflow_delegates_to_shared_bridge(monkeypatch):
    """The deep-research wrapper forwards to ``start_workflow_sync`` with the
    deep-research workflow id prefix + the shared sales task queue."""
    from sales_team.temporal import DeepResearchWorkflow
    from sales_team.temporal import start_workflow as sw

    captured: dict = {}

    def _fake_start_workflow_sync(workflow_run, *args, workflow_id, task_queue, **_kw):
        captured.update(
            workflow_run=workflow_run, args=args, workflow_id=workflow_id, task_queue=task_queue
        )

    monkeypatch.setattr(sw, "start_workflow_sync", _fake_start_workflow_sync)

    sw.start_deep_research_workflow("job-xyz", {"product_name": "x"})

    assert captured["workflow_run"] is DeepResearchWorkflow.run
    assert captured["args"] == ("job-xyz", {"product_name": "x"})
    assert captured["workflow_id"] == "sales-deep-research-job-xyz"
    assert captured["task_queue"] == "sales-queue"
