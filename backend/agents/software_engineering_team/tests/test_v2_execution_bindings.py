"""
Tests for the shared execution-phase bindings factory
(``software_engineering_team.shared.v2_execution_bindings``), which both
``backend_code_v2_team`` and ``frontend_code_v2_team`` use to bind
``run_execution``/``GATE_CONFIG`` from their ``phases/_profile.py`` (see that
module's docstring for the full design rationale).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

from shared.dev_models.models import Task, TaskStatus, TaskType
from software_engineering_team.backend_code_v2_team import models as be_models
from software_engineering_team.backend_code_v2_team.models import (
    Microtask,
    MicrotaskStatus,
    PlanningResult,
    ToolAgentKind,
)
from software_engineering_team.backend_code_v2_team.phases._profile import PROFILE as BE_PROFILE
from software_engineering_team.frontend_code_v2_team import models as fe_models
from software_engineering_team.frontend_code_v2_team.phases._profile import PROFILE as FE_PROFILE
from software_engineering_team.shared.phases.execution import GateOutcome
from software_engineering_team.shared.v2_execution_bindings import (
    ExecutionBindings,
    build_execution_bindings,
    scope_tool_agents_by_kind,
)


class TestScopeToolAgentsByKind:
    def test_none_mapping_returns_none(self):
        assert scope_tool_agents_by_kind(None, "testing_qa") is None

    def test_empty_mapping_returns_none(self):
        assert scope_tool_agents_by_kind({}, "testing_qa") is None

    def test_kind_absent_returns_none(self):
        assert scope_tool_agents_by_kind({"security": object()}, "testing_qa") is None

    def test_kind_present_returns_single_entry_mapping(self):
        qa_agent = object()
        tool_agents = {"testing_qa": qa_agent, "security": object()}
        assert scope_tool_agents_by_kind(tool_agents, "testing_qa") == {"testing_qa": qa_agent}


def _pass_gate(**_kwargs: Any) -> GateOutcome:
    return GateOutcome(passed=True, issues=[], summary="ok")


def _build_test_bindings(**overrides: Any) -> ExecutionBindings:
    kwargs: Dict[str, Any] = dict(
        models=be_models,
        profile=BE_PROFILE,
        execution_prompt="{microtask_description} {requirements} {existing_code} {architecture_context}",
        parse_files_and_summary=lambda raw: {"files": {}, "summary": raw},
        run_code_review_gate=_pass_gate,
        run_qa_gate=_pass_gate,
        run_security_gate=_pass_gate,
        run_batch_coding_fixes=lambda **_kw: None,
        run_documentation_self_review=lambda **_kw: None,
        run_dbc_self_review=None,
        status_code_review=MicrotaskStatus.IN_CODE_REVIEW,
        status_qa=MicrotaskStatus.IN_QA_TESTING,
        status_security=MicrotaskStatus.IN_SECURITY_TESTING,
        status_qa_security=MicrotaskStatus.IN_QA_SECURITY_TESTING,
        max_total_cycles=lambda config: 3,
        code_review_retry_cap=lambda config: 1,
        max_cycles_requires_failing_gate=True,
        startup_log_message=lambda task_id, total, config: f"[{task_id}] {total} microtasks",
        gate_issue_log_verb="found",
        parallelize_qa_security=True,
    )
    kwargs.update(overrides)
    return build_execution_bindings(**kwargs)


class TestBuildExecutionBindings:
    def test_gate_config_carries_the_passed_knobs_and_gate_closures(self):
        cr_gate, qa_gate, sec_gate = _pass_gate, _pass_gate, _pass_gate
        bindings = _build_test_bindings(
            run_code_review_gate=cr_gate,
            run_qa_gate=qa_gate,
            run_security_gate=sec_gate,
        )
        gc = bindings.gate_config
        assert gc.run_code_review_gate is cr_gate
        assert gc.run_qa_gate is qa_gate
        assert gc.run_security_gate is sec_gate
        assert gc.status_code_review == MicrotaskStatus.IN_CODE_REVIEW
        assert gc.status_qa == MicrotaskStatus.IN_QA_TESTING
        assert gc.status_security == MicrotaskStatus.IN_SECURITY_TESTING
        assert gc.status_qa_security == MicrotaskStatus.IN_QA_SECURITY_TESTING
        assert gc.gate_issue_log_verb == "found"
        assert gc.parallelize_qa_security is True
        assert gc.max_cycles_requires_failing_gate is True

    def test_gate_config_run_general_microtask_matches_run_execution_boundary(self):
        """``GatedExecutionConfig.run_general_microtask`` and the closure
        ``run_execution`` uses internally must be the same underlying
        function object (both built once, from the same closed-over
        execution_prompt/profile/parse_files_and_summary), so a gated
        microtask's general-coder fallback behaves identically to the
        non-gated path's."""
        bindings = _build_test_bindings()
        assert bindings.gate_config.run_general_microtask is not None

    def test_run_execution_general_fallback_produces_files(self, tmp_path: Path):
        stub_llm = MagicMock()

        def parse_files_and_summary(raw: str) -> Dict[str, Any]:
            return {"files": {"app.py": "print('hi')"}, "summary": "done"}

        bindings = _build_test_bindings(parse_files_and_summary=parse_files_and_summary)

        task = Task(
            id="t1",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            description="build",
        )
        planning = PlanningResult(
            microtasks=[
                Microtask(id="mt-1", tool_agent=ToolAgentKind.GENERAL, description="general task")
            ],
            language="python",
        )

        result = bindings.run_execution(
            llm=stub_llm,
            task=task,
            planning_result=planning,
            repo_path=tmp_path,
        )
        assert "app.py" in result.files
        assert result.microtasks[0].status == MicrotaskStatus.COMPLETED

    def test_run_dbc_self_review_none_is_preserved_on_gate_config(self):
        bindings = _build_test_bindings(run_dbc_self_review=None)
        assert bindings.gate_config.run_dbc_self_review is None

    def test_run_dbc_self_review_callable_is_wired_through(self):
        def fake_dbc(**_kw: Any) -> Any:
            return None

        bindings = _build_test_bindings(run_dbc_self_review=fake_dbc)
        assert bindings.gate_config.run_dbc_self_review is fake_dbc


