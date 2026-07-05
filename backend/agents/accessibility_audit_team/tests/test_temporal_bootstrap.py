"""Tests for the accessibility_audit Temporal wiring.

Covers three seams:

1. **Import hygiene** — importing ``accessibility_audit_team.temporal`` must be
   side-effect free: no worker bootstrap and no ``os.getenv`` at module load
   (the temporalio workflow sandbox re-imports the module and a self-bootstrap
   races the first dispatch).
2. **Boot + dispatch contract** — the worker entrypoint the team_service looks
   up, and the sync ``start_*_workflow`` bridge, behave per contract.
3. **API dispatch branch** — ``create_audit`` routes through Temporal when
   enabled and falls back to the background task otherwise (unchanged behavior
   when ``TEMPORAL_ADDRESS`` is unset).
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import unittest.mock as mock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from accessibility_audit_team.api.main import router

_test_app = FastAPI()
_test_app.include_router(router)
client = TestClient(_test_app)


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


# ---------------------------------------------------------------------------
# 1. Import hygiene
# ---------------------------------------------------------------------------


def test_importing_temporal_package_does_not_start_worker():
    """Loading the package must NOT spin up a worker thread."""
    import shared_temporal

    _purge("accessibility_audit_team.temporal")
    with mock.patch.object(shared_temporal, "start_team_worker") as patched:
        importlib.import_module("accessibility_audit_team.temporal")
        importlib.import_module("accessibility_audit_team.temporal.start_workflow")
        assert patched.call_count == 0, (
            "Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}); this races the first request."
        )


def test_temporal_init_does_not_call_os_getenv_at_import():
    """The package __init__ is replayed by the temporalio sandbox; it must not
    call ``os.getenv`` (or any restricted builtin) at module top level."""
    import os

    import temporalio  # noqa: F401  ensure temporalio's own import-time getenv already ran

    _purge("accessibility_audit_team.temporal")
    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("accessibility_audit_team.temporal")
        assert spy.call_count == 0, (
            f"accessibility_audit_team.temporal.__init__ called os.getenv "
            f"{spy.call_count} time(s) at import — trips the temporalio sandbox."
        )


def test_audit_execution_import_is_side_effect_free():
    """The Temporal activity runs the audit by importing ``audit_execution`` (not
    ``api.main``). Importing that module must not construct a JobServiceClient or
    start the API's stale-job monitor, so a worker-only process doesn't inherit
    a second monitor."""
    import job_service_client

    _purge("accessibility_audit_team.audit_execution")
    with (
        mock.patch.object(job_service_client, "JobServiceClient") as jsc,
        mock.patch.object(job_service_client, "start_stale_job_monitor") as monitor,
    ):
        importlib.import_module("accessibility_audit_team.audit_execution")
        assert jsc.call_count == 0, "audit_execution built a JobServiceClient at import"
        assert monitor.call_count == 0, "audit_execution started a stale-job monitor at import"


# ---------------------------------------------------------------------------
# 2. Boot + dispatch contract
# ---------------------------------------------------------------------------


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py resolves ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE``; keep that contract pinned against renames."""
    from accessibility_audit_team.temporal import worker

    fn = getattr(worker, "start_accessibility_audit_temporal_worker_thread", None)
    assert callable(fn)


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from accessibility_audit_team.temporal.worker import (
        start_accessibility_audit_temporal_worker_thread,
    )

    assert start_accessibility_audit_temporal_worker_thread() is False


def test_worker_start_delegates_to_start_team_worker_when_enabled(monkeypatch):
    from accessibility_audit_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    captured = {}

    def _fake_start(team, workflows, activities, task_queue):
        captured["team"] = team
        captured["task_queue"] = task_queue
        captured["workflows"] = workflows
        return True

    monkeypatch.setattr(worker, "start_team_worker", _fake_start)

    assert worker.start_accessibility_audit_temporal_worker_thread() is True
    assert captured["team"] == "accessibility_audit"
    assert captured["task_queue"] == "accessibility_audit-queue"
    assert captured["workflows"] is worker.WORKFLOWS


def test_start_workflow_rejects_blank_ids():
    from accessibility_audit_team.temporal.start_workflow import (
        start_accessibility_audit_workflow,
    )

    with pytest.raises(ValueError):
        start_accessibility_audit_workflow("", "audit1", {})
    with pytest.raises(ValueError):
        start_accessibility_audit_workflow("job1", "", {})


