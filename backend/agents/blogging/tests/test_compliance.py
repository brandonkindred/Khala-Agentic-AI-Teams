"""Tests for the blog compliance agent."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from blog_compliance_agent import BlogComplianceAgent
from shared.brand_spec import load_brand_spec_prompt

from llm_service import DummyLLMClient, LLMJsonParseError, LLMTemporaryError


@pytest.fixture
def brand_spec_prompt():
    """Load brand spec prompt from docs or use minimal fallback."""
    path = Path(__file__).resolve().parent.parent / "docs" / "brand_spec_prompt.md"
    if path.exists():
        return load_brand_spec_prompt(path)
    return "Brand: Test. Audience: Developers. Purpose: Clarity."


def test_compliance_agent_run(brand_spec_prompt):
    """BlogComplianceAgent returns a ComplianceReport with status."""
    llm = DummyLLMClient()
    agent = BlogComplianceAgent(llm_client=llm)
    draft = """# Hook

This is a good opening. It has two sentences at least.

# Explain the idea

We explain things clearly. Short sentences work best.

# Wrap up

We wrap up nicely. The end."""
    report = agent.run(draft, brand_spec_prompt)
    assert report.status in ("PASS", "FAIL")
    assert hasattr(report, "violations")
    assert hasattr(report, "required_fixes")


def test_compliance_agent_with_work_dir(brand_spec_prompt, tmp_path):
    """Compliance agent writes compliance_report.json when work_dir provided."""
    llm = DummyLLMClient()
    agent = BlogComplianceAgent(llm_client=llm)
    draft = "Short draft."
    agent.run(draft, brand_spec_prompt, work_dir=tmp_path)
    assert (tmp_path / "compliance_report.json").exists()


def test_compliance_fallback_when_llm_exhausts_transient_retries(
    monkeypatch, brand_spec_prompt, tmp_path
):
    """After transient LLM failures, agent returns FAIL fallback report and writes JSON (no crash)."""
    llm = MagicMock()
    llm.complete_json.side_effect = LLMTemporaryError(
        "Empty response from LLM; treating as transient for retry"
    )
    monkeypatch.setattr("blog_compliance_agent.agent.time.sleep", lambda _s: None)

    agent = BlogComplianceAgent(llm_client=llm)
    report = agent.run("# Draft\n\nHello.", brand_spec_prompt, work_dir=tmp_path)

    assert report.status == "FAIL"
    assert report.required_fixes
    assert report.notes and "tooling" in report.notes.lower()
    assert llm.complete_json.call_count == 3
    assert (tmp_path / "compliance_report.json").exists()


def test_compliance_json_parse_retry_then_success(brand_spec_prompt):
    """On first JSON parse failure, retries and succeeds on second attempt."""
    llm = MagicMock()
    llm.complete_json = MagicMock(
        side_effect=[
            LLMJsonParseError("bad json"),
            {"status": "PASS", "violations": [], "required_fixes": [], "notes": "ok"},
        ]
    )
    agent = BlogComplianceAgent(llm_client=llm)
    report = agent.run("Test draft.", brand_spec_prompt)
    assert report.status == "PASS"
    assert llm.complete_json.call_count == 2


def test_compliance_json_parse_all_retries_fail_returns_fallback(brand_spec_prompt, tmp_path):
    """When all JSON parse retries fail, returns a fallback FAIL report."""
    llm = MagicMock()
    llm.complete_json = MagicMock(side_effect=LLMJsonParseError("bad json"))
    agent = BlogComplianceAgent(llm_client=llm)
    report = agent.run("Test draft.", brand_spec_prompt, work_dir=tmp_path)
    assert report.status == "FAIL"
    assert report.required_fixes
    assert (tmp_path / "compliance_report.json").exists()
