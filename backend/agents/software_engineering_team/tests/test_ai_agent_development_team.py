from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import software_engineering_team.ai_agent_development_team.phases.review as review_mod
from llm_service import DummyLLMClient
from shared.dev_models.models import Task, TaskType
from software_engineering_team.ai_agent_development_team import constants as team_constants
from software_engineering_team.ai_agent_development_team import orchestrator
from software_engineering_team.ai_agent_development_team.constants import (
    ARTIFACT_GATE_DESCRIPTION_PREFIX,
    PLACEHOLDER_ARTIFACT_DIR,
    REQUIRED_ARTIFACT_HINTS,
)
from software_engineering_team.ai_agent_development_team.models import (
    ExecutionResult,
    IntakeResult,
    Phase,
    ProblemSolvingResult,
    ReviewIssue,
    ReviewResult,
    ToolAgentKind,
)
from software_engineering_team.ai_agent_development_team.orchestrator import (
    AIAgentDevelopmentTeamLead,
)
from software_engineering_team.ai_agent_development_team.phases.deliver import run_deliver
from software_engineering_team.ai_agent_development_team.phases.intake import run_intake
from software_engineering_team.ai_agent_development_team.phases.planning import run_planning
from software_engineering_team.ai_agent_development_team.phases.problem_solving import (
    run_problem_solving,
)
from software_engineering_team.ai_agent_development_team.phases.review import run_review
from software_engineering_team.ai_agent_development_team.prompts import (
    intake_system_prompt,
    planning_system_prompt,
)
from software_engineering_team.shared.repo_context_cache import RepoContextCache
from software_engineering_team.tests.conftest import _patch_fenced_response, _strands_model_double


class FakeLLM(DummyLLMClient):
    def complete_json(self, prompt: str, **kwargs):
        if "spec intake specialist" in prompt:
            return {
                "system_goal": "Build a spec-driven support agent system",
                "constraints": ["must include MCP"],
                "risks": ["hallucinations"],
                "success_metrics": ["90% task success"],
                "summary": "Intake done",
            }
        if "AI systems planner" in prompt:
            return {
                "microtasks": [
                    {
                        "id": "mt-prompt",
                        "title": "Prompt assets",
                        "description": "Create blueprint prompts",
                        "tool_agent": "prompt_engineering",
                        "depends_on": [],
                    },
                    {
                        "id": "mt-mcp",
                        "title": "MCP connectivity",
                        "description": "Set up mcp server wiring",
                        "tool_agent": "mcp_server_connectivity",
                        "depends_on": ["mt-prompt"],
                    },
                ],
                "summary": "Planned microtasks",
            }
        if "delivery coordinator" in prompt:
            return {
                "summary": "Delivery package ready",
                "handoff_notes": ["handoff"],
                "runbook": ["runbook"],
            }

        lowered = prompt.lower()
        if "mcp integration specialist" in lowered:
            return {
                "files": {
                    "ai_system/mcp_connectivity_blueprint.md": "# MCP",
                    "ai_system/mcp_runbook.md": "# runbook",
                },
                "recommendations": ["validate auth"],
                "summary": "MCP artifacts generated",
            }

        return {
            "files": {
                "ai_system/system_blueprint.md": "# blueprint",
                "ai_system/evaluation_plan.md": "# evaluation",
                "ai_system/safety_policy.md": "# safety",
            },
            "recommendations": ["continue"],
            "summary": "Generic artifacts generated",
        }


def _build_task() -> Task:
    return Task(
        id="task-ai-1",
        type=TaskType.BACKEND,
        assignee="backend",
        title="Create AI agent team",
        description="Build an AI agent development workflow",
        requirements="Must support MCP",
    )


def test_required_artifact_hints_tuple() -> None:
    """Team-level constant is the sole definition of artifact-category hints."""
    assert REQUIRED_ARTIFACT_HINTS == (
        "blueprint",
        "evaluation",
        "safety",
        "runbook",
        "mcp",
    )


def test_review_uses_shared_required_artifact_hints() -> None:
    """Review must import the team constant, not redefine the five-string tuple."""
    assert review_mod.REQUIRED_ARTIFACT_HINTS is team_constants.REQUIRED_ARTIFACT_HINTS


