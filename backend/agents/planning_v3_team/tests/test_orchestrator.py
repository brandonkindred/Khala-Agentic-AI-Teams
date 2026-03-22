"""Tests for Planning V3 orchestrator."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))


def test_run_workflow_minimal_no_adapters(tmp_path):
    """Run workflow with use_product_analysis=False, use_planning_v2=False so adapters are not called."""
    from planning_v3_team.orchestrator import run_workflow
    repo = str(tmp_path)
    job_updates = []
    def capture(**kwargs):
        job_updates.append(kwargs)
    result = run_workflow(
        repo_path=repo,
        initial_brief="Build a small app",
        use_product_analysis=False,
        use_planning_v2=False,
        use_market_research=False,
        llm=None,
        job_updater=capture,
    )
    assert "success" in result
    assert len(job_updates) >= 1
    assert any("intake" in str(u.get("current_phase", "")) for u in job_updates)


def test_run_workflow_with_llm_no_pra(tmp_path):
    """Run with a dummy LLM; PRA and Planning V2 disabled so no HTTP calls."""
    from planning_v3_team.orchestrator import run_workflow
    repo = str(tmp_path)
    mock_llm = MagicMock()
    mock_llm.complete_text.return_value = '{"problem_summary": "Need X", "opportunity_statement": "Y", "target_users": ["u1"], "success_criteria": ["c1"], "assumptions": []}'
    result = run_workflow(
        repo_path=repo,
        initial_brief="App",
        use_product_analysis=False,
        use_planning_v2=False,
        llm=mock_llm,
        job_updater=None,
    )
    assert result.get("success") is True
    assert result.get("handoff_package") is not None
