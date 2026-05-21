"""Tests for the SE Temporal V2 phase models."""

from __future__ import annotations


def test_spec_parse_result_defaults() -> None:
    from software_engineering_team.temporal.phase_models import SpecParseResult

    r = SpecParseResult()
    assert r.spec_content == ""
    assert r.context_files_count == 0


def test_spec_parse_result_explicit() -> None:
    from software_engineering_team.temporal.phase_models import SpecParseResult

    r = SpecParseResult(
        spec_content="# Title",
        validated_spec="vs",
        requirements_title="title",
        plan_dir="/tmp/plan",
        context_files_count=3,
        pra_iterations=2,
    )
    assert r.spec_content == "# Title"
    assert r.pra_iterations == 2

    # Pydantic round-trip
    dumped = r.model_dump()
    assert dumped["plan_dir"] == "/tmp/plan"
    r2 = SpecParseResult.model_validate(dumped)
    assert r2 == r


def test_plan_result_defaults() -> None:
    from software_engineering_team.temporal.phase_models import PlanResult

    r = PlanResult()
    assert r.adapter_result_dict == {}
    assert r.spec_content_for_planning == ""


def test_plan_result_roundtrip() -> None:
    from software_engineering_team.temporal.phase_models import PlanResult

    r = PlanResult(
        adapter_result_dict={"a": 1},
        spec_content_for_planning="spec",
        requirements_title="t",
    )
    assert r.adapter_result_dict == {"a": 1}
    r2 = PlanResult.model_validate(r.model_dump())
    assert r2 == r


def test_execution_result_defaults() -> None:
    from software_engineering_team.temporal.phase_models import ExecutionResult

    r = ExecutionResult()
    assert r.completed_task_ids == []
    assert r.failed_tasks == []
    assert r.merged_count == 0


def test_execution_result_roundtrip() -> None:
    from software_engineering_team.temporal.phase_models import ExecutionResult

    r = ExecutionResult(
        completed_task_ids=["t1", "t2"],
        failed_tasks=[{"id": "t3", "error": "boom"}],
        merged_count=2,
    )
    assert len(r.completed_task_ids) == 2
    r2 = ExecutionResult.model_validate(r.model_dump())
    assert r2 == r
