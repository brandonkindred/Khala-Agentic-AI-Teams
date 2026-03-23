"""Tests for the blog fact-check agent."""

from unittest.mock import MagicMock

import pytest
from blog_fact_check_agent import BlogFactCheckAgent

from llm_service import DummyLLMClient
from llm_service.interface import LLMError as LLMServiceError
from llm_service.interface import LLMJsonParseError as LLMServiceJsonParseError


def test_fact_check_agent_run():
    """BlogFactCheckAgent returns a FactCheckReport with status fields."""
    llm = DummyLLMClient()
    agent = BlogFactCheckAgent(llm_client=llm)
    report = agent.run("This is a test draft about software engineering.")
    assert report.claims_status in ("PASS", "FAIL")
    assert report.risk_status in ("PASS", "FAIL")


def test_fact_check_json_parse_retry_then_success():
    """On first JSON parse failure, retries and succeeds on second attempt."""
    llm = MagicMock()
    llm.complete_json = MagicMock(
        side_effect=[
            LLMServiceJsonParseError("bad json"),
            {
                "claims_status": "PASS",
                "risk_status": "PASS",
                "claims_verified": [],
                "risk_flags": [],
                "required_disclaimers": [],
                "notes": "ok",
            },
        ]
    )
    agent = BlogFactCheckAgent(llm_client=llm)
    report = agent.run("Test draft.")
    assert report.claims_status == "PASS"
    assert llm.complete_json.call_count == 2


def test_fact_check_json_parse_all_retries_fail_returns_fallback(tmp_path):
    """When all JSON parse retries fail, returns a fallback FAIL report."""
    llm = MagicMock()
    llm.complete_json = MagicMock(side_effect=LLMServiceJsonParseError("bad json"))
    agent = BlogFactCheckAgent(llm_client=llm)
    report = agent.run("Test draft.", work_dir=tmp_path)
    assert report.claims_status == "FAIL"
    assert report.risk_status == "FAIL"
    assert any("Could not parse" in flag for flag in report.risk_flags)
    assert "Fallback" in (report.notes or "")
    assert (tmp_path / "fact_check_report.json").exists()


def test_fact_check_llm_service_error_propagates():
    """LLMServiceError from the LLM client propagates without wrapping."""
    llm = MagicMock()
    llm.complete_json = MagicMock(side_effect=LLMServiceError("rate limit"))
    agent = BlogFactCheckAgent(llm_client=llm)
    with pytest.raises(LLMServiceError, match="rate limit"):
        agent.run("Test draft.")
