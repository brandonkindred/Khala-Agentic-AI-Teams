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
from datetime import timedelta
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


def _run_audit_workflow(monkeypatch, statuses, *, patched=True, payload=None):
    """Drive ``AccessibilityAuditWorkflow.run`` with a stubbed execute_activity/patched.

    ``statuses`` maps an activity ``__name__`` to the dict that activity returns
    (defaulting to PASS); ``patched`` is what the stubbed ``workflow.patched`` reports
    (False replays the pre-decomposition legacy path). Returns ``(ordered names, out)``.
    """
    from accessibility_audit_team.temporal import workflows as wf

    calls: list[str] = []

    async def fake_execute(activity, *args, **kwargs):
        name = getattr(activity, "__name__", str(activity))
        calls.append(name)
        return statuses.get(name, {"status": "PASS", "audit_id": "a1"})

    monkeypatch.setattr(wf.workflow, "execute_activity", fake_execute)
    monkeypatch.setattr(wf.workflow, "patched", lambda _id: patched)
    payload = payload or {"job_id": "j1", "audit_id": "a1", "request": {"tech_stack": {}}}
    out = asyncio.run(wf.AccessibilityAuditWorkflow().run(payload))
    return calls, out


def test_workflow_runs_all_phases_in_order(monkeypatch):
    """Happy path: intake -> discovery -> verification -> report_packaging -> finalize."""
    calls, _ = _run_audit_workflow(monkeypatch, {})
    assert calls == [
        "intake_activity",
        "discovery_activity",
        "verification_activity",
        "report_packaging_activity",
        "finalize_activity",
    ]


@pytest.mark.parametrize(
    "fail_name, expected_calls",
    [
        ("intake_activity", ["intake_activity"]),
        ("discovery_activity", ["intake_activity", "discovery_activity"]),
        (
            "verification_activity",
            ["intake_activity", "discovery_activity", "verification_activity"],
        ),
        (
            "report_packaging_activity",
            [
                "intake_activity",
                "discovery_activity",
                "verification_activity",
                "report_packaging_activity",
            ],
        ),
    ],
)
def test_workflow_short_circuits_on_phase_fail(monkeypatch, fail_name, expected_calls):
    """A ``FAIL`` status from a phase stops the workflow before the next phase."""
    calls, out = _run_audit_workflow(monkeypatch, {fail_name: {"status": "FAIL", "audit_id": "a1"}})
    assert calls == expected_calls
    assert out["status"] == "FAIL"


def test_workflow_passes_tech_stack_to_verification(monkeypatch):
    """The tech_stack from the request dict is threaded to the verification activity."""
    from accessibility_audit_team.temporal import workflows as wf

    captured: dict = {}

    async def fake_execute(activity, *args, **kwargs):
        if getattr(activity, "__name__", "") == "verification_activity":
            captured["args"] = kwargs.get("args")
        return {"status": "PASS", "audit_id": "a1"}

    monkeypatch.setattr(wf.workflow, "execute_activity", fake_execute)
    monkeypatch.setattr(wf.workflow, "patched", lambda _id: True)
    payload = {"job_id": "j1", "audit_id": "a1", "request": {"tech_stack": {"web": "react"}}}
    asyncio.run(wf.AccessibilityAuditWorkflow().run(payload))
    assert captured["args"] == ["j1", "a1", {"web": "react"}]


def test_workflow_unpatched_replays_legacy(monkeypatch):
    """A pre-decomposition history replays the single legacy activity with the
    original 2h timeout + retry policy (byte-for-byte deterministic replay)."""
    from accessibility_audit_team.temporal import workflows as wf

    captured: dict = {}

    async def fake_execute(activity, *args, **kwargs):
        captured["name"] = getattr(activity, "__name__", str(activity))
        captured["kwargs"] = kwargs
        return {"status": "done"}

    monkeypatch.setattr(wf.workflow, "execute_activity", fake_execute)
    monkeypatch.setattr(wf.workflow, "patched", lambda _id: False)

    out = asyncio.run(
        wf.AccessibilityAuditWorkflow().run({"job_id": "j1", "audit_id": "a1", "request": {}})
    )
    assert captured["name"] == "run_pipeline_activity"
    assert captured["kwargs"].get("retry_policy") is wf._AUDIT_RETRY_POLICY
    assert captured["kwargs"].get("start_to_close_timeout") == wf.LEGACY_TIMEOUT
    assert out == {"status": "done"}


def test_retest_workflow_runs_retest_activity(monkeypatch):
    """The retest workflow drives exactly one retest activity with a retry policy."""
    from accessibility_audit_team.temporal import workflows as wf

    captured: dict = {}

    async def fake_execute(activity, *args, **kwargs):
        captured["name"] = getattr(activity, "__name__", str(activity))
        captured["kwargs"] = kwargs
        return {"status": "done", "audit_id": "a1"}

    monkeypatch.setattr(wf.workflow, "execute_activity", fake_execute)

    out = asyncio.run(
        wf.AccessibilityRetestWorkflow().run(
            {"job_id": "j1", "audit_id": "a1", "finding_ids": ["f1"]}
        )
    )
    assert captured["name"] == "retest_activity"
    assert captured["kwargs"].get("args") == ["j1", "a1", ["f1"]]
    assert captured["kwargs"].get("retry_policy") is wf._PHASE_RETRY_POLICY
    assert out == {"status": "done", "audit_id": "a1"}


def test_workflow_timebox_marks_timed_out_when_budget_exceeded(monkeypatch):
    """When the timebox timer wins the race, the workflow abandons the phases and
    marks the job timed out."""
    from accessibility_audit_team.temporal import workflows as wf

    calls: list[str] = []

    async def fake_execute(activity, *args, **kwargs):
        name = getattr(activity, "__name__", str(activity))
        calls.append(name)
        if name == "mark_timed_out_activity":
            return {"status": "TIMEOUT", "audit_id": "a1"}
        await asyncio.sleep(3600)  # phase never completes -> the timer wins

    async def fake_sleep(_duration):
        return  # timebox fires immediately

    monkeypatch.setattr(wf.workflow, "execute_activity", fake_execute)
    monkeypatch.setattr(wf.workflow, "patched", lambda _id: True)
    monkeypatch.setattr(wf.workflow, "sleep", fake_sleep)

    payload = {"job_id": "j1", "audit_id": "a1", "request": {"timebox_hours": 1, "tech_stack": {}}}
    out = asyncio.run(wf.AccessibilityAuditWorkflow().run(payload))
    assert out["status"] == "TIMEOUT"
    assert "mark_timed_out_activity" in calls
    assert "intake_activity" in calls  # a phase was started before the timeout


