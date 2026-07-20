"""Tests for Planning adapters (mocked post_json/get_json)."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))


def test_product_analysis_run_returns_job_id():
    from planning_team.adapters.product_analysis import run_product_analysis

    with patch(
        "planning_team.adapters.product_analysis.post_json", return_value={"job_id": "pa-123"}
    ) as mock_post:
        with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
            out = run_product_analysis(repo_path="/tmp/repo", spec_content="spec")
    assert out == "pa-123"
    mock_post.assert_called_once()
    assert (
        mock_post.call_args.args[0] == "http://test/api/software-engineering/product-analysis/run"
    )


def test_product_analysis_status():
    from planning_team.adapters.product_analysis import get_product_analysis_status

    with patch(
        "planning_team.adapters.product_analysis.get_json",
        return_value={"job_id": "j1", "status": "running", "progress": 30},
    ):
        with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
            out = get_product_analysis_status("j1")
    assert out is not None
    assert out["status"] == "running"


def test_product_analysis_wait_invokes_answer_callback():
    from planning_team.adapters.product_analysis import wait_for_product_analysis_completion

    statuses = iter(
        [
            {"status": "running", "waiting_for_answers": True, "pending_questions": [{"id": "q1"}]},
            {"status": "completed"},
        ]
    )
    submitted = {}

    def _answer_callback(pending):
        assert pending == [{"id": "q1"}]
        return [{"question_id": "q1", "selected_option_id": "o1"}]

    def _submit(job_id, answers):
        submitted["job_id"] = job_id
        submitted["answers"] = answers
        return {"status": "running"}

    with patch(
        "planning_team.adapters.product_analysis.get_product_analysis_status",
        side_effect=lambda job_id: next(statuses),
    ):
        with patch(
            "planning_team.adapters.product_analysis.submit_product_analysis_answers",
            side_effect=_submit,
        ):
            with patch("shared.http.job_polling.time.sleep", return_value=None):
                out = wait_for_product_analysis_completion(
                    "pa-123", answer_callback=_answer_callback
                )

    assert out == {"status": "completed"}
    assert submitted == {
        "job_id": "pa-123",
        "answers": [{"question_id": "q1", "selected_option_id": "o1"}],
    }


def test_run_product_analysis_no_base_url():
    from planning_team.adapters.product_analysis import run_product_analysis

    with patch.dict(os.environ, {}, clear=True):
        out = run_product_analysis(repo_path="/tmp/repo")
    assert out is None


def test_product_analysis_status_no_base_url():
    from planning_team.adapters.product_analysis import get_product_analysis_status

    with patch.dict(os.environ, {}, clear=True):
        out = get_product_analysis_status("j1")
    assert out is None


def test_submit_product_analysis_answers_returns_updated_status():
    from planning_team.adapters.product_analysis import submit_product_analysis_answers

    with patch(
        "planning_team.adapters.product_analysis.post_json",
        return_value={"status": "running"},
    ) as mock_post:
        with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
            out = submit_product_analysis_answers(
                "j1", [{"question_id": "q1", "selected_option_id": "o1"}]
            )
    assert out == {"status": "running"}
    assert (
        mock_post.call_args.args[0]
        == "http://test/api/software-engineering/product-analysis/j1/answers"
    )
    assert mock_post.call_args.args[1] == {
        "answers": [{"question_id": "q1", "selected_option_id": "o1"}]
    }


def test_submit_product_analysis_answers_no_base_url():
    from planning_team.adapters.product_analysis import submit_product_analysis_answers

    with patch.dict(os.environ, {}, clear=True):
        out = submit_product_analysis_answers("j1", [])
    assert out is None


def test_market_research_returns_none_without_base_url():
    from planning_team.adapters.market_research import request_market_research

    with patch.dict(os.environ, {}, clear=True):
        out = request_market_research(product_concept="X", target_users="Y", business_goal="Z")
    assert out is None


def test_market_research_to_evidence():
    from planning_team.adapters.market_research import market_research_to_evidence

    data = {"mission_summary": "Summary", "insights": [], "market_signals": [{"signal": "S1"}]}
    ev = market_research_to_evidence(data)
    assert ev["summary"] == "Summary"
    assert ev["source"] == "market_research_team"
    assert "S1" in ev["market_signals"]


def test_market_research_to_evidence_includes_rationale_and_pain_points():
    from planning_team.adapters.market_research import market_research_to_evidence

    data = {
        "mission_summary": "Summary",
        "recommendation": {"rationale": ["Reason A", "Reason B"]},
        "insights": [{"pain_points": ["Pain 1"]}],
        "market_signals": [],
    }
    ev = market_research_to_evidence(data)
    assert ev["insights"] == ["Reason A", "Reason B", "Pain 1"]


def test_request_market_research_submit_fails():
    from planning_team.adapters.market_research import request_market_research

    with patch("planning_team.adapters.market_research.post_json", return_value=None):
        with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
            out = request_market_research(product_concept="X", target_users="Y", business_goal="Z")
    assert out is None


def test_request_market_research_no_job_id():
    from planning_team.adapters.market_research import request_market_research

    with patch("planning_team.adapters.market_research.post_json", return_value={"job_id": None}):
        with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
            out = request_market_research(product_concept="X", target_users="Y", business_goal="Z")
    assert out is None


def test_request_market_research_success():
    from planning_team.adapters.market_research import request_market_research

    with patch("planning_team.adapters.market_research.post_json", return_value={"job_id": "mr-1"}):
        with patch(
            "planning_team.adapters.market_research.poll_until_terminal",
            return_value={"status": "completed", "result": {"mission_summary": "S"}},
        ) as mock_poll:
            with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
                out = request_market_research(
                    product_concept="X", target_users="Y", business_goal="Z"
                )
    assert out == {"mission_summary": "S"}
    mock_poll.assert_called_once()


def test_request_market_research_non_completed_terminal_status():
    from planning_team.adapters.market_research import request_market_research

    with patch("planning_team.adapters.market_research.post_json", return_value={"job_id": "mr-1"}):
        with patch(
            "planning_team.adapters.market_research.poll_until_terminal",
            return_value={"status": "failed", "error": "boom"},
        ):
            with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
                out = request_market_research(
                    product_concept="X", target_users="Y", business_goal="Z"
                )
    assert out is None


def test_ai_systems_start_build_returns_job_id():
    from planning_team.adapters.ai_systems import start_ai_systems_build

    with patch("planning_team.adapters.ai_systems.post_json", return_value={"job_id": "build-789"}):
        with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
            out = start_ai_systems_build(project_name="p", spec_path="/tmp/spec.md")
    assert out == "build-789"


def test_ai_systems_start_build_passes_output_dir():
    from planning_team.adapters.ai_systems import start_ai_systems_build

    with patch(
        "planning_team.adapters.ai_systems.post_json", return_value={"job_id": "build-789"}
    ) as mock_post:
        with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
            out = start_ai_systems_build(
                project_name="p", spec_path="/tmp/spec.md", output_dir="/tmp/out"
            )
    assert out == "build-789"
    assert mock_post.call_args.args[1]["output_dir"] == "/tmp/out"


def test_ai_systems_start_build_no_base_url():
    from planning_team.adapters.ai_systems import start_ai_systems_build

    with patch.dict(os.environ, {}, clear=True):
        out = start_ai_systems_build(project_name="p", spec_path="/tmp/spec.md")
    assert out is None


def test_ai_systems_get_status_returns_dict():
    from planning_team.adapters.ai_systems import get_ai_systems_build_status

    with patch(
        "planning_team.adapters.ai_systems.get_json",
        return_value={"status": "running", "progress": 10},
    ):
        with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://test"}):
            out = get_ai_systems_build_status("build-789")
    assert out == {"status": "running", "progress": 10}


def test_ai_systems_get_status_no_base_url():
    from planning_team.adapters.ai_systems import get_ai_systems_build_status

    with patch.dict(os.environ, {}, clear=True):
        out = get_ai_systems_build_status("build-789")
    assert out is None


def test_ai_systems_wait_for_completion_delegates_to_poll():
    from planning_team.adapters.ai_systems import wait_for_ai_systems_build_completion

    with patch(
        "planning_team.adapters.ai_systems.get_ai_systems_build_status",
        return_value={"status": "completed", "blueprint": {"name": "agent-x"}},
    ):
        with patch("shared.http.job_polling.time.sleep", return_value=None):
            out = wait_for_ai_systems_build_completion("build-789")
    assert out == {"status": "completed", "blueprint": {"name": "agent-x"}}
