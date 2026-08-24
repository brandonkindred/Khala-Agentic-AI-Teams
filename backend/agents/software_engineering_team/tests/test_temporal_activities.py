"""Tests for the SE Temporal activity wrappers.

Each activity is a thin wrapper around an orchestrator entry point with an
exception-handling outer try/except. The tests cover the happy path (the
wrapped function is called) and the exception path (failure is captured into
the job store via ``update_job``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import pytest
from temporalio.common import RetryPolicy


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    return patched_job_store


def _fake_activity_info(attempt: int, maximum_attempts: int = 3):
    return type(
        "I",
        (),
        {"retry_policy": RetryPolicy(maximum_attempts=maximum_attempts), "attempt": attempt},
    )()


def test_retry_failed_activity_success(monkeypatch) -> None:
    from software_engineering_team.temporal import activities

    called: Dict[str, str] = {}

    def fake(job_id, *, trace_id=None):
        called["id"] = job_id
        called["trace_id"] = trace_id

    monkeypatch.setattr("software_engineering_team.orchestrator.run_failed_tasks", fake)
    activities.retry_failed_activity("j1")
    assert called["id"] == "j1"
    assert called["trace_id"]  # activity generates one when the caller passes none


def test_retry_failed_activity_failure(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("j-fail", repo_path=str(tmp_path))

    def boom(_, **kw):
        raise RuntimeError("retry exploded")

    monkeypatch.setattr("software_engineering_team.orchestrator.run_failed_tasks", boom)
    with pytest.raises(RuntimeError, match="retry exploded"):
        activities.retry_failed_activity("j-fail")
    job = js.get_job("j-fail")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_retry_failed_activity_non_final_attempt_does_not_mark_failed(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """Mirror of the other activities' non-final-attempt tests for retry_failed_activity."""
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("rf-retry", repo_path=str(tmp_path))

    monkeypatch.setattr(activities.activity, "in_activity", lambda: True)
    monkeypatch.setattr(activities.activity, "info", lambda: _fake_activity_info(attempt=1))

    def boom(_, **kw):
        raise RuntimeError("transient retry failure")

    monkeypatch.setattr("software_engineering_team.orchestrator.run_failed_tasks", boom)
    with pytest.raises(RuntimeError, match="transient retry failure"):
        activities.retry_failed_activity("rf-retry")

    job = js.get_job("rf-retry")
    assert job["status"] != js.JOB_STATUS_FAILED