def test_start_workflow_dispatches_and_returns_workflow_id(monkeypatch):
    import shared_temporal
    from accessibility_audit_team.temporal import worker

    # Idempotent worker-start is called first; stub it so no thread spins up.
    monkeypatch.setattr(worker, "start_accessibility_audit_temporal_worker_thread", lambda: True)

    captured = {}

    def _fake_sync(workflow_run, *args, workflow_id, task_queue, **kwargs):
        captured["workflow_id"] = workflow_id
        captured["task_queue"] = task_queue
        captured["args"] = args

    monkeypatch.setattr(shared_temporal, "start_workflow_sync", _fake_sync)
    from accessibility_audit_team.temporal.start_workflow import (
        start_accessibility_audit_workflow,
    )

    result = start_accessibility_audit_workflow("job1", "audit1", {"name": "x"})
    assert result == "accessibility_audit-job1"
    assert captured["workflow_id"] == "accessibility_audit-job1"
    assert captured["task_queue"] == "accessibility_audit-queue"
    assert captured["args"][0] == {
        "job_id": "job1",
        "audit_id": "audit1",
        "request": {"name": "x"},
    }


def test_run_pipeline_activity_delegates_to_run_audit_job(monkeypatch):
    """The activity rebuilds the request and runs the propagating core
    ``run_audit_job`` (NOT the swallowing ``execute_audit_job``), so infra
    failures can surface to Temporal."""
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team import temporal as t

    executed = mock.AsyncMock()
    monkeypatch.setattr(ax, "run_audit_job", executed)

    payload = {"job_id": "j1", "audit_id": "a1", "request": {"web_urls": ["https://e.com"]}}
    out = asyncio.run(t.run_pipeline_activity(payload))

    executed.assert_awaited_once()
    called = executed.await_args.args
    assert called[0] == "j1" and called[1] == "a1"
    assert isinstance(called[2], ax.CreateAuditRequest)
    assert out["job_id"] == "j1"


def test_run_pipeline_activity_propagates_infra_exception(monkeypatch):
    """An infrastructure failure must propagate out of the activity so Temporal
    can retry it (rather than being swallowed into a green workflow)."""
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team import temporal as t

    monkeypatch.setattr(ax, "run_audit_job", mock.AsyncMock(side_effect=RuntimeError("infra")))

    payload = {"job_id": "j1", "audit_id": "a1", "request": {"web_urls": ["https://e.com"]}}
    with pytest.raises(RuntimeError, match="infra"):
        asyncio.run(t.run_pipeline_activity(payload))


def test_workflow_run_invokes_activity_with_retry_policy(monkeypatch):
    from accessibility_audit_team import temporal as t

    exec_activity = mock.AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(t.workflow, "execute_activity", exec_activity)

    out = asyncio.run(t.AccessibilityAuditWorkflow().run({"job_id": "j1"}))
    exec_activity.assert_awaited_once()
    assert out == {"ok": True}
    # a bounded retry policy is attached so infra failures actually retry
    assert exec_activity.await_args.kwargs.get("retry_policy") is t._AUDIT_RETRY_POLICY


# ---------------------------------------------------------------------------
# 3. API dispatch branch + execution core
# ---------------------------------------------------------------------------


def test_temporal_dispatch_none_when_disabled(monkeypatch):
    import shared_temporal

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: False)
    from accessibility_audit_team.api import main

    assert main._temporal_dispatch() is None


def test_temporal_dispatch_none_on_import_error(monkeypatch):
    """If the Temporal stack can't be imported, fall back to the thread path."""
    import sys

    from accessibility_audit_team.api import main

    # A non-module object makes ``from shared_temporal import ...`` raise ImportError.
    monkeypatch.setitem(sys.modules, "shared_temporal", object())
    assert main._temporal_dispatch() is None


def test_temporal_dispatch_returns_starter_when_enabled(monkeypatch):
    import shared_temporal

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    from accessibility_audit_team.api import main
    from accessibility_audit_team.temporal.start_workflow import (
        start_accessibility_audit_workflow,
    )

    assert main._temporal_dispatch() is start_accessibility_audit_workflow


def test_temporal_dispatch_logs_and_falls_back_on_start_workflow_import_error(monkeypatch):
    """Temporal enabled but the team's start_workflow module fails to import:
    log a warning and fall back (return None) rather than raise."""
    import shared_temporal

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    # sys.modules[name] = None makes `from ..temporal.start_workflow import ...` raise ImportError.
    monkeypatch.setitem(sys.modules, "accessibility_audit_team.temporal.start_workflow", None)
    from accessibility_audit_team.api import main

    with mock.patch.object(main.logger, "warning") as warn:
        assert main._temporal_dispatch() is None
        warn.assert_called_once()