def test_workflow_timebox_completes_when_within_budget(monkeypatch):
    """A timeboxed audit that finishes before the timer returns the finalize result
    and never marks itself timed out."""
    from accessibility_audit_team.temporal import workflows as wf

    calls: list[str] = []

    async def fake_execute(activity, *args, **kwargs):
        calls.append(getattr(activity, "__name__", str(activity)))
        return {"status": "PASS", "audit_id": "a1"}

    async def fake_sleep(_duration):
        await asyncio.sleep(3600)  # timer never fires within the test

    monkeypatch.setattr(wf.workflow, "execute_activity", fake_execute)
    monkeypatch.setattr(wf.workflow, "patched", lambda _id: True)
    monkeypatch.setattr(wf.workflow, "sleep", fake_sleep)

    payload = {"job_id": "j1", "audit_id": "a1", "request": {"timebox_hours": 2, "tech_stack": {}}}
    out = asyncio.run(wf.AccessibilityAuditWorkflow().run(payload))
    assert out == {"status": "PASS", "audit_id": "a1"}
    assert "mark_timed_out_activity" not in calls
    assert calls[-1] == "finalize_activity"


def test_workflow_applies_default_timebox_when_unset(monkeypatch):
    """A request that never specifies ``timebox_hours`` still gets an overall
    wall-clock cap (``DEFAULT_TIMEBOX_HOURS``) rather than running unbounded —
    the per-phase timeouts alone have no aggregate bound."""
    from accessibility_audit_team.temporal import workflows as wf

    captured: dict = {}

    async def fake_sleep(duration):
        captured["sleep_duration"] = duration
        await asyncio.sleep(3600)  # timer never actually fires within the test

    async def fake_execute(activity, *args, **kwargs):
        return {"status": "PASS", "audit_id": "a1"}

    monkeypatch.setattr(wf.workflow, "execute_activity", fake_execute)
    monkeypatch.setattr(wf.workflow, "patched", lambda _id: True)
    monkeypatch.setattr(wf.workflow, "sleep", fake_sleep)

    payload = {"job_id": "j1", "audit_id": "a1", "request": {"tech_stack": {}}}
    out = asyncio.run(wf.AccessibilityAuditWorkflow().run(payload))
    assert out == {"status": "PASS", "audit_id": "a1"}
    assert captured["sleep_duration"] == timedelta(hours=wf.DEFAULT_TIMEBOX_HOURS)


def test_workflow_explicit_zero_timebox_remains_unbounded(monkeypatch):
    """An explicit ``0`` is a deliberate "run unbounded" request and must NOT be
    replaced by ``DEFAULT_TIMEBOX_HOURS`` — no timer race is set up at all."""
    from accessibility_audit_team.temporal import workflows as wf

    sleep_called = mock.Mock()

    async def fake_sleep(duration):
        sleep_called(duration)
        await asyncio.sleep(3600)

    async def fake_execute(activity, *args, **kwargs):
        return {"status": "PASS", "audit_id": "a1"}

    monkeypatch.setattr(wf.workflow, "execute_activity", fake_execute)
    monkeypatch.setattr(wf.workflow, "patched", lambda _id: True)
    monkeypatch.setattr(wf.workflow, "sleep", fake_sleep)

    payload = {"job_id": "j1", "audit_id": "a1", "request": {"timebox_hours": 0, "tech_stack": {}}}
    out = asyncio.run(wf.AccessibilityAuditWorkflow().run(payload))
    assert out == {"status": "PASS", "audit_id": "a1"}
    sleep_called.assert_not_called()


def test_workflow_intake_and_finalize_pass_heartbeat_timeout(monkeypatch):
    """intake/finalize now heartbeat (Fix 2), so their activities must be scheduled
    with a ``heartbeat_timeout`` like the long LLM/scan phases, or a live heartbeat
    with no matching timeout is silently ineffective."""
    from accessibility_audit_team.temporal import workflows as wf

    captured: dict = {}

    async def fake_execute(activity, *args, **kwargs):
        name = getattr(activity, "__name__", str(activity))
        captured[name] = kwargs
        return {"status": "PASS", "audit_id": "a1"}

    monkeypatch.setattr(wf.workflow, "execute_activity", fake_execute)
    monkeypatch.setattr(wf.workflow, "patched", lambda _id: True)
    payload = {"job_id": "j1", "audit_id": "a1", "request": {"tech_stack": {}}}
    asyncio.run(wf.AccessibilityAuditWorkflow().run(payload))

    assert captured["intake_activity"].get("heartbeat_timeout") == wf.HEARTBEAT_TIMEOUT
    assert captured["finalize_activity"].get("heartbeat_timeout") == wf.HEARTBEAT_TIMEOUT


def test_mark_timed_out_activity_delegates(monkeypatch):
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    called = mock.AsyncMock()
    monkeypatch.setattr(ax, "mark_audit_timed_out", called)
    out = asyncio.run(acts.mark_timed_out_activity("j1", "a1", 2))
    called.assert_awaited_once_with("j1", "a1", 2)
    assert out == {"status": "TIMEOUT", "audit_id": "a1"}


def test_pattern_a_exports_in_sync():
    """WORKFLOWS/ACTIVITIES exports line up with the workflow classes and name
    constants — the seam that silently hangs a workflow on an unregistered activity."""
    from temporalio import activity

    from accessibility_audit_team import temporal as t
    from accessibility_audit_team.temporal import constants

    assert t.WORKFLOWS == [t.AccessibilityAuditWorkflow, t.AccessibilityRetestWorkflow]
    names = {activity._Definition.must_from_callable(a).name for a in t.ACTIVITIES}
    assert names == {
        constants.ACTIVITY_INTAKE,
        constants.ACTIVITY_DISCOVERY,
        constants.ACTIVITY_VERIFICATION,
        constants.ACTIVITY_REPORT_PACKAGING,
        constants.ACTIVITY_FINALIZE,
        constants.ACTIVITY_RETEST,
        constants.ACTIVITY_TIMEOUT,
        constants.ACTIVITY_RUN_PIPELINE,
    }


