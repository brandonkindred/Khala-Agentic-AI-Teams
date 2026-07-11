"""Tests for the SOC2 compliance team's decomposed Temporal activities and
workflow.

Activities are exercised offline via ``temporalio.testing.ActivityEnvironment``
with the pipeline steps and job-store patched; the workflow ``run`` body is
exercised by monkeypatching ``workflow.execute_activity`` (no worker/server).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import pytest
from temporalio.testing import ActivityEnvironment

from soc2_compliance_team.models import (
    NextStepsDocument,
    RepoContext,
    TSCAuditResult,
    TSCCategory,
)


def _patch_update_job(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Capture ``_update_job`` calls made by the activities."""
    calls: List[Dict[str, Any]] = []

    def _fake(job_id: str, **fields: Any) -> None:
        calls.append({"job_id": job_id, **fields})

    monkeypatch.setattr("soc2_compliance_team.api.main._update_job", _fake)
    return calls


# ---------------------------------------------------------------------------
# load_repo_activity
# ---------------------------------------------------------------------------


def test_load_repo_activity_returns_context_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    from soc2_compliance_team import pipeline
    from soc2_compliance_team.temporal import activities as amod

    calls = _patch_update_job(monkeypatch)
    monkeypatch.setattr(pipeline, "load_context", lambda p: RepoContext(repo_path=p))

    out = ActivityEnvironment().run(amod.load_repo_activity, "job-1", "/repo/x")

    assert out["repo_path"] == "/repo/x"
    statuses = [c.get("status") for c in calls]
    stages = [c.get("current_stage") for c in calls]
    assert "running" in statuses
    assert "Loading repository" in stages
    assert "Running TSC audits" in stages


def test_load_repo_activity_reraises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from soc2_compliance_team import pipeline
    from soc2_compliance_team.temporal import activities as amod

    _patch_update_job(monkeypatch)

    def _boom(_p):
        raise ValueError("bad repo")

    monkeypatch.setattr(pipeline, "load_context", _boom)
    with pytest.raises(ValueError, match="bad repo"):
        ActivityEnvironment().run(amod.load_repo_activity, "job-1", "/repo/x")


# ---------------------------------------------------------------------------
# audit_criterion_activity
# ---------------------------------------------------------------------------


def test_audit_criterion_activity_returns_result_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    from soc2_compliance_team import pipeline
    from soc2_compliance_team.temporal import activities as amod

    captured: Dict[str, Any] = {}

    def _fake_safe(category, context):
        captured["category"] = category
        captured["context"] = context
        return TSCAuditResult(category=category, summary="done", findings=[], compliant=True)

    monkeypatch.setattr(pipeline, "audit_criterion_safe", _fake_safe)

    ctx = RepoContext(repo_path="/repo").model_dump(mode="json")
    out = ActivityEnvironment().run(
        amod.audit_criterion_activity, "job-1", TSCCategory.PRIVACY.value, ctx
    )

    assert out["category"] == TSCCategory.PRIVACY.value
    assert out["summary"] == "done"
    assert captured["category"] is TSCCategory.PRIVACY
    assert isinstance(captured["context"], RepoContext)