def test_intake_and_planning_prompts_include_required_artifact_hints() -> None:
    """Intake/planning system prompts list every shared artifact-category hint."""
    for prompt_fn in (intake_system_prompt, planning_system_prompt):
        prompt = prompt_fn()
        for hint in REQUIRED_ARTIFACT_HINTS:
            assert hint in prompt, f"{hint!r} missing from {prompt_fn.__name__}"
    assert "spec intake specialist" in intake_system_prompt()
    assert "AI systems planner" in planning_system_prompt()


def test_ai_agent_development_workflow_success(tmp_path: Path):
    lead = AIAgentDevelopmentTeamLead(FakeLLM())
    result = lead.run_workflow(repo_path=tmp_path, task=_build_task(), spec_content="Spec text")

    assert result.success is True
    assert result.current_phase == Phase.DELIVER
    assert result.review_result is not None and result.review_result.passed is True
    # After Strands migration the MCP specialist's system_prompt keywords
    # don't flow into the user prompt, so FakeLLM returns generic artifacts.
    # The important thing is the workflow completed successfully with files.
    assert len(result.final_files) >= 1
    assert len(result.trace) >= 4


def test_ai_agent_development_workflow_problem_solving(tmp_path: Path):
    class SparseLLM(FakeLLM):
        def complete_json(self, prompt: str, **kwargs):
            if "delivery coordinator" in prompt:
                return {"summary": "done", "handoff_notes": [], "runbook": []}
            if "AI systems planner" in prompt:
                return {
                    "microtasks": [
                        {
                            "id": "mt-1",
                            "title": "Only one",
                            "description": "x",
                            "tool_agent": "general",
                        }
                    ],
                    "summary": "planned",
                }
            if "spec intake specialist" in prompt:
                return super().complete_json(prompt)
            return {
                "files": {"ai_system/system_blueprint.md": "# blueprint"},
                "recommendations": [],
                "summary": "partial",
            }

    lead = AIAgentDevelopmentTeamLead(SparseLLM())
    result = lead.run_workflow(repo_path=tmp_path, task=_build_task(), spec_content="Spec text")

    assert result.success is True
    assert result.problem_solving_result is not None
    assert result.problem_solving_result.resolved is True
    assert result.iterations_used >= 1
    # final_files must reflect the problem-solving placeholder patches, not
    # the pre-loop snapshot of execution.files.
    assert any("_placeholder.md" in path for path in result.final_files)
    # final_files must alias the execution result's own (post-rebind) files
    # rather than a dict captured before problem-solving rebinds them.
    assert result.final_files is result.execution_result.files