# ---------------------------------------------------------------------------
# 3. API dispatch branch + execution core
# ---------------------------------------------------------------------------


def test_temporal_dispatch_none_when_disabled(monkeypatch):
    import shared_temporal

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: False)
    from accessibility_audit_team.api import main

    assert main._get_temporal_dispatcher() is None


def test_temporal_dispatch_none_on_import_error(monkeypatch):
    """If the Temporal stack can't be imported, fall back to the thread path."""
    import sys

    from accessibility_audit_team.api import main

    # A non-module object makes ``from shared_temporal import ...`` raise ImportError.
    monkeypatch.setitem(sys.modules, "shared_temporal", object())
    assert main._get_temporal_dispatcher() is None


def test_temporal_dispatch_returns_starter_when_enabled(monkeypatch):
    import shared_temporal

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    from accessibility_audit_team.api import main
    from accessibility_audit_team.temporal.start_workflow import (
        start_accessibility_audit_workflow,
    )

    assert main._get_temporal_dispatcher() is start_accessibility_audit_workflow


def test_temporal_dispatch_logs_and_falls_back_on_start_workflow_import_error(monkeypatch):
    """Temporal enabled but the team's start_workflow module fails to import:
    log a warning and fall back (return None) rather than raise."""
    import shared_temporal

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    # sys.modules[name] = None makes `from ..temporal.start_workflow import ...` raise ImportError.
    monkeypatch.setitem(sys.modules, "accessibility_audit_team.temporal.start_workflow", None)
    from accessibility_audit_team.api import main

    with mock.patch.object(main.logger, "warning") as warn:
        assert main._get_temporal_dispatcher() is None
        warn.assert_called_once()


def test_create_audit_uses_temporal_when_dispatch_available(monkeypatch):
    from accessibility_audit_team.api import main

    jm = mock.Mock()
    monkeypatch.setattr(main, "_job_manager", jm)
    dispatch = mock.Mock(return_value="accessibility_audit-wf1")
    monkeypatch.setattr(main, "_get_temporal_dispatcher", lambda: dispatch)
    exec_job = mock.Mock()
    monkeypatch.setattr(main, "execute_audit_job", exec_job)

    resp = client.post("/audit/create", json={"web_urls": ["https://example.com"]})
    assert resp.status_code == 200
    body = resp.json()
    assert "(Temporal)" in body["message"]
    # the workflow id is surfaced in the response for Temporal-UI correlation
    assert body["workflow_id"] == "accessibility_audit-wf1"
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
    # the Temporal dispatch branch must not also schedule the in-process path
    exec_job.assert_not_called()


def test_create_audit_succeeds_when_workflow_id_persist_fails(monkeypatch):
    """The workflow already started server-side by the time workflow_id is
    persisted for correlation, so a failure to persist it must be logged, not
    raised — the request still succeeds (200) with no in-process fallback."""
    from accessibility_audit_team.api import main

    jm = mock.Mock()
    jm.update_job.side_effect = ConnectionError("job-service unreachable")
    monkeypatch.setattr(main, "_job_manager", jm)
    dispatch = mock.Mock(return_value="accessibility_audit-wf1")
    monkeypatch.setattr(main, "_get_temporal_dispatcher", lambda: dispatch)
    exec_job = mock.Mock()
    monkeypatch.setattr(main, "execute_audit_job", exec_job)

    with mock.patch.object(main.logger, "warning") as warn:
        resp = client.post("/audit/create", json={"web_urls": ["https://example.com"]})
        warn.assert_called_once()

    assert resp.status_code == 200
    assert resp.json()["workflow_id"] == "accessibility_audit-wf1"
    exec_job.assert_not_called()


def test_create_audit_uses_background_task_when_temporal_disabled(monkeypatch):
    from accessibility_audit_team.api import main

    monkeypatch.setattr(main, "_job_manager", mock.Mock())
    monkeypatch.setattr(main, "_get_temporal_dispatcher", lambda: None)
    executed = mock.AsyncMock()
    monkeypatch.setattr(main, "execute_audit_job", executed)

    resp = client.post("/audit/create", json={"web_urls": ["https://example.com"]})
    assert resp.status_code == 200
    body = resp.json()
    assert "(Temporal)" not in body["message"]
    # no Temporal workflow on the in-process path
    assert body["workflow_id"] is None
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

    monkeypatch.setattr(main, "_get_temporal_dispatcher", lambda: _boom)
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


def test_create_audit_request_accepts_zero_and_negative_timebox():
    """No lower-bound constraint: 0/negative is accepted and treated as
    "unbounded" downstream (parity with the orchestrator's falsy check and the
    workflow's timebox guard) rather than rejected — a legacy Temporal history
    recorded before a stricter bound was ever considered may carry
    ``timebox_hours=0``, and replaying it must not raise a validation error."""
    from accessibility_audit_team.audit_execution import CreateAuditRequest

    assert CreateAuditRequest(timebox_hours=0).timebox_hours == 0
    assert CreateAuditRequest(timebox_hours=-3).timebox_hours == -3
    assert CreateAuditRequest(timebox_hours=2).timebox_hours == 2
    assert CreateAuditRequest().timebox_hours is None


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


def test_build_llm_client_constructs_agent(monkeypatch):
    """``_build_llm_client`` resolves the accessibility-audit model into a strands Agent
    (with strands/llm_service stubbed so no live provider is needed)."""
    import types

    from accessibility_audit_team import audit_execution as ax

    sentinel = object()
    strands_stub = types.ModuleType("strands")
    strands_stub.Agent = lambda model=None: sentinel
    llm_stub = types.ModuleType("llm_service")
    llm_stub.get_strands_model = lambda key: object()
    monkeypatch.setitem(sys.modules, "strands", strands_stub)
    monkeypatch.setitem(sys.modules, "llm_service", llm_stub)

    assert ax._build_llm_client() is sentinel


