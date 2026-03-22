"""Tests for Planning V3 models."""


import sys
from pathlib import Path

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from planning_v3_team.models import (  # noqa: E402
    AnsweredQuestion,
    ClientContext,
    HandoffPackage,
    OpenQuestion,
    OpenQuestionOption,
    Phase,
    PlanningV3ResultResponse,
    PlanningV3RunRequest,
    PlanningV3RunResponse,
    PlanningV3StatusResponse,
)


def test_phase_enum():
    assert Phase.INTAKE.value == "intake"
    assert Phase.DOCUMENT_PRODUCTION.value == "document_production"


def test_planning_v3_run_request():
    r = PlanningV3RunRequest(repo_path="/tmp/repo", client_name="Acme", initial_brief="Build a dashboard")
    assert r.repo_path == "/tmp/repo"
    assert r.client_name == "Acme"
    assert r.use_product_analysis is True
    r2 = PlanningV3RunRequest(repo_path="/x")
    assert r2.spec_content is None


def test_planning_v3_run_response():
    r = PlanningV3RunResponse(job_id="j1", status="running")
    assert r.job_id == "j1"
    assert "job_id" in r.model_dump()


def test_planning_v3_status_response():
    r = PlanningV3StatusResponse(job_id="j1", status="running", progress=50)
    assert r.progress == 50
    assert r.pending_questions == []


def test_planning_v3_result_response():
    r = PlanningV3ResultResponse(job_id="j1", success=True, handoff_package={"a": 1})
    assert r.success is True
    assert r.handoff_package == {"a": 1}


def test_client_context():
    c = ClientContext(client_name="Acme", problem_summary="Need faster reports", target_users=["analysts"])
    assert c.client_name == "Acme"
    assert "analysts" in c.target_users
    d = c.model_dump()
    assert "client_name" in d
    c2 = ClientContext(**d)
    assert c2.client_name == c.client_name


def test_handoff_package_serialization():
    p = HandoffPackage(
        client_context_document_path="/plan/client_context.md",
        validated_spec_path="/plan/product_analysis/validated_spec.md",
        prd_path="/plan/product_analysis/product_requirements_document.md",
        summary="Done",
    )
    d = p.model_dump()
    assert d["validated_spec_path"] == "/plan/product_analysis/validated_spec.md"
    p2 = HandoffPackage(**d)
    assert p2.summary == "Done"


def test_open_question_and_option():
    opt = OpenQuestionOption(id="opt1", label="Option A", is_default=True)
    q = OpenQuestion(id="q1", question_text="Which?", options=[opt], source="planning_v3")
    assert q.options[0].is_default is True
    d = q.model_dump()
    assert d["id"] == "q1"


def test_answered_question():
    a = AnsweredQuestion(question_id="q1", selected_option_id="opt1", selected_answer="Option A")
    assert a.question_id == "q1"
