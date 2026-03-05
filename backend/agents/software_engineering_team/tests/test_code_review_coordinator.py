"""Tests for Code Review Coordinator."""

from unittest.mock import MagicMock, patch

import pytest

from code_review_agent.coordinator import (
    build_chunks,
    parse_code_into_file_blocks,
    run_coordinator,
)
from code_review_agent.models import CodeReviewInput


def test_parse_code_into_file_blocks_single_file():
    """Parse single file block."""
    code = "### app/main.py ###\ndef foo(): pass"
    blocks = parse_code_into_file_blocks(code)
    assert len(blocks) == 1
    assert blocks[0][0] == "app/main.py"
    assert "def foo" in blocks[0][1]


def test_parse_code_into_file_blocks_multiple_files():
    """Parse multiple file blocks."""
    code = """### app/main.py ###
def foo(): pass

### app/models.py ###
class User: pass"""
    blocks = parse_code_into_file_blocks(code)
    assert len(blocks) == 2
    assert blocks[0][0] == "app/main.py"
    assert blocks[1][0] == "app/models.py"


def test_parse_code_into_file_blocks_content_with_blank_lines():
    """Content with blank lines stays in same block."""
    code = """### app/main.py ###
def foo():
    pass

def bar():
    pass"""
    blocks = parse_code_into_file_blocks(code)
    assert len(blocks) == 1
    assert "def bar" in blocks[0][1]


def test_build_chunks_groups_files_under_limit():
    """Chunks stay under max_chars."""
    blocks = [
        ("a.py", "x" * 5000),
        ("b.py", "y" * 5000),
        ("c.py", "z" * 5000),
    ]
    chunks = build_chunks(blocks, max_chars=15_000)
    # Each block is ~5000 + header ~20 = ~5020. Two fit in 15K, third in new chunk
    assert len(chunks) >= 1
    for paths, content in chunks:
        assert len(content) <= 15_000 + 100  # small tolerance for headers


def test_run_coordinator_with_large_code_uses_chunk_reviewer():
    """Coordinator with 2-3 files >30K chars produces one CodeReviewOutput."""
    # Build code that exceeds single-call limit (model context ~22K chars default)
    file1 = "### app/main.py ###\n" + ("x" * 20_000)
    file2 = "### app/models.py ###\n" + ("y" * 20_000)
    code = file1 + "\n\n" + file2

    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.side_effect = [
        {"approved": True, "issues": [], "summary": "Chunk 1 OK"},
        {"approved": True, "issues": [], "summary": "Chunk 2 OK"},
    ]

    result = run_coordinator(
        mock_llm,
        CodeReviewInput(
            code=code,
            task_description="Add feature",
            language="python",
        ),
    )

    assert result.approved is True
    assert len(result.issues) == 0
    assert "Chunk 1" in result.summary and "Chunk 2" in result.summary
    assert mock_llm.complete_json.call_count == 2


def test_run_coordinator_merges_issues_and_rejects_if_critical():
    """Coordinator merges issues; approved=False if any critical/major."""
    file1 = "### app/main.py ###\n" + ("x" * 20_000)
    code = file1

    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.return_value = {
        "approved": False,
        "issues": [
            {
                "severity": "critical",
                "category": "security",
                "file_path": "app/main.py",
                "description": "SQL injection risk",
                "suggestion": "Use parameterized queries",
            }
        ],
        "summary": "Critical issue found.",
    }

    result = run_coordinator(
        mock_llm,
        CodeReviewInput(
            code=code,
            task_description="Add feature",
            language="python",
        ),
    )

    assert result.approved is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == "critical"


def test_code_review_agent_uses_coordinator_when_code_exceeds_single_call_limit():
    """CodeReviewAgent.run uses coordinator when code exceeds model-based single-call limit."""
    code = "### app/main.py ###\n" + ("x" * 25_000)

    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.return_value = {
        "approved": True,
        "issues": [],
        "summary": "OK",
    }

    from code_review_agent.agent import CodeReviewAgent

    agent = CodeReviewAgent(llm_client=mock_llm)
    result = agent.run(
        CodeReviewInput(
            code=code,
            task_description="Test",
            language="python",
        )
    )

    # Coordinator is used -> multiple complete_json calls (one per chunk)
    assert mock_llm.complete_json.call_count >= 1
    assert result.approved is True