def test_create_audit_uses_temporal_when_dispatch_available(monkeypatch):
    from accessibility_audit_team.api import main

    jm = mock.Mock()
    monkeypatch.setattr(main, "_job_manager", jm)
    dispatch = mock.Mock(return_value="accessibility_audit-wf1")
    monkeypatch.setattr(main, "_temporal_dispatch", lambda: dispatch)

    resp = client.post("/audit/create", json={"web_urls": ["https://example.com"]})
    assert resp.status_code == 200
    body = resp.json()
    assert "(Temporal)" in body["message"]
    dispatch.assert_called_once()
    # dispatched with (job_id, audit_id, request_payload)
    args = dispatch.call_args.args
    assert args[0] == body["job_id"]
    assert args[1] == body["audit_id"]
    assert isinstance(args[2], dict)
    # workflow_id is recorded for correlation, but the API does NOT write status
    # (the worker activity owns status transitions, so no racy running-write).
    wf_writes = [c for c in jm.update_job.call_args_list if "workflow_id" in c.kwargs]
    assert wf_writes
    assert wf_writes[0].kwargs["workflow_id"] == "accessibility_audit-wf1"
    assert all("status" not in c.kwargs for c in wf_writes)


def test_create_audit_uses_background_task_when_temporal_disabled(monkeypatch):
    from accessibility_audit_team.api import main

    monkeypatch.setattr(main, "_job_manager", mock.Mock())
    monkeypatch.setattr(main, "_temporal_dispatch", lambda: None)
    executed = mock.AsyncMock()
    monkeypatch.setattr(main, "execute_audit_job", executed)

    resp = client.post("/audit/create", json={"web_urls": ["https://example.com"]})
    assert resp.status_code == 200
    assert "(Temporal)" not in resp.json()["message"]
    # background task ran the (patched) execution core
    executed.assert_awaited_once()


def test_create_audit_fails_fast_when_dispatch_raises(monkeypatch):
    """A dispatch failure must fail fast (mark the job failed, return 500) rather
    than re-running in-process, which could double-execute an accepted workflow."""
    from accessibility_audit_team.api import main

    jm = mock.Mock()
    monkeypatch.setattr(main, "_job_manager", jm)

    def _boom(*_a, **_k):
        raise RuntimeError("Temporal client not available")

    monkeypatch.setattr(main, "_temporal_dispatch", lambda: _boom)
    executed = mock.AsyncMock()
    monkeypatch.setattr(main, "execute_audit_job", executed)

    resp = client.post("/audit/create", json={"web_urls": ["https://example.com"]})
    assert resp.status_code == 500
    # no in-process fallback (no double execution)
    executed.assert_not_awaited()
    # the job was marked failed rather than orphaned
    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == main.JOB_STATUS_FAILED
    ]
    assert failed


# ---------------------------------------------------------------------------
# build_audit_request / execute_audit_job core (also proves the fixed bugs)
# ---------------------------------------------------------------------------


def test_build_audit_request_converts_and_defaults():
    from accessibility_audit_team.audit_execution import CreateAuditRequest, build_audit_request

    req = CreateAuditRequest(
        name="n",
        web_urls=["https://e.com"],
        mobile_apps=[{"platform": "android", "name": "App"}],
        wcag_levels=["A", "AA", "bogus"],
    )
    ar = build_audit_request(req, "audit_x")
    assert ar.audit_id == "audit_x"
    assert len(ar.mobile_apps) == 1
    assert len(ar.wcag_levels) == 2  # "bogus" dropped


def test_build_audit_request_defaults_wcag_when_all_invalid():
    from accessibility_audit_team.audit_execution import CreateAuditRequest, build_audit_request

    ar = build_audit_request(CreateAuditRequest(wcag_levels=["bogus"]), "audit_y")
    assert len(ar.wcag_levels) == 2


def test_build_audit_request_rejects_blank_audit_id():
    from accessibility_audit_team.audit_execution import CreateAuditRequest, build_audit_request

    with pytest.raises(ValueError):
        build_audit_request(CreateAuditRequest(), "")


def test_create_audit_request_validates_url_scheme():
    from accessibility_audit_team.audit_execution import CreateAuditRequest

    with pytest.raises(ValueError):
        CreateAuditRequest(web_urls=["ftp://bad.example"])