def test_get_job_manager_is_cached(monkeypatch):
    import job_service_client
    from accessibility_audit_team import audit_execution as ax

    monkeypatch.setattr(ax, "_job_manager", None)
    sentinel = object()
    monkeypatch.setattr(job_service_client, "JobServiceClient", lambda team=None: sentinel)
    monkeypatch.setattr(ax, "JobServiceClient", lambda team=None: sentinel)

    assert ax.get_job_manager() is sentinel
    assert ax.get_job_manager() is sentinel  # cached


def _fake_result(success: bool) -> SimpleNamespace:
    """Build a stand-in ``AccessibilityAuditResult`` with the attributes the
    execution core reads (``success``, phases, findings count, ``model_dump``)."""
    return SimpleNamespace(
        success=success,
        total_findings=2,
        failure_reason=None if success else "boom",
        current_phase=SimpleNamespace(value="report_packaging"),
        completed_phases=[SimpleNamespace(value="intake"), SimpleNamespace(value="discovery")],
        model_dump=lambda: {"audit_id": "audit1", "success": success},
    )


def _run_execute(monkeypatch, *, run_audit) -> tuple[mock.Mock, mock.Mock]:
    """Drive ``execute_audit_job`` with a mocked orchestrator + job manager.

    Returns ``(job_manager_mock, orchestrator_mock)`` so callers can assert on
    the recorded ``update_job`` calls and that ``run_audit`` was awaited.
    """
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


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("kaboom"),
        ConnectionError("temporal unreachable"),
        TimeoutError("job-service timed out"),
    ],
)
def test_run_audit_job_propagates_infra_exception(monkeypatch, exc):
    """The core (used by the Temporal activity) must NOT swallow infra
    exceptions — it records running, then lets the exception propagate so
    Temporal can retry. It does not write a terminal 'failed' itself. Covers
    connection/timeout errors, not just a generic RuntimeError."""
    from accessibility_audit_team import audit_execution as ax

    jm = mock.Mock()
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    orch = mock.Mock()
    orch.run_audit = mock.AsyncMock(side_effect=exc)
    monkeypatch.setattr(ax, "get_orchestrator", lambda: orch)

    req = ax.CreateAuditRequest(web_urls=["https://e.com"])
    with pytest.raises(type(exc)):
        asyncio.run(ax.run_audit_job("job1", "audit1", req))

    statuses = [c.kwargs.get("status") for c in jm.update_job.call_args_list]
    assert ax.JOB_STATUS_RUNNING in statuses
    assert "failed" not in statuses  # terminal failure is execute_audit_job's job


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_run_audit_job_is_idempotent_on_retry_after_terminal(monkeypatch, terminal_status):
    """A Temporal retry that fires after the job already reached a terminal
    state (e.g. the retry was triggered by the terminal ``update_job`` call
    itself failing post-hoc) must not re-run the full audit from scratch."""
    from accessibility_audit_team import audit_execution as ax

    jm = mock.Mock()
    jm.get_job.return_value = {"status": terminal_status}
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    orch = mock.Mock()
    orch.run_audit = mock.AsyncMock(return_value=_fake_result(True))
    monkeypatch.setattr(ax, "get_orchestrator", lambda: orch)

    req = ax.CreateAuditRequest(web_urls=["https://e.com"])
    asyncio.run(ax.run_audit_job("job1", "audit1", req))

    orch.run_audit.assert_not_awaited()
    jm.update_job.assert_not_called()


def test_run_audit_job_rejects_blank_ids():
    """The documented precondition (non-empty job_id/audit_id) is enforced at runtime."""
    from accessibility_audit_team import audit_execution as ax

    req = ax.CreateAuditRequest(web_urls=["https://e.com"])
    with pytest.raises(ValueError):
        asyncio.run(ax.run_audit_job("", "a1", req))
    with pytest.raises(ValueError):
        asyncio.run(ax.run_audit_job("j1", "", req))


def test_run_audit_job_runs_when_no_existing_job_row(monkeypatch):
    """No prior job row (``get_job`` returns ``None``) is not a terminal state
    — the audit must still run (covers the first-ever execution)."""
    from accessibility_audit_team import audit_execution as ax

    jm = mock.Mock()
    jm.get_job.return_value = None
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    orch = mock.Mock()
    orch.run_audit = mock.AsyncMock(return_value=_fake_result(True))
    monkeypatch.setattr(ax, "get_orchestrator", lambda: orch)

    req = ax.CreateAuditRequest(web_urls=["https://e.com"])
    asyncio.run(ax.run_audit_job("job1", "audit1", req))

    orch.run_audit.assert_awaited_once()


# ---------------------------------------------------------------------------
# _is_last_attempt / _is_job_terminal — the retry/terminal-write guard helpers
# ---------------------------------------------------------------------------


def test_is_last_attempt_true_outside_activity_context():
    """Direct/thread use (no Temporal activity context) treats the call as the
    last attempt — there's no retry mechanism to defer to."""
    from accessibility_audit_team.temporal import activities as acts

    assert acts._is_last_attempt() is True


def test_is_last_attempt_false_when_retries_unlimited(monkeypatch):
    from accessibility_audit_team.temporal import activities as acts

    info = SimpleNamespace(retry_policy=SimpleNamespace(maximum_attempts=0), attempt=5)
    monkeypatch.setattr(acts.activity, "info", lambda: info)
    assert acts._is_last_attempt() is False


def test_is_last_attempt_false_when_retry_policy_missing(monkeypatch):
    from accessibility_audit_team.temporal import activities as acts

    info = SimpleNamespace(retry_policy=None, attempt=1)
    monkeypatch.setattr(acts.activity, "info", lambda: info)
    assert acts._is_last_attempt() is False


def test_is_last_attempt_true_on_final_scheduled_attempt(monkeypatch):
    from accessibility_audit_team.temporal import activities as acts

    info = SimpleNamespace(retry_policy=SimpleNamespace(maximum_attempts=3), attempt=3)
    monkeypatch.setattr(acts.activity, "info", lambda: info)
    assert acts._is_last_attempt() is True


