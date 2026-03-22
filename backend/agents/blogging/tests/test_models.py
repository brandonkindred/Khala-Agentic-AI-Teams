from blog_research_agent.models import (
    ResearchAgentOutput,
    ResearchBriefInput,
    ResearchReference,
)


def test_research_brief_input_defaults() -> None:
    brief = ResearchBriefInput(brief="Test brief")
    assert brief.max_results == 10
    assert brief.per_query_limit == 8
    assert brief.recency_preference == "latest_12_months"


def test_research_agent_output_structure() -> None:
    ref = ResearchReference(
        title="Example",
        url="https://example.com",
        summary="Summary",
        key_points=["Point 1"],
    )
    output = ResearchAgentOutput(query_plan=[], references=[ref], notes=None)
    assert output.references[0].title == "Example"

