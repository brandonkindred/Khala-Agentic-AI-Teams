"""Tests for LLM request counting in blog agents."""

from blog_research_agent.llm import DummyLLMClient


def test_dummy_llm_tracks_request_count() -> None:
    llm = DummyLLMClient()

    assert llm.request_count == 0

    llm.complete_json('{"core_topics": true, "angle": true, "constraints": true}')
    llm.complete_json('{"queries": [], "query_text": "x"}')
    llm.complete_json('{"summary": "", "key_points": []}')

    assert llm.request_count == 3