class TestBuildExecutionBindingsFrontendParity:
    """Proves ``build_execution_bindings`` is behavior-equivalent for
    ``frontend_code_v2_team``, not just ``backend_code_v2_team`` -- the
    factory itself carries no team-specific branching, so wiring it with
    frontend's ``models``/``PROFILE`` should produce the same shape of
    ``ExecutionBindings`` as the backend cases above."""

    def test_gate_config_carries_the_passed_knobs_and_gate_closures(self):
        cr_gate, qa_gate, sec_gate = _pass_gate, _pass_gate, _pass_gate
        bindings = _build_test_bindings(
            models=fe_models,
            profile=FE_PROFILE,
            run_code_review_gate=cr_gate,
            run_qa_gate=qa_gate,
            run_security_gate=sec_gate,
        )
        gc = bindings.gate_config
        assert gc.run_code_review_gate is cr_gate
        assert gc.run_qa_gate is qa_gate
        assert gc.run_security_gate is sec_gate
        assert gc.status_code_review == MicrotaskStatus.IN_CODE_REVIEW
        assert gc.status_qa == MicrotaskStatus.IN_QA_TESTING
        assert gc.status_security == MicrotaskStatus.IN_SECURITY_TESTING
        assert gc.status_qa_security == MicrotaskStatus.IN_QA_SECURITY_TESTING

    def test_run_execution_general_fallback_produces_files(self, tmp_path: Path):
        stub_llm = MagicMock()

        def parse_files_and_summary(raw: str) -> Dict[str, Any]:
            return {"files": {"app.tsx": "export default function App() {}"}, "summary": "done"}

        bindings = _build_test_bindings(
            models=fe_models,
            profile=FE_PROFILE,
            parse_files_and_summary=parse_files_and_summary,
        )

        task = Task(
            id="t1",
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
            description="build",
        )
        planning = PlanningResult(
            microtasks=[
                Microtask(id="mt-1", tool_agent=ToolAgentKind.GENERAL, description="general task")
            ],
            language="typescript",
        )

        result = bindings.run_execution(
            llm=stub_llm,
            task=task,
            planning_result=planning,
            repo_path=tmp_path,
        )
        assert "app.tsx" in result.files
        assert result.microtasks[0].status == MicrotaskStatus.COMPLETED