def test_is_last_attempt_false_before_final_scheduled_attempt(monkeypatch):
    from accessibility_audit_team.temporal import activities as acts

    info = SimpleNamespace(retry_policy=SimpleNamespace(maximum_attempts=3), attempt=1)
    monkeypatch.setattr(acts.activity, "info", lambda: info)
    assert acts._is_last_attempt() is False


def test_is_job_terminal_true_for_completed():
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    manager = mock.Mock()
    manager.get_job.return_value = {"status": ax.JOB_STATUS_COMPLETED}
    assert acts._is_job_terminal(manager, "j1") is True


def test_is_job_terminal_true_for_failed():
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    manager = mock.Mock()
    manager.get_job.return_value = {"status": ax.JOB_STATUS_FAILED}
    assert acts._is_job_terminal(manager, "j1") is True


def test_is_job_terminal_false_for_running():
    from accessibility_audit_team.temporal import activities as acts

    manager = mock.Mock()
    manager.get_job.return_value = {"status": "running"}
    assert acts._is_job_terminal(manager, "j1") is False


def test_is_job_terminal_false_when_job_missing():
    from accessibility_audit_team.temporal import activities as acts

    manager = mock.Mock()
    manager.get_job.return_value = None
    assert acts._is_job_terminal(manager, "j1") is False


# ---------------------------------------------------------------------------
# _run_phase — terminal-write guard and exception handling (direct, no
# activity wrapper)
# ---------------------------------------------------------------------------


def test_run_phase_skips_terminal_write_when_job_already_terminal_on_fail(monkeypatch):
    """A logical phase failure's terminal write is skipped when a concurrent path
    (e.g. a timebox timeout racing an abandoned activity) already marked the job
    terminal — but the returned status still reflects this attempt's own outcome."""
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = mock.Mock()
    jm.get_job.return_value = {"status": ax.JOB_STATUS_COMPLETED}
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)

    out = asyncio.run(
        acts._run_phase("j1", "a1", "discovery", 40, lambda: _phase_fail_async("boom"))
    )
    assert out == {"status": "FAIL", "audit_id": "a1", "error": "boom"}
    failed_writes = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert not failed_writes


def test_run_phase_writes_terminal_fail_when_job_not_yet_terminal(monkeypatch):
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = mock.Mock()
    jm.get_job.return_value = None
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)

    out = asyncio.run(
        acts._run_phase("j1", "a1", "discovery", 40, lambda: _phase_fail_async("boom"))
    )
    assert out == {"status": "FAIL", "audit_id": "a1", "error": "boom"}
    failed_writes = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed_writes and failed_writes[0].kwargs.get("error") == "boom"


def test_run_phase_marks_failed_on_last_attempt_exception(monkeypatch):
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = mock.Mock()
    jm.get_job.return_value = None
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    monkeypatch.setattr(acts, "_is_last_attempt", lambda: True)

    async def step():
        raise RuntimeError("infra boom")

    with pytest.raises(RuntimeError, match="infra boom"):
        asyncio.run(acts._run_phase("j1", "a1", "discovery", 40, step))

    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed and failed[0].kwargs.get("error") == "infra boom"


def test_run_phase_exception_recovers_progress_and_result_from_persisted_state(monkeypatch):
    """The exception branch has no fresh ``AccessibilityAuditResult`` to report
    from (``step`` raised before returning one), so it must recover
    progress/completed_phases/findings_count/result from whatever was last
    durably persisted — otherwise the terminal write leaves those fields stale
    from the phase's earlier RUNNING write instead of reflecting reality."""
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = mock.Mock()
    jm.get_job.return_value = None
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    monkeypatch.setattr(acts, "_is_last_attempt", lambda: True)

    persisted = SimpleNamespace(
        completed_phases=[SimpleNamespace(value="intake"), SimpleNamespace(value="discovery")],
        total_findings=5,
        model_dump=lambda: {"audit_id": "a1", "success": False},
    )
    monkeypatch.setattr(ax, "load_audit_state", mock.AsyncMock(return_value=persisted))

    async def step():
        raise RuntimeError("infra boom")

    with pytest.raises(RuntimeError, match="infra boom"):
        asyncio.run(acts._run_phase("j1", "a1", "verification", 60, step))

    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed
    kwargs = failed[0].kwargs
    assert kwargs.get("progress") == 100
    assert kwargs.get("completed_phases") == ["intake", "discovery"]
    assert kwargs.get("findings_count") == 5
    assert kwargs.get("result") == {"audit_id": "a1", "success": False}


def test_run_phase_exception_defaults_when_nothing_persisted(monkeypatch):
    """When no state was ever persisted (e.g. intake itself failed before its
    first successful write), the recovered fields fall back to terminal-but-empty
    defaults rather than raising or omitting them."""
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = mock.Mock()
    jm.get_job.return_value = None
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    monkeypatch.setattr(acts, "_is_last_attempt", lambda: True)
    monkeypatch.setattr(ax, "load_audit_state", mock.AsyncMock(return_value=None))

    async def step():
        raise RuntimeError("infra boom")

    with pytest.raises(RuntimeError, match="infra boom"):
        asyncio.run(acts._run_phase("j1", "a1", "intake", 20, step))

    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed
    kwargs = failed[0].kwargs
    assert kwargs.get("progress") == 100
    assert kwargs.get("completed_phases") == []
    assert kwargs.get("findings_count") == 0
    assert kwargs.get("result") is None


def test_run_phase_does_not_mark_failed_when_not_last_attempt(monkeypatch):
    """Temporal will retry a non-final attempt, so the job is left non-terminal
    (RUNNING) rather than marked FAILED prematurely."""
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = mock.Mock()
    jm.get_job.return_value = None
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    monkeypatch.setattr(acts, "_is_last_attempt", lambda: False)

    async def step():
        raise RuntimeError("infra boom")

    with pytest.raises(RuntimeError, match="infra boom"):
        asyncio.run(acts._run_phase("j1", "a1", "discovery", 40, step))

    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert not failed


def test_run_phase_exception_on_last_attempt_skips_write_when_already_terminal(monkeypatch):
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = mock.Mock()
    jm.get_job.return_value = {"status": ax.JOB_STATUS_COMPLETED}
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    monkeypatch.setattr(acts, "_is_last_attempt", lambda: True)

    async def step():
        raise RuntimeError("infra boom")

    with pytest.raises(RuntimeError, match="infra boom"):
        asyncio.run(acts._run_phase("j1", "a1", "discovery", 40, step))

    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert not failed


