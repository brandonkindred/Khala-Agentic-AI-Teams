"""Tests for the SE Temporal activity wrappers.

Each activity is a thin wrapper around an orchestrator entry point with an
exception-handling outer try/except. The tests cover the happy path (the
wrapped function is called) and the exception path (failure is captured into
the job store via ``update_job``).
"""

from __future__ import annotations

from typing import Any, Dict

import pytest


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    return patched_job_store


def test_run_orchestrator_activity_success(monkeypatch, tmp_path) -> None:
    from software_engineering_team.temporal import activities

    called: Dict[str, Any] = {}

    def fake_run_orchestrator(
        job_id,
        repo_path,
        *,
        spec_content_override=None,
        resolved_questions_override=None,
        planning_only=False,
    ):
        called.update(
            job_id=job_id,
            repo_path=repo_path,
            spec_override=spec_content_override,
            planning_only=planning_only,
        )

    monkeypatch.setattr(
        "software_engineering_team.orchestrator.run_orchestrator", fake_run_orchestrator
    )
    activities.run_orchestrator_activity(
        "job1", str(tmp_path), spec_content_override="x", planning_only=True
    )
    assert called["job_id"] == "job1"
    assert called["planning_only"] is True


def test_run_orchestrator_activity_failure_captured(
    monkeypatch, tmp_path, patched_job_store
) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("job-x", repo_path=str(tmp_path))

    def boom(*a, **kw):
        raise RuntimeError("orchestrator crashed")

    monkeypatch.setattr("software_engineering_team.orchestrator.run_orchestrator", boom)
    activities.run_orchestrator_activity("job-x", str(tmp_path))
    job = js.get_job("job-x")
    assert job is not None
    assert job["status"] == js.JOB_STATUS_FAILED
    assert "orchestrator crashed" in (job.get("error") or "")


def test_retry_failed_activity_success(monkeypatch) -> None:
    from software_engineering_team.temporal import activities

    called: Dict[str, str] = {}

    def fake(job_id):
        called["id"] = job_id

    monkeypatch.setattr("software_engineering_team.orchestrator.run_failed_tasks", fake)
    activities.retry_failed_activity("j1")
    assert called["id"] == "j1"


def test_retry_failed_activity_failure(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("j-fail", repo_path=str(tmp_path))

    def boom(_):
        raise RuntimeError("retry exploded")

    monkeypatch.setattr("software_engineering_team.orchestrator.run_failed_tasks", boom)
    activities.retry_failed_activity("j-fail")
    job = js.get_job("j-fail")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_run_frontend_code_v2_activity_failure(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("fv2-j", repo_path=str(tmp_path))

    def boom(*a, **kw):
        raise RuntimeError("v2 frontend failed")

    monkeypatch.setattr(activities, "_run_frontend_code_v2_impl", boom)
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
    activities.run_backend_code_v2_activity("bv2-j", str(tmp_path), {"id": "t1"})
    job = js.get_job("bv2-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_run_product_analysis_activity_failure(monkeypatch, tmp_path, patched_job_store) -> None:
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pa-j", repo_path=str(tmp_path))

    def boom(*a, **kw):
        raise RuntimeError("PA failed")

    monkeypatch.setattr(activities, "_run_product_analysis_impl", boom)
    activities.run_product_analysis_activity("pa-j", str(tmp_path), "spec")
    job = js.get_job("pa-j")
    assert job["status"] == js.JOB_STATUS_FAILED


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


def test_parse_spec_activity_exception_path(monkeypatch, tmp_path, patched_job_store) -> None:
    """No spec file in repo → spec parser raises FileNotFoundError, which the
    outer except in parse_spec_activity captures and re-raises after marking
    the job FAILED."""
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("ps-j", repo_path=str(tmp_path))
    with pytest.raises(Exception):
        activities.parse_spec_activity("ps-j", str(tmp_path))
    job = js.get_job("ps-j")
    assert job["status"] == js.JOB_STATUS_FAILED


def test_plan_project_activity_exception_path(monkeypatch, tmp_path, patched_job_store) -> None:
    """Cover the outer except in plan_project_activity.

    ``plan_project_activity`` calls ``parse_spec_with_llm(..., get_client("spec_intake"))``
    before it ever reaches ``_check_cancellation``, so patching the cancellation
    helper alone would let the activity make a real LLM call first and flake on
    transport errors. Instead, patch ``parse_spec_with_llm`` to raise — this
    deterministically drives the outer ``except`` branch without any network I/O.
    """
    from software_engineering_team.shared import job_store as js
    from software_engineering_team.temporal import activities

    js.create_job("pp-j", repo_path=str(tmp_path))

    # get_client("spec_intake") is evaluated as an argument to parse_spec_with_llm
    # before the patched boom runs; use the dummy provider so it returns a client
    # (rather than raising LLMNotConfiguredError, which would mask the RuntimeError).
    monkeypatch.setenv("LLM_PROVIDER", "dummy")

    def boom(*a, **kw):
        raise RuntimeError("check failed")

    monkeypatch.setattr("spec_parser.parse_spec_with_llm", boom)
    with pytest.raises(RuntimeError):
        activities.plan_project_activity(
            "pp-j",
            str(tmp_path),
            {"spec_content": "spec", "validated_spec": "spec", "plan_dir": str(tmp_path)},
        )
    job = js.get_job("pp-j")
    assert job["status"] == js.JOB_STATUS_FAILED


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

    import coding_team.orchestrator as coding_orch

    monkeypatch.setattr(coding_orch, "run_coding_team_orchestrator", fake_orchestrator)

    from planning_v3_adapter import PlanningV2AdapterResult

    from software_engineering_team.shared.models import ProductRequirements

    adapter_dict = PlanningV2AdapterResult(
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
    pra = se_orch._make_pra_job_updater("j-t")
    pra(progress=100)
    assert (
        written[-1]["progress"]
        == se_orch.PROGRESS_BAND_PRODUCT_ANALYSIS[0] + (se_orch.PROGRESS_BAND_PRODUCT_ANALYSIS[1])
    )

    planning = se_orch._make_planning_v3_job_updater("j-t")
    planning(progress=100)
    base, span = se_orch.PROGRESS_BAND_PLANNING
    assert written[-1]["progress"] == base + span


def test_adapter_result_round_trips_through_dict() -> None:
    """The Temporal planning→coding handoff serializes the adapter dataclass with
    to_dict/from_dict. The old hasattr(model_dump) probe silently produced {} for
    the dataclass, so the coding activity could never reconstruct it — this pins
    a lossless round trip including the nested Pydantic models."""
    import json

    from planning_v3_adapter import PlanningV2AdapterResult

    from software_engineering_team.shared.models import ProductRequirements

    original = PlanningV2AdapterResult(
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

    rebuilt = PlanningV2AdapterResult.from_dict(payload)
    assert rebuilt.requirements == original.requirements
    assert rebuilt.project_overview == original.project_overview
    assert rebuilt.open_questions == original.open_questions
    assert rebuilt.assumptions == original.assumptions
    assert rebuilt.hierarchy is None
    assert rebuilt.final_spec_content == "spec"
    assert rebuilt.architecture_overview == "arch"
    assert rebuilt.shared_planning_doc_path == "/plan/doc.md"
    assert rebuilt.resolved_questions == original.resolved_questions
