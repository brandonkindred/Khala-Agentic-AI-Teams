"""Tests for Code Review agent."""

from unittest.mock import MagicMock

import pytest

from code_review_agent.agent import CodeReviewAgent
from code_review_agent.models import CodeReviewInput


def test_code_review_agent_truncates_long_code_before_llm_call():
    """When code exceeds model-based limit, agent truncates so request body stays under limit."""
    long_code = "x" * 250_000  # exceeds typical model context
    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.return_value = {
        "approved": True,
        "issues": [],
        "summary": "OK",
    }

    agent = CodeReviewAgent(llm_client=mock_llm)
    agent.run(
        CodeReviewInput(
            code=long_code,
            task_description="Test",
            language="typescript",
        )
    )

    # Uses coordinator when > 30K; may have multiple calls
    assert mock_llm.complete_json.call_count >= 1
    for call in mock_llm.complete_json.call_args_list:
        prompt = call[0][0]
        assert long_code not in prompt
        assert len(prompt) < 300_000  # truncated, not full 250K


def test_code_review_agent_small_code_uses_single_call():
    """When code fits in model context, agent uses single LLM call."""
    small_code = "### app/main.py ###\ndef foo(): pass"
    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.return_value = {
        "approved": True,
        "issues": [],
        "summary": "OK",
    }

    agent = CodeReviewAgent(llm_client=mock_llm)
    agent.run(
        CodeReviewInput(
            code=small_code,
            task_description="Test",
            language="python",
        )
    )

    mock_llm.complete_json.assert_called_once()