# ---------------------------------------------------------------------------
# Per-phase activities (the annotation layer over the shared step helpers)
# ---------------------------------------------------------------------------


def _patch_activity_jobmanager(monkeypatch):
    """Patch ``audit_execution.get_job_manager`` with a Mock and return it."""
    from accessibility_audit_team import audit_execution as ax

    jm = mock.Mock()
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    return jm


def test_intake_activity_pass_writes_running(monkeypatch):
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = _patch_activity_jobmanager(monkeypatch)
    monkeypatch.setattr(
        ax, "run_intake_step", mock.AsyncMock(return_value=SimpleNamespace(failure_reason=""))
    )

    out = asyncio.run(acts.intake_activity("j1", "a1", {"web_urls": ["https://e.com"]}))
    assert out == {"status": "PASS", "audit_id": "a1"}
    running = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_RUNNING
    ]
    assert running and running[0].kwargs["current_phase"] == "intake"


def _phase_fail_result(reason: str) -> SimpleNamespace:
    """A ``failure_reason``-set phase result, shaped for ``_run_phase``'s FAIL
    branch (which also reads ``completed_phases``/``total_findings``/``model_dump``
    to write the full partial result, not just the error string)."""
    return SimpleNamespace(
        failure_reason=reason,
        total_findings=0,
        completed_phases=[],
        model_dump=lambda: {"audit_id": "a1", "success": False},
    )


async def _phase_fail_async(reason: str) -> SimpleNamespace:
    """Async wrapper around :func:`_phase_fail_result` for use as a ``step``
    callable's return value in direct ``_run_phase`` tests."""
    return _phase_fail_result(reason)


def test_intake_activity_fail_marks_job_failed(monkeypatch):
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = _patch_activity_jobmanager(monkeypatch)
    monkeypatch.setattr(
        ax,
        "run_intake_step",
        mock.AsyncMock(return_value=_phase_fail_result("unauditable target")),
    )

    out = asyncio.run(acts.intake_activity("j1", "a1", {}))
    assert out["status"] == "FAIL"
    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed and "unauditable target" in failed[0].kwargs.get("error")
    assert failed[0].kwargs.get("progress") == 100
    assert failed[0].kwargs.get("result") == {"audit_id": "a1", "success": False}


def test_intake_activity_rebuild_failure_propagates(monkeypatch):
    """A malformed request (bad URL scheme) fails loudly out of the activity rather
    than reading as a pipeline FAIL — it is rebuilt OUTSIDE the funnel."""
    from accessibility_audit_team.temporal import activities as acts

    _patch_activity_jobmanager(monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(acts.intake_activity("j1", "a1", {"web_urls": ["ftp://bad"]}))


def test_discovery_activity_pass_runs_under_heartbeat(monkeypatch):
    """Discovery runs the step under the heartbeat branch and returns PASS."""
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    _patch_activity_jobmanager(monkeypatch)
    monkeypatch.setattr(
        ax, "run_discovery_step", mock.AsyncMock(return_value=SimpleNamespace(failure_reason=""))
    )
    out = asyncio.run(acts.discovery_activity("j1", "a1"))
    assert out == {"status": "PASS", "audit_id": "a1"}


def test_verification_activity_threads_tech_stack(monkeypatch):
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    _patch_activity_jobmanager(monkeypatch)
    captured: dict = {}

    async def _fake_step(job_id, audit_id, tech_stack):
        captured["tech_stack"] = tech_stack
        return SimpleNamespace(failure_reason="")

    monkeypatch.setattr(ax, "run_verification_step", _fake_step)
    out = asyncio.run(acts.verification_activity("j1", "a1", {"web": "react"}))
    assert out["status"] == "PASS"
    assert captured["tech_stack"] == {"web": "react"}


def test_report_packaging_activity_pass(monkeypatch):
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    _patch_activity_jobmanager(monkeypatch)
    monkeypatch.setattr(
        ax,
        "run_report_packaging_step",
        mock.AsyncMock(return_value=SimpleNamespace(failure_reason="")),
    )
    out = asyncio.run(acts.report_packaging_activity("j1", "a1"))
    assert out == {"status": "PASS", "audit_id": "a1"}


def _finalized_result(success: bool) -> SimpleNamespace:
    return SimpleNamespace(
        success=success,
        total_findings=3,
        failure_reason="" if success else "boom",
        current_phase=SimpleNamespace(value="report_packaging"),
        completed_phases=[SimpleNamespace(value="intake"), SimpleNamespace(value="discovery")],
        model_dump=lambda: {"audit_id": "a1", "success": success},
    )


def test_finalize_activity_marks_completed(monkeypatch):
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = _patch_activity_jobmanager(monkeypatch)
    monkeypatch.setattr(
        ax, "finalize_audit_step", mock.AsyncMock(return_value=_finalized_result(True))
    )

    out = asyncio.run(acts.finalize_activity("j1", "a1"))
    assert out == {"status": "PASS", "audit_id": "a1"}
    completed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_COMPLETED
    ]
    assert completed and completed[0].kwargs["findings_count"] == 3
    assert completed[0].kwargs["progress"] == 100


def test_finalize_activity_marks_failed_when_unsuccessful(monkeypatch):
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = _patch_activity_jobmanager(monkeypatch)
    monkeypatch.setattr(
        ax, "finalize_audit_step", mock.AsyncMock(return_value=_finalized_result(False))
    )

    out = asyncio.run(acts.finalize_activity("j1", "a1"))
    assert out["status"] == "FAIL"
    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed and failed[0].kwargs.get("error") == "boom"


def test_finalize_activity_propagates_exception_and_marks_failed_on_last_attempt(monkeypatch):
    """An infra failure while assembling the result propagates (so Temporal
    retries) and, on the last scheduled attempt, marks the job FAILED first
    rather than leaving it stranded RUNNING forever."""
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = _patch_activity_jobmanager(monkeypatch)
    jm.get_job.return_value = None
    monkeypatch.setattr(
        ax, "finalize_audit_step", mock.AsyncMock(side_effect=RuntimeError("store down"))
    )
    monkeypatch.setattr(acts, "_is_last_attempt", lambda: True)

    with pytest.raises(RuntimeError, match="store down"):
        asyncio.run(acts.finalize_activity("j1", "a1"))

    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed and failed[0].kwargs.get("error") == "store down"


