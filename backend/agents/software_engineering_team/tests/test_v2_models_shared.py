"""Coverage for the six shared v2 microtask models in ``shared.v2_models``.

Pins: (1) ``MicrotaskStatus`` is the superset of both teams' team-local
enums, (2) ``language``/``tool_agent`` are plain settable ``str`` fields
(no hard-coded literal, no team-enum coupling), (3) round-trip
serialization via ``model_dump``/``model_validate``, and (4) required-field
validation on ``Microtask.id``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from software_engineering_team.backend_code_v2_team.models import (
    MicrotaskStatus as BackendMicrotaskStatus,
)
from software_engineering_team.frontend_code_v2_team.models import (
    MicrotaskStatus as FrontendMicrotaskStatus,
)
from software_engineering_team.shared.v2_models import (
    ExecutionResult,
    Microtask,
    MicrotaskStatus,
    Phase,
    PlanningResult,
    ReviewIssue,
    ToolAgentInput,
    ToolAgentPhaseInput,
)


def test_microtask_status_union_covers_both_teams_members() -> None:
    shared_values = {member.value for member in MicrotaskStatus}
    for member in FrontendMicrotaskStatus:
        assert member.value in shared_values
    for member in BackendMicrotaskStatus:
        assert member.value in shared_values


def test_microtask_status_values_have_no_collisions() -> None:
    values = [member.value for member in MicrotaskStatus]
    assert len(values) == len(set(values)) == 12


def test_microtask_required_id_raises() -> None:
    with pytest.raises(ValidationError):
        Microtask()  # type: ignore[call-arg]


def test_microtask_defaults() -> None:
    microtask = Microtask(id="mt-1")

    assert microtask.title == ""
    assert microtask.description == ""
    assert microtask.tool_agent == ""
    assert microtask.status == MicrotaskStatus.PENDING
    assert microtask.depends_on == []
    assert microtask.output_files == {}
    assert microtask.notes == ""


@pytest.mark.parametrize("tool_agent", ["security", "data_engineering", "general"])
def test_microtask_tool_agent_is_plain_str_settable(tool_agent: str) -> None:
    assert Microtask(id="mt-1", tool_agent=tool_agent).tool_agent == tool_agent


def test_planning_result_language_default_and_override() -> None:
    assert PlanningResult().language == ""
    assert PlanningResult(language="python").language == "python"
    assert PlanningResult(language="typescript").language == "typescript"


def test_tool_agent_input_language_default_and_override() -> None:
    base = ToolAgentInput(microtask=Microtask(id="mt-1"))
    assert base.language == ""

    overridden = ToolAgentInput(microtask=Microtask(id="mt-1"), language="python")
    assert overridden.language == "python"


def test_tool_agent_phase_input_defaults_and_language_override() -> None:
    default = ToolAgentPhaseInput()
    assert default.language == ""
    assert default.phase == Phase.PLANNING
    assert default.microtask is None

    overridden = ToolAgentPhaseInput(language="typescript")
    assert overridden.language == "typescript"


def test_execution_result_defaults_and_microtasks_list() -> None:
    assert ExecutionResult().microtasks == []

    result = ExecutionResult(microtasks=[Microtask(id="mt-1")])
    assert result.microtasks[0].id == "mt-1"


def test_microtask_round_trip_model_dump_and_validate() -> None:
    microtask = Microtask(
        id="mt-1",
        title="Add login",
        description="Implement login form",
        tool_agent="ui_design",
        status=MicrotaskStatus.IN_CODE_REVIEW,
        depends_on=["mt-0"],
        output_files={"login.py": "print('hi')"},
        notes="needs follow-up",
    )

    dumped = microtask.model_dump()
    restored = Microtask.model_validate(dumped)

    assert restored == microtask


def test_planning_result_round_trip() -> None:
    result = PlanningResult(
        microtasks=[Microtask(id="mt-1"), Microtask(id="mt-2", status=MicrotaskStatus.SKIPPED)],
        language="python",
        summary="planned two microtasks",
    )

    dumped = result.model_dump()
    restored = PlanningResult.model_validate(dumped)

    assert restored == result


def test_tool_agent_phase_input_round_trip() -> None:
    phase_input = ToolAgentPhaseInput(
        phase=Phase.REVIEW,
        microtask=Microtask(id="mt-1"),
        repo_path="/repo",
        existing_code="print('x')",
        language="python",
        current_files={"a.py": "1"},
        review_issues=[ReviewIssue(source="qa", severity="high", description="bug")],
        task_title="Task",
        task_description="Do the thing",
        task_id="t-1",
        feature_branch_name="feature/x",
        spec_context="spec text",
        build_verifier=None,
        build_verify_label="build",
        linting_tool_agent=None,
        lint_agent_type="ruff",
    )

    dumped = phase_input.model_dump()
    restored = ToolAgentPhaseInput.model_validate(dumped)

    assert restored == phase_input
