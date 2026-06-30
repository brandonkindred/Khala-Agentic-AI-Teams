"""Unit tests for Planning V3 phases (mocked LLM and adapters)."""

import sys
from pathlib import Path
from unittest.mock import create_autospec

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from llm_service import LLMClient  # noqa: E402
from planning_v3_team.models import ClientContext  # noqa: E402
from planning_v3_team.phases import (  # noqa: E402
    run_discovery,
    run_intake,
    run_requirements,
    run_synthesis,
)
from planning_v3_team.tests.conftest import make_llm, multi_heading_doc  # noqa: E402


def _autospec_llm(complete_text_return: str):
    """A strict-spec LLMClient mock: kwargs are validated against the REAL
    ``LLMClient`` signature, so a call passing ``think=``/``objective=`` only succeeds
    if those parameters actually exist. Guards against silent interface drift that a
    permissive ``MagicMock`` would hide."""
    llm = create_autospec(LLMClient, instance=True)
    llm.get_max_context_tokens.return_value = 16384
    llm.complete_text.return_value = complete_text_return
    return llm


def test_run_discovery_complete_text_kwargs_match_real_signature():
    """Proves the phase's complete_text(think=, objective=) call matches LLMClient."""
    llm = _autospec_llm(
        '{"problem_summary": "Need X", "opportunity_statement": "",'
        ' "target_users": [], "success_criteria": [], "assumptions": []}'
    )
    ctx_update, _ = run_discovery({"client_context": ClientContext(), "spec_content": "S"}, llm=llm)
    # If complete_text rejected think/objective, autospec raises TypeError ->
    # map_reduce falls back and problem_summary would be the raw material "S".
    assert ctx_update["client_context"].problem_summary == "Need X"
    llm.complete_text.assert_called_once()


def test_run_requirements_complete_text_kwargs_match_real_signature():
    """Same strict-spec check for the requirements phase's complete_text call."""
    llm = _autospec_llm(
        '{"questions": [{"id": "q1", "question_text": "Where?", "category": "tech",'
        ' "priority": "low", "options": []}]}'
    )
    ctx_update, _ = run_requirements(
        {"client_context": ClientContext(problem_summary="P"), "spec_content": "S"}, llm=llm
    )
    ids = {q.id for q in ctx_update["open_questions"]}
    assert "q1" in ids  # LLM path, not the default fallback
    llm.complete_text.assert_called_once()


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


def test_run_requirements_malformed_questions_entries_fall_back():
    # Syntactically valid JSON but schema-malformed entries must not crash; with no
    # valid questions extracted, the phase falls back to the default question set.
    context = {"client_context": ClientContext(problem_summary="P"), "spec_content": "S"}
    llm = make_llm('{"questions": ["RPO?", 42, null]}')  # all non-dict entries
    ctx_update, _ = run_requirements(context, llm=llm)
    ids = {q.id for q in ctx_update["open_questions"]}
    assert "req_rpo_rto" in ids  # default fallback, no AttributeError


def test_run_requirements_questions_not_a_list_falls_back():
    context = {"client_context": ClientContext(problem_summary="P"), "spec_content": "S"}
    llm = make_llm('{"questions": "RPO?"}')  # questions is a string, not a list
    ctx_update, _ = run_requirements(context, llm=llm)
    assert len(ctx_update["open_questions"]) >= 1


def test_run_requirements_skips_malformed_options():
    # A well-formed question with malformed option entries keeps the question, drops options.
    context = {"client_context": ClientContext(problem_summary="P"), "spec_content": "S"}
    payload = (
        '{"questions": [{"id": "q1", "question_text": "Where?", "category": "tech",'
        ' "priority": "low", "options": ["bad", {"id": "ok", "label": "OK"}]}]}'
    )
    llm = make_llm(payload)
    ctx_update, _ = run_requirements(context, llm=llm)
    q = next(q for q in ctx_update["open_questions"] if q.id == "q1")
    assert [o.id for o in q.options] == ["ok"]  # non-dict option dropped, no crash


def test_run_discovery_multi_section_tolerates_malformed_fields():
    # Two sections; one has non-str scalars, a non-list field, and a non-str list item.
    # The reducer must skip the malformed bits without crashing.
    payloads = iter(
        [
            '{"problem_summary": "", "opportunity_statement": "",'
            ' "target_users": "notalist", "success_criteria": [123, "good"], "assumptions": []}',
            '{"problem_summary": "", "opportunity_statement": "",'
            ' "target_users": ["u1"], "success_criteria": ["good"], "assumptions": []}',
        ]
    )
    context = {"client_context": ClientContext(), "spec_content": multi_heading_doc(2, 5000)}
    llm = make_llm(lambda *a, **k: next(payloads), max_ctx=1000)
    ctx_update, _ = run_discovery(context, llm=llm)
    cc = ctx_update["client_context"]
    assert cc.problem_summary == ""  # no valid str scalar in either section
    assert cc.target_users == ["u1"]  # non-list field skipped
    assert cc.success_criteria == ["good"]  # non-str item skipped, deduped


def test_run_discovery_multi_section_coerces_numeric_scalar():
    # First section returns a numeric problem_summary; reduce coerces it to str rather
    # than discarding it (the other section has an empty summary).
    payloads = iter(
        [
            '{"problem_summary": 42, "opportunity_statement": "",'
            ' "target_users": [], "success_criteria": [], "assumptions": []}',
            '{"problem_summary": "", "opportunity_statement": "",'
            ' "target_users": ["u1"], "success_criteria": [], "assumptions": []}',
        ]
    )
    context = {"client_context": ClientContext(), "spec_content": multi_heading_doc(2, 5000)}
    llm = make_llm(lambda *a, **k: next(payloads), max_ctx=1000)
    ctx_update, _ = run_discovery(context, llm=llm)
    assert ctx_update["client_context"].problem_summary == "42"  # coerced, not discarded


def test_run_discovery_non_object_json_falls_back():
    # A top-level JSON array (not an object) must be treated as no result -> fallback.
    context = {"client_context": ClientContext(), "spec_content": "Material"}
    llm = make_llm('["not", "an", "object"]')
    ctx_update, _ = run_discovery(context, llm=llm)
    # Fallback uses the raw material as problem_summary; no crash.
    assert ctx_update["client_context"].problem_summary == "Material"


def test_run_requirements_dedupes_across_sections():
    # Force multi-section via a tiny context; both sections return the same question id.
    context = {
        "client_context": ClientContext(problem_summary="P"),
        "spec_content": multi_heading_doc(3, 5000),
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
        "spec_content": multi_heading_doc(2, 5000),
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
