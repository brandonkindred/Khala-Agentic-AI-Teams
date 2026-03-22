"""Tests for Planning V3 adapters (mocked httpx)."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))


def test_product_analysis_run_returns_job_id():
    from planning_v3_team.adapters.product_analysis import run_product_analysis
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"job_id": "pa-123"}
    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp
    with patch("planning_v3_team.adapters.product_analysis.httpx") as m:
        m.Client.return_value.__enter__.return_value = mock_client
        m.Client.return_value.__exit__.return_value = None
        with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
            out = run_product_analysis(repo_path="/tmp/repo", spec_content="spec")
    assert out == "pa-123"


def test_product_analysis_status():
    from planning_v3_team.adapters.product_analysis import get_product_analysis_status
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"job_id": "j1", "status": "running", "progress": 30}
    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp
    with patch("planning_v3_team.adapters.product_analysis.httpx") as m:
        m.Client.return_value.__enter__.return_value = mock_client
        m.Client.return_value.__exit__.return_value = None
        with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
            out = get_product_analysis_status("j1")
    assert out is not None
    assert out["status"] == "running"


def test_planning_v2_run_returns_job_id():
    from planning_v3_team.adapters.planning_v2 import run_planning_v2
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"job_id": "p2-456"}
    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp
    with patch("planning_v3_team.adapters.planning_v2.httpx") as m:
        m.Client.return_value.__enter__.return_value = mock_client
        m.Client.return_value.__exit__.return_value = None
        with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
            out = run_planning_v2(spec_content="spec", repo_path="/tmp/repo")
    assert out == "p2-456"


def test_market_research_returns_none_without_base_url():
    from planning_v3_team.adapters.market_research import request_market_research
    with patch.dict(os.environ, {}, clear=True):
        out = request_market_research(product_concept="X", target_users="Y", business_goal="Z")
    assert out is None


def test_market_research_to_evidence():
    from planning_v3_team.adapters.market_research import market_research_to_evidence
    data = {"mission_summary": "Summary", "insights": [], "market_signals": [{"signal": "S1"}]}
    ev = market_research_to_evidence(data)
    assert ev["summary"] == "Summary"
    assert ev["source"] == "market_research_team"
    assert "S1" in ev["market_signals"]


def test_ai_systems_start_build_returns_job_id():
    from planning_v3_team.adapters.ai_systems import start_ai_systems_build
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"job_id": "build-789"}
    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp
    with patch("planning_v3_team.adapters.ai_systems.httpx") as m:
        m.Client.return_value.__enter__.return_value = mock_client
        m.Client.return_value.__exit__.return_value = None
        with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
            out = start_ai_systems_build(project_name="p", spec_path="/tmp/spec.md")
    assert out == "build-789"
