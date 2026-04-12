"""Tests for Code Review Coordinator.

Pure-function tests (``parse_code_into_file_blocks``, ``build_chunks``)
stay as they were — no LLM dependency. The LLM-integration tests use
``DummyLLMClient`` subclasses now that ``ChunkReviewAgent`` is
Strands-backed and bypasses ``llm.complete_json`` in favor of the
``chat_json_round`` + structured-output flow.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from code_review_agent.coordinator import (
    build_chunks,
    parse_code_into_file_blocks,
    run_coordinator,
)
from code_review_agent.models import CodeReviewInput, CodeReviewOutput

from llm_service.clients.dummy import DummyLLMClient

# ---------------------------------------------------------------------------
# Pure-function tests (unchanged from pre-Strands)
# ---------------------------------------------------------------------------


def test_parse_code_into_file_blocks_single_file() -> None:
    """Parse single file block."""
    code = "### app/main.py ###\ndef foo(): pass"
    blocks = parse_code_into_file_blocks(code)
    assert len(blocks) == 1
    assert blocks[0][0] == "app/main.py"
    assert "def foo" in blocks[0][1]


def test_parse_code_into_file_blocks_multiple_files() -> None:
    """Parse multiple file blocks."""
    code = """### app/main.py ###
def foo(): pass

### app/models.py ###
class User: pass"""
    blocks = parse_code_into_file_blocks(code)
    assert len(blocks) == 2
    assert blocks[0][0] == "app/main.py"
    assert blocks[1][0] == "app/models.py"


def test_parse_code_into_file_blocks_content_with_blank_lines() -> None:
    """Content with blank lines stays in same block."""
    code = """### app/main.py ###
def foo():
    pass

def bar():
    pass"""
    blocks = parse_code_into_file_blocks(code)
    assert len(blocks) == 1
    assert "def bar" in blocks[0][1]


def test_build_chunks_groups_files_under_limit() -> None:
    """Chunks stay under max_chars."""
    blocks = [
        ("a.py", "x" * 5000),
        ("b.py", "y" * 5000),
        ("c.py", "z" * 5000),
    ]
    chunks = build_chunks(blocks, max_chars=15_000)
    assert len(chunks) >= 1
    for _paths, content in chunks:
        assert len(content) <= 15_000 + 100  # small tolerance for headers


# ---------------------------------------------------------------------------
# run_coordinator — LLM-integration tests
# ---------------------------------------------------------------------------


class _ScriptedClient(DummyLLMClient):
    """Returns a different canned response on each ``complete_json`` call.

    Used to simulate the coordinator dispatching to multiple chunks and
    each chunk getting its own LLM response.
    """

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0

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
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        # After the scripted responses are exhausted, fall back to the last
        # one so additional chunks don't crash the test.
        return self._responses[-1] if self._responses else {}


def test_run_coordinator_with_multi_file_code_merges_chunk_summaries() -> None:
    """Multiple file blocks → multiple chunks → merged CodeReviewOutput."""
    file1 = "### app/main.py ###\n" + ("x" * 20_000)
    file2 = "### app/models.py ###\n" + ("y" * 20_000)
    code = file1 + "\n\n" + file2

    client = _ScriptedClient(
        [
            {"approved": True, "issues": [], "summary": "Chunk 1 OK"},
            {"approved": True, "issues": [], "summary": "Chunk 2 OK"},
        ]
    )

    result = run_coordinator(
        client,
        CodeReviewInput(
            code=code,
            task_description="Add feature",
            language="python",
        ),
    )

    assert isinstance(result, CodeReviewOutput)
    assert result.approved is True
    assert result.issues == []
    # Coordinator concatenates chunk summaries with blank lines between.
    assert "Chunk 1" in result.summary
    assert "Chunk 2" in result.summary


def test_run_coordinator_merges_issues_and_rejects_if_critical() -> None:
    """Coordinator merges issues across chunks; a single critical issue
    propagates to ``approved=False``."""
    file1 = "### app/main.py ###\n" + ("x" * 20_000)
    code = file1

    client = _ScriptedClient(
        [
            {
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
        ]
    )

    result = run_coordinator(
        client,
        CodeReviewInput(
            code=code,
            task_description="Add feature",
            language="python",
        ),
    )

    assert result.approved is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == "critical"
    assert result.issues[0].file_path == "app/main.py"


def test_code_review_agent_uses_coordinator_when_code_exceeds_limit() -> None:
    """End-to-end: ``CodeReviewAgent.run`` with code larger than the
    single-call limit dispatches to the coordinator and returns a
    merged CodeReviewOutput."""
    from code_review_agent.agent import CodeReviewAgent

    code = "### app/main.py ###\n" + ("x" * 25_000)

    agent = CodeReviewAgent(llm_client=DummyLLMClient())
    result = agent.run(
        CodeReviewInput(
            code=code,
            task_description="Test",
            language="python",
        )
    )

    assert isinstance(result, CodeReviewOutput)
    assert result.approved is True
