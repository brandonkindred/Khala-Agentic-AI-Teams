"""Tests for the blog fact-check agent."""

from blog_fact_check_agent import BlogFactCheckAgent

from llm_service import DummyLLMClient


def test_fact_check_agent_run():
    """BlogFactCheckAgent returns a FactCheckReport with status fields."""
    llm = DummyLLMClient()
    agent = BlogFactCheckAgent(llm_client=llm)
    report = agent.run("This is a test draft about software engineering.")
    assert report.claims_status in ("PASS", "FAIL")
    assert report.risk_status in ("PASS", "FAIL")


def test_fact_check_report_has_required_fields():
    """Verify FactCheckReport has all expected fields."""
    llm = DummyLLMClient()
    agent = BlogFactCheckAgent(llm_client=llm)
    report = agent.run("Test draft about cloud computing and microservices.")
    assert hasattr(report, "claims_status")
    assert hasattr(report, "risk_status")
    assert hasattr(report, "claims_verified")
    assert hasattr(report, "risk_flags")


def test_fact_check_with_work_dir(tmp_path):
    """Fact-check agent writes report JSON when work_dir provided."""
    llm = DummyLLMClient()
    agent = BlogFactCheckAgent(llm_client=llm)
    report = agent.run("Test draft.", work_dir=tmp_path)
    assert report.claims_status in ("PASS", "FAIL")
    assert (tmp_path / "fact_check_report.json").exists()
