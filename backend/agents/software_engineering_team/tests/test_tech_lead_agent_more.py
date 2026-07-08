"""Deep-coverage tests for TechLeadAgent."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from software_engineering_team.shared.models import (
    Epic,
    Initiative,
    PlanningHierarchy,
    ProductRequirements,
    StoryPlan,
    SystemArchitecture,
    Task,
    TaskPlan,
    TaskType,
    TaskUpdate,
)
from software_engineering_team.tech_lead_agent import agent as tla_mod
from software_engineering_team.tech_lead_agent.agent import TechLeadAgent
from software_engineering_team.tech_lead_agent.models import TechLeadInput

from .conftest import ConfigurableLLM


class _StubAgent:
    """A stub strands Agent whose __call__ returns canned JSON in sequence."""

    def __init__(self):
        self.queue: list[dict] = []
        self.calls: list[str] = []

    def __call__(self, prompt: str):
        self.calls.append(prompt)
        if self.queue:
            return json.dumps(self.queue.pop(0))
        return "{}"


@pytest.fixture
def stub_agent(monkeypatch) -> _StubAgent:
    stub = _StubAgent()
    monkeypatch.setattr(tla_mod, "Agent", lambda *a, **kw: stub)
    monkeypatch.setattr(tla_mod, "get_strands_model", lambda key=None, **_kw: object())
    return stub


def _basic_requirements() -> ProductRequirements:
    return ProductRequirements(
        title="My App",
        description="x" * 250,
        acceptance_criteria=["AC1", "AC2", "AC3"],
        constraints=["C1"],
        priority="high",
    )


def _basic_hierarchy() -> PlanningHierarchy:
    task1 = TaskPlan(id="t1", title="Backend X", assignee="backend", description="d")
    task2 = TaskPlan(id="t2", title="Frontend Y", assignee="frontend", description="d2")
    story1 = StoryPlan(id="s1", title="Story 1", tasks=[task1, task2])
    epic1 = Epic(id="e1", title="Epic 1", description="An epic " * 30, stories=[story1])
    init = Initiative(id="i1", title="Initiative", description="bigfeat", epics=[epic1])
    return PlanningHierarchy(initiatives=[init], execution_order=["t1", "t2"])


def test_tech_lead_init_with_provided_llm() -> None:
    llm = ConfigurableLLM()
    a = TechLeadAgent(llm_client=llm)
    assert a.llm is llm


def test_tech_lead_init_strands_model_instance(monkeypatch) -> None:
    from strands.models.model import Model as StrandsModel

    class _M(StrandsModel):
        def __init__(self):
            pass

        def update_config(self, *a, **kw):
            pass

        def get_config(self):
            return {}

        def structured_output(self, *a, **kw):  # pragma: no cover
            return {}

        async def stream(self, *a, **kw):  # pragma: no cover
            yield {}

    m = _M()
    a = TechLeadAgent(llm_client=m)
    assert a._model is m


def test_run_uses_planning_hierarchy_when_provided(stub_agent, tmp_path: Path) -> None:
    """If planning_hierarchy is provided we skip LLM, flatten directly."""
    reqs = _basic_requirements()
    hier = _basic_hierarchy()
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    out = a.run(
        TechLeadInput(
            requirements=reqs,
            planning_hierarchy=hier,
            repo_path=str(tmp_path),
        )
    )
    assert out.assignment is not None
    assert len(out.assignment.tasks) == 2
    # No LLM calls when hierarchy is provided
    assert stub_agent.calls == []
    # The detailed summary should mention initiatives / epics / stories
    assert "Initiative" in out.summary
    assert "Epic 1" in out.summary


def test_run_uses_planning_hierarchy_with_artifacts_content(stub_agent) -> None:
    """plan_artifacts_content is included verbatim in summary."""
    reqs = _basic_requirements()
    hier = _basic_hierarchy()
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    out = a.run(
        TechLeadInput(
            requirements=reqs,
            planning_hierarchy=hier,
            repo_path="/tmp",
            plan_artifacts_content="--- planning_document.md ---\nplanning text",
        )
    )
    assert "Planning Context" in out.summary


def test_run_clarification_path(stub_agent) -> None:
    """LLM returns spec_clarification_needed -> output has questions, no tasks."""
    stub_agent.queue.append(
        {
            "spec_clarification_needed": True,
            "clarification_questions": ["q1", "q2"],
            "summary": "spec was unclear",
        }
    )
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    out = a.run(
        TechLeadInput(
            requirements=_basic_requirements(),
            architecture=SystemArchitecture(overview="api"),
        )
    )
    assert out.spec_clarification_needed
    assert out.clarification_questions == ["q1", "q2"]
    assert out.assignment is None


def test_run_clarification_non_list_questions(stub_agent) -> None:
    """If clarification_questions is a string, it gets wrapped to list."""
    stub_agent.queue.append(
        {
            "spec_clarification_needed": True,
            "clarification_questions": "just one",
            "summary": "",
        }
    )
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    out = a.run(TechLeadInput(requirements=_basic_requirements()))
    assert out.clarification_questions == ["just one"]


def test_run_with_existing_codebase_calls_analyze_first(stub_agent) -> None:
    """When existing_codebase is provided, we call _analyze_codebase first."""
    stub_agent.queue.extend(
        [
            {"summary": "analyzed", "facts": ["x"]},  # _analyze_codebase response
            {  # main planning call
                "spec_clarification_needed": False,
                "initiatives": [
                    {
                        "id": "i1",
                        "title": "I1",
                        "epics": [
                            {
                                "id": "e1",
                                "title": "E1",
                                "stories": [
                                    {
                                        "id": "s1",
                                        "title": "S1",
                                        "tasks": [
                                            {
                                                "id": "t1",
                                                "title": "T",
                                                "assignee": "backend",
                                                "type": "backend",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "execution_order": ["t1"],
                "summary": "done",
                "requirement_task_mapping": [{"spec_item": "AC1", "task_ids": ["t1"]}],
            },
        ]
    )
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    out = a.run(
        TechLeadInput(
            requirements=_basic_requirements(),
            existing_codebase="def x(): pass",
            project_overview={
                "primary_goal": "ship",
                "delivery_strategy": "fast",
                "milestones": [{"name": "M1"}],
                "features_and_functionality_doc": "features",
            },
            open_questions=["q1", "q2"],
            assumptions=["a1"],
            resolved_questions=[{"question": "q1", "answer": "yes"}],
            existing_tasks=[
                Task(
                    id="exist1",
                    type=TaskType.BACKEND,
                    title="Existing",
                    description="d" * 600,
                    assignee="backend",
                    requirements="r" * 400,
                    acceptance_criteria=["x1", "x2"],
                    dependencies=["dep1"],
                )
            ],
            spec_content="spec text",
            architecture=SystemArchitecture(overview="abc"),
            repo_path="/tmp/repo",
        )
    )
    assert out.assignment is not None
    assert out.requirement_task_mapping == [{"spec_item": "AC1", "task_ids": ["t1"]}]
    # The planning prompt (second call) should reference existing tasks, resolved Qs, etc.
    planning_prompt = stub_agent.calls[1]
    assert "USER-PROVIDED RESOLUTIONS" in planning_prompt
    assert "OPEN QUESTIONS" in planning_prompt
    assert "Existing tasks" in planning_prompt
    assert "Architecture" in planning_prompt
    assert "Project Overview" in planning_prompt


def test_run_fallback_to_assignment_parsing(stub_agent) -> None:
    """If hierarchy flattening yields no tasks, parse_assignment_from_data is used."""
    stub_agent.queue.append(
        {
            "spec_clarification_needed": False,
            "initiatives": [],
            "tasks": [
                {
                    "id": "t99",
                    "type": "backend",
                    "title": "Direct",
                    "assignee": "backend",
                    "description": "d",
                }
            ],
            "execution_order": ["t99"],
            "summary": "",
        }
    )
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    out = a.run(TechLeadInput(requirements=_basic_requirements()))
    assert out.assignment.tasks  # fallback created tasks


def test_read_plan_artifacts_reads_from_disk(tmp_path: Path) -> None:
    """Reads markdown files from plan/ and plan/planning_team/ in order."""
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "01_brief.md").write_text("brief content")
    (plan / "02_arch.md").write_text("arch content")
    (plan / "planning_team").mkdir()
    (plan / "planning_team" / "planning_document.md").write_text("PT doc")
    (plan / "empty.md").write_text("   ")

    a = TechLeadAgent(llm_client=ConfigurableLLM())
    content = a._read_plan_artifacts(str(tmp_path))
    assert "brief content" in content
    assert "arch content" in content
    assert "PT doc" in content
    # planning_team should be first
    assert content.index("PT doc") < content.index("brief content")


def test_read_plan_artifacts_missing_dir(tmp_path: Path) -> None:
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    assert a._read_plan_artifacts(str(tmp_path)) == ""


def test_read_plan_artifacts_io_error(monkeypatch, tmp_path: Path) -> None:
    plan = tmp_path / "plan"
    plan.mkdir()
    f = plan / "01.md"
    f.write_text("ok")

    real_read = Path.read_text

    def _bad_read(self, *a, **kw):
        if self == f:
            raise OSError("boom")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _bad_read)
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    # Should not raise, returns empty since the only file is unreadable
    assert a._read_plan_artifacts(str(tmp_path)) == ""


def test_refine_task(stub_agent) -> None:
    stub_agent.queue.append(
        {
            "title": "Refined title",
            "description": "Refined desc",
            "user_story": "As a user...",
            "requirements": "r",
            "acceptance_criteria": ["new1", "new2"],
        }
    )
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    task = Task(
        id="t1",
        type=TaskType.BACKEND,
        title="orig",
        description="d",
        assignee="backend",
        acceptance_criteria=["old"],
    )
    refined = a.refine_task(task, ["Please clarify X"], "spec text")
    assert refined.title == "Refined title"
    assert refined.description == "Refined desc"
    assert refined.acceptance_criteria == ["new1", "new2"]


def test_refine_task_with_architecture(stub_agent) -> None:
    stub_agent.queue.append({"title": "t"})
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    task = Task(id="t1", type=TaskType.BACKEND, assignee="backend")
    arch = SystemArchitecture(overview="big arch")
    a.refine_task(task, ["q"], "spec", architecture=arch)
    assert "big arch" in stub_agent.calls[-1]


def test_evaluate_qa_and_create_fix_tasks(stub_agent) -> None:
    stub_agent.queue.append(
        {
            "tasks": [
                {
                    "id": "fix1",
                    "type": "backend",
                    "title": "Fix bug",
                    "description": "fix",
                    "assignee": "backend",
                    "acceptance_criteria": ["a"],
                    "dependencies": ["t1"],
                },
                {  # Missing id -> skipped
                    "type": "frontend",
                    "title": "Skip me",
                },
                {  # Bad type -> falls back
                    "id": "fix2",
                    "type": "unknown_type",
                    "title": "Other",
                    "acceptance_criteria": "single string",
                },
            ]
        }
    )
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    task = Task(id="t1", type=TaskType.BACKEND, title="orig", assignee="backend")

    class _QA:
        approved = False
        bugs_found = [
            {
                "severity": "high",
                "description": "bug here",
                "location": "file.py:1",
                "recommendation": "fix it",
            }
        ]

    new_tasks = a.evaluate_qa_and_create_fix_tasks(task, _QA(), "spec")
    assert len(new_tasks) == 2
    assert new_tasks[0].id == "fix1"
    # The bad-type entry falls back to original task type
    assert new_tasks[1].type == TaskType.BACKEND
    # String acceptance_criteria becomes a single-element list
    assert new_tasks[1].acceptance_criteria == ["single string"]


def test_evaluate_qa_with_dict_bugs(stub_agent) -> None:
    """Bugs may also come as dicts."""
    stub_agent.queue.append({"tasks": []})
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    task = Task(id="t1", type=TaskType.BACKEND, assignee="backend")

    class _QA:
        approved = False
        bugs_found = [
            {"severity": "low", "description": "small", "location": "x", "recommendation": "y"}
        ]

    out = a.evaluate_qa_and_create_fix_tasks(task, _QA(), "spec")
    assert out == []
    # Prompt should mention the bug content
    assert "small" in stub_agent.calls[-1]


def test_should_run_security_empty_tasks() -> None:
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    assert a.should_run_security([], "spec", []) is False


def test_should_run_security_calls_llm(stub_agent) -> None:
    stub_agent.queue.append({"run_security": True, "rationale": "covers 100%"})
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    assert a.should_run_security(["t1"], "spec", [{"spec_item": "AC1"}]) is True


def test_review_progress_skips_on_llm_connectivity_failure() -> None:
    """A task that failed for connectivity reasons does not trigger new tasks."""
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    update = TaskUpdate(
        task_id="t1", agent_type="backend", status="failed", failure_class="llm_connectivity"
    )
    out = a.review_progress(update, "spec", None, [], [], "")
    assert out == []


def test_review_progress_creates_new_tasks(stub_agent) -> None:
    stub_agent.queue.append(
        {
            "tasks": [
                {
                    "id": "new1",
                    "type": "frontend",
                    "title": "FE fix",
                    "description": "d",
                    "acceptance_criteria": ["a"],
                },
                {  # invalid type -> falls back to BACKEND
                    "id": "new2",
                    "type": "weird",
                    "title": "Other",
                    "acceptance_criteria": "single",
                },
                {  # no id -> dropped
                    "type": "backend",
                    "title": "no id",
                },
            ],
            "spec_compliance_pct": 75,
            "gaps_identified": ["gap1"],
            "rationale": "found issues",
        }
    )
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    update = TaskUpdate(
        task_id="t1",
        agent_type="backend",
        status="completed",
        summary="done",
        files_changed=["a.py"],
        failure_reason="some build error",
    )
    completed = [
        Task(
            id="c1",
            type=TaskType.BACKEND,
            title="prev",
            description="x" * 200,
            assignee="backend",
        )
    ]
    remaining = [
        Task(id="r1", type=TaskType.FRONTEND, title="next", description="", assignee="frontend")
    ]
    out = a.review_progress(
        update,
        "spec",
        SystemArchitecture(overview="arch"),
        completed,
        remaining,
        "codebase summary",
    )
    assert len(out) == 2
    assert out[0].id == "new1"
    assert out[0].type == TaskType.FRONTEND
    assert out[1].type == TaskType.BACKEND  # fallback


def test_trigger_documentation_update_when_readme_missing(monkeypatch, tmp_path: Path) -> None:
    """If README is missing and task was backend/frontend, forces doc update."""
    doc_agent = MagicMock()
    doc_agent.run_full_workflow.return_value = MagicMock(summary="updated")
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    update = TaskUpdate(task_id="t1", agent_type="backend", status="completed", summary="s")
    a.trigger_documentation_update(doc_agent, str(tmp_path), update, "spec", None, "code")
    doc_agent.run_full_workflow.assert_called_once()


def test_trigger_documentation_update_llm_says_no(monkeypatch, tmp_path: Path) -> None:
    """When README exists, ask LLM. If LLM says no, do not call doc agent."""
    (tmp_path / "README.md").write_text("# Big Project\n" + ("x" * 200))

    stub = _StubAgent()
    stub.queue.append({"should_update_docs": False, "rationale": "fine"})
    monkeypatch.setattr(tla_mod, "Agent", lambda *a, **kw: stub)
    monkeypatch.setattr(tla_mod, "get_strands_model", lambda key=None, **_kw: object())

    doc_agent = MagicMock()
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    update = TaskUpdate(task_id="t1", agent_type="qa", status="completed", summary="s")
    a.trigger_documentation_update(doc_agent, str(tmp_path), update, "spec", None, "code")
    doc_agent.run_full_workflow.assert_not_called()


def test_trigger_documentation_update_llm_says_yes(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Big Project\n" + ("x" * 200))
    stub = _StubAgent()
    stub.queue.append({"should_update_docs": True, "rationale": "needed"})
    monkeypatch.setattr(tla_mod, "Agent", lambda *a, **kw: stub)
    monkeypatch.setattr(tla_mod, "get_strands_model", lambda key=None, **_kw: object())

    doc_agent = MagicMock()
    doc_agent.run_full_workflow.return_value = MagicMock(summary="updated")
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    update = TaskUpdate(task_id="t1", agent_type="qa", status="completed", summary="s")
    a.trigger_documentation_update(doc_agent, str(tmp_path), update, "spec", None, "code")
    doc_agent.run_full_workflow.assert_called_once()


def test_trigger_documentation_update_swallows_errors(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("ok ok ok " * 60)
    stub = _StubAgent()
    stub.queue.append({"should_update_docs": True})
    monkeypatch.setattr(tla_mod, "Agent", lambda *a, **kw: stub)
    monkeypatch.setattr(tla_mod, "get_strands_model", lambda key=None, **_kw: object())

    doc_agent = MagicMock()
    doc_agent.run_full_workflow.side_effect = RuntimeError("doc failed")
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    update = TaskUpdate(task_id="t1", agent_type="qa", status="completed", summary="s")
    # Should not raise
    a.trigger_documentation_update(doc_agent, str(tmp_path), update, "spec", None, "code")


def test_trigger_devops_for_backend_skips_non_git(tmp_path: Path) -> None:
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    devops = MagicMock()
    ok = a.trigger_devops_for_backend(devops, str(tmp_path), None, "spec")
    assert ok is False
    devops.run_workflow.assert_not_called()


def test_trigger_devops_for_backend_success(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    devops = MagicMock()
    devops.run_workflow.return_value = MagicMock(success=True, failure_reason=None)
    ok = a.trigger_devops_for_backend(devops, str(tmp_path), None, "spec", existing_pipeline="pipe")
    assert ok is True


def test_trigger_devops_for_backend_pipeline_skip(tmp_path: Path) -> None:
    """When existing_pipeline is the no-code placeholder, it should not be passed."""
    (tmp_path / ".git").mkdir()
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    devops = MagicMock()
    devops.run_workflow.return_value = MagicMock(success=False, failure_reason="boom")
    ok = a.trigger_devops_for_backend(
        devops, str(tmp_path), None, "spec", existing_pipeline="# No code files found"
    )
    assert ok is False
    kwargs = devops.run_workflow.call_args.kwargs
    assert kwargs.get("existing_pipeline") is None


def test_trigger_devops_for_backend_exception(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    devops = MagicMock()
    devops.run_workflow.side_effect = RuntimeError("err")
    ok = a.trigger_devops_for_backend(devops, str(tmp_path), None, "spec")
    assert ok is False


def test_trigger_devops_for_frontend_skips_non_git(tmp_path: Path) -> None:
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    devops = MagicMock()
    ok = a.trigger_devops_for_frontend(devops, str(tmp_path), None, "spec")
    assert ok is False


def test_trigger_devops_for_frontend_success(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    devops = MagicMock()
    devops.run_workflow.return_value = MagicMock(success=True, failure_reason=None)
    ok = a.trigger_devops_for_frontend(devops, str(tmp_path), None, "spec")
    assert ok is True


def test_trigger_devops_for_frontend_failure(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    devops = MagicMock()
    devops.run_workflow.return_value = MagicMock(success=False, failure_reason="boom")
    ok = a.trigger_devops_for_frontend(devops, str(tmp_path), None, "spec")
    assert ok is False


def test_trigger_devops_for_frontend_exception(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    a = TechLeadAgent(llm_client=ConfigurableLLM())
    devops = MagicMock()
    devops.run_workflow.side_effect = RuntimeError("boom")
    ok = a.trigger_devops_for_frontend(devops, str(tmp_path), None, "spec")
    assert ok is False
