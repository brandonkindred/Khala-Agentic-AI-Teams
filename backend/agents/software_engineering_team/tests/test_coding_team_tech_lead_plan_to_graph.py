"""Unit test for Tech Lead plan-to-Task-Graph: given CodingTeamPlanInput, output has tasks with deps and StackSpec list."""

from __future__ import annotations

from software_engineering_team.models import CodingTeamPlanInput
from software_engineering_team.tech_lead_agent import agent as tl_mod
from software_engineering_team.tech_lead_agent.agent import TechLeadAgent


def test_tech_lead_plan_to_task_graph_output_structure(monkeypatch) -> None:
    """Given CodingTeamPlanInput, Tech Lead output contains tasks (with deps) and stacks list."""
    plan = CodingTeamPlanInput(
        requirements_title="Test Project",
        requirements_description="Build a small API and UI.",
        project_overview={"features_and_functionality_doc": "REST API", "goals": "Ship fast"},
        final_spec_content="Spec content here.",
        repo_path="/tmp/repo",
        architecture_overview="Backend FastAPI, frontend Angular.",
    )
    # The Tech Lead drives a strands Agent via _agent_call_json; stub both so no real LLM runs.
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt, required_keys=None: {
            "tasks": [
                {
                    "id": "t1",
                    "title": "Backend API",
                    "description": "Implement endpoints",
                    "dependencies": [],
                },
                {
                    "id": "t2",
                    "title": "Frontend UI",
                    "description": "Implement UI",
                    "dependencies": ["t1"],
                },
            ],
            "stacks": [
                {"name": "backend", "tools_services": ["Python", "FastAPI"]},
                {"name": "frontend", "tools_services": ["Angular", "TypeScript"]},
            ],
        },
    )
    agent = TechLeadAgent(model=object())
    out = agent.run_plan_to_task_graph(plan)
    assert "tasks" in out
    assert "stacks" in out
    tasks = out["tasks"]
    stacks = out["stacks"]
    assert len(tasks) == 2
    assert tasks[0]["id"] == "t1"
    assert tasks[0]["dependencies"] == []
    assert tasks[1]["id"] == "t2"
    assert tasks[1]["dependencies"] == ["t1"]
    assert len(stacks) == 2
    assert stacks[0]["name"] == "backend"
    assert "FastAPI" in stacks[0]["tools_services"]
    assert stacks[1]["name"] == "frontend"


def test_tech_lead_plan_to_task_graph_preserves_target_team(monkeypatch) -> None:
    """Tech Lead task output carries the implementation team routing hint."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt, required_keys=None: {
            "tasks": [
                {
                    "id": "ui",
                    "title": "Frontend UI",
                    "description": "Build Angular components",
                    "dependencies": [],
                    "target_team": "frontend_v2",
                },
                {
                    "id": "api",
                    "title": "Backend API",
                    "description": "Build FastAPI routes",
                    "dependencies": ["ui"],
                    "target_team": "backend_v2",
                },
            ],
            "stacks": [
                {"name": "frontend_v2", "tools_services": ["Angular", "TypeScript"]},
                {"name": "backend_v2", "tools_services": ["Python", "FastAPI"]},
            ],
        },
    )
    out = TechLeadAgent(model=object()).run_plan_to_task_graph(
        CodingTeamPlanInput(requirements_title="X", repo_path="/tmp")
    )
    assert out["tasks"][0]["target_team"] == "frontend_v2"
    assert out["tasks"][1]["target_team"] == "backend_v2"


def test_tech_lead_plan_to_task_graph_ignores_legacy_team_fields(monkeypatch) -> None:
    """Legacy team/stack/assignee_stack fields are no longer read; only target_team counts."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt, required_keys=None: {
            "tasks": [
                {"id": "ui", "title": "UI", "team": "frontend_v2"},
                {"id": "api", "title": "API", "stack": "backend_v2"},
                {"id": "deploy", "title": "Deploy", "assignee_stack": "devops"},
            ],
            "stacks": [],
        },
    )

    out = TechLeadAgent(model=object()).run_plan_to_task_graph(
        CodingTeamPlanInput(requirements_title="X", repo_path="/tmp")
    )

    assert [task["target_team"] for task in out["tasks"]] == ["", "", ""]
    assert {s["name"] for s in out["stacks"]} == {"frontend_v2", "backend_v2"}


