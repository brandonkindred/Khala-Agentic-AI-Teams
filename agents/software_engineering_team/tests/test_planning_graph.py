"""Tests for PlanningGraph and compiler."""

from __future__ import annotations

import pytest

from planning_team.planning_graph import (
    PlanningDomain,
    PlanningEdge,
    PlanningGraph,
    PlanningNode,
    PlanningNodeKind,
    EdgeType,
    Phase,
    compile_planning_graph_to_task_assignment,
    ensure_dict,
    ensure_str_list,
)
from shared.models import TaskType


def test_planning_graph_add_and_merge():
    """PlanningGraph supports add_node, add_edge, and merge."""
    g = PlanningGraph()
    n1 = PlanningNode(id="backend-api", domain=PlanningDomain.BACKEND, kind=PlanningNodeKind.TASK, summary="API")
    g.add_node(n1)
    g.add_edge(PlanningEdge(from_id="git-setup", to_id="backend-api", type=EdgeType.BLOCKS))

    assert "backend-api" in g.nodes
    assert len(g.edges) == 1

    g2 = PlanningGraph()
    n2 = PlanningNode(id="frontend-ui", domain=PlanningDomain.FRONTEND, kind=PlanningNodeKind.TASK, summary="UI")
    g2.add_node(n2)
    g.merge(g2)
    assert "frontend-ui" in g.nodes
    assert len(g.nodes) == 2


def test_compile_empty_graph():
    """Empty graph compiles to empty TaskAssignment."""
    g = PlanningGraph()
    assignment = compile_planning_graph_to_task_assignment(g)
    assert len(assignment.tasks) == 0
    assert len(assignment.execution_order) == 0


def test_compile_single_task():
    """Single TASK node compiles to one Task."""
    g = PlanningGraph()
    g.add_node(PlanningNode(
        id="backend-todo-crud",
        domain=PlanningDomain.BACKEND,
        kind=PlanningNodeKind.TASK,
        summary="Todo CRUD API",
        details="Implement REST CRUD for todos with FastAPI.",
        acceptance_criteria=["GET /todos", "POST /todos", "Spec compliance"],
    ))
    assignment = compile_planning_graph_to_task_assignment(g)
    assert len(assignment.tasks) == 1
    t = assignment.tasks[0]
    assert t.id == "backend-todo-crud"
    assert t.type == TaskType.BACKEND
    assert t.assignee == "backend"
    assert len(t.acceptance_criteria) >= 3
    assert assignment.execution_order == ["backend-todo-crud"]


def test_compile_respects_blocks_edges():
    """BLOCKS edges produce correct execution order."""
    g = PlanningGraph()
    g.add_node(PlanningNode(id="git-setup", domain=PlanningDomain.GIT_SETUP, kind=PlanningNodeKind.TASK, summary="Git"))
    g.add_node(PlanningNode(id="backend-1", domain=PlanningDomain.BACKEND, kind=PlanningNodeKind.TASK, summary="B1"))
    g.add_node(PlanningNode(id="frontend-1", domain=PlanningDomain.FRONTEND, kind=PlanningNodeKind.TASK, summary="F1"))
    g.add_edge(PlanningEdge(from_id="git-setup", to_id="backend-1", type=EdgeType.BLOCKS))
    g.add_edge(PlanningEdge(from_id="git-setup", to_id="frontend-1", type=EdgeType.BLOCKS))

    assignment = compile_planning_graph_to_task_assignment(g)
    assert len(assignment.tasks) == 3
    order = assignment.execution_order
    # git-setup should come first (prefix)
    assert order[0] == "git-setup"
    # backend and frontend can follow in any interleaved order
    assert set(order[1:]) == {"backend-1", "frontend-1"}


def test_compile_epic_feature_ignored():
    """EPIC and FEATURE nodes are not emitted as tasks."""
    g = PlanningGraph()
    g.add_node(PlanningNode(id="epic-1", domain=PlanningDomain.BACKEND, kind=PlanningNodeKind.EPIC, summary="Epic"))
    g.add_node(PlanningNode(id="feature-1", domain=PlanningDomain.BACKEND, kind=PlanningNodeKind.FEATURE, summary="Feature"))
    g.add_node(PlanningNode(id="task-1", domain=PlanningDomain.BACKEND, kind=PlanningNodeKind.TASK, summary="Task"))
    assignment = compile_planning_graph_to_task_assignment(g)
    assert len(assignment.tasks) == 1
    assert assignment.tasks[0].id == "task-1"


def test_compile_qa_tasks_skipped():
    """QA domain tasks are skipped (orchestrator invokes QA separately)."""
    g = PlanningGraph()
    g.add_node(PlanningNode(id="qa-e2e", domain=PlanningDomain.QA, kind=PlanningNodeKind.TASK, summary="E2E tests"))
    assignment = compile_planning_graph_to_task_assignment(g)
    assert len(assignment.tasks) == 0


def test_compile_includes_all_tasks():
    """All TASK nodes are compiled into tasks (no overly-broad filtering)."""
    g = PlanningGraph()
    g.add_node(PlanningNode(
        id="backend-monolithic",
        domain=PlanningDomain.BACKEND,
        kind=PlanningNodeKind.TASK,
        summary="Full API",
        details="Implement models and schemas and endpoints and validation and error handling",
        acceptance_criteria=["Done"],
    ))
    g.add_node(PlanningNode(
        id="backend-granular",
        domain=PlanningDomain.BACKEND,
        kind=PlanningNodeKind.TASK,
        summary="Task models",
        details="Add SQLAlchemy models for tasks.",
        acceptance_criteria=["Models exist", "Schema correct"],
    ))
    assignment = compile_planning_graph_to_task_assignment(g)
    assert len(assignment.tasks) == 2
    ids = {t.id for t in assignment.tasks}
    assert "backend-monolithic" in ids
    assert "backend-granular" in ids


