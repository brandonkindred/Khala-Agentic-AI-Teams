"""Tests for LLM request counting in blog agents."""

from llm_service import DummyLLMClient


def test_dummy_llm_tracks_request_count() -> None:
    llm = DummyLLMClient()

    assert llm.request_count == 0

    llm.complete_json('{"core_topics": true, "angle": true, "constraints": true}', objective="test")
    llm.complete_json('{"queries": [], "query_text": "x"}', objective="test")
    llm.complete_json('{"summary": "", "key_points": []}', objective="test")

    assert llm.request_count == 3
