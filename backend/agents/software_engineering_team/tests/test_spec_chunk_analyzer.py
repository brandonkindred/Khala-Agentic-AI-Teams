"""Tests for Spec Chunk Analyzer agent."""

from unittest.mock import MagicMock

import pytest
from planning_team.spec_chunk_analyzer import (
    SpecChunkAnalysis,
    SpecChunkAnalyzer,
    SpecChunkAnalyzerInput,
)


@pytest.fixture
def mock_llm() -> MagicMock:
    """LLM that returns valid spec chunk analysis JSON."""
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = 16384
    llm.complete_json.return_value = {
        "data_entities": [{"name": "User", "attributes": ["id", "email"]}],
        "api_endpoints": [
            {"method": "GET", "path": "/users", "description": "List users", "auth_required": True}
        ],
        "ui_screens": [],
        "user_flows": [],
        "non_functional": [],
        "infrastructure": [],
        "integrations": [],
        "total_deliverable_count": 2,
        "summary": "Chunk requires User entity and users API.",
    }
    return llm


def test_spec_chunk_analyzer_returns_valid_schema(mock_llm: MagicMock) -> None:
    """Spec Chunk Analyzer returns SpecChunkAnalysis with expected schema."""
    agent = SpecChunkAnalyzer(llm_client=mock_llm)
    chunk = "## Users\nUser entity with id and email. GET /api/users to list."
    result = agent.run(
        SpecChunkAnalyzerInput(
            spec_chunk=chunk,
            chunk_index=1,
            total_chunks=2,
            requirements_header={"title": "App", "description": "Test"},
        )
    )
    assert isinstance(result, SpecChunkAnalysis)
    assert len(result.data_entities) == 1
    assert result.data_entities[0]["name"] == "User"
    assert len(result.api_endpoints) == 1
    assert result.api_endpoints[0]["path"] == "/users"
    assert result.total_deliverable_count == 2
    assert result.summary


def test_spec_chunk_analyzer_prompt_contains_chunk_not_full_spec(mock_llm: MagicMock) -> None:
    """Prompt contains only the chunk, not a full large spec."""
    agent = SpecChunkAnalyzer(llm_client=mock_llm)
    chunk = "Small chunk content."
    full_spec_not_in_chunk = "x" * 50000
    agent.run(
        SpecChunkAnalyzerInput(
            spec_chunk=chunk,
            chunk_index=1,
            total_chunks=1,
            requirements_header={},
        )
    )
    call_args = mock_llm.complete_json.call_args
    prompt = call_args[0][0]
    assert chunk in prompt
    assert full_spec_not_in_chunk not in prompt
    assert "chunk 1 of 1" in prompt


def test_spec_chunk_analyzer_truncates_oversized_chunk(mock_llm: MagicMock) -> None:
    """Chunk exceeding model-based limit is truncated."""
    mock_llm.get_max_context_tokens.return_value = 8000  # small context -> ~7K chars max
    agent = SpecChunkAnalyzer(llm_client=mock_llm)
    oversized = "a" * 50_000  # exceeds any reasonable chunk limit
    agent.run(
        SpecChunkAnalyzerInput(
            spec_chunk=oversized,
            chunk_index=1,
            total_chunks=1,
            requirements_header={},
        )
    )
    call_args = mock_llm.complete_json.call_args
    prompt = call_args[0][0]
    assert "[truncated]" in prompt
