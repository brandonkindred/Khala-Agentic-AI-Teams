"""Tests for Planning orchestrator."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))


def test_run_workflow_minimal_no_adapters(tmp_path):
    """Run workflow with use_product_analysis=False so adapters are not called."""
    from planning_team.orchestrator import run_workflow

    repo = str(tmp_path)
    job_updates = []

    def capture(**kwargs):
        job_updates.append(kwargs)

    result = run_workflow(
        repo_path=repo,
        initial_brief="Build a small app",
        use_product_analysis=False,
        use_market_research=False,
        llm=None,
        job_updater=capture,
    )
    assert "success" in result
    assert len(job_updates) >= 1
    assert any("intake" in str(u.get("current_phase", "")) for u in job_updates)


def test_run_workflow_with_llm_no_pra(tmp_path):
    """Run with a dummy LLM; PRA disabled so no HTTP calls."""
    from planning_team.orchestrator import run_workflow

    repo = str(tmp_path)
    mock_llm = MagicMock()
    # Required: the digestion path sizes sections from get_max_context_tokens (int math)
    # and may call complete() for the compaction fallback. Without these the budget math
    # raises TypeError, map_reduce swallows it to fallback, and the feature is untested.
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete.return_value = "CONDENSED"
    mock_llm.complete_text.return_value = '{"problem_summary": "Need X", "opportunity_statement": "Y", "target_users": ["u1"], "success_criteria": ["c1"], "assumptions": []}'
    # The mock is injected directly via run_workflow's `llm=` parameter (it forwards
    # llm to run_discovery/run_requirements); _get_llm is not involved on this path.
    result = run_workflow(
        repo_path=repo,
        initial_brief="App",
        use_product_analysis=False,
        llm=mock_llm,
        job_updater=None,
    )
    assert result.get("success") is True
    handoff = result.get("handoff_package")
    assert handoff is not None
    # Prove the digestion path actually ran under the mock (not the real client):
    # the mocked discovery output must surface in the handoff's client context.
    assert mock_llm.complete_text.called
    assert handoff["client_context"]["problem_summary"] == "Need X"


def test_run_workflow_open_questions_separate_from_empty_handoff_copy(tmp_path):
    """result['open_questions'] carries the actual discovery questions, even though
    handoff_package['open_questions'] is deliberately left empty (see the inline
    comment in run_workflow) so downstream SE gating on a non-empty handoff isn't
    tripped by every run."""
    from planning_team.orchestrator import run_workflow

    repo = str(tmp_path)
    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete.return_value = "CONDENSED"
    mock_llm.complete_text.return_value = (
        '{"problem_summary": "Need X", "opportunity_statement": "Y", '
        '"target_users": ["u1"], "success_criteria": ["c1"], "assumptions": [], '
        '"questions": [{"id": "q1", "question_text": "Scope?", '
        '"options": [{"id": "o1", "label": "A", "is_default": true}]}]}'
    )

    result = run_workflow(
        repo_path=repo,
        initial_brief="App",
        use_product_analysis=False,
        llm=mock_llm,
        job_updater=None,
    )

    assert result.get("success") is True
    assert result["handoff_package"]["open_questions"] == []
    assert result["handoff_package"]["resolved_questions"] == []
    # The actual question must be recoverable from the top-level result key,
    # as a plain JSON-safe dict (not an OpenQuestion model instance) — even
    # though the handoff's own copy stays empty on purpose.
    assert result["resolved_questions"] == []
    assert len(result["open_questions"]) == 1
    assert result["open_questions"][0]["question_text"] == "Scope?"
    assert isinstance(result["open_questions"][0], dict)


def test_get_llm_returns_llm_client(monkeypatch):
    """_get_llm must return whatever get_client yields (a real LLMClient), not a Strands Agent."""
    from planning_team.api import main as api_main

    sentinel = object()
    # _get_llm now imports get_client at module top, so patch the name in its module.
    monkeypatch.setattr(api_main, "get_client", lambda agent_key=None: sentinel)
    assert api_main._get_llm() is sentinel