def test_finalize_activity_exception_recovers_progress_and_result_from_persisted_state(monkeypatch):
    """Same recovery contract as ``_run_phase``: ``finalize_audit_step`` raised
    before returning a fresh result, so the terminal write must recover
    progress/completed_phases/findings_count/result from persisted state instead
    of omitting them."""
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = _patch_activity_jobmanager(monkeypatch)
    jm.get_job.return_value = None
    monkeypatch.setattr(
        ax, "finalize_audit_step", mock.AsyncMock(side_effect=RuntimeError("store down"))
    )
    monkeypatch.setattr(acts, "_is_last_attempt", lambda: True)
    persisted = SimpleNamespace(
        completed_phases=[SimpleNamespace(value="report_packaging")],
        total_findings=7,
        model_dump=lambda: {"audit_id": "a1", "success": False},
    )
    monkeypatch.setattr(ax, "load_audit_state", mock.AsyncMock(return_value=persisted))

    with pytest.raises(RuntimeError, match="store down"):
        asyncio.run(acts.finalize_activity("j1", "a1"))

    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_FAILED
    ]
    assert failed
    kwargs = failed[0].kwargs
    assert kwargs.get("progress") == 100
    assert kwargs.get("completed_phases") == ["report_packaging"]
    assert kwargs.get("findings_count") == 7
    assert kwargs.get("result") == {"audit_id": "a1", "success": False}


def test_finalize_activity_skips_terminal_write_when_already_terminal(monkeypatch):
    """A concurrent path (e.g. a timebox timeout) already marked the job terminal
    while this attempt was in flight — the terminal write is skipped so it can't
    clobber that status, but the returned status still reflects this attempt."""
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    jm = _patch_activity_jobmanager(monkeypatch)
    jm.get_job.return_value = {"status": ax.JOB_STATUS_FAILED}
    monkeypatch.setattr(
        ax, "finalize_audit_step", mock.AsyncMock(return_value=_finalized_result(True))
    )

    out = asyncio.run(acts.finalize_activity("j1", "a1"))
    assert out == {"status": "PASS", "audit_id": "a1"}
    completed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == ax.JOB_STATUS_COMPLETED
    ]
    assert not completed


def test_retest_activity_delegates_to_run_retest_job(monkeypatch):
    from accessibility_audit_team import audit_execution as ax
    from accessibility_audit_team.temporal import activities as acts

    called = mock.AsyncMock()
    monkeypatch.setattr(ax, "run_retest_job", called)
    out = asyncio.run(acts.retest_activity("j1", "a1", ["f1"]))
    called.assert_awaited_once_with("j1", "a1", ["f1"])
    assert out == {"status": "done", "audit_id": "a1"}


# ---------------------------------------------------------------------------
# Retest execution core (symmetric with run_audit_job / execute_audit_job)
# ---------------------------------------------------------------------------


def test_run_retest_job_records_running_then_completed(monkeypatch):
    from accessibility_audit_team import audit_execution as ax

    jm = mock.Mock()
    jm.get_job.return_value = None
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    orch = mock.Mock()
    orch.run_retest = mock.AsyncMock(return_value=_fake_result(True))
    monkeypatch.setattr(ax, "get_orchestrator", lambda: orch)

    asyncio.run(ax.run_retest_job("j1", "a1", ["f1"]))
    orch.run_retest.assert_awaited_once_with("a1", ["f1"])
    statuses = [c.kwargs.get("status") for c in jm.update_job.call_args_list]
    assert ax.JOB_STATUS_RUNNING in statuses and "completed" in statuses


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_run_retest_job_idempotent_terminal_skip(monkeypatch, terminal_status):
    from accessibility_audit_team import audit_execution as ax

    jm = mock.Mock()
    jm.get_job.return_value = {"status": terminal_status}
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    orch = mock.Mock()
    orch.run_retest = mock.AsyncMock(return_value=_fake_result(True))
    monkeypatch.setattr(ax, "get_orchestrator", lambda: orch)

    asyncio.run(ax.run_retest_job("j1", "a1", []))
    orch.run_retest.assert_not_awaited()
    jm.update_job.assert_not_called()


def test_run_retest_job_propagates_infra_exception(monkeypatch):
    from accessibility_audit_team import audit_execution as ax

    jm = mock.Mock()
    jm.get_job.return_value = None
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    orch = mock.Mock()
    orch.run_retest = mock.AsyncMock(side_effect=RuntimeError("infra"))
    monkeypatch.setattr(ax, "get_orchestrator", lambda: orch)

    with pytest.raises(RuntimeError, match="infra"):
        asyncio.run(ax.run_retest_job("j1", "a1", []))
    statuses = [c.kwargs.get("status") for c in jm.update_job.call_args_list]
    assert ax.JOB_STATUS_RUNNING in statuses and "failed" not in statuses


def test_execute_retest_job_captures_exception(monkeypatch):
    from accessibility_audit_team import audit_execution as ax

    jm = mock.Mock()
    jm.get_job.return_value = None
    monkeypatch.setattr(ax, "get_job_manager", lambda: jm)
    orch = mock.Mock()
    orch.run_retest = mock.AsyncMock(side_effect=RuntimeError("kaboom"))
    monkeypatch.setattr(ax, "get_orchestrator", lambda: orch)

    asyncio.run(ax.execute_retest_job("j1", "a1", []))
    failed = [c for c in jm.update_job.call_args_list if c.kwargs.get("status") == "failed"]
    assert failed and any("kaboom" in str(c.kwargs.get("error")) for c in failed)


# ---------------------------------------------------------------------------
# API retest dispatch branch
# ---------------------------------------------------------------------------