def test_tech_lead_run_groom_task(monkeypatch) -> None:
    """run_groom_task returns the enriched grooming fields parsed from the LLM JSON."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt, required_keys=None: {
            "acceptance_criteria": ["done when tests pass"],
            "out_of_scope": "no UI",
            "description_enriched": "more detail",
            "priority": "high",
            "subtasks": [{"id": "s1", "title": "sub", "description": "", "dependencies": []}],
            "task_dependencies": ["d1"],
        },
    )
    out = TechLeadAgent(model=object()).run_groom_task("t1", "T", "desc", ["d1"], "plan ctx")
    assert out["acceptance_criteria"] == ["done when tests pass"]
    assert out["priority"] == "high"
    assert out["subtasks"][0]["id"] == "s1"
    assert out["task_dependencies"] == ["d1"]


def test_tech_lead_run_groom_task_llm_failure_returns_defaults(monkeypatch) -> None:
    """When grooming's LLM call fails, return safe defaults that preserve the input dependencies."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr("llm_service.util.time.sleep", lambda *_a, **_k: None)

    def boom(agent, prompt, required_keys=None):
        raise RuntimeError("LLM error")

    monkeypatch.setattr(tl_mod, "_agent_call_json", boom)
    out = TechLeadAgent(model=object()).run_groom_task("t1", "T", "desc", ["d1"], "ctx")
    assert out["priority"] == "medium"
    assert out["acceptance_criteria"] == []
    assert out["task_dependencies"] == ["d1"]


