"""
Cross-team parity tests for representative microtasks through the merged v2 team.

Story 3b/3c/3d each added parity tests scoped to their own seam
(``V2TeamConfig``/base orchestrator, the collapsed near-identical wrappers, and
the unified divergent wrappers, respectively -- see ``test_v2_config_parity.py``,
``test_v2_shared_phases.py``, ``test_v2_execution_bindings.py``,
``test_v2_gated_execution_shared.py``, and ``test_microtask_review_gates.py``).
None of them drives a representative microtask through *both* teams' real,
production entry points and asserts the two runs are structurally equivalent
except on the documented divergence axes -- that is this module's job (Story
3e Step 1).

Three representative scenarios, each run through ``CodegenDevelopmentAgent``'s
real phase entry points for both the backend and frontend stacks:

1. Execution + review-gate path (``run_execution_with_review_gates``) --
   exercises the unified ``build_execution_bindings`` seam (Story 3d).
2. Planning + documentation-phase path (``run_planning`` /
   ``run_documentation_phase``) -- exercises the collapsed
   ``build_phase_bindings`` seam (Story 3c) and, via language detection, the
   ``V2TeamConfig``/``StackProfile`` seam (Story 3b).
3. The frontend-only accessibility clause / ``scope_tool_agents_by_kind``
   config hook -- exercises the one deliberately-not-collapsed divergence
   point (Story 3d), asserting the backend path is provably unaffected.

This module intentionally does not re-assert what the prior stories' files
already cover (config/property axes, orchestrator-class wiring, branch
coverage of the shared phase implementations in isolation).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from tests.test_microtask_review_gates import _CallableTextClient, _FakeToolAgent

_CLEAN_APPROVAL = {
    "approved": True,
    "issues": [],
    "summary": "All good.",
    "spec_compliance_notes": "",
}


def _make_task(team: str):
    """Build the same-shaped ``Task`` for a given team ("backend"/"frontend")."""
    from shared.dev_models.models import Task, TaskStatus, TaskType

    return Task(
        id=f"t-{team}",
        type=TaskType.BACKEND if team == "backend" else TaskType.FRONTEND,
        assignee=f"{team}-code-v2",
        status=TaskStatus.PENDING,
        title="Add a health endpoint" if team == "backend" else "Add a status widget",
        description="Representative microtask for parity testing.",
    )


def _clean_execution_client(good_file: str, good_code: str) -> _CallableTextClient:
    """Scripted LLM: approves every review, generates one file on the first
    text-template call, and returns a neutral review-status template
    afterwards (matches the pattern in ``test_microtask_review_gates.py``)."""

    call_count = [0]

    def _side_effect(prompt: str) -> Any:
        if "code to review" in prompt.lower():
            return _CLEAN_APPROVAL
        call_count[0] += 1
        if call_count[0] == 1:
            return f"## FILE {good_file} ##\n{good_code}\n\n## SUMMARY ##\nImplemented.\n"
        return "## REVIEW_STATUS ##\npassed\n\n## ISSUES ##\n\n## SUMMARY ##\nAll good.\n"

    return _CallableTextClient(_side_effect)


def _assert_microtask_completed(result, expected_file: str) -> None:
    """Shared shape assertion: exactly one microtask, COMPLETED, with the
    expected file present in the result."""
    assert len(result.microtasks) == 1
    assert result.microtasks[0].status.value == "completed"
    assert expected_file in result.files


# ---------------------------------------------------------------------------
# Scenario 1: execution + review-gate path (build_execution_bindings seam)
# ---------------------------------------------------------------------------


class TestRepresentativeExecutionReviewGateParity:
    """A clean-approval microtask driven through each team's real
    ``run_execution_with_review_gates`` -- the production entry point built by
    ``build_execution_bindings`` (Story 3d) -- reaches the same COMPLETED
    outcome shape for both teams."""

    def test_backend_execution_completes_representative_microtask(self, tmp_path):
        from software_engineering_team.codegen_team.models import (
            Microtask,
            MicrotaskReviewConfig,
            PlanningResult,
            ToolAgentKind,
        )
        from software_engineering_team.codegen_team.stacks.backend.profile import (
            ReviewDependencies,
            run_execution_with_review_gates,
        )

        (tmp_path / ".git").mkdir()
        task = _make_task("backend")
        mt = Microtask(id="mt-1", title="Add health endpoint", tool_agent=ToolAgentKind.GENERAL)
        planning_result = PlanningResult(microtasks=[mt], language="python")

        mock_llm = _clean_execution_client("app.py", "print('ok')")
        mock_qa = MagicMock()
        mock_qa.run.return_value = MagicMock(bugs_found=[], issues=[])
        mock_sec = MagicMock()
        mock_sec.run.return_value = MagicMock(vulnerabilities=[], issues=[])
        result = run_execution_with_review_gates(
            llm=mock_llm,
            task=task,
            planning_result=planning_result,
            repo_path=tmp_path,
            review_config=MicrotaskReviewConfig(max_retries=1),
            review_deps=ReviewDependencies(qa_agent=mock_qa, security_agent=mock_sec),
        )

        _assert_microtask_completed(result, "app.py")

    def test_frontend_execution_completes_representative_microtask(self, tmp_path):
        from software_engineering_team.codegen_team.models import (
            Microtask,
            MicrotaskReviewConfig,
            PlanningResult,
            ToolAgentKind,
        )
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            ReviewDependencies,
            run_execution_with_review_gates,
        )

        (tmp_path / ".git").mkdir()
        task = _make_task("frontend")
        mt = Microtask(id="mt-1", title="Add status widget", tool_agent=ToolAgentKind.GENERAL)
        planning_result = PlanningResult(microtasks=[mt], language="typescript")

        mock_llm = _clean_execution_client("src/status.ts", "export const status = () => 'ok';")
        result = run_execution_with_review_gates(
            llm=mock_llm,
            task=task,
            planning_result=planning_result,
            repo_path=tmp_path,
            review_config=MicrotaskReviewConfig(max_retries=1),
            review_deps=ReviewDependencies(),
        )

        _assert_microtask_completed(result, "src/status.ts")

    def test_both_teams_accept_the_same_review_gate_call_shape(self):
        """Both teams' real ``run_execution_with_review_gates`` accept the
        identical kwarg set -- proving the shared ``build_execution_bindings``
        binding produced structurally identical public signatures for both
        teams, not just similar-looking ones."""
        import inspect

        from software_engineering_team.codegen_team.stacks.backend.profile import (
            run_execution_with_review_gates as be_run,
        )
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            run_execution_with_review_gates as fe_run,
        )

        be_params = set(inspect.signature(be_run).parameters)
        fe_params = set(inspect.signature(fe_run).parameters)
        assert be_params == fe_params


# ---------------------------------------------------------------------------
# Scenario 2: planning + documentation-phase path (build_phase_bindings seam)
# ---------------------------------------------------------------------------


class TestRepresentativePlanningDocumentationParity:
    """A representative task, with no repo-level language signal, driven
    through each team's real ``run_planning``/``run_documentation_phase`` --
    the production entry points built by ``build_phase_bindings`` (Story 3c).

    ``run_planning`` resolves language via ``config.stack_profile`` (the
    ``V2TeamConfig``/``StackProfile`` seam, Story 3b): with no repo signal,
    backend falls back to its default (python) and frontend to its default
    (typescript) -- proving the config seam actually drives real behavior,
    not just that the two objects hold different data.
    """

    def test_backend_planning_and_documentation_real_pipeline(self, tmp_path):
        from software_engineering_team.codegen_team.models import (
            DocumentationPhaseResult,
            ExecutionResult,
        )
        from software_engineering_team.codegen_team.stacks.backend.profile import (
            run_documentation_phase,
            run_planning,
        )

        task = _make_task("backend")
        mock_llm = _CallableTextClient(lambda _p: "## SUMMARY ##\nno-op\n")

        planning_result = run_planning(
            llm=mock_llm,
            task=task,
            repo_path=tmp_path,
        )
        assert planning_result.language == "python"
        assert len(planning_result.microtasks) == 1  # fallback microtask synthesized

        execution_result = ExecutionResult(files={"app.py": "print('ok')\n"})
        doc_result = run_documentation_phase(
            llm=mock_llm,
            task=task,
            repo_path=tmp_path,
            execution_result=execution_result,
            planning_result=planning_result,
            tool_agents={},
            max_iterations=5,
        )
        assert isinstance(doc_result, DocumentationPhaseResult)
        assert "skipped" in doc_result.summary.lower()

    def test_frontend_planning_and_documentation_real_pipeline(self, tmp_path):
        from software_engineering_team.codegen_team.models import (
            DocumentationPhaseResult,
            ExecutionResult,
        )
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            run_documentation_phase,
            run_planning,
        )

        task = _make_task("frontend")
        mock_llm = _CallableTextClient(lambda _p: "## SUMMARY ##\nno-op\n")

        planning_result = run_planning(
            llm=mock_llm,
            task=task,
            repo_path=tmp_path,
        )
        assert planning_result.language == "typescript"
        assert len(planning_result.microtasks) == 1  # fallback microtask synthesized

        execution_result = ExecutionResult(files={"src/status.ts": "export const x = 1;\n"})
        doc_result = run_documentation_phase(
            llm=mock_llm,
            task=task,
            repo_path=tmp_path,
            execution_result=execution_result,
            planning_result=planning_result,
            tool_agents={},
            max_iterations=5,
        )
        assert isinstance(doc_result, DocumentationPhaseResult)
        assert "skipped" in doc_result.summary.lower()

    def test_default_languages_diverge_only_on_the_documented_axis(self, tmp_path):
        """Same empty repo, same task shape, same scripted LLM response for both
        teams -- the only difference in the planning outcome is the
        config-resolved default language, exactly the divergence
        ``V2TeamConfig``/``StackProfile`` documents."""
        from software_engineering_team.codegen_team.stacks.backend.profile import (
            run_planning as be_run_planning,
        )
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            run_planning as fe_run_planning,
        )

        mock_llm = _CallableTextClient(lambda _p: "## SUMMARY ##\nno-op\n")

        be_result = be_run_planning(llm=mock_llm, task=_make_task("backend"), repo_path=tmp_path)
        fe_result = fe_run_planning(llm=mock_llm, task=_make_task("frontend"), repo_path=tmp_path)

        assert be_result.language == "python"
        assert fe_result.language == "typescript"
        assert be_result.language != fe_result.language
        # Both still produce the identical fallback-microtask shape (the LLM's
        # own "no-op" summary survives -- fallback only overrides an empty one).
        assert be_result.summary == fe_result.summary == "no-op"
        assert len(be_result.microtasks) == len(fe_result.microtasks) == 1
        assert be_result.microtasks[0].id == fe_result.microtasks[0].id == "mt-implement-task"


# ---------------------------------------------------------------------------
# Scenario 3: accessibility clause / scope_tool_agents_by_kind config hook
# ---------------------------------------------------------------------------


class TestRepresentativeAccessibilityConfigHookParity:
    """The one deliberately-not-collapsed divergence point (Story 3d): frontend's
    QA/security gates must narrow a multi-kind tool-agent registry down to a
    single kind via ``scope_tool_agents_by_kind``; backend's gates already
    self-scope internally and must never call it. Both are asserted side by
    side against the *same* multi-kind registry shape, proving the divergence
    is exactly the documented one and nothing more.
    """

    def _multi_kind_tool_agents(self, ToolAgentKind):
        return {
            ToolAgentKind.TESTING_QA: _FakeToolAgent(),
            ToolAgentKind.SECURITY: _FakeToolAgent(),
        }

    def test_frontend_qa_gate_narrows_via_scope_tool_agents_by_kind(self, tmp_path):
        from software_engineering_team.codegen_team.models import Microtask, ToolAgentKind
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            ReviewDependencies,
            _qa_gate,
        )

        tool_agents = self._multi_kind_tool_agents(ToolAgentKind)
        deps = ReviewDependencies(tool_agents=tool_agents)
        task = _make_task("frontend")
        mt = Microtask(id="mt-1", title="Test Microtask")

        _qa_gate(
            llm=MagicMock(),
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files={"src/app.ts": "const x = 1;"},
            deps=deps,
            detail_callback=lambda _d: None,
        )

        assert tool_agents[ToolAgentKind.TESTING_QA].review_calls == 1
        assert tool_agents[ToolAgentKind.SECURITY].review_calls == 0

    def test_backend_qa_gate_never_calls_scope_tool_agents_by_kind(self, tmp_path, monkeypatch):
        import software_engineering_team.codegen_team.stacks.backend.profile as backend_profile
        from software_engineering_team.codegen_team.models import Microtask, ToolAgentKind
        from software_engineering_team.codegen_team.stacks.backend.profile import (
            ReviewDependencies,
            _qa_gate,
        )
        from software_engineering_team.shared import v2_execution_bindings

        calls: list = []
        spy = lambda *a, **kw: calls.append((a, kw))  # noqa: E731
        monkeypatch.setattr(v2_execution_bindings, "scope_tool_agents_by_kind", spy)
        monkeypatch.setattr(backend_profile, "scope_tool_agents_by_kind", spy, raising=False)

        tool_agents = self._multi_kind_tool_agents(ToolAgentKind)
        deps = ReviewDependencies(tool_agents=tool_agents)
        task = _make_task("backend")
        mt = Microtask(id="mt-1", title="Test Microtask")

        _qa_gate(
            llm=MagicMock(),
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files={"main.py": "print('hi')"},
            deps=deps,
            detail_callback=lambda _d: None,
        )

        assert calls == []
        # Backend still narrows the effective call to the QA kind, just via its
        # own shared phase function's internal self-scoping rather than this hook.
        assert tool_agents[ToolAgentKind.TESTING_QA].review_calls == 1
        assert tool_agents[ToolAgentKind.SECURITY].review_calls == 0

    def test_build_task_requirements_diverges_only_on_accessibility_clause(self):
        """Identical base task-requirements string through both teams' real
        orchestrators -- frontend appends its accessibility clause, backend
        returns the base unchanged, and nothing else differs."""
        from software_engineering_team.codegen_team.orchestrator import CodegenDevelopmentAgent

        base = "Review the diff for correctness."
        be_agent = CodegenDevelopmentAgent(MagicMock(), "backend")
        fe_agent = CodegenDevelopmentAgent(MagicMock(), "frontend")

        be_result = be_agent.build_task_requirements(base)
        fe_result = fe_agent.build_task_requirements(base)

        assert be_result == base
        assert fe_result == base + "\n\n" + fe_agent.extra_review_clause
        assert be_agent.extra_review_clause == ""
        assert fe_agent.extra_review_clause != ""
