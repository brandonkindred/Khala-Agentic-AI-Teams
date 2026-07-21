from unittest.mock import patch

from agents.blogging.blog_research_agent.agent import ResearchAgent
from agents.blogging.blog_research_agent.models import ResearchBriefInput

from .conftest import make_stub_fetcher_class, make_stub_llm_class, make_stub_search_class


def test_research_agent_run_end_to_end() -> None:
    llm = make_stub_llm_class()()
    agent = ResearchAgent(
        llm_client=llm,
        web_search=make_stub_search_class()(),
        web_fetcher=make_stub_fetcher_class()(),
    )

    # Avoid real arXiv HTTP calls in tests
    with patch("agents.blogging.blog_research_agent.agent.search_arxiv", return_value=[]):
        brief = ResearchBriefInput(
            brief="Test brief about a topic",
            audience="Testers",
            tone_or_purpose="educational",
            max_results=3,
        )

        result = agent.run(brief)

    assert result.references, "Expected at least one reference"
    ref = result.references[0]
    assert ref.summary.startswith("Test summary")
    assert ref.key_points
    assert result.query_plan, "Expected non-empty query plan"
    assert result.compiled_document, "Expected compiled document with links and summaries"
    assert "# Blog Post Research" in result.compiled_document
    assert "## Sources" in result.compiled_document
    assert "## Academic sources" in result.compiled_document
    assert "## Similar topics" in result.compiled_document
    assert "example.com" in result.compiled_document or "http" in result.compiled_document
    assert "-- " in result.compiled_document
    assert result.similar_topics or "Similar" in result.compiled_document