def test_audit_criterion_activity_isolates_underlying_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the real audit fails, the activity returns a fail-closed placeholder
    (via ``audit_criterion_safe``) rather than raising."""
    from soc2_compliance_team import pipeline
    from soc2_compliance_team.temporal import activities as amod

    def _boom(_key: str) -> Any:
        raise RuntimeError("no llm configured")

    monkeypatch.setattr(pipeline, "get_client", _boom)

    ctx = RepoContext(repo_path="/repo").model_dump(mode="json")
    out = ActivityEnvironment().run(
        amod.audit_criterion_activity, "job-1", TSCCategory.SECURITY.value, ctx
    )

    assert out["category"] == TSCCategory.SECURITY.value
    assert out["compliant"] is False
    assert "could not be completed" in out["summary"].lower()


def test_audit_criterion_activity_rejects_bad_criterion() -> None:
    """A criterion string that is not a valid TSCCategory value raises."""
    from soc2_compliance_team.temporal import activities as amod

    ctx = RepoContext(repo_path="/repo").model_dump(mode="json")
    with pytest.raises(ValueError):
        ActivityEnvironment().run(amod.audit_criterion_activity, "job-1", "bogus", ctx)


# ---------------------------------------------------------------------------
# write_report_activity
# ---------------------------------------------------------------------------


def test_write_report_activity_persists_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    from soc2_compliance_team import pipeline
    from soc2_compliance_team.temporal import activities as amod

    calls = _patch_update_job(monkeypatch)
    monkeypatch.setattr(
        pipeline, "write_report", lambda rp, tsc: (None, NextStepsDocument(title="Next"))
    )

    tsc_dicts = [
        TSCAuditResult(category=c, summary="s", findings=[], compliant=True).model_dump(mode="json")
        for c in TSCCategory
    ]
    out = ActivityEnvironment().run(amod.write_report_activity, "job-1", "/repo", tsc_dicts)

    assert out["status"] == "completed"
    assert out["has_findings"] is False
    completed = [c for c in calls if c.get("status") == "completed"]
    assert completed and completed[0]["result"]["status"] == "completed"


def test_write_report_activity_reraises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from soc2_compliance_team import pipeline
    from soc2_compliance_team.temporal import activities as amod

    _patch_update_job(monkeypatch)

    def _boom(rp, tsc):
        raise RuntimeError("report boom")

    monkeypatch.setattr(pipeline, "write_report", _boom)
    with pytest.raises(RuntimeError, match="report boom"):
        ActivityEnvironment().run(amod.write_report_activity, "job-1", "/repo", [])


# ---------------------------------------------------------------------------
# mark_failed_activity
# ---------------------------------------------------------------------------


def test_mark_failed_activity_writes_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from soc2_compliance_team.temporal import activities as amod

    calls = _patch_update_job(monkeypatch)
    ActivityEnvironment().run(amod.mark_failed_activity, "job-1", "kaboom")
    assert calls[0]["status"] == "failed"
    assert calls[0]["error"] == "kaboom"


def test_mark_failed_activity_swallows_job_store_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from soc2_compliance_team.temporal import activities as amod

    def _boom(job_id, **fields):
        raise RuntimeError("job store down")

    monkeypatch.setattr("soc2_compliance_team.api.main._update_job", _boom)
    # Must not raise — terminal write is best-effort.
    ActivityEnvironment().run(amod.mark_failed_activity, "job-1", "kaboom")


# ---------------------------------------------------------------------------
# workflow run body
# ---------------------------------------------------------------------------


class _FakeExecute:
    """Records execute_activity calls and returns canned per-activity values."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.fail_on = fail_on

    async def __call__(self, activity, args=None, **kwargs):  # noqa: ANN001
        name = getattr(activity, "__name__", str(activity))
        self.calls.append({"name": name, "args": args})
        if self.fail_on and name == self.fail_on:
            raise RuntimeError(f"{name} failed")
        if name == "load_repo_activity":
            return {"repo_path": args[1]}
        if name == "audit_criterion_activity":
            return {"category": args[1], "summary": "x", "findings": [], "compliant": True}
        if name == "write_report_activity":
            return {"status": "completed", "has_findings": False}
        return None


def test_workflow_runs_load_fanout_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from soc2_compliance_team.temporal import workflows as wmod

    fake = _FakeExecute()
    monkeypatch.setattr(wmod.workflow, "execute_activity", fake)

    result = asyncio.run(wmod.Soc2AuditWorkflow().run("job-1", "/repo/path"))

    names = [c["name"] for c in fake.calls]
    assert names[0] == "load_repo_activity"
    assert names.count("audit_criterion_activity") == 5
    assert names[-1] == "write_report_activity"
    # The five criteria are fanned out in canonical order.
    audit_criteria = [c["args"][1] for c in fake.calls if c["name"] == "audit_criterion_activity"]
    assert audit_criteria == [c.value for c in TSCCategory]
    assert result == {"status": "completed", "has_findings": False}


def test_workflow_marks_failed_on_activity_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from soc2_compliance_team.temporal import workflows as wmod

    fake = _FakeExecute(fail_on="write_report_activity")
    monkeypatch.setattr(wmod.workflow, "execute_activity", fake)

    with pytest.raises(RuntimeError, match="write_report_activity failed"):
        asyncio.run(wmod.Soc2AuditWorkflow().run("job-1", "/repo/path"))

    # The except-path fires the terminal failure marker.
    assert any(c["name"] == "mark_failed_activity" for c in fake.calls)


def test_workflow_reraises_original_when_mark_failed_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``mark_failed_activity`` itself raises, the ORIGINAL audit failure must
    still propagate (not be replaced by the marker's error)."""
    from soc2_compliance_team.temporal import workflows as wmod

    class _BothFail:
        async def __call__(self, activity, args=None, **kwargs):  # noqa: ANN001
            name = getattr(activity, "__name__", "")
            if name == "load_repo_activity":
                return {"repo_path": args[1]}
            if name == "audit_criterion_activity":
                return {"category": args[1], "summary": "x", "findings": [], "compliant": True}
            if name == "write_report_activity":
                raise RuntimeError("original report failure")
            if name == "mark_failed_activity":
                raise RuntimeError("mark-failed also failed")
            return None

    monkeypatch.setattr(wmod.workflow, "execute_activity", _BothFail())
    # The logger call in the inner except needs no workflow context here.
    monkeypatch.setattr(wmod.workflow, "logger", logging.getLogger("test-soc2-wf"))

    with pytest.raises(RuntimeError, match="original report failure"):
        asyncio.run(wmod.Soc2AuditWorkflow().run("job-1", "/repo/path"))
