"""Tests for Spec Analysis Merger agent."""

from unittest.mock import MagicMock

import pytest
from planning_team.spec_analysis_merger import (
    MergedSpecAnalysis,
    SpecAnalysisMerger,
    SpecAnalysisMergerInput,
)


@pytest.fixture
def mock_llm() -> MagicMock:
    """LLM that returns merged spec analysis."""
    llm = MagicMock()
    llm.complete_json.return_value = {
        "data_entities": [
            {"name": "User", "attributes": ["id", "email"], "relationships": [], "validation_rules": []},
            {"name": "Task", "attributes": ["id", "title"], "relationships": [], "validation_rules": []},
        ],
        "api_endpoints": [
            {"method": "GET", "path": "/users", "description": "List users", "auth_required": True},
            {"method": "POST", "path": "/tasks", "description": "Create task", "auth_required": True},
        ],
        "ui_screens": [],
        "user_flows": [],
        "non_functional": [],
        "infrastructure": [],
        "integrations": [],
        "total_deliverable_count": 4,
        "summary": "Merged: User and Task entities, users and tasks APIs.",
    }
    return llm


def test_spec_analysis_merger_returns_merged_schema(mock_llm: MagicMock) -> None:
    """Spec Analysis Merger returns MergedSpecAnalysis with deduplicated content."""
    agent = SpecAnalysisMerger(llm_client=mock_llm)
    chunk_results = [
        {"data_entities": [{"name": "User"}], "api_endpoints": [], "ui_screens": [], "user_flows": [],
         "non_functional": [], "infrastructure": [], "integrations": [], "total_deliverable_count": 1, "summary": "Chunk 1"},
        {"data_entities": [{"name": "Task"}], "api_endpoints": [], "ui_screens": [], "user_flows": [],
         "non_functional": [], "infrastructure": [], "integrations": [], "total_deliverable_count": 1, "summary": "Chunk 2"},
    ]
    result = agent.run(SpecAnalysisMergerInput(chunk_results=chunk_results))
    assert isinstance(result, MergedSpecAnalysis)
    assert len(result.data_entities) == 2
    assert result.total_deliverable_count == 4
    assert result.summary


def test_spec_analysis_merger_single_chunk_passthrough(mock_llm: MagicMock) -> None:
    """Single chunk is returned without LLM call."""
    agent = SpecAnalysisMerger(llm_client=mock_llm)
    chunk_results = [
        {"data_entities": [{"name": "User"}], "api_endpoints": [], "ui_screens": [], "user_flows": [],
         "non_functional": [], "infrastructure": [], "integrations": [], "total_deliverable_count": 1, "summary": "Only chunk"},
    ]
    result = agent.run(SpecAnalysisMergerInput(chunk_results=chunk_results))
    mock_llm.complete_json.assert_not_called()
    assert result.data_entities[0]["name"] == "User"
    assert result.summary == "Only chunk"


def test_spec_analysis_merger_empty_returns_empty() -> None:
    """Empty chunk_results returns empty MergedSpecAnalysis."""
    agent = SpecAnalysisMerger(llm_client=MagicMock())
    result = agent.run(SpecAnalysisMergerInput(chunk_results=[]))
    assert isinstance(result, MergedSpecAnalysis)
    assert result.data_entities == []
    assert result.summary == ""
