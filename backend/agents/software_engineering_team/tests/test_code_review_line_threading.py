"""Tests for the additive per-issue ``line`` field on code-review findings.

Covers ``coerce_line``, the model round-trip, threading of LLM-provided line
numbers through small and multi-chunk reviews, re-anchoring of lines reported
inside split segments, and pre-numbered PR-diff passthrough. The line field
powers inline PR review comments downstream.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from code_review_agent import CodeReviewAgent
from code_review_agent.chunk_reviewer import CODE_TO_REVIEW_HEADER
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
                "spec_compliance_notes": "",
            }
        ]
    )
    code = "\n".join(f"x{i} = {i}" for i in range(50))
    agent = CodeReviewAgent(llm_client=client, force_in_process=True)
    result = agent.run(CodeReviewInput(files={"app/main.py": code}, language="python"))
    assert len(result.issues) == 1
    assert result.issues[0].line == 42


def test_single_call_bad_line_becomes_none() -> None:
    """A non-positive line number is schema-valid (an int) but semantically
    bad (``coerce_line`` treats it as absent) -- unlike a non-numeric string,
    which ``ChunkReviewIssueLLM.line: Optional[int]`` now rejects outright at
    the schema layer rather than letting it through for downstream coercion."""
    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "line": 0,
                        "description": "issue",
                        "suggestion": "fix",
                    }
                ],
                "summary": "needs work",
                "spec_compliance_notes": "",
            }
        ]
    )
    agent = CodeReviewAgent(llm_client=client, force_in_process=True)
    result = agent.run(CodeReviewInput(files={"app/main.py": "x=1"}, language="python"))
    assert result.issues[0].line is None


# ---------------------------------------------------------------------------
# Coordinator (large-code) path threads line
# ---------------------------------------------------------------------------


def test_coordinator_threads_line() -> None:
    big = "\n".join(f"x{i} = {i}" for i in range(100))
    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "critical",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "line": 13,
                        "description": "injection",
                        "suggestion": "parameterize",
                    }
                ],
                "summary": "bad",
                "spec_compliance_notes": "",
            }
        ]
    )
    agent = CodeReviewAgent(llm_client=client, force_in_process=True)
    result = agent.run(CodeReviewInput(files={"app/main.py": big}, language="python"))
    assert len(result.issues) == 1
    assert result.issues[0].line == 13


# ---------------------------------------------------------------------------
# Split-segment re-anchoring
# ---------------------------------------------------------------------------


def test_split_segments_cite_absolute_prefixed_lines() -> None:
    """Split segments are rendered with original line-number prefixes, so a
    reviewer cites absolute lines directly — no re-anchoring arithmetic.

    The expected lines are computed from the splitter's actual boundaries, not
    hardcoded: a scripted reviewer cites the first prefixed number visible in
    each chunk, which must come back verbatim as that segment's ``start_line``.
    """
    import re as _re

    from code_review_agent.coordinator import build_review_chunks, run_coordinator

    from software_engineering_team.shared.context_sizing import (
        compute_code_review_map_chunk_chars,
    )

    content = "\n".join(f"line {i:05d}".ljust(40, "x") for i in range(1, 1_001))  # ~41K chars

    class _CiteFirstPrefixed(DummyLLMClient):
        """Cites the first original-line prefix found in the chunk prompt."""

        def __init__(self) -> None:
            super().__init__()
            self._tls = threading.local()

        def complete(self, prompt: str, **kwargs: Any) -> str:
            if CODE_TO_REVIEW_HEADER in prompt:
                m = _re.search(r"^[+>]?[ ]*(\d+)[:|] line", prompt, _re.M)
                assert m is not None, "split segments must render prefixed lines"
                self._tls.cited = int(m.group(1))
            return super().complete(prompt, **kwargs)

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            cited = getattr(self._tls, "cited", None)
            if cited is None:
                return super().complete_json(prompt, **kwargs)
            self._tls.cited = None
            return {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "big.py",
                        "line": cited,
                        "start_line": cited,
                        "description": f"issue at original line {cited}",
                        "suggestion": "fix it",
                    }
                ],
                "summary": "per-chunk issue",
                "spec_compliance_notes": "",
            }

    client = _CiteFirstPrefixed()
    cap = compute_code_review_map_chunk_chars(client)
    chunks = build_review_chunks([("big.py", content)], cap)
    segments = [seg for chunk in chunks for seg in chunk.segments]
    assert len(segments) > 1, "test premise: the file must split"

    result = run_coordinator(client, CodeReviewInput(files={"big.py": content}, language="python"))

    expected_lines = sorted(seg.start_line for seg in segments)
    assert sorted(i.line for i in result.issues) == expected_lines
    assert sorted(i.start_line for i in result.issues) == expected_lines
    assert all(i.file_path == "big.py" for i in result.issues)


def test_pre_numbered_split_segment_keeps_cited_line() -> None:
    """PR-diff hunks carry original line numbers as prefixes (declared by the
    producer via ``pre_numbered=True``); a split must not shift the cited
    numbers (offset 0)."""
    from code_review_agent.coordinator import run_coordinator

    hunk = "\n".join(f"{4000 + i}: code_{i}()".ljust(45, " ") for i in range(1_000))  # ~46K
    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "src/feature.py",
                        "line": 4242,
                        "description": "off-by-one in loop bound",
                        "suggestion": "use <=",
                    }
                ],
                "summary": "found one",
                "spec_compliance_notes": "",
            }
        ]
    )

    result = run_coordinator(
        client,
        CodeReviewInput(files={"src/feature.py": hunk}, pre_numbered=True, language="python"),
    )

    # Same issue reported per chunk → deduped to one; the cited pre-numbered
    # line passes through unchanged.
    assert len(result.issues) == 1
    assert result.issues[0].line == 4242


def test_blank_issue_path_resolves_to_sole_segment_and_strips_lines_suffix() -> None:
    """An issue with a blank path in a single-segment chunk resolves to that
    segment's path (the coordinator owns this fallback; the chunk reviewer
    passes blank paths through untouched)."""
    from code_review_agent.coordinator import run_coordinator

    content = "\n".join(f"line {i:05d}".ljust(40, "x") for i in range(1, 1_001))
    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        # No file_path: the coordinator resolves it to the
                        # chunk's sole segment path.
                        "line": 2,
                        "description": "second visible line is wrong",
                        "suggestion": "fix",
                    }
                ],
                "summary": "per-chunk issue",
                "spec_compliance_notes": "",
            }
        ]
    )

    result = run_coordinator(client, CodeReviewInput(files={"big.py": content}, language="python"))

    assert result.issues, "issues must survive normalization"
    assert all(i.file_path == "big.py" for i in result.issues)
    # Re-anchoring still applies after the suffix strip: snippet line 2 of the
    # first segment is original line 2.
    assert min(i.line for i in result.issues) == 2