def test_tech_lead_run_groom_task_retries_transient_error_then_succeeds(monkeypatch) -> None:
    """A transient grooming error (rate limit/timeout) is retried, not turned into defaults."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr("llm_service.util.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def flaky(agent, prompt, required_keys=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {
            "acceptance_criteria": ["done when tests pass"],
            "out_of_scope": "no UI",
            "description_enriched": "more detail",
            "priority": "high",
            "subtasks": [],
            "task_dependencies": ["d1"],
        }

    monkeypatch.setattr(tl_mod, "_agent_call_json", flaky)
    out = TechLeadAgent(model=object()).run_groom_task("t1", "T", "desc", ["d1"], "ctx")
    assert calls["n"] == 2
    assert out["priority"] == "high"
    assert out["acceptance_criteria"] == ["done when tests pass"]


def test_tech_lead_plan_to_task_graph_llm_failure_returns_defaults(monkeypatch) -> None:
    """When the LLM call fails, return empty tasks and the canonical v2 stack roster."""
    plan = CodingTeamPlanInput(
        requirements_title="X",
        requirements_description="",
        repo_path="/tmp",
    )

    def boom(agent, prompt, required_keys=None):
        raise RuntimeError("LLM error")

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr("llm_service.util.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(tl_mod, "_agent_call_json", boom)
    agent = TechLeadAgent(model=object())
    out = agent.run_plan_to_task_graph(plan)
    assert out["tasks"] == []
    assert len(out["stacks"]) == 2
    assert {s["name"] for s in out["stacks"]} == {"frontend_v2", "backend_v2"}


def test_tech_lead_plan_to_task_graph_retries_transient_error_then_succeeds(monkeypatch) -> None:
    """A transient planning error (rate limit/timeout) is retried, not turned into defaults."""
    plan = CodingTeamPlanInput(requirements_title="X", repo_path="/tmp")
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr("llm_service.util.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def flaky(agent, prompt, required_keys=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {
            "tasks": [{"id": "t1", "title": "T1", "description": "", "dependencies": []}],
            "stacks": [{"name": "backend_v2", "tools_services": []}],
            "open_questions": [],
            "already_complete": False,
        }

    monkeypatch.setattr(tl_mod, "_agent_call_json", flaky)
    out = TechLeadAgent(model=object()).run_plan_to_task_graph(plan)
    assert calls["n"] == 2
    assert len(out["tasks"]) == 1
    assert out["tasks"][0]["id"] == "t1"
    assert out["stacks"][0]["name"] == "backend_v2"


def test_plan_text_passes_all_fields_uncut() -> None:
    """Every plan field reaches the Task-Graph prompt in full — inputs are never truncated."""
    big_desc = "D" * 50000
    big_overview_value = "O" * 50000
    big_spec = "S" * 50000
    big_arch = "A" * 50000
    plan = CodingTeamPlanInput(
        requirements_title="Big Project",
        requirements_description=big_desc,
        project_overview={"doc": big_overview_value},
        final_spec_content=big_spec,
        repo_path="/tmp/repo",
        architecture_overview=big_arch,
    )
    text = tl_mod._plan_text(plan)
    assert big_desc in text
    assert big_overview_value in text
    assert big_spec in text
    assert big_arch in text


def test_plan_text_includes_completed_work_summary() -> None:
    """Already-completed work (completed_work_summary) reaches the planning prompt under the
    'work already completed' heading so the Tech Lead can recognize it and short-circuit."""
    plan = CodingTeamPlanInput(
        requirements_title="X",
        requirements_description="do the thing",
        repo_path="/tmp",
        completed_work_summary="Already-completed sub-issues:\n- #12 Add login\n- #13 Add logout",
    )
    text = tl_mod._plan_text(plan)
    assert "#12 Add login" in text
    assert "already merged/done" in text.lower() or "already completed" in text.lower()


def test_plan_text_omits_existing_code_summary_from_prompt() -> None:
    """Existing repository code is deliberately NOT surfaced to the planner: feeding repo source into
    the plan prompt risks a false already_complete on the main SE path (where this field carries the
    whole repo). Only completed_work_summary reaches the prompt."""
    plan = CodingTeamPlanInput(
        requirements_title="X",
        requirements_description="do the thing",
        repo_path="/tmp",
        existing_code_summary="def login():\n    ...  # current repo source for reference",
    )
    text = tl_mod._plan_text(plan)
    assert "current repo source for reference" not in text  # repo code never reaches the planner
    assert "do NOT recreate" not in text  # and no already-completed framing is emitted


def test_plan_to_task_graph_already_complete(monkeypatch) -> None:
    """already_complete + no tasks is surfaced with the evidence so the caller can short-circuit."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt, required_keys=None: {
            "tasks": [],
            "stacks": [],
            "already_complete": True,
            "completion_evidence": "All sub-issues #12 and #13 are closed and merged.",
        },
    )
    out = TechLeadAgent(model=object()).run_plan_to_task_graph(
        CodingTeamPlanInput(requirements_title="X", repo_path="/tmp")
    )
    assert out["already_complete"] is True
    assert "#12" in out["completion_evidence"]
    assert out["tasks"] == []


def test_plan_to_task_graph_already_complete_ignored_when_tasks_present(monkeypatch) -> None:
    """A true flag alongside real tasks is contradictory; the tasks win (work is never dropped)."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt, required_keys=None: {
            "tasks": [{"id": "t1", "title": "Real task", "description": "", "dependencies": []}],
            "stacks": [],
            "already_complete": True,
            "completion_evidence": "ignored",
        },
    )
    out = TechLeadAgent(model=object()).run_plan_to_task_graph(
        CodingTeamPlanInput(requirements_title="X", repo_path="/tmp")
    )
    assert out["already_complete"] is False
    assert out["completion_evidence"] == ""
    assert len(out["tasks"]) == 1


def test_plan_to_task_graph_already_complete_string_false_is_not_truthy(monkeypatch) -> None:
    """LLM schema drift: a STRING "false" must NOT short-circuit to already_complete. bool("false")
    is True, so a naive cast would wrongly close the issue with no PR."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt, required_keys=None: {
            "tasks": [],
            "stacks": [],
            "already_complete": "false",  # string, not bool
            "completion_evidence": "should be ignored",
        },
    )
    out = TechLeadAgent(model=object()).run_plan_to_task_graph(
        CodingTeamPlanInput(requirements_title="X", repo_path="/tmp")
    )
    assert out["already_complete"] is False
    assert out["completion_evidence"] == ""