def _patch_retest_api(monkeypatch, *, audit_found=True):
    """Mock ``main._job_manager`` and the orchestrator for retest-endpoint tests.

    ``audit_found`` controls what ``orchestrator.get_audit_state`` resolves to (a
    sentinel object when True, ``None`` when False → 404). Returns ``(main, jm)`` so
    callers can assert on the recorded ``update_job`` calls.
    """
    from accessibility_audit_team.api import main

    jm = mock.Mock()
    monkeypatch.setattr(main, "_job_manager", jm)
    orch = mock.Mock()
    orch.get_audit_state = mock.AsyncMock(return_value=object() if audit_found else None)
    monkeypatch.setattr(main, "get_orchestrator", lambda: orch)
    return main, jm


def test_retest_dispatcher_none_when_disabled(monkeypatch):
    import shared_temporal

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: False)
    from accessibility_audit_team.api import main

    assert main._get_retest_temporal_dispatcher() is None


def test_retest_dispatcher_returns_starter_when_enabled(monkeypatch):
    import shared_temporal

    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    from accessibility_audit_team.api import main
    from accessibility_audit_team.temporal.start_workflow import (
        start_accessibility_audit_retest_workflow,
    )

    assert main._get_retest_temporal_dispatcher() is start_accessibility_audit_retest_workflow


def test_retest_404_when_audit_missing(monkeypatch):
    _patch_retest_api(monkeypatch, audit_found=False)
    resp = client.post("/audit/nope/retest", json={"finding_ids": []})
    assert resp.status_code == 404


def test_retest_uses_temporal_when_enabled(monkeypatch):
    main, jm = _patch_retest_api(monkeypatch)
    dispatch = mock.Mock(return_value="accessibility_audit-retest-wf1")
    monkeypatch.setattr(main, "_get_retest_temporal_dispatcher", lambda: dispatch)
    exec_job = mock.AsyncMock()
    monkeypatch.setattr(main, "execute_retest_job", exec_job)

    resp = client.post("/audit/a1/retest", json={"finding_ids": ["f1"]})
    assert resp.status_code == 200
    body = resp.json()
    assert "(Temporal)" in body["message"]
    assert body["workflow_id"] == "accessibility_audit-retest-wf1"
    dispatch.assert_called_once()
    # Dispatcher is called positionally as (job_id, audit_id, finding_ids).
    args = dispatch.call_args.args
    assert args[1] == "a1" and args[2] == ["f1"]
    exec_job.assert_not_awaited()


def test_retest_uses_background_when_disabled(monkeypatch):
    main, _ = _patch_retest_api(monkeypatch)
    monkeypatch.setattr(main, "_get_retest_temporal_dispatcher", lambda: None)
    executed = mock.AsyncMock()
    monkeypatch.setattr(main, "execute_retest_job", executed)

    resp = client.post("/audit/a1/retest", json={"finding_ids": []})
    assert resp.status_code == 200
    body = resp.json()
    assert "(Temporal)" not in body["message"]
    assert body["workflow_id"] is None
    executed.assert_awaited_once()


def test_retest_fails_fast_on_dispatch_error(monkeypatch):
    main, jm = _patch_retest_api(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("temporal down")

    monkeypatch.setattr(main, "_get_retest_temporal_dispatcher", lambda: _boom)
    executed = mock.AsyncMock()
    monkeypatch.setattr(main, "execute_retest_job", executed)

    resp = client.post("/audit/a1/retest", json={"finding_ids": []})
    assert resp.status_code == 500
    executed.assert_not_awaited()
    failed = [
        c for c in jm.update_job.call_args_list if c.kwargs.get("status") == main.JOB_STATUS_FAILED
    ]
    assert failed


# ---------------------------------------------------------------------------
# Cache-only read endpoints (findings/report/export/case-study) — existence
# check against the persisted audit state, not just the in-memory cache
# ---------------------------------------------------------------------------


def _patch_orchestrator_for_reads(monkeypatch, *, audit_found=True):
    """Mock ``main.get_orchestrator`` for the findings/report/export/case-study
    endpoint tests. ``audit_found`` controls what ``get_audit_state`` resolves to
    (a sentinel when True, ``None`` when False -> 404). Returns the orchestrator
    mock so callers can further configure/assert on it."""
    from accessibility_audit_team.api import main

    orch = mock.Mock()
    orch.get_audit_state = mock.AsyncMock(return_value=object() if audit_found else None)
    monkeypatch.setattr(main, "get_orchestrator", lambda: orch)
    return orch


def test_get_audit_findings_404_when_audit_missing(monkeypatch):
    orch = _patch_orchestrator_for_reads(monkeypatch, audit_found=False)
    orch.get_findings = mock.Mock(return_value=[])

    resp = client.get("/audit/nope/findings")

    assert resp.status_code == 404
    orch.get_findings.assert_not_called()


def test_get_audit_findings_found_cross_process_returns_empty_list(monkeypatch):
    """A Temporal-executed audit whose findings live only in the artifact store
    (not this API process's in-memory orchestrator cache) must resolve via
    ``get_audit_state`` rather than false-404ing — the prior reactive check,
    which only inspected the (empty, uncached) in-memory findings list, would
    404 here even though the audit exists."""
    orch = _patch_orchestrator_for_reads(monkeypatch, audit_found=True)
    orch.get_findings = mock.Mock(return_value=[])

    resp = client.get("/audit/a1/findings")

    assert resp.status_code == 200
    assert resp.json()["findings"] == []


def test_get_audit_report_404_when_audit_missing(monkeypatch):
    _patch_orchestrator_for_reads(monkeypatch, audit_found=False)

    resp = client.get("/audit/nope/report")

    assert resp.status_code == 404


def test_get_audit_report_400_when_not_complete(monkeypatch):
    orch = _patch_orchestrator_for_reads(monkeypatch, audit_found=True)
    orch.get_audit_status = mock.Mock(return_value={"status": "in_progress"})

    resp = client.get("/audit/a1/report")

    assert resp.status_code == 400


def test_export_backlog_404_when_audit_missing(monkeypatch):
    _patch_orchestrator_for_reads(monkeypatch, audit_found=False)

    resp = client.post("/audit/nope/export")

    assert resp.status_code == 404


def test_generate_case_study_404_when_audit_missing(monkeypatch):
    _patch_orchestrator_for_reads(monkeypatch, audit_found=False)

    resp = client.post("/audit/nope/case-study", json={})

    assert resp.status_code == 404
