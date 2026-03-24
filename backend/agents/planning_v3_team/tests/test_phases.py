"""Unit tests for Planning V3 phases (mocked LLM and adapters)."""

import sys
from pathlib import Path

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from planning_v3_team.models import ClientContext  # noqa: E402
from planning_v3_team.phases import run_intake, run_requirements, run_synthesis  # noqa: E402


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
    mock_llm = type("LLM", (), {})()
    mock_llm.complete_text = lambda prompt, temperature=0: (
        """{"questions": [
        {"id": "req_1", "question_text": "RPO/RTO?", "context": "...", "category": "business", "priority": "high",
         "options": [{"id": "opt_none", "label": "None", "is_default": true}]}
    ]}"""
    )
    ctx_update, artifacts = run_requirements(context, llm=mock_llm)
    assert "open_questions" in ctx_update
    assert len(ctx_update["open_questions"]) >= 1
    assert artifacts["open_questions"]
