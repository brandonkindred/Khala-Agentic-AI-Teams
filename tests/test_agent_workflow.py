from strands_research_agent.agent import ResearchAgent
from strands_research_agent.llm import LLMClient
from strands_research_agent.models import ResearchBriefInput, SearchQuery, CandidateResult, SourceDocument


class StubLLM(LLMClient):
    """Deterministic LLM stub for tests."""

    def complete_json(self, prompt: str, *, temperature: float = 0.0):
        lowered = prompt.lower()
        if "core_topics" in lowered and "angle" in lowered and "constraints" in lowered:
            return {
                "core_topics": ["test topic"],
                "angle": "overview",
                "constraints": [],
            }
        if '"queries"' in lowered and "query_text" in lowered:
            return {
                "queries": [
                    {"query_text": "test overview query", "intent": "overview"},
                    {"query_text": "test how-to query", "intent": "how-to"},
                ]
            }
        if "relevance_score" in lowered and "type" in lowered:
            return {
                "relevance_score": 0.9,
                "type": "guides",
                "tags": ["testing"],
            }
        if "summary:" in lowered and "key_points" in lowered:
            return {
                "summary": "Test summary.",
                "key_points": ["Key point A", "Key point B"],
                "is_promotional": False,
            }
        return {
            "analysis": "Test analysis.",
            "outline": ["Intro", "Body", "Conclusion"],
        }


class StubSearch:
    def search(self, query: SearchQuery, *, max_results: int, recency_preference=None):
        # Return a single deterministic candidate per query
        return [
            CandidateResult(
                title=f"{query.query_text} result",
                url="https://example.com",
                snippet="Example snippet",
                source="stub",
                rank=1,
            )
        ]


class StubFetcher:
    def fetch(self, url):
        return SourceDocument(
            url=url,
            title="Example title",
            content="Example content about the test topic.",
            publish_date=None,
            domain="example.com",
            language="en",
            metadata={},
        )


def test_research_agent_run_end_to_end() -> None:
    llm = StubLLM()
    agent = ResearchAgent(llm_client=llm, web_search=StubSearch(), web_fetcher=StubFetcher())

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