def test_run_frontend_code_v2_activity_failure(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("fv2-j", repo_path=str(tmp_path))

    def boom(*a, **kw):
        raise RuntimeError("v2 frontend failed")

    monkeypatch.setattr(activities, "_run_frontend_code_v2_impl", boom)
    with pytest.raises(RuntimeError, match="v2 frontend failed"):
        activities.run_frontend_code_v2_activity("fv2-j", str(tmp_path), {"id": "t1"})
    job = js.get_job("fv2-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_run_backend_code_v2_activity_failure(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("bv2-j", repo_path=str(tmp_path))

    def boom(*a, **kw):
        raise RuntimeError("v2 backend failed")

    monkeypatch.setattr(activities, "_run_backend_code_v2_impl", boom)
    with pytest.raises(RuntimeError, match="v2 backend failed"):
        activities.run_backend_code_v2_activity("bv2-j", str(tmp_path), {"id": "t1"})
    job = js.get_job("bv2-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_run_frontend_code_v2_activity_non_final_attempt_does_not_mark_failed(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """On a non-final Temporal attempt, a transient failure does NOT mark the job
    FAILED (Temporal will retry) — only the final attempt marks it, so a retry
    that later succeeds never leaves a transient FAILED status behind."""
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("fv2-retry", repo_path=str(tmp_path))

    monkeypatch.setattr(activities.activity, "in_activity", lambda: True)
    monkeypatch.setattr(activities.activity, "info", lambda: _fake_activity_info(attempt=1))

    def boom(*a, **kw):
        raise RuntimeError("transient frontend failure")

    monkeypatch.setattr(activities, "_run_frontend_code_v2_impl", boom)
    with pytest.raises(RuntimeError, match="transient frontend failure"):
        activities.run_frontend_code_v2_activity("fv2-retry", str(tmp_path), {"id": "t1"})

    job = js.get_job("fv2-retry")
    assert job["status"] != js.JOB_STATUS_FAILED


def test_run_backend_code_v2_activity_non_final_attempt_does_not_mark_failed(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """Mirror of the frontend non-final-attempt test for the backend activity."""
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("bv2-retry", repo_path=str(tmp_path))

    monkeypatch.setattr(activities.activity, "in_activity", lambda: True)
    monkeypatch.setattr(activities.activity, "info", lambda: _fake_activity_info(attempt=1))

    def boom(*a, **kw):
        raise RuntimeError("transient backend failure")

    monkeypatch.setattr(activities, "_run_backend_code_v2_impl", boom)
    with pytest.raises(RuntimeError, match="transient backend failure"):
        activities.run_backend_code_v2_activity("bv2-retry", str(tmp_path), {"id": "t1"})

    job = js.get_job("bv2-retry")
    assert job["status"] != js.JOB_STATUS_FAILED


def test_run_product_analysis_activity_failure(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pa-j", repo_path=str(tmp_path))

    def boom(*a, **kw):
        raise RuntimeError("PA failed")

    monkeypatch.setattr(activities, "_run_product_analysis_impl", boom)
    with pytest.raises(RuntimeError, match="PA failed"):
        activities.run_product_analysis_activity("pa-j", str(tmp_path), "spec")
    job = js.get_job("pa-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_run_product_analysis_activity_non_final_attempt_does_not_mark_failed(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """Mirror of the frontend/backend non-final-attempt tests for product analysis."""
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pa-retry", repo_path=str(tmp_path))

    monkeypatch.setattr(activities.activity, "in_activity", lambda: True)
    monkeypatch.setattr(activities.activity, "info", lambda: _fake_activity_info(attempt=1))

    def boom(*a, **kw):
        raise RuntimeError("transient PA failure")

    monkeypatch.setattr(activities, "_run_product_analysis_impl", boom)
    with pytest.raises(RuntimeError, match="transient PA failure"):
        activities.run_product_analysis_activity("pa-retry", str(tmp_path), "spec")

    job = js.get_job("pa-retry")
    assert job["status"] != js.JOB_STATUS_FAILED


def test_run_frontend_code_v2_activity_happy(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("fv2-ok", repo_path=str(tmp_path))
    called = {}

    def fake_impl(job_id, repo_path, task_dict, arch):
        called["job_id"] = job_id

    monkeypatch.setattr(activities, "_run_frontend_code_v2_impl", fake_impl)
    activities.run_frontend_code_v2_activity("fv2-ok", str(tmp_path), {"id": "t"}, "arch")
    assert called["job_id"] == "fv2-ok"


def test_run_backend_code_v2_activity_happy(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("bv2-ok", repo_path=str(tmp_path))
    called = {}

    def fake_impl(job_id, repo_path, task_dict, arch):
        called["job_id"] = job_id

    monkeypatch.setattr(activities, "_run_backend_code_v2_impl", fake_impl)
    activities.run_backend_code_v2_activity("bv2-ok", str(tmp_path), {"id": "t"}, "arch")
    assert called["job_id"] == "bv2-ok"


def test_run_product_analysis_activity_happy(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pa-ok", repo_path=str(tmp_path))
    called = {}

    def fake_impl(job_id, repo_path, spec, initial_spec_path=None):
        called["job_id"] = job_id

    monkeypatch.setattr(activities, "_run_product_analysis_impl", fake_impl)
    activities.run_product_analysis_activity("pa-ok", str(tmp_path), "spec")
    assert called["job_id"] == "pa-ok"


def test_parse_spec_activity_exception_path(
    monkeypatch, tmp_path, patched_job_store, caplog
) -> None:
    """No spec file in repo → spec parser raises FileNotFoundError, which the
    outer except in parse_spec_activity captures and re-raises after marking
    the job FAILED. The failure log carries the trace id bound for this activity."""
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("ps-j", repo_path=str(tmp_path))
    caplog.set_level(logging.ERROR)
    with pytest.raises(Exception):
        activities.parse_spec_activity("ps-j", str(tmp_path), trace_id="parse-spec-trace-id")
    job = js.get_job("ps-j")
    assert job["status"] == js.JOB_STATUS_FAILED

    failure_records = [r for r in caplog.records if "parse_spec_activity failed" in r.message]
    assert failure_records, "expected the failure log to be emitted"
    assert failure_records[-1].trace_id == "parse-spec-trace-id"


def test_parse_spec_activity_non_final_attempt_does_not_mark_failed(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """No spec file in repo → spec parser raises FileNotFoundError. On a non-final
    Temporal attempt the job is NOT marked FAILED (Temporal will retry) — only the
    final attempt marks it, mirroring the code-v2 activities' guard."""
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("ps-retry", repo_path=str(tmp_path))

    monkeypatch.setattr(activities.activity, "in_activity", lambda: True)
    monkeypatch.setattr(activities.activity, "info", lambda: _fake_activity_info(attempt=1))

    with pytest.raises(Exception):
        activities.parse_spec_activity("ps-retry", str(tmp_path), trace_id="parse-spec-retry")

    job = js.get_job("ps-retry")
    assert job["status"] != js.JOB_STATUS_FAILED


def test_parse_spec_activity_with_sprint_id_matches_shared_helper_output(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """When sprint_id is set, parse_spec_activity synthesizes spec content via
    shared.sprint_scope.load_requirements_from_sprint (same helper the V1 path
    uses) instead of reading an on-disk spec, and skips the LLM parse + PRA agent
    entirely — no LLM/PRA mocking is needed since neither runs on this path."""
    from datetime import datetime, timezone

    from product_delivery.models import Sprint, SprintWithStories, Story
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.shared.sprint_scope import load_requirements_from_sprint
    from software_engineering_team.temporal import activities

    def _now():
        return datetime.now(tz=timezone.utc)

    story = Story(
        id="story-1",
        epic_id="epic-1",
        title="Login form",
        user_story="As a user, I want to log in",
        status="proposed",
        wsjf_score=None,
        rice_score=None,
        estimate_points=None,
        author="tester",
        created_at=_now(),
        updated_at=_now(),
    )
    sprint = Sprint(
        id="sprint-1",
        product_id="product-1",
        name="Iteration 5",
        capacity_points=13.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="tester",
        created_at=_now(),
        updated_at=_now(),
    )
    sprint_view = SprintWithStories(
        sprint=sprint, stories=[story], acceptance_criteria_by_story_id={}
    )

    class _StubStore:
        def get_sprint_with_stories(self, sprint_id: str):
            return sprint_view

    import product_delivery as pd_mod

    monkeypatch.setattr(pd_mod, "get_store", lambda: _StubStore())

    js.create_job("ps-sprint", repo_path=str(tmp_path))
    result = activities.parse_spec_activity(
        "ps-sprint", str(tmp_path), trace_id="t1", sprint_id="sprint-1"
    )

    expected_requirements, expected_spec_content = load_requirements_from_sprint("sprint-1")
    assert result["spec_content"] == expected_spec_content
    assert result["requirements_title"] == expected_requirements.title
    # Sprint path skips PRA: the synthesized spec is used as-is, no PRA iterations.
    assert result["validated_spec"] == expected_spec_content
    assert result["pra_iterations"] == 0

    job = js.get_job("ps-sprint")
    assert job["status"] != js.JOB_STATUS_FAILED


def test_parse_spec_activity_rejects_sprint_id_and_spec_content_override_together(
    tmp_path, patched_job_store
) -> None:
    """sprint_id and spec_content_override are mutually exclusive. Must raise (not
    return normally) — RunTeamWorkflowV2 doesn't inspect SpecParseResult for a
    failure sentinel, so a normal return would let the workflow barrel into
    Phase 2/3 on an empty spec instead of stopping after this activity fails."""
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("ps-both", repo_path=str(tmp_path))
    with pytest.raises(ValueError, match="mutually exclusive"):
        activities.parse_spec_activity(
            "ps-both",
            str(tmp_path),
            spec_content_override="explicit spec",
            sprint_id="sprint-1",
        )
    job = js.get_job("ps-both")
    assert job["status"] == js.JOB_STATUS_FAILED
    assert "mutually exclusive" in (job.get("error") or "")


def test_plan_project_activity_exception_path(monkeypatch, tmp_path, patched_job_store) -> None:
    """Cover the outer except in plan_project_activity.

    Patch ``run_planning_workflow`` to raise — deterministically drives the outer
    ``except`` branch without any network I/O. ``_get_agents`` is stubbed too since
    it runs unconditionally before ``run_planning_workflow`` and would otherwise
    build a real (LLM-backed) agent fleet.
    """
    from unittest.mock import MagicMock

    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pp-j", repo_path=str(tmp_path))

    # get_client("project_planning") is evaluated as an argument to
    # run_planning_workflow before the patched boom runs; use the dummy provider so
    # it returns a client (rather than raising LLMNotConfiguredError, which would
    # mask the RuntimeError).
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    monkeypatch.setattr(
        "software_engineering_team.orchestrator._get_agents",
        lambda: {"architecture": MagicMock()},
    )

    def boom(*a, **kw):
        raise RuntimeError("check failed")

    monkeypatch.setattr("planning_team.orchestrator.run_workflow", boom)
    with pytest.raises(RuntimeError):
        activities.plan_project_activity(
            "pp-j",
            str(tmp_path),
            {"spec_content": "spec", "validated_spec": "spec", "plan_dir": str(tmp_path)},
        )
    job = js.get_job("pp-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_plan_project_activity_non_final_attempt_does_not_mark_failed(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """Mirror of test_plan_project_activity_exception_path with a non-final Temporal
    attempt: the job is NOT marked FAILED (Temporal will retry), only re-raised."""
    from unittest.mock import MagicMock

    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pp-retry", repo_path=str(tmp_path))

    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    monkeypatch.setattr(
        "software_engineering_team.orchestrator._get_agents",
        lambda: {"architecture": MagicMock()},
    )
    monkeypatch.setattr(activities.activity, "in_activity", lambda: True)
    monkeypatch.setattr(activities.activity, "info", lambda: _fake_activity_info(attempt=1))

    def boom(*a, **kw):
        raise RuntimeError("transient planning failure")

    monkeypatch.setattr("planning_team.orchestrator.run_workflow", boom)
    with pytest.raises(RuntimeError):
        activities.plan_project_activity(
            "pp-retry",
            str(tmp_path),
            {"spec_content": "spec", "validated_spec": "spec", "plan_dir": str(tmp_path)},
        )
    job = js.get_job("pp-retry")
    assert job["status"] != js.JOB_STATUS_FAILED


def test_plan_project_activity_wires_lazy_architecture_callback(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """The activity hands Planning a working architecture callback.

    Regression guard: the callback must resolve ``agents["architecture"]`` lazily (only
    when invoked, not when the activity builds it) and return the agent's overview. This
    exercises the shared ``_make_planning_architecture_fn`` wiring end-to-end through the
    Temporal path, which the mocked-``run_workflow`` tests otherwise never invoke.
    """
    from unittest.mock import MagicMock

    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pp-wire", repo_path=str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "dummy")

    # A duck-typed architecture agent whose run() yields a scripted overview.
    arch_agent = MagicMock()
    arch_output = MagicMock()
    arch_output.architecture = MagicMock(overview="Wired overview")
    arch_agent.run.return_value = arch_output

    resolved = {"n": 0}

    class _Registry:
        def __getitem__(self, key):
            assert key == "architecture"
            resolved["n"] += 1
            return arch_agent

    monkeypatch.setattr("software_engineering_team.orchestrator._get_agents", lambda: _Registry())

    captured: Dict[str, Any] = {}

    def _fake_run_workflow(*args, **kwargs):
        captured["run_architecture_fn"] = kwargs["run_architecture_fn"]
        # Short-circuit: a failure result makes plan_project_activity return early,
        # before the adapter runs, so this test stays focused on the callback wiring.
        return {"success": False, "failure_reason": "stop here"}

    monkeypatch.setattr("planning_team.orchestrator.run_workflow", _fake_run_workflow)

    mock_record_planning_run = MagicMock()
    monkeypatch.setattr(
        "software_engineering_team.shared.planning_audit.record_se_planning_run",
        mock_record_planning_run,
    )

    activities.plan_project_activity(
        "pp-wire",
        str(tmp_path),
        {"spec_content": "spec", "validated_spec": "spec", "plan_dir": str(tmp_path)},
    )

    # The callback was passed but not yet invoked → the registry must not have been read.
    assert resolved["n"] == 0, "architecture agent must be resolved lazily, not eagerly"

    run_architecture_fn = captured["run_architecture_fn"]
    overview = run_architecture_fn(
        spec_content="# Spec", prd_content=None, repo_path=str(tmp_path), client_context=None
    )

    assert overview == "Wired overview"
    assert resolved["n"] == 1
    # run_workflow short-circuited with success=False above, so the audit write
    # must not fire for this run.
    mock_record_planning_run.assert_not_called()


def test_plan_project_activity_records_planning_run_on_success(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """On a successful planning run, the activity records a planning_runs audit row.

    Mirrors ``test_plan_project_activity_wires_lazy_architecture_callback``'s setup but
    drives ``run_workflow`` to success instead of short-circuiting on failure — no other
    test in this file reaches ``plan_project_activity``'s success path. ``adapt_planning_result``
    is patched so the test stays focused on the audit-write wiring rather than the adapter.
    """
    from unittest.mock import MagicMock

    from shared.dev_models.models import ProductRequirements
    from software_engineering_team.planning_adapter import PlanningAdapterResult
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pp-success", repo_path=str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "dummy")

    monkeypatch.setattr(
        "software_engineering_team.orchestrator._get_agents",
        lambda: {"architecture": MagicMock()},
    )

    planning_result = {
        "success": True,
        "summary": "Planning completed; handoff package ready.",
        "handoff_package": {"summary": "Build a widget API."},
        "open_questions": [],
        "resolved_questions": [],
    }
    monkeypatch.setattr("planning_team.orchestrator.run_workflow", lambda *a, **kw: planning_result)

    adapter_result = PlanningAdapterResult(
        requirements=ProductRequirements(
            title="Test",
            description="Desc",
            acceptance_criteria=["Ship it"],
            constraints=[],
        ),
        project_overview={"goals": "Ship", "features_and_functionality_doc": "API"},
        open_questions=[],
        assumptions=[],
    )
    monkeypatch.setattr(
        "software_engineering_team.planning_adapter.adapt_planning_result",
        lambda *a, **kw: adapter_result,
    )

    mock_record_planning_run = MagicMock()
    monkeypatch.setattr(
        "software_engineering_team.shared.planning_audit.record_se_planning_run",
        mock_record_planning_run,
    )

    activities.plan_project_activity(
        "pp-success",
        str(tmp_path),
        {"spec_content": "spec", "validated_spec": "spec", "plan_dir": str(tmp_path)},
    )

    mock_record_planning_run.assert_called_once_with("pp-success", planning_result)


def test_coding_update_callback_forwards_without_heartbeat(monkeypatch) -> None:
    """The callback forwards kwargs to update_job and must NOT heartbeat.

    Liveness is owned solely by the background beater (single-liveness owner), so the
    update callback only persists progress.
    """
    from software_engineering_team.temporal import activities

    captured: Dict[str, Any] = {}
    beats = {"n": 0}
    monkeypatch.setattr(
        activities, "update_job", lambda jid, **kw: captured.update({"jid": jid, **kw})
    )
    monkeypatch.setattr(
        activities.activity, "heartbeat", lambda *a, **k: beats.__setitem__("n", beats["n"] + 1)
    )

    cb = activities._coding_update_callback("job-x")
    cb(status_text="implementing")

    assert captured["jid"] == "job-x"
    assert captured["status_text"] == "implementing"
    assert beats["n"] == 0, "update callback must not emit a heartbeat (single-liveness owner)"


def test_execute_coding_team_activity_exception_path(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """Bogus adapter_result_dict triggers an exception inside the activity;
    the outer except marks the job FAILED and re-raises."""
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("ec-j", repo_path=str(tmp_path))
    with pytest.raises(Exception):
        activities.execute_coding_team_activity(
            "ec-j",
            str(tmp_path),
            {"adapter_result_dict": {}, "spec_content_for_planning": ""},
        )
    job = js.get_job("ec-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_coding_heartbeat_interval_env(monkeypatch) -> None:
    """Interval: valid positive float honored; zero/negative/garbage/unset fall back to 30s."""
    from software_engineering_team.temporal import activities

    monkeypatch.setenv("CODING_TEAM_HEARTBEAT_INTERVAL_S", "12.5")
    assert activities._coding_heartbeat_interval_s() == 12.5
    monkeypatch.setenv("CODING_TEAM_HEARTBEAT_INTERVAL_S", "0")
    assert activities._coding_heartbeat_interval_s() == 30.0
    monkeypatch.setenv("CODING_TEAM_HEARTBEAT_INTERVAL_S", "-5")
    assert activities._coding_heartbeat_interval_s() == 30.0
    monkeypatch.setenv("CODING_TEAM_HEARTBEAT_INTERVAL_S", "garbage")
    assert activities._coding_heartbeat_interval_s() == 30.0
    monkeypatch.delenv("CODING_TEAM_HEARTBEAT_INTERVAL_S", raising=False)
    assert activities._coding_heartbeat_interval_s() == 30.0


def test_execute_coding_team_activity_passes_band_and_default_llm_getter(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """The Temporal coding activity must mirror the thread path's call contract:
    pass the coding progress band (or the bar collapses to the standalone 0-95
    defaults at the planning handoff) and NOT pass a raw get_llm (the coding
    team's default getter builds strands models with reasoning-stream capture;
    a raw client both looks stalled during long calls and cannot construct
    strands Agent objects)."""
    from software_engineering_team.orchestrator import PROGRESS_BAND_CODING
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("ec-band", repo_path=str(tmp_path))

    captured: Dict[str, Any] = {}

    def fake_orchestrator(job_id, repo_path, plan_input, **kwargs):
        captured.update(kwargs, job_id=job_id)

    import software_engineering_team.coding_team_orchestrator as coding_orch

    monkeypatch.setattr(coding_orch, "run_coding_team_orchestrator", fake_orchestrator)

    from planning_adapter import PlanningAdapterResult

    from shared.dev_models.models import ProductRequirements

    adapter_dict = PlanningAdapterResult(
        requirements=ProductRequirements(
            title="T",
            description="d",
            acceptance_criteria=[],
            constraints=[],
            priority="medium",
            metadata={},
        ),
        project_overview={},
        open_questions=[],
        assumptions=[],
    ).to_dict()
    activities.execute_coding_team_activity(
        "ec-band",
        str(tmp_path),
        {"adapter_result_dict": adapter_dict, "spec_content_for_planning": "s"},
    )

    base, span = PROGRESS_BAND_CODING
    assert captured["job_id"] == "ec-band"
    assert captured["progress_base"] == base
    assert captured["progress_span"] == span
    assert "get_llm" not in captured, (
        "raw get_llm must not be injected: it bypasses the reasoning-stream getter "
        "and hands TechLeadAgent a non-strands client"
    )


def test_execute_coding_team_activity_stays_on_block_pause_strategy(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """RunTeamWorkflowV2's Phase 3 activity must not opt into pause_strategy="return":
    neither RunTeamWorkflow (V1) nor RunTeamWorkflowV2 defines a submit_answers Temporal
    signal, so a pause under "return" would raise _ActivityPauseSignal with nothing to
    resume it — POST /run-team/{job_id}/answers only ever writes to the job store, it
    never signals a workflow. Regression guard: this pins the "V2 HITL == V1 HITL"
    equivalence this codebase currently relies on for job-store-poll-based resume."""
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("ec-pause-strategy", repo_path=str(tmp_path))

    captured: Dict[str, Any] = {}

    def fake_orchestrator(job_id, repo_path, plan_input, **kwargs):
        captured.update(kwargs)

    import software_engineering_team.coding_team_orchestrator as coding_orch

    monkeypatch.setattr(coding_orch, "run_coding_team_orchestrator", fake_orchestrator)

    from planning_adapter import PlanningAdapterResult

    from shared.dev_models.models import ProductRequirements

    adapter_dict = PlanningAdapterResult(
        requirements=ProductRequirements(
            title="T",
            description="d",
            acceptance_criteria=[],
            constraints=[],
            priority="medium",
            metadata={},
        ),
        project_overview={},
        open_questions=[],
        assumptions=[],
    ).to_dict()
    activities.execute_coding_team_activity(
        "ec-pause-strategy",
        str(tmp_path),
        {"adapter_result_dict": adapter_dict, "spec_content_for_planning": "s"},
    )

    assert captured.get("pause_strategy", "block") == "block", (
        'execute_coding_team_activity must not pass pause_strategy="return" until '
        "RunTeamWorkflowV2 defines a submit_answers signal to resume it"
    )


def test_temporal_pra_and_planning_updaters_are_the_shared_band_factories(monkeypatch) -> None:
    """The Temporal activities must use the same updater factories as the thread
    path so sub-agent 0-100 progress is rescaled onto the phase bands — a raw
    pass-through updater lets the bar sprint to 100 during planning and collapse
    at the coding handoff."""
    import software_engineering_team.orchestrator as se_orch

    written: list = []
    monkeypatch.setattr(se_orch, "update_job", lambda job_id, **kw: written.append(kw))

    # The factories are what the activities now bind; assert their band behavior
    # end-to-end through the same entry points the activities import.
    pra = se_orch._make_phase_job_updater(
        "j-t",
        subprocess_key="analysis_subprocess",
        completed_key="analysis_completed_phases",
        phase_order=se_orch.PRA_PHASE_ORDER,
        progress_band=se_orch.PROGRESS_BAND_PRODUCT_ANALYSIS,
        phase="product_analysis",
    )
    pra(progress=100)
    assert (
        written[-1]["progress"]
        == se_orch.PROGRESS_BAND_PRODUCT_ANALYSIS[0] + (se_orch.PROGRESS_BAND_PRODUCT_ANALYSIS[1])
    )

    planning = se_orch._make_phase_job_updater(
        "j-t",
        subprocess_key="planning_subprocess",
        completed_key="planning_completed_phases",
        phase_order=se_orch.PLANNING_PHASE_ORDER,
        progress_band=se_orch.PROGRESS_BAND_PLANNING,
    )
    planning(progress=100)
    base, span = se_orch.PROGRESS_BAND_PLANNING
    assert written[-1]["progress"] == base + span


def test_adapter_result_round_trips_through_dict() -> None:
    """The Temporal planning→coding handoff serializes the adapter dataclass with
    to_dict/from_dict. The old hasattr(model_dump) probe silently produced {} for
    the dataclass, so the coding activity could never reconstruct it — this pins
    a lossless round trip including the nested Pydantic models."""
    import json

    from planning_adapter import PlanningAdapterResult

    from shared.dev_models.models import ProductRequirements

    original = PlanningAdapterResult(
        requirements=ProductRequirements(
            title="Build it",
            description="desc",
            acceptance_criteria=["works"],
            constraints=["python"],
            priority="high",
            metadata={"k": "v"},
        ),
        project_overview={"goals": "g"},
        open_questions=["q1"],
        assumptions=["a1"],
        final_spec_content="spec",
        architecture_overview="arch",
        shared_planning_doc_path="/plan/doc.md",
        resolved_questions=[{"id": "q1", "answer": "yes"}],
    )

    payload = original.to_dict()
    json.dumps(payload)  # must be JSON-safe for the Temporal payload converter

    rebuilt = PlanningAdapterResult.from_dict(payload)
    assert rebuilt.requirements == original.requirements
    assert rebuilt.project_overview == original.project_overview
    assert rebuilt.open_questions == original.open_questions
    assert rebuilt.assumptions == original.assumptions
    assert rebuilt.hierarchy is None
    assert rebuilt.final_spec_content == "spec"
    assert rebuilt.architecture_overview == "arch"
    assert rebuilt.shared_planning_doc_path == "/plan/doc.md"
    assert rebuilt.resolved_questions == original.resolved_questions


def test_parse_spec_activity_binds_the_passed_trace_id(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """Each phase activity runs as its own process/thread invocation (no shared contextvar
    context with the workflow or the other phase activities), so ``trace_id`` must be passed
    explicitly and re-bound inside the activity — this pins that ``parse_spec_activity`` does so."""
    from shared.observability import current_trace_id
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("ps-trace", repo_path=str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    (tmp_path / "initial_spec.md").write_text("# Test\n\nSpec.", encoding="utf-8")

    seen = {}

    def fake_parse_spec_with_llm(*a, **kw):
        seen["trace_id"] = current_trace_id()
        raise RuntimeError("stop after capturing trace_id")

    monkeypatch.setattr(
        "software_engineering_team.spec_parser.parse_spec_with_llm", fake_parse_spec_with_llm
    )
    with pytest.raises(RuntimeError, match="stop after capturing trace_id"):
        activities.parse_spec_activity("ps-trace", str(tmp_path), trace_id="fixed-trace-1")
    assert seen["trace_id"] == "fixed-trace-1"
    assert current_trace_id() == ""  # unbound again once the activity returns/raises


def test_plan_project_activity_binds_the_passed_trace_id(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """See ``test_parse_spec_activity_binds_the_passed_trace_id``; same contract for Phase 2."""
    from unittest.mock import MagicMock

    from shared.observability import current_trace_id
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pp-trace", repo_path=str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    monkeypatch.setattr(
        "software_engineering_team.orchestrator._get_agents",
        lambda: {"architecture": MagicMock()},
    )

    seen = {}

    def fake_run_planning_workflow(*a, **kw):
        seen["trace_id"] = current_trace_id()
        raise RuntimeError("stop after capturing trace_id")

    monkeypatch.setattr("planning_team.orchestrator.run_workflow", fake_run_planning_workflow)
    with pytest.raises(RuntimeError, match="stop after capturing trace_id"):
        activities.plan_project_activity(
            "pp-trace",
            str(tmp_path),
            {"spec_content": "spec", "validated_spec": "spec", "plan_dir": str(tmp_path)},
            trace_id="fixed-trace-2",
        )
    assert seen["trace_id"] == "fixed-trace-2"
    assert current_trace_id() == ""


def test_execute_coding_team_activity_binds_the_passed_trace_id(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """See ``test_parse_spec_activity_binds_the_passed_trace_id``; same contract for Phase 3 —
    including the ``parallel_map`` fan-out inside ``run_coding_team_orchestrator``, which inherits
    the bound id via ``contextvars.copy_context()`` once it is bound here."""
    from planning_adapter import PlanningAdapterResult

    from shared.dev_models.models import ProductRequirements
    from shared.observability import current_trace_id
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("ec-trace", repo_path=str(tmp_path))

    seen = {}

    def fake_run_coding_team_orchestrator(job_id, repo_path, plan_input, **kwargs):
        seen["trace_id"] = current_trace_id()

    monkeypatch.setattr(
        "software_engineering_team.coding_team_orchestrator.run_coding_team_orchestrator",
        fake_run_coding_team_orchestrator,
    )

    adapter_dict = PlanningAdapterResult(
        requirements=ProductRequirements(
            title="T", description="d", acceptance_criteria=[], constraints=[]
        ),
        project_overview={},
        open_questions=[],
        assumptions=[],
    ).to_dict()
    activities.execute_coding_team_activity(
        "ec-trace",
        str(tmp_path),
        {"adapter_result_dict": adapter_dict, "spec_content_for_planning": "s"},
        trace_id="fixed-trace-3",
    )
    assert seen["trace_id"] == "fixed-trace-3"
    assert current_trace_id() == ""


def test_phase_activities_generate_a_trace_id_when_none_supplied(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    """A blank ``trace_id`` (Temporal's default when a caller omits it) still binds a non-empty,
    generated id rather than leaving the phase's logs uncorrelated."""
    from shared.observability import current_trace_id
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("ps-notrace", repo_path=str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    (tmp_path / "initial_spec.md").write_text("# Test\n\nSpec.", encoding="utf-8")

    seen = {}

    def fake_parse_spec_with_llm(*a, **kw):
        seen["trace_id"] = current_trace_id()
        raise RuntimeError("stop after capturing trace_id")

    monkeypatch.setattr(
        "software_engineering_team.spec_parser.parse_spec_with_llm", fake_parse_spec_with_llm
    )
    with pytest.raises(RuntimeError):
        activities.parse_spec_activity("ps-notrace", str(tmp_path))
    assert seen["trace_id"]