def test_get_orchestrator_builds_and_caches(monkeypatch):
    """Exercise the lazy orchestrator build (with strands/llm_service stubbed)."""
    import types

    from accessibility_audit_team import audit_execution as ax

    monkeypatch.setattr(ax, "_orchestrator", None)
    fake_orch = object()
    monkeypatch.setattr(ax, "AccessibilityAuditOrchestrator", lambda **kw: fake_orch)

    strands_stub = types.ModuleType("strands")
    strands_stub.Agent = lambda model=None: object()
    llm_stub = types.ModuleType("llm_service")
    llm_stub.get_strands_model = lambda key: object()
    monkeypatch.setitem(sys.modules, "strands", strands_stub)
    monkeypatch.setitem(sys.modules, "llm_service", llm_stub)

    assert ax.get_orchestrator() is fake_orch
    assert ax.get_orchestrator() is fake_orch  # cached, not rebuilt


def test_get_job_manager_is_cached(monkeypatch):
    import job_service_client
    from accessibility_audit_team import audit_execution as ax

    monkeypatch.setattr(ax, "_job_manager", None)
    sentinel = object()
    monkeypatch.setattr(job_service_client, "JobServiceClient", lambda team=None: sentinel)
    monkeypatch.setattr(ax, "JobServiceClient", lambda team=None: sentinel)

    assert ax.get_job_manager() is sentinel
    assert ax.get_job_manager() is sentinel  # cached


def _fake_result(success: bool):
    return SimpleNamespace(
        success=success,
        total_findings=2,
        failure_reason=None if success else "boom",
        current_phase=SimpleNamespace(value="report_packaging"),
        completed_phases=[SimpleNamespace(value="intake"), SimpleNamespace(value="discovery")],
        model_dump=lambda: {"audit_id": "audit1", "success": success},
    )


def _run_execute(monkeypatch, *, run_audit):
    """Drive ``execute_audit_job`` with a mocked orchestrator + job manager."""
    from accessibility_audit_team import audit_execution as ax

    jm = mock.Mock()
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    orch = mock.Mock()
    orch.run_audit = run_audit
    monkeypatch.setattr(ax, "get_orchestrator", lambda: orch)

    req = ax.CreateAuditRequest(web_urls=["https://e.com"])
    asyncio.run(ax.execute_audit_job("job1", "audit1", req))
    return jm, orch


def test_execute_audit_job_success(monkeypatch):
    run_audit = mock.AsyncMock(return_value=_fake_result(True))
    jm, orch = _run_execute(monkeypatch, run_audit=run_audit)

    orch.run_audit.assert_awaited_once()
    statuses = [c.kwargs.get("status") for c in jm.update_job.call_args_list]
    assert "completed" in statuses


def test_execute_audit_job_marks_failed_on_unsuccessful_result(monkeypatch):
    run_audit = mock.AsyncMock(return_value=_fake_result(False))
    jm, _ = _run_execute(monkeypatch, run_audit=run_audit)

    statuses = [c.kwargs.get("status") for c in jm.update_job.call_args_list]
    assert "failed" in statuses


def test_execute_audit_job_captures_exception(monkeypatch):
    run_audit = mock.AsyncMock(side_effect=RuntimeError("kaboom"))
    jm, _ = _run_execute(monkeypatch, run_audit=run_audit)

    failed = [c for c in jm.update_job.call_args_list if c.kwargs.get("status") == "failed"]
    assert failed
    assert any("kaboom" in str(c.kwargs.get("error")) for c in failed)


def test_run_audit_job_propagates_infra_exception(monkeypatch):
    """The core (used by the Temporal activity) must NOT swallow infra
    exceptions — it records running, then lets the exception propagate so
    Temporal can retry. It does not write a terminal 'failed' itself."""
    from accessibility_audit_team import audit_execution as ax

    jm = mock.Mock()
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    orch = mock.Mock()
    orch.run_audit = mock.AsyncMock(side_effect=RuntimeError("kaboom"))
    monkeypatch.setattr(ax, "get_orchestrator", lambda: orch)

    req = ax.CreateAuditRequest(web_urls=["https://e.com"])
    with pytest.raises(RuntimeError, match="kaboom"):
        asyncio.run(ax.run_audit_job("job1", "audit1", req))

    statuses = [c.kwargs.get("status") for c in jm.update_job.call_args_list]
    assert ax.JOB_STATUS_RUNNING in statuses
    assert "failed" not in statuses  # terminal failure is execute_audit_job's job
