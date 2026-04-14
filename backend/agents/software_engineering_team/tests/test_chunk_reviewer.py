"""Tests for Code Review Chunk Reviewer (Strands-migrated).

Uses ``DummyLLMClient`` subclasses instead of ``MagicMock``: the Strands
adapter path doesn't call ``llm.complete_json`` directly (it goes through
``chat_json_round`` with a ``StructuredOutputTool``), so mock-based
assertions on ``complete_json.call_args`` are no longer meaningful.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from code_review_agent.chunk_reviewer import ChunkReviewAgent, review_chunk
from code_review_agent.models import ChunkReviewInput, ChunkReviewOutput

from llm_service.clients.dummy import DummyLLMClient


class _StubClient(DummyLLMClient):
    """DummyLLMClient subclass returning a canned CodeReview-shaped dict."""

    def __init__(self, canned: Dict[str, Any]) -> None:
        super().__init__()
        self._canned = canned

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return self._canned


def _chunk_input(**overrides: Any) -> ChunkReviewInput:
    base = {
        "code_chunk": "### app/main.py ###\ndef foo(): pass",
        "file_path_or_label": "app/main.py",
        "task_description": "Add endpoint",
        "task_requirements": "",
        "acceptance_criteria": [],
        "spec_excerpt": "",
        "architecture_overview": "",
        "existing_codebase_excerpt": None,
    }
    base.update(overrides)
    return ChunkReviewInput(**base)  # type: ignore[arg-type]


def test_review_chunk_legacy_wrapper_returns_dict_with_expected_keys() -> None:
    """Legacy ``review_chunk`` helper delegates to ChunkReviewAgent but
    still returns a plain dict for backward compat."""
    result = review_chunk(
        llm=DummyLLMClient(),
        code_chunk="### app/main.py ###\ndef foo(): pass",
        file_paths_label="app/main.py",
        task_description="Add endpoint",
        task_requirements="",
        acceptance_criteria=[],
        spec_excerpt="",
        architecture_overview="",
        existing_codebase_excerpt=None,
    )
    assert isinstance(result, dict)
    # Dummy stub returns approved=True with no issues.
    assert result["approved"] is True
    assert result["issues"] == []
    assert "summary" in result


def test_chunk_review_agent_run_returns_chunk_review_output() -> None:
    agent = ChunkReviewAgent(llm=DummyLLMClient())
    result = agent.run(_chunk_input())
    assert isinstance(result, ChunkReviewOutput)
    assert result.approved is True
    assert result.issues == []


def test_chunk_review_agent_carries_file_path_from_issue() -> None:
    """When the LLM sets a file_path on an issue, it flows through to the
    output unchanged."""
    agent = ChunkReviewAgent(
        llm=_StubClient(
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "critical",
                        "category": "security",
                        "file_path": "app/models.py",
                        "description": "Missing docstring on User",
                        "suggestion": "Add a docstring describing fields",
                    },
                ],
                "summary": "One issue found.",
            }
        )
    )
    result = agent.run(_chunk_input(file_path_or_label="app/models.py"))
    assert isinstance(result, ChunkReviewOutput)
    assert result.approved is False
    assert len(result.issues) == 1
    # ``ChunkReviewOutput.issues`` remains ``List[Dict[str, Any]]`` for
    # backward compat — callers can still index by key.
    assert result.issues[0]["file_path"] == "app/models.py"
    assert result.issues[0]["description"] == "Missing docstring on User"
    assert "One issue" in result.summary


def test_chunk_review_agent_falls_back_file_path_to_label_when_issue_omits_it() -> None:
    """If the LLM leaves an issue's file_path blank, we fill it from the
    chunk's ``file_path_or_label``."""
    agent = ChunkReviewAgent(
        llm=_StubClient(
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "naming",
                        "description": "Use snake_case",
                        "suggestion": "Rename to get_user",
                    },
                ],
                "summary": "Fix naming.",
            }
        )
    )
    result = agent.run(_chunk_input(file_path_or_label="app/main.py"))
    assert len(result.issues) == 1
    assert result.issues[0]["file_path"] == "app/main.py"
    assert result.issues[0]["severity"] == "high"