def test_compile_backfills_user_story_for_backend_frontend_when_missing():
    """Backend and frontend tasks get default user_story when metadata has none."""
    g = PlanningGraph()
    g.add_node(PlanningNode(
        id="backend-git-setup",
        domain=PlanningDomain.BACKEND,
        kind=PlanningNodeKind.TASK,
        summary="Git setup",
        details="Initialize git repo and branches.",
        acceptance_criteria=["Repo exists", "Main branch", "Feature branch"],
        metadata={},  # no user_story
    ))
    g.add_node(PlanningNode(
        id="frontend-dashboard",
        domain=PlanningDomain.FRONTEND,
        kind=PlanningNodeKind.TASK,
        summary="Dashboard page",
        details="Build the main dashboard UI.",
        acceptance_criteria=["Page loads", "Layout correct", "Responsive"],
        metadata={},  # no user_story
    ))
    assignment = compile_planning_graph_to_task_assignment(g)
    assert len(assignment.tasks) == 2
    for t in assignment.tasks:
        assert t.user_story, f"Task {t.id} should have backfilled user_story"
        assert "As a developer" in t.user_story
        assert "so that the system meets the requirements" in t.user_story
        if t.id == "backend-git-setup":
            assert "Git setup" in t.user_story
        else:
            assert "Dashboard page" in t.user_story


def test_compile_preserves_user_story_when_provided():
    """When metadata has user_story, it is used (no backfill overwrite)."""
    g = PlanningGraph()
    custom_story = "As an API consumer, I want REST endpoints for todos so that the frontend can manage todos."
    g.add_node(PlanningNode(
        id="backend-todo-api",
        domain=PlanningDomain.BACKEND,
        kind=PlanningNodeKind.TASK,
        summary="Todo API",
        details="Implement REST CRUD.",
        acceptance_criteria=["GET", "POST", "PUT", "DELETE"],
        metadata={"user_story": custom_story},
    ))
    assignment = compile_planning_graph_to_task_assignment(g)
    assert len(assignment.tasks) == 1
    assert assignment.tasks[0].user_story == custom_story


def test_ensure_str_list_handles_malformed_inputs():
    """ensure_str_list coerces None, string, and non-list values to List[str]."""
    assert ensure_str_list(None) == []
    assert ensure_str_list("") == []
    assert ensure_str_list("single") == ["single"]
    assert ensure_str_list(["a", "b"]) == ["a", "b"]
    assert ensure_str_list([]) == []
    assert ensure_str_list(123) == []


def test_ensure_dict_handles_malformed_inputs():
    """ensure_dict coerces None and non-dict values to Dict."""
    assert ensure_dict(None) == {}
    assert ensure_dict({}) == {}
    assert ensure_dict({"a": 1}) == {"a": 1}
    assert ensure_dict("invalid") == {}


def test_compile_reclassifies_frontend_id_with_wrong_domain():
    """When node id starts with frontend- but domain is backend, reclassify to frontend."""
    g = PlanningGraph()
    g.add_node(PlanningNode(
        id="frontend-app-init",
        domain=PlanningDomain.BACKEND,  # Misclassified by planner
        kind=PlanningNodeKind.TASK,
        summary="Initialize Angular frontend app",
        details="Create Angular app shell with routing.",
        acceptance_criteria=["App created", "Routing works"],
    ))
    assignment = compile_planning_graph_to_task_assignment(g)
    assert len(assignment.tasks) == 1
    t = assignment.tasks[0]
    assert t.id == "frontend-app-init"
    assert t.type == TaskType.FRONTEND
    assert t.assignee == "frontend"


def test_compile_data_domain_maps_to_backend_assignee():
    """DATA and PERF domains map to backend assignee (no separate data agent)."""
    g = PlanningGraph()
    g.add_node(PlanningNode(
        id="backend-schema-migrations",
        domain=PlanningDomain.DATA,
        kind=PlanningNodeKind.TASK,
        summary="Database migrations",
        details="Add Alembic migrations for schema.",
        acceptance_criteria=["Migrations run", "Schema correct"],
    ))
    assignment = compile_planning_graph_to_task_assignment(g)
    assert len(assignment.tasks) == 1
    t = assignment.tasks[0]
    assert t.assignee == "backend"
    assert t.type == TaskType.BACKEND


def test_planning_node_from_malformed_llm_dict():
    """PlanningNode can be built from LLM dict with inputs/outputs/acceptance_criteria as string or null."""
    n = {
        "id": "frontend-task-list",
        "inputs": "api",  # LLM sometimes returns string instead of list
        "outputs": None,
        "acceptance_criteria": "one item",  # string instead of list
        "metadata": "invalid",  # non-dict
    }
    node = PlanningNode(
        id=n["id"],
        domain=PlanningDomain.FRONTEND,
        kind=PlanningNodeKind.TASK,
        summary=n.get("summary", ""),
        details=n.get("details", ""),
        inputs=ensure_str_list(n.get("inputs")),
        outputs=ensure_str_list(n.get("outputs")),
        acceptance_criteria=ensure_str_list(n.get("acceptance_criteria")),
        metadata=ensure_dict(n.get("metadata")),
    )
    assert node.id == "frontend-task-list"
    assert node.inputs == ["api"]
    assert node.outputs == []
    assert node.acceptance_criteria == ["one item"]
    assert node.metadata == {}