def test_as_bool_strict_coercion() -> None:
    """Only a real True or an explicit true-like string counts; everything else is False."""
    assert tl_mod._as_bool(True) is True
    assert tl_mod._as_bool("true") is True
    assert tl_mod._as_bool(" TRUE ") is True
    assert tl_mod._as_bool("yes") is True
    assert tl_mod._as_bool("1") is True
    assert tl_mod._as_bool(False) is False
    assert tl_mod._as_bool("false") is False  # the bug this guards
    assert tl_mod._as_bool("0") is False
    assert tl_mod._as_bool("no") is False
    assert tl_mod._as_bool(None) is False
    assert tl_mod._as_bool(0) is False
    assert tl_mod._as_bool("maybe") is False


def test_run_revision_adjudication_verdicts(monkeypatch) -> None:
    """Each valid verdict is parsed through; the reason is preserved."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    for verdict in ("done", "fail", "continue"):
        monkeypatch.setattr(
            tl_mod,
            "_agent_call_json",
            lambda agent, prompt, _v=verdict, required_keys=None: {
                "verdict": _v.upper(),
                "reason": f"because {_v}",
            },
        )
        out = TechLeadAgent(model=object()).run_revision_adjudication(
            "T1", "desc", ["ac"], "summary", [{"source": "tech_lead", "reason": "nope"}]
        )
        assert out["verdict"] == verdict
        assert out["reason"] == f"because {verdict}"


def test_run_revision_adjudication_fails_closed(monkeypatch) -> None:
    """An LLM error or an unusable verdict must fail closed to 'fail', never re-enter the loop."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())

    def boom(agent, prompt, required_keys=None):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(tl_mod, "_agent_call_json", boom)
    out = TechLeadAgent(model=object()).run_revision_adjudication("T", "d", [], "s", [])
    assert out["verdict"] == "fail"

    monkeypatch.setattr(
        tl_mod, "_agent_call_json", lambda agent, prompt, required_keys=None: {"verdict": "maybe"}
    )
    out = TechLeadAgent(model=object()).run_revision_adjudication("T", "d", [], "s", [])
    assert out["verdict"] == "fail"


def test_run_groom_task_passes_full_inputs_to_llm(monkeypatch) -> None:
    """A large task description and plan context reach the groom prompt in full, never truncated."""
    captured: dict[str, str] = {}

    def _capture(agent, prompt, required_keys=None):
        captured["prompt"] = prompt
        return {}

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(tl_mod, "_agent_call_json", _capture)
    agent = TechLeadAgent(model=object())
    big_desc = "D" * 50000
    big_context = "C" * 50000

    agent.run_groom_task(
        task_id="t1",
        task_title="T1",
        task_description=big_desc,
        task_dependencies=[],
        plan_context=big_context,
    )

    assert big_desc in captured["prompt"]
    assert big_context in captured["prompt"]


def test_tech_lead_run_assignments_retries_transient_error_then_succeeds(monkeypatch) -> None:
    """A transient assignment error (rate limit/timeout) is retried, not silently dropped to []."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr("llm_service.util.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def flaky(agent, prompt, required_keys=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {"assignments": [{"agent_id": "backend_v2", "task_id": "t1"}]}

    monkeypatch.setattr(tl_mod, "_agent_call_json", flaky)
    out = TechLeadAgent(model=object()).run_assignments(
        agent_ids=["backend_v2"], ready_tasks=[{"id": "t1"}], free_agents=["backend_v2"]
    )
    assert calls["n"] == 2
    assert out["assignments"] == [{"agent_id": "backend_v2", "task_id": "t1"}]


def test_tech_lead_run_assignments_llm_failure_returns_defaults(monkeypatch) -> None:
    """When assignments' LLM call fails, return an empty assignments list."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr("llm_service.util.time.sleep", lambda *_a, **_k: None)

    def boom(agent, prompt, required_keys=None):
        raise RuntimeError("LLM error")

    monkeypatch.setattr(tl_mod, "_agent_call_json", boom)
    out = TechLeadAgent(model=object()).run_assignments(
        agent_ids=["backend_v2"], ready_tasks=[{"id": "t1"}], free_agents=["backend_v2"]
    )
    assert out["assignments"] == []
