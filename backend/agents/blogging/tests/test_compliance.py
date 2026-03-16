"""Tests for the blog compliance agent."""

import pytest
from pathlib import Path
from blog_compliance_agent import BlogComplianceAgent
from llm_service import DummyLLMClient
from shared.brand_spec import load_brand_spec_prompt


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
    report = agent.run(draft, brand_spec_prompt, work_dir=tmp_path)
    assert (tmp_path / "compliance_report.json").exists()
