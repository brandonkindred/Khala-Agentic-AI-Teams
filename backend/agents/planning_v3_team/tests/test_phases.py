"""Unit tests for Planning V3 phases (mocked LLM and adapters)."""

import sys
from pathlib import Path

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from planning_v3_team.models import ClientContext  # noqa: E402
from planning_v3_team.phases import (  # noqa: E402
    run_discovery,
    run_intake,
    run_requirements,
    run_synthesis,
)
from planning_v3_team.tests.conftest import make_llm  # noqa: E402


def test_run_intake():
    ctx_update, artifacts = run_intake(
        repo_path="/tmp/repo",
        client_name="Acme",
        initial_brief="Build a dashboard",
        spec_content="# Spec\n\nFeatures.",
    )
    assert "client_context" in ctx_update
    assert ctx_update["repo_path"] == "/tmp/repo"
    assert ctx_update["spec_content"] == "# Spec\n\nFeatures."
    assert "client_context" in artifacts


def test_run_synthesis_no_evidence():
    context = {"client_context": ClientContext(client_name="Acme")}
    ctx_update, artifacts = run_synthesis(context, market_research_evidence=None)
    assert not ctx_update
    assert artifacts.get("evidence") is None


def test_run_synthesis_with_evidence():
    context = {"client_context": ClientContext(client_name="Acme")}
    evidence = {"summary": "Market is growing", "insights": ["i1"], "market_signals": []}
    ctx_update, artifacts = run_synthesis(context, market_research_evidence=evidence)
    assert "market_research_evidence" in ctx_update
    assert artifacts["evidence"] == evidence
    assert "client_context" in ctx_update
    updated_ctx = ctx_update["client_context"]
    assert updated_ctx.constraints.get("market_research_summary") == "Market is growing"


def test_run_requirements_with_mock_llm():
    context = {
        "client_context": ClientContext(problem_summary="Need reports"),
        "initial_brief": "Brief",
        "spec_content": "Spec",
    }
    llm = make_llm(
        """{"questions": [
        {"id": "req_1", "question_text": "RPO/RTO?", "context": "...", "category": "business", "priority": "high",
         "options": [{"id": "opt_none", "label": "None", "is_default": true}]}
    ]}"""
    )
    ctx_update, artifacts = run_requirements(context, llm=llm)
    assert "open_questions" in ctx_update
    # LLM path (not defaults): the question id from the JSON is present.
    ids = {q.id for q in ctx_update["open_questions"]}
    assert "req_1" in ids
    assert artifacts["open_questions"]


def test_run_requirements_empty_falls_back_to_defaults():
    context = {"client_context": ClientContext(problem_summary="P")}
    # complete_text returns no questions -> reduce yields empty -> default questions.
    llm = make_llm('{"questions": []}')
    ctx_update, _ = run_requirements(context, llm=llm)
    assert len(ctx_update["open_questions"]) >= 1
    # Default set ids.
    ids = {q.id for q in ctx_update["open_questions"]}
    assert "req_rpo_rto" in ids


def test_run_requirements_dedupes_across_sections():
    # Force multi-section via a tiny context; both sections return the same question id.
    context = {
        "client_context": ClientContext(problem_summary="P"),
        "spec_content": _multi_heading_doc(3, 5000),
    }
    payload = (
        '{"questions": [{"id": "dup", "question_text": "Same?", "category": "tech",'
        ' "priority": "low", "options": []}]}'
    )
    llm = make_llm(payload, max_ctx=1000)  # floor budget -> multiple sections
    ctx_update, _ = run_requirements(context, llm=llm)
    ids = [q.id for q in ctx_update["open_questions"]]
    assert ids.count("dup") == 1  # deduped


def test_run_discovery_with_mock_llm():
    context = {
        "client_context": ClientContext(client_name="Acme"),
        "initial_brief": "Brief",
        "spec_content": "Spec body",
    }
    llm = make_llm(
        '{"problem_summary": "Need X", "opportunity_statement": "Y",'
        ' "target_users": ["u1"], "success_criteria": ["c1"], "assumptions": ["a1"]}'
    )
    ctx_update, artifacts = run_discovery(context, llm=llm)
    cc = ctx_update["client_context"]
    assert cc.problem_summary == "Need X"
    assert "u1" in cc.target_users
    assert "c1" in cc.success_criteria
    assert artifacts["discovery"]["opportunity_statement"] == "Y"


def test_run_discovery_brief_only_and_spec_only():
    for key in ("initial_brief", "spec_content"):
        context = {"client_context": ClientContext(), key: "Some material"}
        llm = make_llm(
            '{"problem_summary": "P", "opportunity_statement": "",'
            ' "target_users": [], "success_criteria": [], "assumptions": []}'
        )
        ctx_update, _ = run_discovery(context, llm=llm)
        assert ctx_update["client_context"].problem_summary == "P"


def test_run_discovery_multi_section_unions_lists():
    # Two sections return overlapping + distinct personas; reduce should union+dedupe.
    payloads = iter(
        [
            '{"problem_summary": "P1", "opportunity_statement": "O1",'
            ' "target_users": ["admin", "user"], "success_criteria": ["fast"], "assumptions": []}',
            '{"problem_summary": "P2", "opportunity_statement": "O2",'
            ' "target_users": ["User", "guest"], "success_criteria": ["fast", "cheap"], "assumptions": ["x"]}',
        ]
    )
    context = {
        "client_context": ClientContext(),
        "spec_content": _multi_heading_doc(2, 5000),
    }
    llm = make_llm(lambda *a, **k: next(payloads), max_ctx=1000)
    ctx_update, _ = run_discovery(context, llm=llm)
    cc = ctx_update["client_context"]
    # First non-empty scalar wins; lists are case-insensitively deduped unions.
    assert cc.problem_summary == "P1"
    assert sorted(u.lower() for u in cc.target_users) == ["admin", "guest", "user"]
    assert sorted(cc.success_criteria) == ["cheap", "fast"]


def test_run_discovery_accepts_dict_client_context():
    context = {
        "client_context": {"client_name": "Acme"},  # dict, not ClientContext
        "spec_content": "Spec",
    }
    llm = make_llm(
        '{"problem_summary": "P", "opportunity_statement": "",'
        ' "target_users": [], "success_criteria": [], "assumptions": []}'
    )
    ctx_update, _ = run_discovery(context, llm=llm)
    assert ctx_update["client_context"].client_name == "Acme"


def test_run_requirements_accepts_dict_client_context():
    context = {
        "client_context": {"problem_summary": "P"},  # dict, not ClientContext
        "spec_content": "Spec",
    }
    llm = make_llm('{"questions": []}')
    ctx_update, _ = run_requirements(context, llm=llm)
    assert ctx_update["open_questions"]  # defaults applied


def _multi_heading_doc(n: int, body_chars: int) -> str:
    return "".join(f"# Heading {i}\n" + ("b" * body_chars) + "\n" for i in range(n))
