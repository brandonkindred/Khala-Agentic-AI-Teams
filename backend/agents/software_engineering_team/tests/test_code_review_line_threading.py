"""Tests for the additive per-issue ``line`` field on code-review findings.

Covers ``coerce_line``, the model round-trip, and that a line number provided by
the LLM is threaded through both the single-call agent path and the coordinator
(chunk) path. The line field powers inline PR review comments downstream.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from code_review_agent import CodeReviewAgent
from code_review_agent.models import CodeReviewInput, CodeReviewIssue, coerce_line

from llm_service.clients.dummy import DummyLLMClient


class _ScriptedClient(DummyLLMClient):
    """Returns a canned JSON response on each ``complete_json`` call."""

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
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return resp


# ---------------------------------------------------------------------------
# coerce_line
# ---------------------------------------------------------------------------


def test_coerce_line_valid_values() -> None:
    assert coerce_line(5) == 5
    assert coerce_line("12") == 12


def test_coerce_line_invalid_values_return_none() -> None:
    assert coerce_line(None) is None
    assert coerce_line("not-a-number") is None
    assert coerce_line(0) is None
    assert coerce_line(-3) is None


# ---------------------------------------------------------------------------
# Model round-trip
# ---------------------------------------------------------------------------


def test_issue_model_accepts_line_and_defaults_none() -> None:
    issue = CodeReviewIssue(description="x", line=7, start_line=3)
    assert issue.line == 7
    assert issue.start_line == 3
    assert CodeReviewIssue(description="y").line is None


# ---------------------------------------------------------------------------
# Single-call agent path threads line
# ---------------------------------------------------------------------------


def test_single_call_threads_line() -> None:
    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "line": 42,
                        "description": "off-by-one",
                        "suggestion": "use <= ",
                    }
                ],
                "summary": "needs work",
            }
        ]
    )
    agent = CodeReviewAgent(llm_client=client)
    result = agent.run(
        CodeReviewInput(code="### app/main.py ###\ndef f(): pass", language="python")
    )
    assert len(result.issues) == 1
    assert result.issues[0].line == 42


def test_single_call_bad_line_becomes_none() -> None:
    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "line": "bogus",
                        "description": "issue",
                        "suggestion": "fix",
                    }
                ],
                "summary": "needs work",
            }
        ]
    )
    agent = CodeReviewAgent(llm_client=client)
    result = agent.run(CodeReviewInput(code="### app/main.py ###\nx=1", language="python"))
    assert result.issues[0].line is None


# ---------------------------------------------------------------------------
# Coordinator (large-code) path threads line
# ---------------------------------------------------------------------------


def test_coordinator_threads_line() -> None:
    big = "### app/main.py ###\n" + ("x" * 25_000)
    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "critical",
                        "category": "security",
                        "file_path": "app/main.py",
                        "line": 13,
                        "description": "injection",
                        "suggestion": "parameterize",
                    }
                ],
                "summary": "bad",
            }
        ]
    )
    agent = CodeReviewAgent(llm_client=client)
    result = agent.run(CodeReviewInput(code=big, language="python"))
    assert len(result.issues) == 1
    assert result.issues[0].line == 13
