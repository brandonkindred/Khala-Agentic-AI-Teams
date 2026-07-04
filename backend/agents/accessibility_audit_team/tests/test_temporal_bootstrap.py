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


def test_start_workflow_dispatches_via_start_workflow_sync(monkeypatch):
    import shared_temporal

    captured = {}

    def _fake_sync(workflow_run, *args, workflow_id, task_queue, **kwargs):
        captured["workflow_id"] = workflow_id
        captured["task_queue"] = task_queue
        captured["args"] = args

    monkeypatch.setattr(shared_temporal, "start_workflow_sync", _fake_sync)
    from accessibility_audit_team.temporal.start_workflow import (
        start_accessibility_audit_workflow,
    )

    start_accessibility_audit_workflow("job1", "audit1", {"name": "x"})
    assert captured["workflow_id"] == "accessibility_audit-job1"
    assert captured["task_queue"] == "accessibility_audit-queue"
    assert captured["args"][0] == {
        "job_id": "job1",
        "audit_id": "audit1",
        "request": {"name": "x"},
    }


def test_run_pipeline_activity_delegates_to_execute(monkeypatch):
    """The activity rebuilds the request and runs the shared execution core
    (proves the sync/await + CreateAuditRequest->AuditRequest bugs are fixed)."""
    from accessibility_audit_team import temporal as t
    from accessibility_audit_team.api import main

    executed = mock.AsyncMock()
    monkeypatch.setattr(main, "_execute_audit_job", executed)

    payload = {"job_id": "j1", "audit_id": "a1", "request": {"web_urls": ["https://e.com"]}}
    out = asyncio.run(t.run_pipeline_activity(payload))

    executed.assert_awaited_once()
    called = executed.await_args.args
    assert called[0] == "j1" and called[1] == "a1"
    assert isinstance(called[2], main.CreateAuditRequest)
    assert out["job_id"] == "j1"


def test_workflow_run_invokes_activity(monkeypatch):
    from accessibility_audit_team import temporal as t

    exec_activity = mock.AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(t.workflow, "execute_activity", exec_activity)

    out = asyncio.run(t.AccessibilityAuditWorkflow().run({"job_id": "j1"}))
    exec_activity.assert_awaited_once()
    assert out == {"ok": True}


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


def test_create_audit_uses_temporal_when_dispatch_available(monkeypatch):
    from accessibility_audit_team.api import main

    monkeypatch.setattr(main, "_job_manager", mock.Mock())
    dispatch = mock.Mock()
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


def test_create_audit_uses_background_task_when_temporal_disabled(monkeypatch):
    from accessibility_audit_team.api import main

    monkeypatch.setattr(main, "_job_manager", mock.Mock())
    monkeypatch.setattr(main, "_temporal_dispatch", lambda: None)
    executed = mock.AsyncMock()
    monkeypatch.setattr(main, "_execute_audit_job", executed)

    resp = client.post("/audit/create", json={"web_urls": ["https://example.com"]})
    assert resp.status_code == 200
    assert "(Temporal)" not in resp.json()["message"]
    # background task ran the (patched) execution core
    executed.assert_awaited_once()


# ---------------------------------------------------------------------------
# _build_audit_request / _execute_audit_job core (also proves the fixed bugs)
# ---------------------------------------------------------------------------


def test_build_audit_request_converts_and_defaults():
    from accessibility_audit_team.api.main import CreateAuditRequest, _build_audit_request

    req = CreateAuditRequest(
        name="n",
        web_urls=["https://e.com"],
        mobile_apps=[{"platform": "android", "name": "App"}],
        wcag_levels=["A", "AA", "bogus"],
    )
    ar = _build_audit_request(req, "audit_x")
    assert ar.audit_id == "audit_x"
    assert len(ar.mobile_apps) == 1
    assert len(ar.wcag_levels) == 2  # "bogus" dropped


def test_build_audit_request_defaults_wcag_when_all_invalid():
    from accessibility_audit_team.api.main import CreateAuditRequest, _build_audit_request

    ar = _build_audit_request(CreateAuditRequest(wcag_levels=["bogus"]), "audit_y")
    assert len(ar.wcag_levels) == 2


def test_build_audit_request_rejects_blank_audit_id():
    from accessibility_audit_team.api.main import CreateAuditRequest, _build_audit_request

    with pytest.raises(ValueError):
        _build_audit_request(CreateAuditRequest(), "")


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
    """Drive ``_execute_audit_job`` with a mocked orchestrator + job manager."""
    from accessibility_audit_team.api import main

    jm = mock.Mock()
    monkeypatch.setattr(main, "_job_manager", jm)
    orch = mock.Mock()
    orch.run_audit = run_audit
    monkeypatch.setattr(main, "get_orchestrator", lambda: orch)

    req = main.CreateAuditRequest(web_urls=["https://e.com"])
    asyncio.run(main._execute_audit_job("job1", "audit1", req))
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