def test_ai_agent_development_workflow_aborts_when_fix_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """When problem-solving can't resolve a review failure, the bounded retry
    loop aborts on the first iteration instead of retrying to exhaustion."""
    monkeypatch.setattr(
        orchestrator,
        "run_execution",
        lambda **kwargs: ExecutionResult(files={}, microtasks=[], notes=[], summary="executed"),
    )
    monkeypatch.setattr(
        orchestrator,
        "run_review",
        lambda **kwargs: ReviewResult(
            passed=False,
            issues=[
                ReviewIssue(
                    source="execution",
                    severity="high",
                    description="Microtask failed: mt-1",
                    recommendation="Re-run with clarified acceptance criteria.",
                )
            ],
            required_artifacts_ok=True,
            summary="Review failed with 1 high/critical issue.",
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "run_problem_solving",
        lambda **kwargs: ProblemSolvingResult(
            resolved=False,
            fixes_applied=[],
            files={},
            summary="No deterministic fixes were available.",
        ),
    )

    lead = AIAgentDevelopmentTeamLead(FakeLLM())
    result = lead.run_workflow(repo_path=tmp_path, task=_build_task(), spec_content="Spec text")

    assert result.success is False
    assert result.needs_followup is True
    assert result.failure_reason == "Review failed and no deterministic fix was available."
    assert result.iterations_used == 1


def test_ai_agent_development_workflow_exhausts_max_iterations(tmp_path: Path, monkeypatch) -> None:
    """When every problem-solving pass resolves something but review never
    passes, the bounded retry loop runs all iterations then reports failure."""
    monkeypatch.setattr(
        orchestrator,
        "run_execution",
        lambda **kwargs: ExecutionResult(files={}, microtasks=[], notes=[], summary="executed"),
    )
    monkeypatch.setattr(
        orchestrator,
        "run_review",
        lambda **kwargs: ReviewResult(
            passed=False,
            issues=[
                ReviewIssue(
                    source="artifact_gate",
                    severity="high",
                    description="Missing expected artifact category: blueprint",
                    recommendation="Add at least one artifact path containing 'blueprint'.",
                )
            ],
            required_artifacts_ok=False,
            summary="Review failed with 1 high/critical issue.",
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "run_problem_solving",
        lambda **kwargs: ProblemSolvingResult(
            resolved=True, fixes_applied=["placeholder"], files={}, summary="Applied a fix."
        ),
    )

    lead = AIAgentDevelopmentTeamLead(FakeLLM())
    result = lead.run_workflow(repo_path=tmp_path, task=_build_task(), spec_content="Spec text")

    assert result.success is False
    assert result.needs_followup is True
    assert result.failure_reason == "Review did not pass after max iterations."
    assert result.iterations_used == orchestrator.MAX_REVIEW_ITERATIONS


def test_run_intake_recovers_fenced_json_response(monkeypatch) -> None:
    """A markdown-fenced LLM response is recovered via complete_json_with_continuation
    instead of crashing run_intake's former bare json.loads."""
    _patch_fenced_response(
        monkeypatch,
        {
            "system_goal": "Build a fenced support agent system",
            "constraints": ["must include MCP"],
            "risks": ["hallucinations"],
            "success_metrics": ["90% task success"],
            "summary": "Intake recovered from fence",
        },
    )
    result = run_intake(llm=_strands_model_double(), task=_build_task(), spec_content="Spec text")

    assert result.system_goal == "Build a fenced support agent system"
    assert result.constraints == ["must include MCP"]
    assert result.summary == "Intake recovered from fence"


def test_run_planning_recovers_fenced_json_response(monkeypatch) -> None:
    """A markdown-fenced LLM response is recovered via complete_json_with_continuation
    instead of crashing run_planning's former bare json.loads."""
    _patch_fenced_response(
        monkeypatch,
        {
            "microtasks": [
                {
                    "id": "mt-fenced",
                    "title": "Fenced task",
                    "description": "Created from a fenced response",
                    "tool_agent": "general",
                    "depends_on": [],
                }
            ],
            "summary": "Planned from fenced response",
        },
    )
    intake_result = IntakeResult(system_goal="Build a spec-driven support agent system")

    result = run_planning(
        llm=_strands_model_double(),
        task=_build_task(),
        intake_result=intake_result,
        spec_content="Spec text",
    )

    assert len(result.microtasks) == 1
    assert result.microtasks[0].id == "mt-fenced"
    assert result.summary == "Planned from fenced response"


def test_run_planning_skips_invalid_items_and_defaults_unknown_tool_agent(monkeypatch) -> None:
    """A microtask entry missing ``id`` is dropped, and an unrecognized
    ``tool_agent`` value falls back to ``ToolAgentKind.GENERAL`` instead of
    raising."""
    _patch_fenced_response(
        monkeypatch,
        {
            "microtasks": [
                {"title": "No id, should be skipped"},
                "not-a-dict-should-be-skipped",
                {
                    "id": "mt-unknown-kind",
                    "title": "Unknown tool agent kind",
                    "description": "tool_agent value the model invented",
                    "tool_agent": "not_a_real_kind",
                },
            ],
            "summary": "Planned with one bad and one recoverable entry",
        },
    )
    intake_result = IntakeResult(system_goal="Build a spec-driven support agent system")

    result = run_planning(
        llm=_strands_model_double(),
        task=_build_task(),
        intake_result=intake_result,
        spec_content="Spec text",
    )

    assert len(result.microtasks) == 1
    assert result.microtasks[0].id == "mt-unknown-kind"
    assert result.microtasks[0].tool_agent == ToolAgentKind.GENERAL


def test_run_deliver_recovers_fenced_json_response(monkeypatch) -> None:
    """A markdown-fenced LLM response is recovered via complete_json_with_continuation
    instead of crashing run_deliver's former bare json.loads."""
    _patch_fenced_response(
        monkeypatch,
        {
            "summary": "Fenced delivery package ready",
            "handoff_notes": ["handoff note"],
            "runbook": ["runbook step"],
        },
    )
    execution_result = ExecutionResult(
        files={"ai_system/system_blueprint.md": "# blueprint"}, summary="executed"
    )
    review_result = ReviewResult(passed=True, required_artifacts_ok=True, summary="ok")

    result = run_deliver(
        llm=_strands_model_double(),
        execution_result=execution_result,
        review_result=review_result,
    )

    assert result.summary == "Fenced delivery package ready"
    assert result.handoff_notes == ["handoff note"]
    assert result.runbook == ["runbook step"]


def test_run_intake_raises_on_non_object_json_response(monkeypatch) -> None:
    """A validly-parsed but non-object JSON response (e.g. a fenced array)
    must raise ValueError instead of crashing with an unclassified
    AttributeError on raw.get(...)."""
    _patch_fenced_response(monkeypatch, ["not", "an", "object"])
    with pytest.raises(ValueError, match="not a JSON object"):
        run_intake(llm=_strands_model_double(), task=_build_task(), spec_content="Spec text")


def test_run_planning_raises_on_non_object_json_response(monkeypatch) -> None:
    """A validly-parsed but non-object JSON response (e.g. a fenced array)
    must raise ValueError instead of crashing with an unclassified
    AttributeError on raw.get(...)."""
    _patch_fenced_response(monkeypatch, ["not", "an", "object"])
    intake_result = IntakeResult(system_goal="Build a spec-driven support agent system")
    with pytest.raises(ValueError, match="not a JSON object"):
        run_planning(
            llm=_strands_model_double(),
            task=_build_task(),
            intake_result=intake_result,
            spec_content="Spec text",
        )


def test_run_deliver_raises_on_non_object_json_response(monkeypatch) -> None:
    """A validly-parsed but non-object JSON response (e.g. a fenced array)
    must raise ValueError instead of crashing with an unclassified
    AttributeError on raw.get(...)."""
    _patch_fenced_response(monkeypatch, ["not", "an", "object"])
    execution_result = ExecutionResult(
        files={"ai_system/system_blueprint.md": "# blueprint"}, summary="executed"
    )
    review_result = ReviewResult(passed=True, required_artifacts_ok=True, summary="ok")
    with pytest.raises(ValueError, match="not a JSON object"):
        run_deliver(
            llm=_strands_model_double(),
            execution_result=execution_result,
            review_result=review_result,
        )


def test_ai_agent_repo_context_cache_is_lazy_and_reused(tmp_path: Path) -> None:
    """Same resolved repo reuses one RepoContextCache; a different repo gets another."""
    lead = AIAgentDevelopmentTeamLead(FakeLLM())
    first = lead._repo_context_cache_for(tmp_path)
    second = lead._repo_context_cache_for(tmp_path)
    assert first is second

    other = tmp_path / "other"
    other.mkdir()
    third = lead._repo_context_cache_for(other)
    assert third is not first


def test_ai_agent_read_repo_code_second_call_hits_cache(tmp_path: Path) -> None:
    """Second _read_repo_code on an unchanged tree does not re-_render files."""
    (tmp_path / "a.py").write_text("A = 1\n")
    (tmp_path / "b.md").write_text("# B\n")

    lead = AIAgentDevelopmentTeamLead(FakeLLM())
    first = lead._read_repo_code(tmp_path)
    assert "a.py" in first and "b.md" in first

    renders: list[Path] = []
    real_render = RepoContextCache._render

    def _spy(f: Path, repo_path: Path):
        renders.append(f)
        return real_render(f, repo_path)

    with patch.object(RepoContextCache, "_render", staticmethod(_spy)):
        second = lead._read_repo_code(tmp_path)

    assert second == first
    assert renders == []


def test_ai_agent_lead_wires_repo_briefing_contract(tmp_path: Path) -> None:
    lead = AIAgentDevelopmentTeamLead(FakeLLM())
    assert lead._extensions == frozenset({".py", ".md", ".yaml", ".yml", ".json", ".toml"})
    assert lead._exclude_dirs == frozenset(
        {".git", "node_modules", "__pycache__", ".venv", "venv"}
    )
    assert lead._max_chars == 20_000
    cache = lead._repo_context_cache_for(tmp_path)
    assert cache._ext_set == lead._extensions
    assert cache._excl_set == lead._exclude_dirs
    assert cache._max_chars == 20_000


def test_artifact_gate_description_prefix_stable() -> None:
    """Review and problem-solving share this exact prefix string."""
    assert ARTIFACT_GATE_DESCRIPTION_PREFIX == "Missing expected artifact category: "


def test_run_review_artifact_gate_uses_shared_prefix() -> None:
    """Missing categories produce descriptions tied to REQUIRED_ARTIFACT_HINTS."""
    result = run_review(execution_result=ExecutionResult(files={}, microtasks=[], notes=[]))
    gate_issues = [i for i in result.issues if i.source == "artifact_gate"]
    assert gate_issues
    for issue in gate_issues:
        assert issue.description.startswith(ARTIFACT_GATE_DESCRIPTION_PREFIX)
        hint = issue.description[len(ARTIFACT_GATE_DESCRIPTION_PREFIX) :].strip()
        assert hint
        assert hint in REQUIRED_ARTIFACT_HINTS
        assert hint in issue.recommendation
    assert {
        i.description[len(ARTIFACT_GATE_DESCRIPTION_PREFIX) :].strip() for i in gate_issues
    } == set(REQUIRED_ARTIFACT_HINTS)


def test_run_problem_solving_synthesizes_placeholder_from_prefix() -> None:
    """A well-formed artifact-gate issue yields ai_system/{hint}_placeholder.md."""
    hint = REQUIRED_ARTIFACT_HINTS[0]
    review = ReviewResult(
        passed=False,
        issues=[
            ReviewIssue(
                source="artifact_gate",
                severity="high",
                description=f"{ARTIFACT_GATE_DESCRIPTION_PREFIX}{hint}",
                recommendation=f"Add at least one artifact path containing '{hint}'.",
            )
        ],
        required_artifacts_ok=False,
        summary="Review failed.",
    )
    result = run_problem_solving(
        execution_result=ExecutionResult(files={"existing.md": "# keep"}, microtasks=[]),
        review_result=review,
    )
    assert result.resolved is True
    assert f"{PLACEHOLDER_ARTIFACT_DIR}/{hint}_placeholder.md" in result.files
    assert "existing.md" in result.files
    assert any(hint in fix for fix in result.fixes_applied)


def test_run_problem_solving_skips_unknown_artifact_hint_category() -> None:
    """Shared-prefix descriptions with an unregistered hint create no placeholder."""
    unknown_hint = "not_a_registered_hint"
    assert unknown_hint not in REQUIRED_ARTIFACT_HINTS
    review = ReviewResult(
        passed=False,
        issues=[
            ReviewIssue(
                source="artifact_gate",
                severity="high",
                description=f"{ARTIFACT_GATE_DESCRIPTION_PREFIX}{unknown_hint}",
                recommendation=f"Add at least one artifact path containing '{unknown_hint}'.",
            )
        ],
        required_artifacts_ok=False,
        summary="Review failed.",
    )
    result = run_problem_solving(
        execution_result=ExecutionResult(files={}, microtasks=[]),
        review_result=review,
    )
    assert result.resolved is False
    assert result.files == {}
    assert result.fixes_applied == []
    assert not any("_placeholder.md" in path for path in result.files)


def test_run_problem_solving_skips_malformed_artifact_gate_description() -> None:
    """artifact_gate issues without the shared prefix do not invent placeholder paths."""
    review = ReviewResult(
        passed=False,
        issues=[
            ReviewIssue(
                source="artifact_gate",
                severity="high",
                description="category missing somehow: not-a-hint",
                recommendation="Fix the description format.",
            )
        ],
        required_artifacts_ok=False,
        summary="Review failed.",
    )
    result = run_problem_solving(
        execution_result=ExecutionResult(files={}, microtasks=[]),
        review_result=review,
    )
    assert result.resolved is False
    assert result.files == {}
    assert result.fixes_applied == []
    assert not any("_placeholder.md" in path for path in result.files)


def test_run_problem_solving_skips_empty_artifact_gate_token() -> None:
    """Prefix-only (or whitespace-only) descriptions do not create empty-token paths."""
    review = ReviewResult(
        passed=False,
        issues=[
            ReviewIssue(
                source="artifact_gate",
                severity="high",
                description=f"{ARTIFACT_GATE_DESCRIPTION_PREFIX}   ",
                recommendation="Add a real category hint.",
            )
        ],
        required_artifacts_ok=False,
        summary="Review failed.",
    )
    result = run_problem_solving(
        execution_result=ExecutionResult(files={}, microtasks=[]),
        review_result=review,
    )
    assert result.resolved is False
    assert result.files == {}
    assert f"{PLACEHOLDER_ARTIFACT_DIR}/_placeholder.md" not in result.files
