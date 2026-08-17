"""Tests for the code-review false-positive verification pass.

The filter re-checks each genuine reviewer finding against the whole submission
(the chunk reviewer only saw a bounded slice) and drops the ones a full-codebase
read confirms are false positives. Its governing rule is fail-safe: a finding is
removed ONLY on an explicit, confident false-positive verdict; every ambiguous
case (no path, unknown path, unparsable verdict, verifier error, low confidence)
keeps the finding.

The LLM seam is exercised with ``DummyLLMClient`` subclasses (which implement the
Strands ``Model`` ABC), matching the chunk-reviewer/synthesis test style: the
stub's ``complete_json`` branches on the prompt so one client can serve both the
chunk review and the verification call in an end-to-end run.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

import pytest
from code_review_agent.coordinator import run_coordinator
from code_review_agent.false_positive_filter import (
    _MAX_DUPLICATE_TOOL_CALLS,
    _MAX_TOTAL_TOOL_CALLS,
    _READ_LINES_MAX_SPAN,
    DEFAULT_VERIFY_MAX_FINDINGS_PER_GROUP,
    DEFAULT_VERIFY_TIMEOUT_SECONDS,
    CodebaseIndex,
    _agent_read_the_cited_file,
    _build_group_prompt,
    _build_tools,
    _code_fence_for,
    _coerce_verdict,
    _parse_verdicts,
    _render_finding_block,
    _sanitize_finding_field,
    _strip_numbered_prefixes,
    _verify_max_findings_per_group,
    _verify_timeout_seconds,
    filter_false_positives,
)
from code_review_agent.models import CodeReviewInput, CodeReviewIssue

from llm_service.clients.dummy import DummyLLMClient

# --------------------------------------------------------------------------- helpers


def _issue(
    *,
    file_path: str = "app/main.py",
    line: Optional[int] = 1,
    severity: str = "high",
    description: str = "foo is never defined",
    category: str = "logic",
    suggestion: str = "define foo",
) -> CodeReviewIssue:
    """Build a ``CodeReviewIssue`` with test defaults; override any field by kwarg."""
    return CodeReviewIssue(
        severity=severity,
        category=category,
        file_path=file_path,
        line=line,
        description=description,
        suggestion=suggestion,
    )


def _input(files: Optional[Dict[str, str]] = None, **overrides: Any) -> CodeReviewInput:
    """Build a ``CodeReviewInput`` with a default one-file submission and overrides."""
    base: Dict[str, Any] = {
        "files": files if files is not None else {"app/main.py": "def bar():\n    return foo()\n"},
        "task_description": "wire up foo",
        "acceptance_criteria": ["foo works"],
    }
    base.update(overrides)
    return CodeReviewInput(**base)


_READ_FILE_CALL_RE = re.compile(r'read_file\("([^"]+)"\)')


def _first_user_text(messages: List[Any]) -> str:
    """Extract the text of the *first* user message in a Strands message list.

    Unlike ``DummyLLMClient``'s own ``_last_user_text`` (which returns the
    *latest* user turn -- the tool-result turn after a simulated tool call),
    this returns the original verification prompt so a stub can keep routing
    on it after the tool round-trip.
    """
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        parts = []
        for block in msg.get("content") or []:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _tool_use_stream_events(
    tool_use_id: str, name: str, tool_input: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Build the Strands stream-event sequence for one simulated tool-use turn."""
    return [
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolUse": {"toolUseId": tool_use_id, "name": name}},
            },
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": json.dumps(tool_input)}},
            },
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {
            "messageStop": {"stopReason": "tool_use"},
            "metadata": {
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "metrics": {"latencyMs": 1},
            },
        },
    ]


def _final_text_stream_events(text: str) -> List[Dict[str, Any]]:
    """Build the Strands stream-event sequence for one simulated final-text turn."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {
            "messageStop": {"stopReason": "end_turn"},
            "metadata": {
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "metrics": {"latencyMs": 1},
            },
        },
    ]


def _first_user_text_from_chat_messages(messages: List[Any]) -> str:
    """Extract the first user message text from OpenAI-style chat messages."""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    parts.append(str(block["text"]))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        return str(content or "")
    return ""


def _chat_tool_result_count(messages: List[Any]) -> int:
    """Count tool-result messages in OpenAI-style chat history."""
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool")


def _chat_return_tool_call(
    tool_use_id: str, name: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    return {
        "__tool_calls__": [
            {
                "id": tool_use_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ]
    }


def _is_fpf_reasoning_prompt(text: str) -> bool:
    """Stable anchor for false-positive verification reasoning user prompts."""
    return "findings to check for false positives" in text.lower()


def _any_tool_use_called(messages: List[Any]) -> bool:
    """Whether any assistant message in ``messages`` already contains a
    ``toolUse`` block.

    Used by test stubs that need to tell "first turn" (no tool call yet)
    apart from "post-tool-call turn" (answer with a verdict). A bare
    ``"toolUse" in str(messages)`` substring check is fragile -- it would
    misfire if that literal word ever appeared in ordinary prompt text (a
    finding description, task text) -- so this inspects the actual message
    structure instead.
    """
    return any(
        isinstance(message, dict)
        and any(
            isinstance(block, dict) and "toolUse" in block
            for block in (message.get("content") or [])
        )
        for message in messages
    )


class _SimulatesFileReadToolCall(DummyLLMClient):
    """``DummyLLMClient`` variant that issues one real, successful ``read_file``
    call for the CITED file before answering a false-positive-verification
    prompt, mirroring a well-behaved model. ``_build_group_prompt`` never
    inlines the cited file's content (only names it and directs the model to
    read it), and ``_verify_group`` discards any false-positive verdict from a
    run that never obtained that exact file's full content via a successful
    ``read_file`` call (see ``_agent_read_the_cited_file``) -- stubs that want
    a drop honored must actually exercise that tool-call turn instead of
    answering on the first turn, exactly as this mixin does (it extracts the
    cited path from the prompt's ``read_file("...")`` directive, so it always
    targets the right file). Subclasses still customize the final verdict via
    ``complete_json`` on call 2 (format pass), which still branches on a
    ``verdicts`` anchor in the format prompt.
    """

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        objective: str = "dummy",
        response_format: str = "json",
        temperature: float = 0.2,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        has_tool_result = any(isinstance(m, dict) and m.get("role") == "tool" for m in messages)
        has_read_file_tool = any(
            (t or {}).get("function", {}).get("name") == "read_file" for t in (tools or [])
        )
        first_text = _first_user_text_from_chat_messages(messages)
        if has_read_file_tool and _is_fpf_reasoning_prompt(first_text):
            if not has_tool_result:
                match = _READ_FILE_CALL_RE.search(first_text)
                path = match.group(1) if match else "unknown.py"
                return {
                    "__tool_calls__": [
                        {
                            "id": "sim_read_file",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": {"path": path}},
                        }
                    ]
                }
            self._request_count += 1
            match = _READ_FILE_CALL_RE.search(first_text)
            path = match.group(1) if match else "unknown.py"
            prose = (
                f"Verified findings for {path}: "
                "Finding 0: is_real_issue=true, confidence=high — inspected cited file."
            )
            if response_format == "text":
                return prose
            return {"output": prose}
        return super().chat(
            messages,
            objective=objective,
            response_format=response_format,
            temperature=temperature,
            tools=tools,
            think=think,
            max_tokens=max_tokens,
            **kwargs,
        )

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):  # type: ignore[override]
        already_called = _any_tool_use_called(messages)
        has_read_file_tool = any(
            isinstance(spec, dict) and spec.get("name") == "read_file"
            for spec in (tool_specs or [])
        )
        first_text = _first_user_text(messages)
        if has_read_file_tool and _is_fpf_reasoning_prompt(first_text):
            if not already_called:
                match = _READ_FILE_CALL_RE.search(first_text)
                path = match.group(1) if match else "unknown.py"
                for event in _tool_use_stream_events("sim_read_file", "read_file", {"path": path}):
                    yield event
                return
            self._request_count += 1
            match = _READ_FILE_CALL_RE.search(first_text)
            path = match.group(1) if match else "unknown.py"
            prose = (
                f"Verified findings for {path}: "
                "Finding 0: is_real_issue=true, confidence=high — inspected cited file."
            )
            for event in _final_text_stream_events(prose):
                yield event
            return
        async for event in super().stream(
            messages, tool_specs=tool_specs, system_prompt=system_prompt, **kwargs
        ):
            yield event


class _VerdictStub(_SimulatesFileReadToolCall):
    """Returns canned verdicts for the verification call.

    Optionally serves a configured chunk-review response when ``chunk_issues`` is
    supplied; otherwise delegates to the base client (which rejects) for
    chunk-review prompts. ``complete_json`` branches on the prompt: the
    verification user prompt contains the anchor "verdicts" (the contract asks
    for a ``verdicts`` array), so the stub can serve both the chunk reviewer and
    the verifier from one injected client in an end-to-end coordinator run.
    """

    def __init__(
        self,
        verdicts: List[Dict[str, Any]],
        chunk_issues: Optional[List[Dict[str, Any]]] = None,
    ):
        super().__init__()
        self._verdicts = verdicts
        self._chunk_issues = chunk_issues

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        if "verdicts" in prompt.lower():
            return {"verdicts": self._verdicts}
        if self._chunk_issues is not None:
            return {
                "approved": False,
                "issues": self._chunk_issues,
                "summary": "Found issues (stub).",
                "spec_compliance_notes": "",
                "suggested_commit_message": "",
            }
        return super().complete_json(prompt, **kwargs)


class _RaisingStub(DummyLLMClient):
    """Raises on the verification call to exercise the fail-safe keep path."""

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        if "verdicts" in prompt.lower():
            raise RuntimeError("verifier exploded")
        return super().complete_json(prompt, **kwargs)


class _BadJsonStub(DummyLLMClient):
    """Returns a non-JSON string from complete_json on the verification call
    (unparsable → keep)."""

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:  # type: ignore[override]
        if "verdicts" in prompt.lower():
            return "not a json object"
        return super().complete_json(prompt, **kwargs)


class _FencedJsonVerdictStub(_SimulatesFileReadToolCall):
    """Returns verdicts from the format pass ``complete_json`` call."""

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:  # type: ignore[override]
        if "verdicts" in prompt.lower():
            return {
                "verdicts": [
                    {
                        "index": 0,
                        "is_real_issue": False,
                        "confidence": "high",
                        "reasoning": "foo is defined in util.py",
                    }
                ]
            }
        return super().complete_json(prompt, **kwargs)


@pytest.fixture(autouse=True)
def _enable_filter(monkeypatch):
    """The filter is default-on; make tests independent of the ambient env."""
    monkeypatch.delenv("CODE_REVIEW_FALSE_POSITIVE_FILTER", raising=False)
    yield


# --------------------------------------------------------------------------- CodebaseIndex


def test_index_from_files_keeps_whitespace_only() -> None:
    """``from_input`` keeps whitespace-only files; only None/empty-string content is dropped."""
    idx = CodebaseIndex.from_input(
        _input(files={"a.py": "x = 1\n", "b.py": "   ", "c.py": "", "d.py": "\n"})
    )
    assert set(idx.files) == {"a.py", "b.py", "d.py"}


def test_index_from_input_overlays_full_content_when_it_covers_every_path() -> None:
    """A ``pre_numbered`` submission whose ``full_content`` covers EVERY path
    the index would otherwise hold has it overlaid, and the index reports
    ``full_content_complete=True`` -- so whole-codebase passes reading via the
    index see complete content instead of the bounded pre-numbered excerpt."""
    full = "def bar():\n    return foo()\n\ndef extra():\n    pass\n"
    idx = CodebaseIndex.from_input(
        CodeReviewInput(
            files={"app/main.py": "1: def bar():\n2:     return foo()\n"},
            pre_numbered=True,
            full_content={"app/main.py": full},
            task_description="t",
        )
    )
    assert idx.files["app/main.py"] == full
    assert idx.full_content_complete is True


def test_index_from_input_ignores_full_content_paths_outside_the_submission() -> None:
    """``full_content`` covering every submission path PLUS an extra, unrelated
    path (e.g. a caller that scoped it too broadly) must not pull that extra
    path into the index -- a whole-codebase pass reading ``index.files`` would
    otherwise treat it as part of this submission's changed-file set even
    though the submission itself never included it."""
    full = "def bar():\n    return foo()\n\ndef extra():\n    pass\n"
    idx = CodebaseIndex.from_input(
        CodeReviewInput(
            files={"app/main.py": "1: def bar():\n2:     return foo()\n"},
            pre_numbered=True,
            full_content={
                "app/main.py": full,
                "app/unrelated.py": "def untouched(): pass\n",
            },
            task_description="t",
        )
    )
    assert idx.files["app/main.py"] == full
    assert "app/unrelated.py" not in idx.files
    assert idx.full_content_complete is True


def test_index_from_input_does_not_overlay_partial_full_content() -> None:
    """A ``full_content`` that covers only SOME of the submission's paths is not
    applied at all (all-or-nothing) -- overlaying just the covered subset would
    leave the rest as bounded ``N: ``-prefixed excerpts sitting alongside full
    bodies, with no way for a downstream pass to tell them apart. Both paths
    keep their original (pre-numbered) content, and the index reports
    ``full_content_complete=False``."""
    idx = CodebaseIndex.from_input(
        CodeReviewInput(
            files={
                "app/main.py": "1: def bar():\n2:     return foo()",
                "app/util.py": "1: def foo():\n2:     return 1",
            },
            pre_numbered=True,
            # Covers only app/main.py, not app/util.py.
            full_content={"app/main.py": "def bar():\n    return foo()\n"},
            task_description="t",
        )
    )
    assert idx.files["app/main.py"] == "1: def bar():\n2:     return foo()"
    assert idx.files["app/util.py"] == "1: def foo():\n2:     return 1"
    assert idx.full_content_complete is False


def test_index_from_input_ignores_full_content_when_not_pre_numbered() -> None:
    """``full_content`` is documented as a no-op unless ``pre_numbered=True`` --
    a ``files=`` submission (never pre-numbered by construction) is unaffected."""
    idx = CodebaseIndex.from_input(
        CodeReviewInput(
            files={"app/main.py": "def bar(): pass\n"},
            full_content={"app/main.py": "SHOULD NOT APPEAR"},
            task_description="t",
        )
    )
    assert idx.files["app/main.py"] == "def bar(): pass\n"
    assert idx.full_content_complete is False


def test_verdict_invariant_rejects_low_confidence_false_positive() -> None:
    """``_Verdict`` rejects is_false_positive=True without high/medium confidence."""
    from code_review_agent.false_positive_filter import _Verdict

    with pytest.raises(ValueError):
        _Verdict(is_false_positive=True, confidence="low")
    with pytest.raises(ValueError):
        _Verdict(is_false_positive=True, confidence="")
    ok = _Verdict(is_false_positive=True, confidence="high")
    assert ok.is_false_positive is True


def test_codebase_index_is_frozen_and_isolates_files_dict() -> None:
    """Frozen index isolates its files map from the caller's dict and rejects reassignment."""
    src = {"a.py": "x"}
    idx = CodebaseIndex(files=src)
    src["a.py"] = "mutated"
    assert idx.files["a.py"] == "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        idx.files = {}  # type: ignore[misc]


def test_read_file_exact_and_existing_codebase() -> None:
    """``read_file`` returns a body by exact path and the excerpt by its pseudo-path."""
    idx = CodebaseIndex(files={"app/main.py": "BODY"}, existing_codebase="OLD CODE")
    assert idx.read_file("app/main.py") == "BODY"
    assert idx.read_file(CodebaseIndex.EXISTING_CODEBASE_PATH) == "OLD CODE"


def test_read_file_blank_and_missing() -> None:
    """``read_file`` returns an error string for blank, missing, and excerpt-less pseudo-paths."""
    idx = CodebaseIndex(files={"app/main.py": "BODY"})
    assert idx.read_file("  ").startswith("Error")
    assert "not found" in idx.read_file("does/not/exist.py")
    # Existing-codebase pseudo-path with no excerpt is an error, not an empty hit.
    assert idx.read_file(CodebaseIndex.EXISTING_CODEBASE_PATH).startswith("Error")


def test_existing_codebase_pseudo_path_ignores_repo_reader_when_no_excerpt() -> None:
    """The existing-codebase pseudo-path never consults the repo reader when the excerpt is missing."""
    reader = _FakeReader({CodebaseIndex.EXISTING_CODEBASE_PATH: "REPO_CONTENT"})
    idx = CodebaseIndex(
        files={"app/main.py": "BODY"},
        existing_codebase="",
        repo_reader=reader,  # type: ignore[arg-type]
    )
    msg = idx.read_file(CodebaseIndex.EXISTING_CODEBASE_PATH)
    assert msg.startswith("Error")
    assert "no existing-codebase excerpt" in msg
    assert "REPO_CONTENT" not in msg
    # resolve_path must also return None — never fall through to the repo reader
    assert idx.resolve_path(CodebaseIndex.EXISTING_CODEBASE_PATH) is None


def test_read_file_unique_suffix_match() -> None:
    """``read_file`` resolves a bare or ``./``-prefixed name to a uniquely matching suffix path."""
    idx = CodebaseIndex(files={"app/services/main.py": "BODY"})
    assert idx.read_file("main.py") == "BODY"
    assert idx.read_file("./main.py") == "BODY"


def test_read_file_ambiguous_suffix() -> None:
    """``read_file`` reports ambiguity (naming both candidates) when a suffix matches multiple files."""
    idx = CodebaseIndex(files={"a/main.py": "A", "b/main.py": "B"})
    msg = idx.read_file("main.py")
    assert "ambiguous" in msg
    assert "a/main.py" in msg and "b/main.py" in msg


def test_read_file_or_none_matches_read_file_on_success() -> None:
    """``read_file_or_none`` returns the same content as ``read_file`` on every hit path."""
    idx = CodebaseIndex(files={"app/services/main.py": "Error: this is real code, not a failure"})
    # A real file whose content happens to start with "Error:" is still returned in full —
    # unlike sniffing read_file's return value, read_file_or_none never mistakes it for a
    # failure sentinel.
    assert (
        idx.read_file_or_none("app/services/main.py") == "Error: this is real code, not a failure"
    )
    assert idx.read_file_or_none("main.py") == "Error: this is real code, not a failure"


def test_read_file_or_none_returns_none_on_failure() -> None:
    """``read_file_or_none`` returns None (not an error string) for blank, missing, and ambiguous paths."""
    idx = CodebaseIndex(files={"a/main.py": "A", "b/main.py": "B"})
    assert idx.read_file_or_none("  ") is None
    assert idx.read_file_or_none("does/not/exist.py") is None
    assert idx.read_file_or_none("main.py") is None  # ambiguous suffix
    assert idx.read_file_or_none(CodebaseIndex.EXISTING_CODEBASE_PATH) is None  # no excerpt


def test_read_lines_returns_inclusive_numbered_slice() -> None:
    """Valid range returns header + numbered body for only the requested lines."""
    idx = CodebaseIndex(files={"app/main.py": "a\nb\nc\nd\ne\n"})
    result = idx.read_lines("app/main.py", 2, 4)
    assert result.startswith("app/main.py lines 2–4 (3 lines):")
    assert "2| b" in result
    assert "3| c" in result
    assert "4| d" in result
    assert "1| a" not in result
    assert "5| e" not in result


def test_read_lines_inverted_range_errors() -> None:
    """start > end returns an explicit inverted-range error."""
    idx = CodebaseIndex(files={"app/main.py": "a\nb\nc\n"})
    msg = idx.read_lines("app/main.py", 3, 1)
    assert msg.startswith("Error:")
    assert "invalid range" in msg
    assert "start (3) > end (1)" in msg


def test_read_lines_oversize_span_errors() -> None:
    """Span larger than _READ_LINES_MAX_SPAN returns an explicit oversize error."""
    body = "\n".join(f"line-{i}" for i in range(1, 500)) + "\n"
    idx = CodebaseIndex(files={"big.py": body})
    span = _READ_LINES_MAX_SPAN + 1
    msg = idx.read_lines("big.py", 1, span)
    assert msg.startswith("Error:")
    assert f"range spans {span} lines" in msg
    assert f"maximum is {_READ_LINES_MAX_SPAN}" in msg


def test_read_lines_clamps_end_past_eof() -> None:
    """end past EOF clamps to the last line when start is in range."""
    idx = CodebaseIndex(files={"app/main.py": "a\nb\nc\n"})
    result = idx.read_lines("app/main.py", 2, 99)
    assert result.startswith("app/main.py lines 2–3 (2 lines):")
    assert "2| b" in result
    assert "3| c" in result


def test_read_lines_start_past_eof_errors() -> None:
    """start beyond file length returns an explicit beyond-EOF error."""
    idx = CodebaseIndex(files={"app/main.py": "a\nb\n"})
    msg = idx.read_lines("app/main.py", 5, 6)
    assert msg.startswith("Error:")
    assert "beyond the end" in msg
    assert "file has 2 lines" in msg


def test_read_lines_rejects_non_positive_bounds() -> None:
    """Non-positive or non-int start/end return Error strings (never raise)."""
    idx = CodebaseIndex(files={"app/main.py": "a\n"})
    assert "positive integer" in idx.read_lines("app/main.py", 0, 1)
    assert "positive integer" in idx.read_lines("app/main.py", 1, True)  # type: ignore[arg-type]


def test_read_lines_pre_numbered_single_hunk_header_matches_body() -> None:
    """Pre-numbered single-hunk excerpt: header's claimed range must match the
    body's own embedded original line numbers (the exact bug-report fixture)."""
    content = "100: def earlier():\n101:     pass\n102: \n"
    idx = CodebaseIndex(files={"app/main.py": content})
    result = idx.read_lines("app/main.py", 100, 102)
    assert result.startswith("app/main.py lines 100–102 (3 lines):")
    assert "100| def earlier():" in result
    assert "101|     pass" in result
    assert "102| " in result
    # No physical/stripped line numbers (1-3) leak into the header or body.
    assert "lines 1–3" not in result
    assert "1| def earlier():" not in result


def test_read_lines_pre_numbered_start_outside_coverage_errors() -> None:
    """A start outside the excerpt's real coverage errors instead of returning
    a self-contradictory header (the literal read_lines(path, 1, 3) repro)."""
    content = "100: def earlier():\n101:     pass\n102: \n"
    idx = CodebaseIndex(files={"app/main.py": content})
    msg = idx.read_lines("app/main.py", 1, 3)
    assert msg.startswith("Error:")
    assert "start line 1" in msg
    assert "100-102" in msg
    assert "lines 1-3" not in msg  # no self-contradictory header claiming 1-3
    assert "lines 1–3" not in msg


def test_read_lines_pre_numbered_cross_hunk_gap_errors() -> None:
    """A start/end pair spanning two non-contiguous hunk segments errors,
    naming both segments' real coverage, and never leaks the gap marker or
    the unrelated hunk's content."""
    content = "100: def earlier():\n101:     pass\n...\n200: def later():\n201:     pass\n"
    idx = CodebaseIndex(files={"app/main.py": content})
    msg = idx.read_lines("app/main.py", 100, 201)
    assert msg.startswith("Error:")
    assert "100-101" in msg
    assert "200-201" in msg
    assert "..." not in msg
    assert "def later" not in msg


def test_read_lines_pre_numbered_clamps_end_past_hunk() -> None:
    """end far beyond a pre-numbered hunk's last real line clamps to that
    last line, mirroring the plain-content 'end past EOF clamps' behavior."""
    content = "100: def earlier():\n101:     pass\n102: \n"
    idx = CodebaseIndex(files={"app/main.py": content})
    result = idx.read_lines("app/main.py", 100, 199)
    assert result.startswith("app/main.py lines 100–102 (3 lines):")
    assert "100| def earlier():" in result
    assert "102| " in result


def test_read_lines_pre_numbered_missing_line_falls_back() -> None:
    """A start/end citing a line absent from the excerpt (e.g. a removed diff
    line) falls back to the nearest preceding available line."""
    content = "100: def earlier():\n101:     pass\n103:     return None\n"
    idx = CodebaseIndex(files={"app/main.py": content})
    result = idx.read_lines("app/main.py", 102, 103)
    assert not result.startswith("Error:")
    assert result.startswith("app/main.py lines 101–103 (2 lines):")
    assert "101|     pass" in result
    assert "103|     return None" in result


def test_read_function_returns_method_in_class_body() -> None:
    """Line inside a method returns only that method's construct body."""
    src = "class C:\n    def m(self):\n        return 1\n\ndef other():\n    return 2\n"
    idx = CodebaseIndex(files={"app/mod.py": src})
    # Line 3 is inside C.m
    result = idx.read_function("app/mod.py", 3)
    assert result.startswith("app/mod.py function C.m lines 2–3 (2 lines):")
    assert "2|     def m(self):" in result
    assert "3|         return 1" in result
    assert "class C" not in result.split("\n", 1)[1]  # body excludes class header
    assert "def other" not in result


def test_read_function_unresolved_module_level_errors() -> None:
    """Module-level line with no enclosing construct returns a clear error."""
    idx = CodebaseIndex(files={"app/mod.py": "x = 1\n\ndef f():\n    return x\n"})
    msg = idx.read_function("app/mod.py", 1)
    assert msg.startswith("Error:")
    assert "no enclosing function/class" in msg
    assert "line 1" in msg


def test_read_function_non_python_errors() -> None:
    """Non-Python paths return a clear Python-only error."""
    idx = CodebaseIndex(files={"app/main.ts": "function f() { return 1; }\n"})
    msg = idx.read_function("app/main.ts", 1)
    assert msg.startswith("Error:")
    assert "Python file" in msg
    assert "app/main.ts" in msg


def test_read_function_rejects_non_positive_line() -> None:
    """Non-positive or non-int line returns Error (never raises)."""
    idx = CodebaseIndex(files={"app/mod.py": "def f():\n    return 1\n"})
    assert "positive integer" in idx.read_function("app/mod.py", 0)
    assert "positive integer" in idx.read_function("app/mod.py", True)  # type: ignore[arg-type]


def test_read_function_by_name_unique_match() -> None:
    src = "class C:\n    def m(self):\n        return 1\n\ndef other():\n    return 2\n"
    idx = CodebaseIndex(files={"app/mod.py": src})
    by_name = idx.read_function_by_name("app/mod.py", "C.m")
    by_line = idx.read_function("app/mod.py", 3)
    assert by_name == by_line
    assert by_name.startswith("app/mod.py function C.m lines 2–3 (2 lines):")


def test_read_function_by_name_missing_errors() -> None:
    idx = CodebaseIndex(files={"app/mod.py": "def f():\n    return 1\n"})
    msg = idx.read_function_by_name("app/mod.py", "missing")
    assert msg.startswith("Error:")
    assert "no function/class named 'missing'" in msg


def test_read_function_by_name_ambiguous_errors() -> None:
    """Two same-named top-level defs in one AST file → ambiguous exact match."""
    src = "def twin():\n    return 1\n\ndef twin():\n    return 2\n"
    idx = CodebaseIndex(files={"app/mod.py": src})
    msg = idx.read_function_by_name("app/mod.py", "twin")
    assert msg.startswith("Error:")
    assert "ambiguous" in msg
    assert "twin" in msg
    assert "line number" in msg


def test_read_function_by_name_property_setter_not_ambiguous() -> None:
    """Property getter and setter share bare name but are distinct lookup keys."""
    src = (
        "class C:\n"
        "    @property\n"
        "    def x(self):\n"
        "        return self._x\n"
        "\n"
        "    @x.setter\n"
        "    def x(self, value):\n"
        "        self._x = value\n"
    )
    idx = CodebaseIndex(files={"app/mod.py": src})
    getter = idx.read_function_by_name("app/mod.py", "C.x")
    setter = idx.read_function_by_name("app/mod.py", "C.x.setter")
    assert getter.startswith("app/mod.py function C.x lines")
    assert setter.startswith("app/mod.py function C.x.setter lines")
    assert "ambiguous" not in getter
    assert "ambiguous" not in setter
    assert "@property" in getter or "return self._x" in getter
    assert "self._x = value" in setter


def test_read_function_by_name_ambiguous_pre_numbered_shows_original_lines() -> None:
    """Pre-numbered twin defs → ambiguous error uses original line numbers, not physical."""
    src = "100: def twin():\n101:     return 1\n\n102: def twin():\n103:     return 2\n"
    idx = CodebaseIndex(files={"app/mod.py": src})
    msg = idx.read_function_by_name("app/mod.py", "twin")
    assert msg.startswith("Error:")
    assert "ambiguous" in msg
    assert "100–101" in msg
    assert "102–103" in msg
    assert "lines 1–" not in msg
    assert "lines 2–" not in msg


def test_read_function_by_name_multi_hunk_finds_construct_despite_sibling() -> None:
    """Pre-numbered multi-hunk excerpt: name lookup survives an unparseable sibling hunk."""
    content = (
        "100: def alpha():\n"
        "101:     return 1\n"
        "...\n"
        "150:     changed()\n"
        "...\n"
        "200: def beta():\n"
        "201:     return 2\n"
    )
    idx = CodebaseIndex(files={"app/mod.py": content})
    by_name = idx.read_function_by_name("app/mod.py", "beta")
    by_line = idx.read_function("app/mod.py", 200)
    assert by_name == by_line
    assert by_name.startswith("app/mod.py function beta lines 200–201 (2 lines):")
    assert "200| def beta():" in by_name


def test_read_function_by_name_empty_name_errors() -> None:
    idx = CodebaseIndex(files={"app/mod.py": "def f():\n    return 1\n"})
    assert "non-empty string" in idx.read_function_by_name("app/mod.py", "")
    assert "non-empty string" in idx.read_function_by_name("app/mod.py", "   ")
    assert "non-empty string" in idx.read_function_by_name("app/mod.py", None)  # type: ignore[arg-type]


def test_read_function_by_name_non_python_errors() -> None:
    idx = CodebaseIndex(files={"app/main.ts": "function f() { return 1; }\n"})
    msg = idx.read_function_by_name("app/main.ts", "f")
    assert msg.startswith("Error:")
    assert "Python file" in msg
    assert "app/main.ts" in msg


def test_read_function_tool_dispatches_line_and_name() -> None:
    src = "def f():\n    return 1\n"
    idx = CodebaseIndex(files={"app/mod.py": src})
    tools = _build_tools(idx)
    names = {t.tool_name for t in tools}
    assert "read_function" in names
    read_function = next(t for t in tools if t.tool_name == "read_function")
    by_line = read_function("app/mod.py", 1)
    by_digit = read_function("app/mod.py", "1")
    by_name = read_function("app/mod.py", "f")
    assert by_line == by_digit == by_name
    assert by_name.startswith("app/mod.py function f lines 1–2")


def test_read_function_tool_rejects_bool_and_non_str_non_int() -> None:
    """Bool must not coerce to int; other non-str/non-int types error."""
    idx = CodebaseIndex(files={"app/mod.py": "def f():\n    return 1\n"})
    read_function = next(t for t in _build_tools(idx) if t.tool_name == "read_function")
    for bad in (True, False, 1.5, None, ["f"]):
        msg = read_function("app/mod.py", bad)
        assert msg.startswith("Error:")
        assert "line number or name" in msg


def test_read_lines_missing_path_errors() -> None:
    """A path that resolves to nothing (no repo_reader) returns a not-found error."""
    idx = CodebaseIndex(files={"app/main.py": "a\nb\n"})
    msg = idx.read_lines("app/missing.py", 1, 2)
    assert msg.startswith("Error:")
    assert "file not found: app/missing.py" in msg


def test_read_lines_at_max_span_succeeds() -> None:
    """A span exactly equal to _READ_LINES_MAX_SPAN succeeds (only span+1 errors)."""
    body = "\n".join(f"line-{i}" for i in range(1, _READ_LINES_MAX_SPAN + 2)) + "\n"
    idx = CodebaseIndex(files={"big.py": body})
    result = idx.read_lines("big.py", 1, _READ_LINES_MAX_SPAN)
    assert not result.startswith("Error:")
    assert result.startswith(
        f"big.py lines 1–{_READ_LINES_MAX_SPAN} ({_READ_LINES_MAX_SPAN} lines):"
    )


def test_read_function_missing_path_errors() -> None:
    """A path that resolves to nothing (no repo_reader) returns a not-found error."""
    idx = CodebaseIndex(files={"app/mod.py": "def f():\n    return 1\n"})
    msg = idx.read_function("app/missing.py", 1)
    assert msg.startswith("Error:")
    assert "file not found: app/missing.py" in msg


def test_read_function_by_name_missing_path_errors() -> None:
    """A path that resolves to nothing (no repo_reader) returns a not-found error."""
    idx = CodebaseIndex(files={"app/mod.py": "def f():\n    return 1\n"})
    msg = idx.read_function_by_name("app/missing.py", "f")
    assert msg.startswith("Error:")
    assert "file not found: app/missing.py" in msg


def test_false_positive_prompt_documents_read_function() -> None:
    """Verifier system prompt must advertise the unified read_function tool."""
    from code_review_agent.prompts import FALSE_POSITIVE_VERIFY_PROMPT

    assert "read_function(path, name_or_line)" in FALSE_POSITIVE_VERIFY_PROMPT
    assert "read_lines(path, start, end)" in FALSE_POSITIVE_VERIFY_PROMPT


def test_false_positive_prompt_prefers_scoped_reads_over_whole_file() -> None:
    """The verifier prompt must default to find_references -> read_function/read_lines,
    not "read the entire file / never use partial ranges"."""
    from code_review_agent.prompts import FALSE_POSITIVE_VERIFY_PROMPT

    lower = FALSE_POSITIVE_VERIFY_PROMPT.lower()
    assert "do not examine the file in a series of partial ranges" not in lower
    assert "read_file always returns the complete file" not in lower
    assert "find_references" in FALSE_POSITIVE_VERIFY_PROMPT
    find_references_idx = FALSE_POSITIVE_VERIFY_PROMPT.index("find_references")
    non_default_idx = FALSE_POSITIVE_VERIFY_PROMPT.index("Non-default")
    assert find_references_idx < non_default_idx


def test_list_files_appends_existing_codebase_only_when_present() -> None:
    """``list_files`` appends the existing-codebase pseudo-path only when an excerpt is present."""
    assert CodebaseIndex(files={"a.py": "x"}).list_files() == ["a.py"]
    with_existing = CodebaseIndex(files={"a.py": "x"}, existing_codebase="old")
    assert with_existing.list_files() == ["a.py", CodebaseIndex.EXISTING_CODEBASE_PATH]


def test_search_matches_and_blank_and_existing() -> None:
    """``search`` finds case-insensitive matches across files and the excerpt with 1-based lines; a blank query returns nothing."""
    idx = CodebaseIndex(
        files={"a.py": "def foo():\n    pass\n", "b.py": "FOO_CONST = 1\n"},
        existing_codebase="legacy_foo()\n",
    )
    hits = idx.search("foo")
    paths = {p for p, _, _ in hits}
    assert paths == {"a.py", "b.py", CodebaseIndex.EXISTING_CODEBASE_PATH}
    # case-insensitive line numbers are 1-based
    assert ("a.py", 1, "def foo():") in hits
    assert idx.search("   ") == []


def test_search_respects_max_matches() -> None:
    """``search`` caps the number of returned hits at ``max_matches``."""
    idx = CodebaseIndex(files={"a.py": "x\n" * 100})
    assert len(idx.search("x", max_matches=5)) == 5


@pytest.mark.parametrize("bad_max", [0, -1, -100])
def test_search_rejects_nonpositive_max(bad_max: int) -> None:
    """``search`` raises ``ValueError`` on any non-positive ``max_matches`` (precondition guard)."""
    with pytest.raises(ValueError):
        CodebaseIndex(files={"a.py": "x"}).search("x", max_matches=bad_max)


def test_search_pre_numbered_returns_original_line_and_stripped_text() -> None:
    """``search`` on pre-numbered hunk content reports the original file line
    number, not the physical/storage index, and strips the ``N: `` prefix."""
    content = "500: EARLIER = 1\n501: NEEDLE_A = 2\n"
    idx = CodebaseIndex(files={"mod.py": content})
    hits = idx.search("NEEDLE_A")
    assert hits == [("mod.py", 501, "NEEDLE_A = 2")]


def test_search_mixed_pre_numbered_and_plain_sources_resolve_independently() -> None:
    """``search`` resolves a pre-numbered file and a plain file independently
    in the same index -- one file's numbering never affects the other's."""
    pre_numbered = "700: EARLIER = 1\n701: NEEDLE_B = 2\n"
    plain = "OTHER = 1\nNEEDLE_B = 3\n"
    idx = CodebaseIndex(files={"pre.py": pre_numbered, "plain.py": plain})
    hits = idx.search("NEEDLE_B")
    assert hits == [
        ("pre.py", 701, "NEEDLE_B = 2"),
        ("plain.py", 2, "NEEDLE_B = 3"),
    ]


_NO_REPO = "No repository access is available beyond this submission."

_HIT_LOC_RE = re.compile(r"^.+:\d+$")


def _hit_body(result: str) -> str:
    """Strip trailing no-reader / truncation banners from a find_references result."""
    for marker in ("\n\n(Scan truncated", f"\n\n{_NO_REPO}"):
        if marker in result:
            return result.split(marker, 1)[0]
    return result


def _hit_locs(result: str) -> list[str]:
    """Return path:line locator lines from the hit body (ignore excerpt bodies)."""
    return [ln for ln in _hit_body(result).splitlines() if _HIT_LOC_RE.match(ln)]


def test_find_references_returns_capped_path_line_hits() -> None:
    """Hits include path:line locators across files and the excerpt."""
    idx = CodebaseIndex(
        files={
            "a.py": "def foo():\n    pass\n",
            "b.py": "FOO_CONST = 1\n",
        },
        existing_codebase="legacy_foo()\n",
    )
    result = idx.find_references("foo")
    locs = _hit_locs(result)
    assert "a.py:1" in locs
    assert "b.py:1" in locs
    assert f"{CodebaseIndex.EXISTING_CODEBASE_PATH}:1" in locs
    assert _NO_REPO in result


def test_find_references_empty_and_blank_symbol() -> None:
    """Unknown or whitespace-only symbol returns the empty-references message."""
    idx = CodebaseIndex(files={"a.py": "def foo():\n    pass\n"})
    assert idx.find_references("zzz-not-there") == (
        f"No references for 'zzz-not-there'.\n\n{_NO_REPO}"
    )
    blank = idx.find_references("   ")
    assert blank.startswith("No references for '   '.")
    assert _NO_REPO in blank
    assert "not searched" not in blank  # no-reader path uses the access note only


def test_find_references_blank_symbol_with_reader_does_not_imply_complete_scan() -> None:
    """Blank symbol with a reader must not look like a finished empty repo search."""
    idx = CodebaseIndex(
        files={"a.py": "def foo():\n    pass\n"},
        repo_reader=_FakeReader({"other.py": "foo()\n"}),
    )
    result = idx.find_references("   ")
    assert "No references for '   '." in result
    assert "not searched" in result
    assert "does NOT prove" in result
    assert "other.py" not in result
    assert _NO_REPO not in result


def test_find_references_respects_max_matches() -> None:
    """Result is capped at max_matches path:line lines."""
    idx = CodebaseIndex(files={"a.py": "x\n" * 100})
    result = idx.find_references("x", max_matches=5)
    assert _hit_locs(result) == [f"a.py:{i}" for i in range(1, 6)]
    assert _NO_REPO in result


@pytest.mark.parametrize("bad_max", [0, -1, -100])
def test_find_references_rejects_nonpositive_max(bad_max: int) -> None:
    """Non-positive max_matches raises ValueError (same precondition as search)."""
    with pytest.raises(ValueError):
        CodebaseIndex(files={"a.py": "x"}).find_references("x", max_matches=bad_max)


def test_find_references_includes_repo_reader_hits() -> None:
    """When a reader is present, out-of-submission matches appear as path:line."""
    idx = CodebaseIndex(
        files={"changed.py": "x = 1\n"},
        repo_reader=_FakeReader({"other/caller.py": "from changed import x\nx()\n"}),
    )
    result = idx.find_references("changed")
    assert "other/caller.py:1" in _hit_locs(result)
    assert "No references" not in result


def test_find_references_merges_submission_then_repo_under_cap() -> None:
    """Submission hits come first; total length respects max_matches."""
    idx = CodebaseIndex(
        files={"a.py": "needle\n"},
        repo_reader=_FakeReader(
            {
                "r1.py": "needle\n",
                "r2.py": "needle\n",
                "r3.py": "needle\n",
            }
        ),
    )
    result = idx.find_references("needle", max_matches=3)
    locs = _hit_locs(result)
    assert locs[0] == "a.py:1"
    assert len(locs) == 3
    assert "Scan truncated" in result


def test_find_references_skips_submission_paths_in_repo_half() -> None:
    """A reader path that is also a submission key is not double-counted from repo."""
    idx = CodebaseIndex(
        files={"shared.py": "needle\n"},
        repo_reader=_FakeReader(
            {
                "shared.py": "needle\nneedle\n",  # would add extra lines if not skipped
                "only_repo.py": "needle\n",
            }
        ),
    )
    result = idx.find_references("needle", max_matches=10)
    locs = _hit_locs(result)
    assert locs.count("shared.py:1") == 1
    assert "shared.py:2" not in locs
    assert "only_repo.py:1" in locs


def test_search_repo_references_respects_max_files_scanned() -> None:
    """File-scan cap limits how many non-submission reader files are opened."""
    from software_engineering_team.code_review_agent.false_positive_filter import (
        _search_repo_references,
    )

    reader_files = {f"f{i}.py": "needle\n" for i in range(5)}
    idx = CodebaseIndex(files={"sub.py": "other\n"}, repo_reader=_FakeReader(reader_files))
    hits, truncated = _search_repo_references(idx, "needle", max_matches=10, max_files_scanned=2)
    assert len(hits) == 2
    assert truncated is True
    assert {path for path, _, _ in hits} <= set(reader_files)


@pytest.mark.parametrize("raise_error", [True, False])
def test_search_repo_references_per_file_failure_keeps_other_hits(raise_error: bool) -> None:
    """One file's read_file failing (raise or None) skips just that file, not the scan.

    Distinct from the ``_BoomReader`` case (list_files itself fails): here the
    listing succeeds and most files read fine, so the other hits must still
    surface, with ``truncated`` set to flag the incomplete coverage.
    """
    from software_engineering_team.code_review_agent.false_positive_filter import (
        _search_repo_references,
    )

    files = {"a.py": "needle\n", "bad.py": "needle\n", "c.py": "needle\n"}
    idx = CodebaseIndex(
        files={"sub.py": "other\n"},
        repo_reader=_PartialFailReader(files, fail_paths=["bad.py"], raise_error=raise_error),
    )
    hits, truncated = _search_repo_references(idx, "needle", max_matches=10)
    assert {path for path, _, _ in hits} == {"a.py", "c.py"}
    assert truncated is True


def test_find_references_no_reader_unchanged() -> None:
    """Without a reader, results stay submission-only and note that explicitly."""
    idx = CodebaseIndex(files={"a.py": "def foo():\n    pass\n"})
    result = idx.find_references("foo")
    assert "a.py:1" in _hit_locs(result)
    assert "function foo" in result
    assert _NO_REPO in result
    assert idx.find_references("zzz") == f"No references for 'zzz'.\n\n{_NO_REPO}"


def test_find_references_no_reader_note_on_hits() -> None:
    idx = CodebaseIndex(files={"a.py": "foo\n"})
    result = idx.find_references("foo")
    assert "a.py:1" in _hit_locs(result)
    assert _NO_REPO in result


def test_find_references_truncated_banner_when_match_cap_skips_repo() -> None:
    """Submission fills max_matches with a reader present → truncated (repo not searched)."""
    idx = CodebaseIndex(
        files={"a.py": "x\nx\nx\n"},
        repo_reader=_FakeReader({"r.py": "x\n"}),
    )
    result = idx.find_references("x", max_matches=2)
    assert _hit_locs(result) == ["a.py:1", "a.py:2"]
    assert "Scan truncated" in result
    assert "more matches" in result


def test_find_references_truncated_empty_message(monkeypatch) -> None:
    """Repo scan hits file-scan cap with no matches → empty-truncated wording."""
    import code_review_agent.false_positive_filter as fpf

    monkeypatch.setattr(fpf, "_REPO_SEARCH_FILE_SCAN_LIMIT", 2)
    idx = CodebaseIndex(
        files={"sub.py": "other\n"},
        repo_reader=_FakeReader({f"f{i}.py": "zzz\n" for i in range(5)}),
    )
    result = idx.find_references("needle")
    assert "No references for 'needle'" in result
    assert "truncated" in result
    assert "does NOT prove" in result


def test_find_references_list_files_failure_is_empty_truncated() -> None:
    """Reader list_files failure must surface as empty-truncated, not a complete miss."""
    idx = CodebaseIndex(
        files={"sub.py": "other\n"},
        repo_reader=_BoomReader(),
    )
    result = idx.find_references("needle")
    assert "No references for 'needle'" in result
    assert "truncated" in result
    assert "does NOT prove" in result


def test_find_references_attaches_enclosing_construct_excerpt() -> None:
    """A hit inside a Python function includes the construct slice."""
    src = "def outer():\n    return 1\n\ndef caller():\n    return outer()\n"
    idx = CodebaseIndex(files={"mod.py": src})
    result = idx.find_references("outer")
    assert "mod.py:5" in _hit_locs(result)
    assert "function caller" in result
    assert "return outer()" in result
    assert "def outer():" in result  # definition hit may also appear
    assert _NO_REPO in result


def test_find_references_repo_hit_unreadable_at_format_time_returns_locator_only() -> None:
    """A repo hit found during the scan but unreadable on the second, format-time read
    degrades to a bare path:line locator instead of raising or dropping the hit."""
    idx = CodebaseIndex(
        files={"sub.py": "other\n"},
        repo_reader=_FlakyReader({"caller.py": "def caller():\n    return needle()\n"}),
    )
    result = idx.find_references("needle")
    assert result == "caller.py:2"
    assert "function caller" not in result
    assert "def caller" not in result


def test_find_references_repo_hit_construct_excerpt_unaffected_by_lineno_fix() -> None:
    """Repo-half hits (always plain content, never pre-numbered) still resolve their
    enclosing construct correctly -- the ``lineno``-as-original-line fix for
    submission hits must not regress the plain-content (``mapper is None``) path."""
    idx = CodebaseIndex(
        files={"sub.py": "other\n"},
        repo_reader=_FakeReader({"caller.py": "def caller():\n    return needle()\n"}),
    )
    result = idx.find_references("needle")
    assert "caller.py:2" in _hit_locs(result)
    assert "function caller" in result
    assert "return needle()" in result


def test_find_references_module_level_hit_gets_line_window_fallback() -> None:
    """Module-level hits (no enclosing construct) get a bounded raw-line window."""
    src = "A = 1\nB = 2\nNEEDLE = 3\nC = 4\nD = 5\n"
    idx = CodebaseIndex(files={"mod.py": src})
    result = idx.find_references("NEEDLE")
    assert _hit_locs(result) == ["mod.py:3"]
    body = _hit_body(result)
    assert "function" not in body
    assert "class" not in body
    assert "window" in body
    assert "NEEDLE = 3" in body
    assert _NO_REPO in result


def test_find_references_unparsable_python_file_gets_line_window() -> None:
    """A .py file that fails to parse still gets a bounded window, not just a locator."""
    src = "def broken(:\n    NEEDLE = 1\n"
    idx = CodebaseIndex(files={"broken.py": src})
    result = idx.find_references("NEEDLE")
    assert _hit_locs(result) == ["broken.py:2"]
    body = _hit_body(result)
    assert "window" in body
    assert "NEEDLE = 1" in body


def test_find_references_non_python_file_gets_line_window() -> None:
    """Non-Python files get a bounded raw-line window instead of an empty excerpt."""
    src = "line one\nline two\nNEEDLE here\nline four\n"
    idx = CodebaseIndex(files={"notes.md": src})
    result = idx.find_references("NEEDLE")
    assert _hit_locs(result) == ["notes.md:3"]
    body = _hit_body(result)
    assert "window" in body
    assert "NEEDLE here" in body


def test_find_references_line_window_clamps_to_file_bounds() -> None:
    """A window near a small file's edges doesn't request out-of-range lines."""
    src = "NEEDLE = 1\nB = 2\nC = 3\n"
    idx = CodebaseIndex(files={"mod.py": src})
    result = idx.find_references("NEEDLE")
    body = _hit_body(result)
    assert "NEEDLE = 1" in body
    assert "of 3 lines" in body


def test_find_references_construct_exceeding_cap_uses_window(monkeypatch) -> None:
    """A construct bigger than the excerpt cap is windowed, not dumped in full."""
    import code_review_agent.false_positive_filter as fpf

    monkeypatch.setattr(fpf, "_EXCERPT_MAX_LINES", 3)
    monkeypatch.setattr(fpf, "_EXCERPT_WINDOW_LINES", 3)
    lines = ["def big():"] + [f"    x{i} = {i}" for i in range(20)] + ["    return NEEDLE"]
    src = "\n".join(lines) + "\n"
    idx = CodebaseIndex(files={"mod.py": src})
    result = idx.find_references("NEEDLE")
    hit_line = len(lines)
    assert f"mod.py:{hit_line}" in _hit_locs(result)
    body = _hit_body(result)
    assert "return NEEDLE" in body
    assert "window" in body
    assert "x0 = 0" not in body
    assert "function big" not in body


def test_find_references_pre_numbered_uses_original_line_and_correct_excerpt() -> None:
    """Annotated hunk hits remap storage indices to original lines and the right construct."""
    src = "100: def earlier():\n101:     pass\n102: \n103: def later():\n104:     return NEEDLE\n"
    idx = CodebaseIndex(files={"mod.py": src})
    result = idx.find_references("NEEDLE")
    assert _hit_locs(result) == ["mod.py:104"]
    assert "function later" in result
    assert "return NEEDLE" in result
    assert "function earlier" not in result
    assert _NO_REPO in result


def test_find_references_pre_numbered_second_hunk_resolves_correct_construct() -> None:
    """A hit inside the second hunk of a multi-hunk excerpt resolves to that hunk's
    own construct, not the first hunk's."""
    src = "10: def first():\n11:     return 1\n...\n50: def second():\n51:     return NEEDLE\n"
    idx = CodebaseIndex(files={"mod.py": src})
    result = idx.find_references("NEEDLE")
    assert _hit_locs(result) == ["mod.py:51"]
    assert "function second" in result
    assert "return NEEDLE" in result
    assert "function first" not in result
    assert "return 1" not in result
    assert _NO_REPO in result


def test_find_references_no_construct_window_never_crosses_hunk_gap(monkeypatch) -> None:
    """A no-construct hit near the end of hunk1 gets a window clipped to hunk1 only --
    it must not cross the "..." gap marker into unrelated hunk2 content."""
    import code_review_agent.false_positive_filter as fpf

    monkeypatch.setattr(fpf, "_EXCERPT_WINDOW_LINES", 6)
    src = (
        "100: A = 1\n"
        "101: B = 2\n"
        "102: C = 3\n"
        "103: NEEDLE = 4\n"
        "...\n"
        "200: D = 1\n"
        "201: E = 2\n"
        "202: F = 3\n"
        "203: G = 4\n"
    )
    idx = CodebaseIndex(files={"mod.py": src})
    result = idx.find_references("NEEDLE")
    assert _hit_locs(result) == ["mod.py:103"]
    body = _hit_body(result)
    assert "window" in body
    assert "NEEDLE = 4" in body
    assert "..." not in body
    assert "D = 1" not in body


def test_find_references_no_construct_window_plain_content_not_clipped(monkeypatch) -> None:
    """The equivalent window on plain, non-pre-numbered content is deliberately NOT clipped --
    a literal "..." line in ordinary content carries no gap-marker meaning."""
    import code_review_agent.false_positive_filter as fpf

    monkeypatch.setattr(fpf, "_EXCERPT_WINDOW_LINES", 6)
    src = "A = 1\nB = 2\nC = 3\nNEEDLE = 4\n...\nD = 1\nE = 2\nF = 3\nG = 4\n"
    idx = CodebaseIndex(files={"notes.txt": src})
    result = idx.find_references("NEEDLE")
    assert _hit_locs(result) == ["notes.txt:4"]
    body = _hit_body(result)
    assert "window" in body
    assert "NEEDLE = 4" in body
    assert "..." in body
    assert "D = 1" in body


# --------------------------------------------------------------------------- tools


def test_build_tools_delegate_to_index() -> None:
    """``_build_tools`` returns seven tools that delegate to the index."""
    idx = CodebaseIndex(files={"app/main.py": "def foo(): pass\n"}, existing_codebase="old")
    (
        read_file,
        read_lines,
        read_function,
        list_files,
        search_codebase,
        find_function_at_line,
        find_references,
    ) = _build_tools(idx)
    assert {
        read_file.tool_name,
        read_lines.tool_name,
        read_function.tool_name,
        list_files.tool_name,
        search_codebase.tool_name,
        find_function_at_line.tool_name,
        find_references.tool_name,
    } == {
        "read_file",
        "read_lines",
        "read_function",
        "list_files",
        "search_codebase",
        "find_function_at_line",
        "find_references",
    }
    read_result = read_file("app/main.py")
    assert read_result == {"status": "success", "content": [{"text": "def foo(): pass\n"}]}
    listed = list_files()
    assert "app/main.py" in listed and CodebaseIndex.EXISTING_CODEBASE_PATH in listed
    assert "app/main.py:1: def foo(): pass" in search_codebase("foo")
    assert "No matches" in search_codebase("zzz-not-there")
    slice_text = read_lines("app/main.py", 1, 1)
    assert slice_text.startswith("app/main.py lines 1–1 (1 lines):")
    assert "1| def foo(): pass" in slice_text
    assert "app/main.py:1" in find_references("foo")
    assert "No references" in find_references("zzz-not-there")


def test_search_codebase_tool_pre_numbered_reports_original_line_no_leak() -> None:
    """search_codebase's "path:line: text" output uses the real original line
    number and never leaks the raw "N: " prefix into the displayed text."""
    content = "300: NEEDLE_C = 1\n"
    idx = CodebaseIndex(files={"svc.py": content})
    (
        _read_file,
        _read_lines,
        _read_function,
        _list_files,
        search_codebase,
        _find_function_at_line,
        _find_references,
    ) = _build_tools(idx)
    result = search_codebase("NEEDLE_C")
    assert result == "svc.py:300: NEEDLE_C = 1"


def test_build_tools_includes_find_references() -> None:
    """``_build_tools`` exposes find_references alongside the existing six tools."""
    idx = CodebaseIndex(files={"app/main.py": "def foo(): pass\n"})
    tools = _build_tools(idx)
    names = {t.tool_name for t in tools}
    assert names == {
        "read_file",
        "read_lines",
        "read_function",
        "list_files",
        "search_codebase",
        "find_function_at_line",
        "find_references",
    }


def test_list_files_tool_handles_empty_index() -> None:
    """The list_files tool returns a placeholder string for an empty index."""
    _, _, _, list_files, _, _, _ = _build_tools(CodebaseIndex(files={}))
    assert list_files() == "(no files available)"


def test_truncate_for_log_caps_length() -> None:
    """``_truncate_for_log`` leaves short text alone and caps long text with an ellipsis."""
    from code_review_agent.false_positive_filter import _truncate_for_log

    assert _truncate_for_log("abc", 10) == "abc"
    assert _truncate_for_log(None, 10) == ""
    assert len(_truncate_for_log("x" * 500, 400)) == 403  # 400 + "..."
    assert _truncate_for_log("x" * 500, 400).endswith("...")
    with pytest.raises(ValueError):
        _truncate_for_log("x", 0)


def test_build_tools_never_raise_on_index_errors(monkeypatch) -> None:
    """Index-backed tools return Error strings when the underlying index raises."""
    idx = CodebaseIndex(files={"a.py": "x"})
    read_file, read_lines, read_function, list_files, search_codebase, _find, find_references = (
        _build_tools(idx)
    )

    def _boom_read(_self: CodebaseIndex, path: str):
        raise RuntimeError("index boom")

    def _boom_read_lines(_self: CodebaseIndex, path: str, start: int, end: int) -> str:
        raise RuntimeError("index boom")

    def _boom_read_function(_self: CodebaseIndex, path: str, line: int) -> str:
        raise RuntimeError("index boom")

    def _boom_read_function_by_name(_self: CodebaseIndex, path: str, name: str) -> str:
        raise RuntimeError("index boom")

    def _boom_list(_self: CodebaseIndex) -> List[str]:
        raise RuntimeError("index boom")

    def _boom_search(_self: CodebaseIndex, query: str, max_matches: int = 60):
        raise RuntimeError("index boom")

    monkeypatch.setattr(CodebaseIndex, "_read", _boom_read)
    monkeypatch.setattr(CodebaseIndex, "read_lines", _boom_read_lines)
    monkeypatch.setattr(CodebaseIndex, "read_function", _boom_read_function)
    monkeypatch.setattr(CodebaseIndex, "read_function_by_name", _boom_read_function_by_name)
    monkeypatch.setattr(CodebaseIndex, "list_files", _boom_list)
    monkeypatch.setattr(CodebaseIndex, "search", _boom_search)

    read_result = read_file("a.py")
    assert read_result["status"] == "error"
    assert read_result["content"][0]["text"].startswith("Error:")
    assert read_lines("a.py", 1, 1).startswith("Error:")
    assert read_function("a.py", 1).startswith("Error:")
    assert read_function("a.py", "f").startswith("Error:")
    assert list_files().startswith("Error:")
    assert search_codebase("x").startswith("Error:")
    assert find_references("x").startswith("Error:")


def test_read_lines_tool_enforces_max_span() -> None:
    """The read_lines tool surfaces the oversize-span error from the index."""
    body = "\n".join(f"L{i}" for i in range(1, 450)) + "\n"
    idx = CodebaseIndex(files={"big.py": body})
    _, read_lines, _, _, _, _, _ = _build_tools(idx)
    msg = read_lines("big.py", 1, _READ_LINES_MAX_SPAN + 1)
    assert msg.startswith("Error:")
    assert f"maximum is {_READ_LINES_MAX_SPAN}" in msg


# --------------------------------------------------------------------------- duplicate/budget guard


def test_repeated_identical_tool_call_gets_a_stop_note() -> None:
    """A tool called with the exact same arguments more than
    ``_MAX_DUPLICATE_TOOL_CALLS`` times still returns its real result, but with a
    note telling the model it already has this information -- the defense
    against a verifier that keeps re-asking the same question instead of
    converging on a verdict."""
    idx = CodebaseIndex(files={"app/main.py": "def foo(): pass\n"})
    _, _, _, _, search_codebase, _, _ = _build_tools(idx)
    for _ in range(_MAX_DUPLICATE_TOOL_CALLS):
        result = search_codebase("foo")
        assert "app/main.py:1: def foo(): pass" in result
        assert "already called" not in result
    # One more call than the duplicate budget allows.
    result = search_codebase("foo")
    assert "app/main.py:1: def foo(): pass" in result
    assert "already called search_codebase" in result
    assert "answer now" in result


def test_repeated_calls_with_different_args_are_not_duplicates() -> None:
    """Interleaved calls to the same tool with different arguments track
    independent counters -- calling it with one query never counts toward the
    duplicate budget for a different query, up to each signature's own
    duplicate budget."""
    idx = CodebaseIndex(files={"app/main.py": "def foo(): pass\ndef bar(): pass\n"})
    _, _, _, _, search_codebase, _, _ = _build_tools(idx)
    for _ in range(_MAX_DUPLICATE_TOOL_CALLS):
        assert "already called" not in search_codebase("foo")
        assert "already called" not in search_codebase("bar")


def test_tool_call_budget_short_circuits_after_total_exhausted() -> None:
    """Once total tool calls across every tool exceed ``_MAX_TOTAL_TOOL_CALLS``,
    every further call -- even a fresh, never-before-seen one -- skips its real
    lookup and returns a stop directive, bounding the cost of a verifier that
    never converges."""
    idx = CodebaseIndex(files={"app/main.py": "def foo(): pass\n"})
    read_file, _, _, list_files, search_codebase, _, _ = _build_tools(idx)
    for i in range(_MAX_TOTAL_TOOL_CALLS):
        # Vary the query so none of these trip the duplicate-call path first.
        search_codebase(f"needle-{i}")
    # The budget is now exhausted: a brand-new call to a different tool is
    # short-circuited too, not just repeats of what was already called.
    result = list_files()
    assert "tool call budget" in result
    assert "exhausted" in result
    assert "app/main.py" not in result
    read_result = read_file("app/main.py")
    assert read_result["status"] == "error"
    assert "tool call budget" in read_result["content"][0]["text"]


def test_tool_call_guard_tolerates_unhashable_arguments() -> None:
    """A malformed model-supplied argument (e.g. a list where a string was
    expected) must not crash the duplicate/budget tracker -- tools built here
    never raise on bad input, and the tracker keys on ``repr(args)`` precisely
    so an unhashable argument stays trackable instead of raising."""
    idx = CodebaseIndex(files={"app/mod.py": "def f():\n    return 1\n"})
    _, _, read_function, _, _, _, _ = _build_tools(idx)
    for _ in range(_MAX_DUPLICATE_TOOL_CALLS + 1):
        msg = read_function("app/mod.py", ["f"])
        assert isinstance(msg, str)


# --------------------------------------------------------------------------- find_function_at_line


def test_find_function_at_line_python_top_level() -> None:
    """Tool returns the enclosing top-level function for a Python file."""
    code = "def alpha():\n    x = 1\n    return x\n\ndef beta():\n    pass\n"
    idx = CodebaseIndex(files={"app/main.py": code})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("app/main.py", 2)
    assert "alpha" in result
    assert "beta" not in result


def test_find_function_at_line_line_one_is_one_based() -> None:
    """``line_number=1`` resolves the construct starting on the first line (1-based contract)."""
    code = "def alpha():\n    return 1\n"
    idx = CodebaseIndex(files={"app/main.py": code})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("app/main.py", 1)
    assert "alpha" in result
    assert not result.startswith("Error:")


def test_find_function_at_line_python_nested() -> None:
    """Tool returns the innermost (nested) function, not the outer one."""
    code = (
        "def outer():\n"  # line 1
        "    x = 1\n"  # line 2
        "    def inner():\n"  # line 3
        "        return x\n"  # line 4
        "\n"  # line 5
    )
    idx = CodebaseIndex(files={"svc.py": code})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("svc.py", 4)
    assert "inner" in result
    assert "outer" not in result


def test_find_function_at_line_python_class_method() -> None:
    """Tool returns both the method name and its enclosing class name."""
    code = (
        "class Foo:\n"  # line 1
        "    def bar(self):\n"  # line 2
        "        return 42\n"  # line 3
    )
    idx = CodebaseIndex(files={"models.py": code})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("models.py", 3)
    assert "bar" in result
    assert "Foo" in result


def test_find_function_at_line_python_module_level() -> None:
    """Tool reports 'module level' when the line is not inside any construct."""
    code = "X = 1\nY = 2\n"
    idx = CodebaseIndex(files={"config.py": code})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("config.py", 1)
    assert "module level" in result


def test_find_function_at_line_non_python_heuristic() -> None:
    """Tool falls back to the column-0 heuristic for non-Python files."""
    code = "function doWork() {\n  const x = 1;\n  return x;\n}\n"
    idx = CodebaseIndex(files={"app.ts": code})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("app.ts", 2)
    # Heuristic returns the start line of the enclosing construct.
    assert "starting at line 1" in result


def test_find_function_at_line_unknown_path() -> None:
    """Tool returns an error string for a path not in the index."""
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("does/not/exist.py", 5)
    assert result.startswith("Error")


def test_find_function_at_line_content_literally_starting_with_error() -> None:
    """A readable file whose content starts with 'Error:' is not mistaken for a failure."""
    code = "Error: this is file content, not a read failure.\ndef alpha():\n    pass\n"
    idx = CodebaseIndex(files={"fixtures/log_sample.py": code})
    # Contract under test: content beginning with ``Error:`` is still readable.
    assert idx.read_file_or_none("fixtures/log_sample.py") == code
    assert code.startswith("Error:")
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("fixtures/log_sample.py", 2)
    # Must not treat the content as a read-failure sentinel.
    assert "is not a readable path" not in result
    # Content is invalid Python, so the AST path reports a parse error —
    # which still proves the bytes were obtained and inspected.
    assert "Could not parse" in result
    assert not result.startswith("Error:")


def test_find_function_at_line_python_syntax_error() -> None:
    """Tool returns a parse-error message for a Python file with invalid syntax."""
    code = "def foo(:\n    pass\n"  # SyntaxError: missing closing paren
    idx = CodebaseIndex(files={"broken.py": code})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("broken.py", 2)
    assert "Could not parse" in result


def test_find_function_at_line_python_async_def() -> None:
    """Tool correctly identifies an async function as the enclosing construct."""
    code = "async def fetch():\n    return await something()\n"
    idx = CodebaseIndex(files={"service.py": code})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("service.py", 2)
    assert "fetch" in result


def test_find_function_at_line_python_decorated() -> None:
    """Tool reports the decorator start line as the construct start."""
    code = (
        "@decorator\n"  # line 1
        "def greet():\n"  # line 2
        "    return 'hi'\n"  # line 3
    )
    idx = CodebaseIndex(files={"views.py": code})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("views.py", 3)
    assert "greet" in result
    assert "lines 1" in result  # decorator line is the reported start


def test_find_function_at_line_non_python_no_construct() -> None:
    """Tool returns 'Could not identify' when no column-0 declaration precedes the target line."""
    code = "  const x = 1;\n  return x;\n"  # every line is indented
    idx = CodebaseIndex(files={"snippet.ts": code})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("snippet.ts", 1)
    assert "Could not identify" in result


def test_find_function_at_line_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected internal error is caught and returned as an error string.

    Regression test: ``find_function_at_line`` documents (and ``_build_tools``
    relies on) a "never raises" postcondition, but the body used to call
    ``index.resolve_path`` with no exception handling, so an internal error
    there would propagate out of the ``@tool`` and abort the verifier's
    tool-calling loop instead of surfacing as an ordinary tool result.
    """
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})

    def _boom(_self: CodebaseIndex, path: str) -> str:
        raise RuntimeError("boom")

    # Patch on the class: CodebaseIndex is frozen, so instance setattr fails.
    monkeypatch.setattr(CodebaseIndex, "resolve_path", _boom)
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("app/main.py", 1)
    assert result.startswith("Error")
    assert "boom" in result


# --------------------------------------------------------------------------- pre-numbered content


def test_strip_numbered_prefixes_plain_content_unchanged() -> None:
    """Plain content (no ``N: `` prefixes) is returned unchanged with no remap."""
    content = "function foo() {\n  return 1;\n}\n"
    stripped, physical, mapper = _strip_numbered_prefixes(content, line_number=2)
    assert stripped == content
    assert physical == 2
    assert mapper is None


def test_strip_numbered_prefixes_detects_and_strips() -> None:
    """Pre-numbered hunk content is stripped and the target remapped to a physical index."""
    # Simulate render_annotated_hunks output: original lines 4240-4242.
    content = "4240: const a = 1;\n4241: const b = 2;\n4242: return a + b;\n"
    stripped, physical, mapper = _strip_numbered_prefixes(content, line_number=4242)
    assert "4242:" not in stripped
    assert stripped == "const a = 1;\nconst b = 2;\nreturn a + b;"
    # Target original line 4242 maps to physical line 3.
    assert physical == 3
    assert mapper is not None
    assert mapper(3) == 4242  # physical 3 → original 4242
    assert mapper(1) == 4240  # physical 1 → original 4240


def test_strip_numbered_prefixes_fallback_to_last_before() -> None:
    """When the exact target line is absent (e.g., a removed line), use the last line before it."""
    # Only lines 100 and 102 are present; line 101 was a removed line not in the hunk.
    content = "100: const x = 1;\n102: const y = 2;\n"
    stripped, physical, mapper = _strip_numbered_prefixes(content, line_number=101)
    # physical index should be 1 (original line 100, last before 101).
    assert physical == 1
    assert mapper(1) == 100


def test_strip_numbered_prefixes_empty_content() -> None:
    """Empty content returns unchanged with no remap."""
    stripped, physical, mapper = _strip_numbered_prefixes("", line_number=1)
    assert stripped == ""
    assert physical == 1
    assert mapper is None


def test_find_function_at_line_rejects_nonpositive_line() -> None:
    """Tool returns an error string for invalid line numbers instead of guessing or raising."""
    idx = CodebaseIndex(files={"app/main.py": "def f():\n    return 1\n"})
    _, _, _, _, _, find_fn, _ = _build_tools(idx)
    for bad in (0, -1, -3, True, False, "5"):
        msg = find_fn("app/main.py", bad)  # type: ignore[arg-type]
        assert msg.startswith("Error:"), bad
        assert "positive" in msg.lower(), bad
    missing = find_fn("does/not/exist.py", 1)
    assert missing.startswith("Error:")
    assert "not a readable path" in missing


def test_strip_numbered_prefixes_rejects_bad_preconditions() -> None:
    """Helper raises when documented preconditions are violated."""
    with pytest.raises((TypeError, ValueError, AssertionError)):
        _strip_numbered_prefixes(None, 1)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError, AssertionError)):
        _strip_numbered_prefixes("x = 1\n", 0)


def test_find_heuristic_beyond_eof() -> None:
    """Heuristic finder reports beyond-EOF instead of attributing the last construct."""
    from code_review_agent.false_positive_filter import _find_heuristic_function_at_line

    content = "function alpha() {\n  return 1;\n}\n"
    msg = _find_heuristic_function_at_line(content, 99, "app.ts")
    assert "beyond" in msg.lower()


def test_find_function_at_line_python_beyond_eof() -> None:
    """Python AST finder returns an explicit beyond-EOF message for out-of-range lines."""
    idx = CodebaseIndex(files={"app/main.py": "def alpha():\n    return 1\n"})
    _, _, _, _, _, find_fn, _ = _build_tools(idx)
    msg = find_fn("app/main.py", 99)
    assert "beyond the end" in msg.lower()
    assert "file has" in msg.lower()


def test_find_function_at_line_pre_numbered_python() -> None:
    """Tool strips N: prefixes and reports original line numbers in the enclosing range."""
    # Simulate a hunk starting at original line 100. The def is at original line 101.
    content = "100: x = setup()\n101: def process(data):\n102:     return data * 2\n"
    idx = CodebaseIndex(files={"worker.py": content})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    # Ask for original line 102, which is inside 'process'.
    result = find_function_at_line("worker.py", 102)
    assert "process" in result
    # The reported range must use original line numbers (101–102), not physical (2–3).
    assert "101" in result
    assert "102" in result
    assert "lines 2" not in result  # physical line 2 must NOT appear as a range bound


def test_find_function_at_line_pre_numbered_non_python() -> None:
    """Tool strips N: prefixes and reports the original line numbers for non-Python files."""
    # Simulate a TypeScript hunk at original lines 4240-4243.
    content = (
        "4240: export class DataService {\n"
        "4241:   private data: string;\n"
        "4242:   process() {\n"
        "4243:     return this.data;\n"
    )
    idx = CodebaseIndex(files={"service.ts": content})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    # Ask for original line 4243.
    result = find_function_at_line("service.ts", 4243)
    # Should report the original line number, not the physical line 1.
    assert "4240" in result
    # Should NOT report a physical line like "1" as the start.
    assert "starting at line 1" not in result


def test_find_function_at_line_pre_numbered_large_line_number() -> None:
    """A large original line number (as from a real PR diff) does not confuse the heuristic."""
    # The bug: with line_number=4242 and a 3-line file (physical lines 1-3),
    # ``i > line_number`` never fired and every column-0 line looked like a start.
    content = (
        "4240: const a = 1;\n4241: const b = 2;\n4242: function getResult() { return a + b; }\n"
    )
    idx = CodebaseIndex(files={"util.js": content})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("util.js", 4242)
    # Must report original line 4242 (the function line), not physical line 3.
    assert "4242" in result
    # Must not claim a line like "3" as the start (that would be a pre-fix bug).
    assert "starting at line 3" not in result


def test_find_function_at_line_hunk_separator_not_treated_as_construct() -> None:
    """The ``...`` hunk separator from multi-hunk diffs is not counted as a construct start."""
    # Simulate two hunks separated by "...". The first hunk has indented-only lines;
    # the separator "..." is column-0. Without the fix it would be the best_start.
    content = (
        "10:   const a = 1;\n"
        "...\n"  # separator emitted by render_annotated_hunks
        "50: function doWork() {\n"
        "51:   return a;\n"
    )
    idx = CodebaseIndex(files={"util.js": content})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("util.js", 51)
    # The construct start must be the "doWork" line (original 50), not the separator.
    assert "50" in result
    assert "..." not in result  # separator must not appear in the output as a construct


def test_find_function_at_line_module_level_hunk_not_broken_by_sibling_hunk() -> None:
    """A module-level target in its own valid hunk still resolves when a
    *different* hunk elsewhere in the file would fail to parse if joined."""
    # Three hunks: a complete function, a standalone module-level statement
    # (the target), and a dangling indented continuation with no declaration
    # in its own excerpt. Naively re-parsing the whole stripped content would
    # raise IndentationError on the third hunk and misreport the target
    # (module-level line 2 of hunk B) as unparseable.
    content = "10: def first():\n11:     return 1\n...\n20: x = 1\n...\n30:     changed()\n"
    idx = CodebaseIndex(files={"worker.py": content})
    _, _, _, _, _, find_function_at_line, _ = _build_tools(idx)
    result = find_function_at_line("worker.py", 20)
    assert "module level" in result.lower()
    assert "could not parse" not in result.lower()


# --------------------------------------------------------------------------- verdict parsing


def test_coerce_verdict_variants() -> None:
    """``_coerce_verdict`` drops only on an explicit false verdict at high/medium confidence; every other shape keeps the finding or returns None."""
    # explicit false + high confidence → false positive (drop)
    idx, v = _coerce_verdict({"index": 2, "is_real_issue": False, "confidence": "high"})
    assert idx == 2 and v.is_false_positive is True
    # explicit false + medium confidence → false positive (the prompt accepts
    # "medium" as a confident drop, so a regression to "high"-only must fail here)
    _, v = _coerce_verdict({"index": 0, "is_real_issue": False, "confidence": "medium"})
    assert v.is_false_positive is True
    # false but low confidence → keep
    _, v = _coerce_verdict({"index": 0, "is_real_issue": False, "confidence": "low"})
    assert v.is_false_positive is False
    # false but an UNRECOGNIZED confidence → keep. The gate is an allowlist
    # ("high"/"medium"), not a denylist, so an off-contract value ("unsure",
    # "none", "n/a", or a non-string the model returned) is treated as
    # not-confident and the finding is kept — a regression to `not in ("","low")`
    # would drop these and must fail here.
    for off_contract in ("unsure", "none", "n/a", "uncertain"):
        _, v = _coerce_verdict({"index": 0, "is_real_issue": False, "confidence": off_contract})
        assert v.is_false_positive is False, off_contract
    # a non-string confidence the model might emit also coerces to a kept verdict
    _, v = _coerce_verdict({"index": 0, "is_real_issue": False, "confidence": 1})
    assert v.is_false_positive is False
    # false but no confidence → keep
    _, v = _coerce_verdict({"index": 0, "is_real_issue": False})
    assert v.is_false_positive is False
    # explicit JSON null confidence → keep (the `or ""` guard maps None to "",
    # never the string "none", so a null-confidence verdict can't drop a finding)
    _, v = _coerce_verdict({"index": 0, "is_real_issue": False, "confidence": None})
    assert v.is_false_positive is False
    # missing is_real_issue key → kept (a valid verdict, not None: index parsed, but
    # without an explicit "not a real issue" the finding is never dropped)
    _, v = _coerce_verdict({"index": 0, "confidence": "high"})
    assert v.is_false_positive is False
    # real issue → keep
    _, v = _coerce_verdict({"index": 0, "is_real_issue": True, "confidence": "high"})
    assert v.is_false_positive is False
    # missing/garbage index → None
    assert _coerce_verdict({"is_real_issue": False, "confidence": "high"}) is None
    assert _coerce_verdict({"index": "x"}) is None
    assert _coerce_verdict("not a dict") is None
    # bool / float / negative indices are rejected (not coerced)
    assert _coerce_verdict({"index": True, "is_real_issue": False, "confidence": "high"}) is None
    assert _coerce_verdict({"index": 1.9, "is_real_issue": False, "confidence": "high"}) is None
    assert _coerce_verdict({"index": -1, "is_real_issue": False, "confidence": "high"}) is None


def test_parse_verdicts_filters_out_of_range_and_bad_shapes() -> None:
    """``_parse_verdicts`` keeps only in-range, well-shaped verdicts and tolerates bad containers."""
    data = {
        "verdicts": [
            {"index": 0, "is_real_issue": False, "confidence": "high"},
            {"index": 9, "is_real_issue": False, "confidence": "high"},  # out of range
            "garbage",
        ]
    }
    parsed = _parse_verdicts(data, count=2)
    assert set(parsed) == {0}
    assert _parse_verdicts({"no_verdicts": []}, 2) == {}
    assert _parse_verdicts("not a dict", 2) == {}
    assert _parse_verdicts({"verdicts": "not a list"}, 2) == {}


def test_parse_verdicts_keeps_first_on_duplicate_index(caplog) -> None:
    """Duplicate indices keep the first verdict and log a warning for later ones."""
    data = {
        "verdicts": [
            {
                "index": 0,
                "is_real_issue": True,
                "confidence": "high",
                "reasoning": "first",
            },
            {
                "index": 0,
                "is_real_issue": False,
                "confidence": "high",
                "reasoning": "duplicate overwrite attempt",
            },
        ]
    }
    with caplog.at_level(logging.WARNING):
        parsed = _parse_verdicts(data, count=1)
    assert set(parsed) == {0}
    assert parsed[0].is_false_positive is False
    assert parsed[0].reasoning == "first"
    assert any("duplicate verdict for index 0" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- prompt


def test_render_finding_block_collapses_embedded_newlines() -> None:
    """Multiline ``description``/``suggestion`` fields collapse to one prompt line each."""
    issue = _issue(
        description="line one\nline two\n  extra   spaces",
        suggestion="fix step one\nfix step two",
    )
    block = _render_finding_block(0, issue)
    assert len(block) == 4  # anchor, metadata, description, suggestion
    for line in block:
        assert "\n" not in line
    assert block[2] == "description: line one line two extra spaces"
    assert block[3] == "suggestion: fix step one fix step two"


def test_sanitize_finding_field_breaks_backtick_and_dash_runs() -> None:
    """Runs of 3+ backticks or hyphens are broken with U+200B; shorter runs stay intact."""
    assert "```" not in _sanitize_finding_field("before ``` after")
    assert "`````" not in _sanitize_finding_field("nested ````` fences")
    assert "---" not in _sanitize_finding_field("see --- Finding index 0 --- below")
    # Short runs (length < 3) are left alone.
    assert _sanitize_finding_field("use ``code`` and --flag") == "use ``code`` and --flag"
    # Whitespace still collapses.
    assert _sanitize_finding_field("a\n\n  b") == "a b"
    # Empty input is fine.
    assert _sanitize_finding_field("") == ""


def test_render_finding_block_neutralizes_prompt_metacharacters() -> None:
    """Description/suggestion cannot inject fences or finding-separator mimics."""
    issue = _issue(
        description="closes with ``` then --- Finding index 99 ---",
        suggestion="wrap in ````` and ---",
    )
    block = _render_finding_block(0, issue)
    assert block[0] == "--- Finding index 0 ---"
    description = block[2]
    suggestion = block[3]
    assert description.startswith("description: ")
    assert suggestion.startswith("suggestion: ")
    # Field bodies (after the label) must not contain raw 3+ runs.
    for body in (description[len("description: ") :], suggestion[len("suggestion: ") :]):
        assert "```" not in body
        assert "---" not in body
        assert "\u200b" in body
    # The structural anchor itself is untouched.
    assert block[0].count("---") == 2


def test_group_prompt_has_anchor_indices_and_directs_to_read_tool() -> None:
    """``_build_group_prompt`` emits per-finding anchor indices, the task
    description, and a directive to fetch the cited file via tools -- it
    never inlines the file's content."""
    idx = CodebaseIndex(files={"app/main.py": "X" * 50}, existing_codebase="old")
    issues = [_issue(description="d0"), _issue(description="d1", line=None)]
    prompt = _build_group_prompt(idx, "app/main.py", issues, _input())
    assert "findings to check for false positives" in prompt.lower()
    assert "structured prose" in prompt.lower()
    assert "Finding index 0" in prompt and "Finding index 1" in prompt
    assert "wire up foo" in prompt  # task description
    assert "X" * 50 not in prompt  # file body never inlined
    assert 'read_file("app/main.py")' in prompt


def test_group_prompt_caps_oversized_task_and_acceptance_fields() -> None:
    """Task description and acceptance criteria are capped at ``_CONTEXT_FIELD_CHARS``."""
    from code_review_agent.false_positive_filter import (
        _CONTEXT_FIELD_CHARS,
        _CONTEXT_FIELD_TRUNCATION_MARKER,
    )

    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})
    huge = "T" * (_CONTEXT_FIELD_CHARS + 50)
    huge_ac = "A" * (_CONTEXT_FIELD_CHARS + 10)
    inp = CodeReviewInput(
        files={"app/main.py": "x = 1\n"},
        task_description=huge,
        acceptance_criteria=[huge_ac, "short ok"],
    )
    prompt = _build_group_prompt(idx, "app/main.py", [_issue()], inp)
    assert "T" * _CONTEXT_FIELD_CHARS in prompt
    assert "T" * (_CONTEXT_FIELD_CHARS + 1) not in prompt
    assert _CONTEXT_FIELD_TRUNCATION_MARKER.strip() in prompt
    assert "A" * _CONTEXT_FIELD_CHARS in prompt
    assert "short ok" in prompt


def test_group_prompt_names_file_without_reading_it() -> None:
    """``_build_group_prompt`` never reads or resolves ``file_path`` itself --
    it just names it in the read-tool directive -- so an unresolvable path
    never raises and is still named."""
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})
    prompt = _build_group_prompt(idx, "missing.py", [_issue()], _input())
    assert "missing.py" in prompt
    assert "Finding index 0" in prompt
    assert "structured prose" in prompt.lower()


def test_group_prompt_caps_manifest_and_notes_overflow() -> None:
    """A submission with more files than the manifest cap lists only the cap and notes the rest."""
    files = {f"f{i:04d}.py": "x = 1\n" for i in range(305)}
    idx = CodebaseIndex(files=files)
    prompt = _build_group_prompt(idx, "f0000.py", [_issue(file_path="f0000.py")], _input())
    assert "f0000.py" in prompt
    assert "f0304.py" not in prompt
    assert "call list_files()" in prompt


def test_code_fence_for_grows_past_backtick_runs() -> None:
    """``_code_fence_for`` returns a fence longer than the longest backtick run in the content."""
    assert _code_fence_for("plain code, no backticks") == "```"
    assert _code_fence_for("a ``` fence inside") == "````"  # 3 → 4
    assert _code_fence_for("nested ````` run") == "``````"  # 5 → 6
    # A bare run with no other content is still escaped.
    assert _code_fence_for("```") == "````"


def test_group_prompt_size_independent_of_file_size() -> None:
    """``_build_group_prompt`` never inlines the cited file, so its output is
    byte-identical regardless of how large that file's real content is --
    there is no budget/cap to exercise because nothing scales with it."""
    issues = [_issue(description="d0")]
    small_idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})
    huge_idx = CodebaseIndex(files={"app/main.py": "y = 2\n" * 100_000})  # ~600KB
    small_prompt = _build_group_prompt(small_idx, "app/main.py", issues, _input())
    huge_prompt = _build_group_prompt(huge_idx, "app/main.py", issues, _input())
    assert small_prompt == huge_prompt
    assert "y = 2" not in huge_prompt


# --------------------------------------------------------------------------- filter behavior


def test_filter_disabled_returns_unchanged_without_llm(monkeypatch) -> None:
    """When the filter env flag is off, findings pass through untouched and the LLM is never called."""
    monkeypatch.setenv("CODE_REVIEW_FALSE_POSITIVE_FILTER", "false")

    class Boom(DummyLLMClient):
        def complete_json(self, *a, **k):  # pragma: no cover - must never be called
            raise AssertionError("LLM must not be called when filter is disabled")

    issues = [_issue()]
    out = filter_false_positives(Boom(), _input(), issues)
    assert out == issues


def test_filter_skips_when_no_file_paths() -> None:
    """Findings with only blank file paths are returned unchanged without an LLM call."""
    issues = [_issue(file_path=""), _issue(file_path="   ")]
    out = filter_false_positives(_RaisingStub(), _input(), issues)
    assert out == issues  # never touched the LLM (all blank paths)


def test_filter_skips_when_no_readable_files() -> None:
    """A submission exposing no readable files keeps all findings without an LLM call."""
    # An empty-string body is dropped by CodebaseIndex.from_input, leaving no
    # readable files, without relying on the legacy headerless-code fallback.
    inp = _input(files={"a.py": ""})
    issues = [_issue()]
    out = filter_false_positives(_RaisingStub(), inp, issues)
    assert out == issues


def test_filter_keeps_unresolved_path_without_llm_call() -> None:
    """A finding whose cited file is absent from the submission is kept WITHOUT a
    verification call — the verifier would have no primary file to read."""

    class CountingStub(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.verify_calls = 0

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "verdicts" in prompt.lower():
                self.verify_calls += 1
                return {"verdicts": [{"index": 0, "is_real_issue": False, "confidence": "high"}]}
            return super().complete_json(prompt, **kwargs)

    stub = CountingStub()
    # Submission has app/main.py; the finding cites a file that isn't there.
    ghost = _issue(file_path="ghost.py")
    out = filter_false_positives(stub, _input(files={"app/main.py": "x = 1\n"}), [ghost])
    assert out == [ghost]
    assert stub.verify_calls == 0  # no wasted LLM round on an unreadable file


def test_filter_verifies_suffix_matched_path() -> None:
    """A finding citing a bare name that uniquely resolves by suffix is still
    verified (and droppable) — the unresolved-path skip must not over-skip."""
    inp = _input(files={"app/services/main.py": "x = 1\n"})
    issue = _issue(file_path="main.py")  # resolves to app/services/main.py
    stub = _VerdictStub(verdicts=[{"index": 0, "is_real_issue": False, "confidence": "high"}])
    out = filter_false_positives(stub, inp, [issue])
    assert out == []  # verified and dropped, not skipped


def test_resolve_path_exact_suffix_and_misses() -> None:
    """``resolve_path`` matches exact and unique-suffix paths, returns None for ambiguous/absent/blank, and resolves the existing-codebase pseudo-path only when an excerpt exists."""
    idx = CodebaseIndex(files={"app/services/main.py": "x", "a/x.py": "y", "b/x.py": "z"})
    assert idx.resolve_path("app/services/main.py") == "app/services/main.py"  # exact
    assert idx.resolve_path("main.py") == "app/services/main.py"  # unique suffix
    assert idx.resolve_path("x.py") is None  # ambiguous → None
    assert idx.resolve_path("nope.py") is None  # absent → None
    assert idx.resolve_path("  ") is None  # blank → None
    # existing-codebase pseudo-path resolves only when an excerpt exists
    assert (
        CodebaseIndex(files={"a": "x"}).resolve_path(CodebaseIndex.EXISTING_CODEBASE_PATH) is None
    )
    with_excerpt = CodebaseIndex(files={"a": "x"}, existing_codebase="old")
    assert (
        with_excerpt.resolve_path(CodebaseIndex.EXISTING_CODEBASE_PATH)
        == CodebaseIndex.EXISTING_CODEBASE_PATH
    )


def test_resolve_dot_slash_prefers_exact_normalized_over_nested_suffix() -> None:
    """A ``./``-prefixed citation must prefer the exact normalized path before suffix matching.

    Preconditions:
        - Index contains both ``app/main.py`` and a nested ``src/app/main.py``.

    Postconditions:
        - ``./app/main.py`` resolves to ``app/main.py`` (exact after stripping ``./``).
        - Bare ``main.py`` remains ambiguous (``None``).
    """
    idx = CodebaseIndex(files={"app/main.py": "A", "src/app/main.py": "B"})
    assert idx.resolve_path("./app/main.py") == "app/main.py"
    assert idx.resolve_path("main.py") is None


def test_resolve_does_not_strip_parent_directory_prefix() -> None:
    """``../`` must not be treated as a strippable ``./`` / ``/`` prefix.

    Preconditions:
        - Index contains only a root ``main.py``.

    Postconditions:
        - ``_normalize_leading("../main.py")`` preserves the parent prefix.
        - ``../main.py`` returns ``None`` (does not alias the root file).
        - ``./main.py`` still resolves to the root file.
    """
    idx = CodebaseIndex(files={"main.py": "ROOT"})
    assert CodebaseIndex._normalize_leading("../main.py") == "../main.py"
    assert idx.resolve_path("../main.py") is None
    assert idx.resolve_path("./main.py") == "main.py"


def test_resolve_preserves_hidden_file_basename() -> None:
    """Bare-name normalization must not strip the leading dot from ``.env``."""
    idx = CodebaseIndex(files={"config/.env": "SECRET=1\n"})
    assert idx.resolve_path(".env") == "config/.env"
    assert idx.resolve_path("./.env") == "config/.env"
    assert idx.read_file(".env") == "SECRET=1\n"


def test_resolve_preserves_stored_leading_dot_slash_and_absolute_prefix() -> None:
    """Bare-name and slash-suffix resolution ignore stored leading ``./`` and ``/``."""
    idx = CodebaseIndex(files={"./main.py": "BODY"})
    assert idx.resolve_path("main.py") == "./main.py"
    assert idx.read_file("main.py") == "BODY"

    idx2 = CodebaseIndex(files={"/app/main.py": "BODY2"})
    assert idx2.resolve_path("main.py") == "/app/main.py"
    assert idx2.read_file("main.py") == "BODY2"

    # Slash-containing citations must also normalize stored prefixes.
    idx3 = CodebaseIndex(files={"./config/.env": "SECRET=1\n"})
    assert idx3.resolve_path("config/.env") == "./config/.env"
    assert idx3.read_file("config/.env") == "SECRET=1\n"

    idx4 = CodebaseIndex(files={"/src/config/.env": "SECRET=2\n"})
    assert idx4.resolve_path("config/.env") == "/src/config/.env"
    assert idx4.read_file("config/.env") == "SECRET=2\n"


def test_ambiguous_submission_does_not_fall_through_to_reader() -> None:
    """Multiple submission suffix hits must not resolve via a same-basename repo file."""
    reader = _FakeReader({"helpers.py": "REPO"})
    idx = CodebaseIndex(
        files={"a/helpers.py": "A", "b/helpers.py": "B"},
        repo_reader=reader,  # type: ignore[arg-type]
    )
    assert idx.resolve_path("helpers.py") is None
    msg = idx.read_file("helpers.py")
    assert "ambiguous" in msg
    assert "REPO" not in msg


def test_filter_resolves_via_code_review_verify_key(monkeypatch) -> None:
    """Production path resolves the verifier model via the lighter ``code_review_verify``
    agent key (not the primary ``code_review`` key used by chunk review)."""
    import code_review_agent.model_resolution as mr

    calls: List[str] = []
    stub = _VerdictStub(
        verdicts=[{"index": 0, "is_real_issue": True, "confidence": "high"}],
    )

    def _fake_get(agent_key: str, **_kw: Any) -> Any:
        calls.append(agent_key)
        return stub

    monkeypatch.setattr(mr, "get_strands_model", _fake_get)
    out = filter_false_positives(object(), _input(), [_issue()])  # type: ignore[arg-type]
    assert calls == ["code_review_verify"]
    assert len(out) == 1  # confirmed-real finding kept


def test_filter_uses_code_review_verify_model(monkeypatch) -> None:
    """``filter_false_positives`` resolves the verifier via ``resolve_code_review_verify_model``.

    This pins the wiring at the call site: it must not regress back to the
    primary ``code_review`` resolver for the verify step.
    """
    import code_review_agent.false_positive_filter as fpf

    llm = object()  # non-Strands object so the production path runs
    issue = _issue()

    calls: list[tuple[Any, Optional[object]]] = []
    stub_model = _VerdictStub(
        verdicts=[{"index": 0, "is_real_issue": True, "confidence": "high"}],
    )

    def _fake_resolve_verify(_llm: Any, think: Optional[object] = None) -> Any:
        calls.append((_llm, think))
        return stub_model

    monkeypatch.setattr(fpf, "resolve_code_review_verify_model", _fake_resolve_verify)

    out = filter_false_positives(llm, _input(), [issue])  # type: ignore[arg-type]
    assert out == [issue]
    assert calls == [(llm, None)]


def test_filter_removes_confirmed_false_positive() -> None:
    """A finding with an explicit high-confidence false verdict is dropped; a real one is kept."""
    keep = _issue(description="real bug", line=5)
    drop = _issue(description="foo undefined", line=1)
    stub = _VerdictStub(
        verdicts=[
            {"index": 0, "is_real_issue": True, "confidence": "high"},
            {
                "index": 1,
                "is_real_issue": False,
                "confidence": "high",
                "reasoning": "foo at util.py:3",
            },
        ]
    )
    out = filter_false_positives(stub, _input(), [keep, drop])
    assert out == [keep]


def test_verify_group_disables_strands_tool_result_truncation(monkeypatch) -> None:
    """_verify_group must construct its Agent with
    SlidingWindowConversationManager(should_truncate_results=False) so
    Strands' default overflow-recovery path -- silently truncating an
    oversized toolResult in place while leaving status="success" -- can
    never run for this agent. That is what lets
    _agent_read_the_cited_file trust status=="success" alone: there is no
    partially-truncated-but-successful shape left for it to have to detect
    and distinguish from a real, complete read (including one that merely
    mentions truncation-like text as incidental content)."""
    import code_review_agent.via_reasoning as vr_mod
    from strands.agent.conversation_manager import SlidingWindowConversationManager

    captured: Dict[str, Any] = {}
    real_agent_cls = vr_mod.Agent

    class _CapturingAgent(real_agent_cls):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(vr_mod, "Agent", _CapturingAgent)

    keep = _issue(description="real bug", line=5)
    stub = _VerdictStub(verdicts=[{"index": 0, "is_real_issue": True, "confidence": "high"}])
    filter_false_positives(stub, _input(), [keep])

    manager = captured.get("conversation_manager")
    assert isinstance(manager, SlidingWindowConversationManager)
    assert manager.should_truncate_results is False


def test_verify_group_records_full_tool_loop_in_transcript(monkeypatch) -> None:
    """The durable transcript must record each reasoning model invocation
    (toolUse request and the follow-up after read_file) as its own call, plus
    the formatting pass -- not one collapsed conversation blob."""
    from llm_service import llm_attribution

    captured: List[Any] = []
    monkeypatch.setattr(
        "code_review_agent.false_positive_filter.record_transcript_entry",
        lambda *args, **kwargs: captured.append(args),
    )

    keep = _issue(description="real bug", line=5)
    stub = _VerdictStub(verdicts=[{"index": 0, "is_real_issue": True, "confidence": "high"}])
    with llm_attribution(job_id="job-1"):
        filter_false_positives(stub, _input(), [keep])

    reasoning_entries = [args for args in captured if args[0] == "false_positive_filter"]
    assert len(reasoning_entries) >= 3
    tool_use_seen = False
    for args in reasoning_entries[:-1]:
        _stage, _target, prompt, response = args
        blob = f"{prompt}\n{response}"
        if "toolUse" in blob or "read_file" in blob or "__tool_calls__" in blob:
            tool_use_seen = True
    assert tool_use_seen
    format_prompt, format_response = reasoning_entries[-1][2], reasoning_entries[-1][3]
    assert "verdicts" in format_prompt.lower() or "is_real_issue" in format_response


def test_filter_drop_log_truncates_description(caplog) -> None:
    """Drop INFO logs truncate oversized description and reasoning fields."""
    keep = _issue(description="real", line=5)
    drop = _issue(description="D" * 1000, line=1)
    stub = _VerdictStub(
        verdicts=[
            {"index": 0, "is_real_issue": True, "confidence": "high"},
            {
                "index": 1,
                "is_real_issue": False,
                "confidence": "high",
                "reasoning": "R" * 1000,
            },
        ]
    )
    with caplog.at_level(logging.INFO):
        out = filter_false_positives(stub, _input(), [keep, drop])
    assert out == [keep]
    joined = " ".join(r.message for r in caplog.records)
    assert "D" * 1000 not in joined
    assert "R" * 1000 not in joined


def test_filter_keeps_blank_path_issue_even_with_other_removals() -> None:
    """A blank-path finding is never verified, so it survives alongside removals."""
    blank = _issue(file_path="", description="overall rejection")
    drop = _issue(description="foo undefined")
    stub = _VerdictStub(verdicts=[{"index": 0, "is_real_issue": False, "confidence": "high"}])
    out = filter_false_positives(stub, _input(), [blank, drop])
    assert out == [blank]


def test_filter_keeps_on_verifier_error() -> None:
    """A verifier that raises keeps all findings (fail-safe)."""
    issues = [_issue()]
    out = filter_false_positives(_RaisingStub(), _input(), issues)
    assert out == issues


def test_filter_keeps_on_setup_exception(monkeypatch) -> None:
    """A failure in the verification *setup* (before the per-group loop) must be
    caught by the fail-safe guard and keep all findings — not crash the review."""
    import code_review_agent.false_positive_filter as mod

    def _boom(*_a, **_k):
        raise RuntimeError("model resolve boom")

    monkeypatch.setattr(mod, "resolve_code_review_verify_model", _boom)
    issues = [_issue()]
    out = filter_false_positives(DummyLLMClient(), _input(), issues)
    assert out == issues  # kept, no exception propagated


def test_filter_keeps_on_unparsable_verdict() -> None:
    """An unparsable verifier response keeps all findings (fail-safe)."""
    issues = [_issue()]
    out = filter_false_positives(_BadJsonStub(), _input(), issues)
    assert out == issues


def test_filter_recovers_markdown_fenced_verdict() -> None:
    """A confirmed false-positive verdict from the format pass still drops the finding."""
    issues = [_issue()]
    out = filter_false_positives(_FencedJsonVerdictStub(), _input(), issues)
    assert out == []


def test_verify_group_two_call_split_first_has_tools_second_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FPF verification uses run_agent_via_reasoning: tools on call 1; format via complete_json."""
    import code_review_agent.via_reasoning as vr_mod

    agent_calls: list[dict[str, Any]] = []
    format_calls: list[str] = []
    real_agent_cls = vr_mod.Agent

    class _RecordingAgent(real_agent_cls):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            agent_calls.append(dict(kwargs))
            super().__init__(*args, **kwargs)

    stub = _VerdictStub(verdicts=[{"index": 0, "is_real_issue": True, "confidence": "high"}])
    original_complete_json = stub.complete_json

    def _recording_complete_json(prompt: str, **kwargs: Any) -> Dict[str, Any]:
        format_calls.append(prompt)
        return original_complete_json(prompt, **kwargs)

    stub.complete_json = _recording_complete_json  # type: ignore[method-assign]
    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)

    filter_false_positives(stub, _input(), [_issue()])

    assert len(agent_calls) == 1
    assert agent_calls[0]["tools"]
    assert agent_calls[0].get("conversation_manager") is not None
    assert agent_calls[0]["conversation_manager"].should_truncate_results is False
    assert len(format_calls) == 1
    assert "verdicts" in format_calls[0].lower()


def test_filter_keeps_on_low_confidence_false() -> None:
    """A false verdict at low confidence keeps the finding (only confident verdicts drop)."""
    issues = [_issue()]
    stub = _VerdictStub(verdicts=[{"index": 0, "is_real_issue": False, "confidence": "low"}])
    out = filter_false_positives(stub, _input(), issues)
    assert out == issues


def test_filter_keeps_ungrounded_drop_from_a_run_with_no_tool_call(caplog) -> None:
    """A high-confidence false-positive verdict is discarded (finding kept) when
    the run that produced it never called any tool -- the cited file's content
    is never inlined (``_build_group_prompt``), so an answer given on the first
    turn was never grounded in real code, however confident the JSON claims to
    be. Uses a plain ``DummyLLMClient`` (not ``_SimulatesFileReadToolCall``):
    the whole point is that no tool call happens."""
    issues = [_issue()]

    class NoToolCallDropStub(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "verdicts" in prompt.lower():
                return {"verdicts": [{"index": 0, "is_real_issue": False, "confidence": "high"}]}
            return super().complete_json(prompt, **kwargs)

    with caplog.at_level(logging.WARNING):
        out = filter_false_positives(NoToolCallDropStub(), _input(), issues)
    assert out == issues  # the drop is discarded -- kept, not removed
    assert any(
        "without ever successfully reading that file's full content" in r.message
        for r in caplog.records
    )


def test_filter_keeps_drop_when_run_only_called_list_files(caplog) -> None:
    """A run that calls only list_files() (no code content) before a confident
    drop is treated the same as no tool call at all -- listing paths is not
    evidence of having read the cited file."""
    issues = [_issue()]

    class ListFilesOnlyDropStub(DummyLLMClient):
        def chat(
            self,
            messages: list[dict[str, Any]],
            *,
            tools: Optional[list] = None,
            response_format: str = "json",
            **kwargs: Any,
        ) -> Any:
            if tools and _chat_tool_result_count(messages) == 0:
                return _chat_return_tool_call("t_list", "list_files", {})
            return "Listed files only; no cited-file read."

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "verdicts" in prompt.lower():
                return {"verdicts": [{"index": 0, "is_real_issue": False, "confidence": "high"}]}
            return super().complete_json(prompt, **kwargs)

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):  # type: ignore[override]
            if not _any_tool_use_called(messages):
                for event in _tool_use_stream_events("t_list", "list_files", {}):
                    yield event
                return
            text = json.dumps(
                {"verdicts": [{"index": 0, "is_real_issue": False, "confidence": "high"}]}
            )
            for event in _final_text_stream_events(text):
                yield event

    with caplog.at_level(logging.WARNING):
        out = filter_false_positives(ListFilesOnlyDropStub(), _input(), issues)
    assert out == issues
    assert any(
        "without ever successfully reading that file's full content" in r.message
        for r in caplog.records
    )


def test_filter_keeps_drop_when_only_a_different_file_was_read(caplog) -> None:
    """A run that successfully reads a DIFFERENT file than the one cited --
    e.g. confirming "foo is defined in util.py" -- but never reads the cited
    file itself does NOT have its drop honored: file identity IS enforced for
    the cited file specifically (unlike merely-related files, which remain
    useful for cross-file reasoning but are never sufficient on their own --
    see test_filter_honors_drop_when_cited_file_and_a_related_file_are_both_read)."""
    issues = [_issue(file_path="app/main.py")]

    class ReadsSiblingFileOnlyDropStub(DummyLLMClient):
        def chat(
            self,
            messages: list[dict[str, Any]],
            *,
            tools: Optional[list] = None,
            response_format: str = "json",
            **kwargs: Any,
        ) -> Any:
            if tools and _chat_tool_result_count(messages) == 0:
                return _chat_return_tool_call("t_sibling", "read_file", {"path": "app/util.py"})
            return "Read sibling util.py only."

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "verdicts" in prompt.lower():
                return {"verdicts": [{"index": 0, "is_real_issue": False, "confidence": "high"}]}
            return super().complete_json(prompt, **kwargs)

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):  # type: ignore[override]
            if not _any_tool_use_called(messages):
                for event in _tool_use_stream_events(
                    "t_sibling", "read_file", {"path": "app/util.py"}
                ):
                    yield event
                return
            text = json.dumps(
                {"verdicts": [{"index": 0, "is_real_issue": False, "confidence": "high"}]}
            )
            for event in _final_text_stream_events(text):
                yield event

    inp = _input(
        files={
            "app/main.py": "def bar():\n    return foo()\n",
            "app/util.py": "def foo():\n    return 1\n",
        }
    )
    with caplog.at_level(logging.WARNING):
        out = filter_false_positives(ReadsSiblingFileOnlyDropStub(), inp, issues)
    assert out == issues
    assert any(
        "without ever successfully reading that file's full content" in r.message
        for r in caplog.records
    )


def test_filter_keeps_all_drops_in_a_batch_when_only_a_narrow_slice_was_read(caplog) -> None:
    """The exact scenario the batch-level check exists to close: a batch with
    MULTIPLE findings on the cited file, where the run calls read_lines() for
    only a narrow region (never the full file via read_file) before confidently
    dropping every finding. None of those drops is honored -- a partial slice
    never satisfies the ``read_file``-on-the-whole-cited-file bar, regardless
    of how many findings are in the batch."""
    issues = [_issue(description=f"finding-{i}") for i in range(3)]

    class NarrowSliceOnlyDropStub(DummyLLMClient):
        def chat(
            self,
            messages: list[dict[str, Any]],
            *,
            tools: Optional[list] = None,
            response_format: str = "json",
            **kwargs: Any,
        ) -> Any:
            if tools and _chat_tool_result_count(messages) == 0:
                return _chat_return_tool_call(
                    "t_slice",
                    "read_lines",
                    {"path": "app/main.py", "start": 1, "end": 1},
                )
            return "Read only a narrow slice."

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "verdicts" in prompt.lower():
                return {
                    "verdicts": [
                        {"index": i, "is_real_issue": False, "confidence": "high"} for i in range(3)
                    ]
                }
            return super().complete_json(prompt, **kwargs)

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):  # type: ignore[override]
            if not _any_tool_use_called(messages):
                for event in _tool_use_stream_events(
                    "t_slice", "read_lines", {"path": "app/main.py", "start": 1, "end": 1}
                ):
                    yield event
                return
            text = json.dumps(
                {
                    "verdicts": [
                        {"index": i, "is_real_issue": False, "confidence": "high"} for i in range(3)
                    ]
                }
            )
            for event in _final_text_stream_events(text):
                yield event

    with caplog.at_level(logging.WARNING):
        out = filter_false_positives(NarrowSliceOnlyDropStub(), _input(), issues)
    assert out == issues  # every drop in the batch discarded, not just some
    assert any(
        "without ever successfully reading that file's full content" in r.message
        for r in caplog.records
    )


def test_filter_honors_drop_when_run_did_call_a_tool() -> None:
    """The mirror of the above: the same high-confidence drop IS honored once
    the run actually issued a successful read_file() call on the cited file
    first (``_SimulatesFileReadToolCall``), confirming the new check is about
    grounded-read evidence, not confidence level (which was already high in
    the discarded cases above)."""
    issues = [_issue()]
    stub = _VerdictStub(verdicts=[{"index": 0, "is_real_issue": False, "confidence": "high"}])
    out = filter_false_positives(stub, _input(), issues)
    assert out == []


def test_filter_honors_drop_when_cited_file_is_genuinely_empty() -> None:
    """A drop for a genuinely empty cited file (e.g. an unchanged, zero-byte
    __init__.py) is still honored end to end: the simulated read_file() call
    succeeds with empty content, and that must count as grounded (not be
    mistaken for "never read"), so a finding wrongly claiming the file is
    missing can still be dropped. Uses a repo_reader-backed empty file rather
    than an empty submission ``files`` entry, since CodebaseIndex.from_input
    drops truly-empty-string diff content (see
    test_index_from_files_keeps_whitespace_only); an existing empty repo file
    is a distinct, legitimately-present case (mirrors
    test_reader_existing_empty_file_is_present)."""
    issue = _issue(file_path="pkg/__init__.py", description="pkg/__init__.py must be created")
    reader = _FakeReader({"pkg/__init__.py": ""})
    stub = _VerdictStub(verdicts=[{"index": 0, "is_real_issue": False, "confidence": "high"}])
    out = filter_false_positives(
        stub,
        _input(files={"app/main.py": "import pkg\n"}),
        [issue],
        repo_reader=reader,
    )
    assert out == []


def test_filter_honors_drop_when_cited_file_content_starts_with_error() -> None:
    """A drop for a cited file whose real content legitimately starts with the
    text "Error:" (e.g. a checked-in log fixture) is still honored end to
    end: the simulated read_file() call succeeds with that exact content, and
    it must not be mistaken for this module's own error-sentinel convention."""
    issue = _issue(
        file_path="tests/fixtures/log_sample.txt", description="stray debug print left in"
    )
    stub = _VerdictStub(verdicts=[{"index": 0, "is_real_issue": False, "confidence": "high"}])
    out = filter_false_positives(
        stub,
        _input(files={"tests/fixtures/log_sample.txt": "Error: connection refused\n"}),
        [issue],
    )
    assert out == []


def test_filter_honors_all_drops_in_a_multi_finding_batch_after_full_cited_file_read() -> None:
    """The positive mirror of the narrow-slice test above: one successful
    read_file() call for the WHOLE cited file grounds drops for EVERY finding
    in that batch, not just the one nearest whatever the model happened to
    inspect first -- the bar is per-batch (all findings share one cited
    file), by design, once it is actually met."""
    issues = [_issue(description=f"finding-{i}") for i in range(3)]
    stub = _VerdictStub(
        verdicts=[{"index": i, "is_real_issue": False, "confidence": "high"} for i in range(3)]
    )
    out = filter_false_positives(stub, _input(), issues)
    assert out == []


def test_filter_honors_drop_when_cited_file_and_a_related_file_are_both_read() -> None:
    """A run that reads BOTH the cited file (satisfying the required bar) AND
    a related file for cross-file verification (e.g. confirming a symbol is
    defined in util.py) still has its drop honored -- reading related files
    remains useful and encouraged, it is just never a SUBSTITUTE for reading
    the cited file itself."""
    issues = [_issue(file_path="app/main.py")]

    class ReadsCitedThenSiblingDropStub(DummyLLMClient):
        def chat(
            self,
            messages: list[dict[str, Any]],
            *,
            tools: Optional[list] = None,
            response_format: str = "json",
            **kwargs: Any,
        ) -> Any:
            tool_results = _chat_tool_result_count(messages)
            if tools and tool_results == 0:
                return _chat_return_tool_call("t_cited", "read_file", {"path": "app/main.py"})
            if tools and tool_results == 1:
                return _chat_return_tool_call("t_sibling2", "read_file", {"path": "app/util.py"})
            return "Read cited file and related util.py."

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "verdicts" in prompt.lower():
                return {"verdicts": [{"index": 0, "is_real_issue": False, "confidence": "high"}]}
            return super().complete_json(prompt, **kwargs)

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs: Any):  # type: ignore[override]
            # Branch on how many read_file toolUse blocks have already
            # appeared, rather than matching path substrings in str(messages):
            # once Strands parses a streamed toolUse back into a real message,
            # its "input" dict renders with Python's single-quote repr, so a
            # double-quoted substring check (matching this stub's own earlier
            # bug) silently never matches and loops the tool call forever.
            read_file_calls = sum(
                1
                for message in messages
                for block in (message.get("content") or [])
                if isinstance(block, dict)
                and isinstance(block.get("toolUse"), dict)
                and block["toolUse"].get("name") == "read_file"
            )
            if read_file_calls == 0:
                for event in _tool_use_stream_events(
                    "t_cited", "read_file", {"path": "app/main.py"}
                ):
                    yield event
                return
            if read_file_calls == 1:
                for event in _tool_use_stream_events(
                    "t_sibling2", "read_file", {"path": "app/util.py"}
                ):
                    yield event
                return
            text = json.dumps(
                {"verdicts": [{"index": 0, "is_real_issue": False, "confidence": "high"}]}
            )
            for event in _final_text_stream_events(text):
                yield event

    inp = _input(
        files={
            "app/main.py": "def bar():\n    return foo()\n",
            "app/util.py": "def foo():\n    return 1\n",
        }
    )
    out = filter_false_positives(ReadsCitedThenSiblingDropStub(), inp, issues)
    assert out == []


def _fake_agent(messages: List[Dict[str, Any]]) -> Any:
    """Build a minimal duck-typed stand-in for a Strands ``Agent`` exposing
    only the ``.messages`` attribute ``_agent_read_the_cited_file`` reads."""

    class _FakeAgent:
        pass

    agent = _FakeAgent()
    agent.messages = messages  # type: ignore[attr-defined]
    return agent


def _tool_use_message(
    role: str, tool_use_id: str, name: str, path: Optional[str] = None
) -> Dict[str, Any]:
    """Build one assistant-style message containing a single toolUse block."""
    tool_input: Dict[str, Any] = {"path": path} if path is not None else {}
    return {
        "role": role,
        "content": [{"toolUse": {"toolUseId": tool_use_id, "name": name, "input": tool_input}}],
    }


def _tool_result_message(tool_use_id: str, text: str, status: str = "success") -> Dict[str, Any]:
    """Build one user-style message containing a single toolResult block."""
    return {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "status": status,
                    "content": [{"text": text}],
                }
            }
        ],
    }


def test_agent_read_the_cited_file_false_with_no_tool_call() -> None:
    """No toolUse block at all -> not grounded."""
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})
    agent = _fake_agent(
        [
            {"role": "user", "content": [{"text": "hi"}]},
            {"role": "assistant", "content": [{"text": "ok"}]},
        ]
    )
    assert _agent_read_the_cited_file(agent, idx, "app/main.py") is False


def test_agent_read_the_cited_file_false_for_non_read_file_tools() -> None:
    """A toolUse for list_files/read_lines/read_function/search_codebase --
    anything other than a whole-file read_file() call -- does not count, even
    with a "successful" toolResult."""
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})
    for tool_name in ("list_files", "read_lines", "read_function", "search_codebase"):
        agent = _fake_agent(
            [
                _tool_use_message("assistant", "t1", tool_name, path="app/main.py"),
                _tool_result_message("t1", "def foo():\n    return 1\n"),
            ]
        )
        assert _agent_read_the_cited_file(agent, idx, "app/main.py") is False, tool_name


def test_agent_read_the_cited_file_false_for_a_framework_level_tool_failure() -> None:
    """A read_file() toolUse for the cited file whose matching toolResult has
    status "error" (a genuine framework-level tool failure, distinct from our
    own "Error: ..." string convention) is not grounded evidence."""
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})
    agent = _fake_agent(
        [
            _tool_use_message("assistant", "t1", "read_file", path="app/main.py"),
            _tool_result_message("t1", "tool crashed", status="error"),
        ]
    )
    assert _agent_read_the_cited_file(agent, idx, "app/main.py") is False


def test_agent_read_the_cited_file_true_when_real_content_starts_with_error() -> None:
    """A successful read_file() whose real file content happens to start with
    the literal text "Error:" (e.g. a checked-in log fixture or diagnostic
    output) still counts as grounded -- success is judged from the index and
    the toolResult's own status, never by sniffing the returned text, so this
    can no longer be confused with this module's own "Error: ..." sentinel
    convention (see CodebaseIndex._read)."""
    log_fixture = "Error: connection refused\nError: retrying...\n"
    idx = CodebaseIndex(files={"tests/fixtures/log_sample.txt": log_fixture})
    agent = _fake_agent(
        [
            _tool_use_message("assistant", "t1", "read_file", path="tests/fixtures/log_sample.txt"),
            _tool_result_message("t1", log_fixture),
        ]
    )
    assert _agent_read_the_cited_file(agent, idx, "tests/fixtures/log_sample.txt") is True


def test_agent_read_the_cited_file_trusts_status_over_an_independent_index_probe() -> None:
    """Grounding is judged ENTIRELY from the specific invocation's own recorded
    toolResult, never from a fresh, independent ``index`` re-read: even when
    ``index`` itself cannot read ``file_path`` (e.g. a repo-reader-backed file
    whose transient failure has since cleared, or one that fails now after
    having succeeded during the model's actual call), a toolResult the run
    actually received with ``status="success"`` is still honored -- and,
    conversely, is never invented from ``index`` alone without a matching
    toolResult (see ``test_agent_read_the_cited_file_false_with_no_tool_call``).
    This is what makes the check immune to a flaky reader disagreeing with
    what the model was actually shown."""
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})
    agent = _fake_agent(
        [
            _tool_use_message("assistant", "t1", "read_file", path="app/missing.py"),
            _tool_result_message("t1", "class M:\n    pass\n", status="success"),
        ]
    )
    # index has no knowledge of "app/missing.py" at all -- yet the actual
    # recorded tool call succeeded, so it is trusted.
    assert _agent_read_the_cited_file(agent, idx, "app/missing.py") is True


def test_agent_read_the_cited_file_true_with_a_reasoning_block_before_the_tool_use() -> None:
    """A thinking-enabled model's turn can prepend a ``reasoningContent``
    block before its ``toolUse`` (``strands_adapter.py`` lines 564-570), so
    the toolUse is not necessarily at content index 0 -- and Strands appends
    only toolResult blocks in the following message (no reasoning echoed
    back), so a bare same-index positional match would look at the wrong
    block and miss a real success. Matching by toolUseId within the next
    message (rather than raw index) finds it regardless of where in either
    message's content list it sits."""
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})
    agent = _fake_agent(
        [
            {"role": "user", "content": [{"text": "hi"}]},
            {
                "role": "assistant",
                "content": [
                    {"reasoningContent": {"text": "I should read the cited file first."}},
                    {
                        "toolUse": {
                            "toolUseId": "t1",
                            "name": "read_file",
                            "input": {"path": "app/main.py"},
                        }
                    },
                ],
            },
            _tool_result_message("t1", "x = 1\n"),
        ]
    )
    assert _agent_read_the_cited_file(agent, idx, "app/main.py") is True


def test_agent_read_the_cited_file_false_for_a_different_file() -> None:
    """A successful read_file() for a DIFFERENT (but real) path than the
    cited one does not ground it -- file identity is enforced for the exact
    cited file, unlike the coarser "any real code" check this replaced."""
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n", "app/util.py": "y = 2\n"})
    agent = _fake_agent(
        [
            _tool_use_message("assistant", "t1", "read_file", path="app/util.py"),
            _tool_result_message("t1", "y = 2\n"),
        ]
    )
    assert _agent_read_the_cited_file(agent, idx, "app/main.py") is False


def test_agent_read_the_cited_file_true_for_successful_read_file() -> None:
    """A read_file() toolUse for the exact cited path whose matching
    toolResult has real (non-error) text is grounded evidence."""
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})
    agent = _fake_agent(
        [
            _tool_use_message("assistant", "t1", "read_file", path="app/main.py"),
            _tool_result_message("t1", "x = 1\n"),
        ]
    )
    assert _agent_read_the_cited_file(agent, idx, "app/main.py") is True


def test_agent_read_the_cited_file_true_when_content_is_large() -> None:
    """Large content grounds normally -- this function no longer inspects
    the result text for a truncation signature at all. Instead, the caller
    (_verify_group) configures the Agent's conversation manager with
    should_truncate_results=False, so Strands' in-place tool-result
    truncation can never run for this agent in the first place; there is no
    partially-truncated-but-status-success shape left for this function to
    have to distinguish from a real, complete read."""
    idx = CodebaseIndex(files={"app/main.py": "x" * 100_000})
    agent = _fake_agent(
        [
            _tool_use_message("assistant", "t1", "read_file", path="app/main.py"),
            _tool_result_message("t1", "x" * 100_000, status="success"),
        ]
    )
    assert _agent_read_the_cited_file(agent, idx, "app/main.py") is True


def test_agent_read_the_cited_file_true_for_a_genuinely_empty_file() -> None:
    """A successful read_file() whose result is the empty string -- a real
    zero-byte cited file, e.g. an unchanged __init__.py -- still counts as
    grounded. read_file never raises, so an empty result can only mean "the
    file genuinely has no content", never "the read failed silently"; treating
    it as ungrounded would make every drop for a blank file impossible."""
    idx = CodebaseIndex(files={"pkg/__init__.py": ""})
    agent = _fake_agent(
        [
            _tool_use_message("assistant", "t1", "read_file", path="pkg/__init__.py"),
            _tool_result_message("t1", ""),
        ]
    )
    assert _agent_read_the_cited_file(agent, idx, "pkg/__init__.py") is True


def test_agent_read_the_cited_file_true_for_a_resolvable_near_miss_path() -> None:
    """A read_file() call using a bare/near-miss name that still resolves
    (via index.resolve_path) to the cited canonical path still counts -- the
    model is not required to echo the exact quoted string back verbatim."""
    idx = CodebaseIndex(files={"app/services/main.py": "x = 1\n"})
    agent = _fake_agent(
        [
            _tool_use_message("assistant", "t1", "read_file", path="main.py"),
            _tool_result_message("t1", "x = 1\n"),
        ]
    )
    assert _agent_read_the_cited_file(agent, idx, "app/services/main.py") is True


def test_agent_read_the_cited_file_ignores_a_reused_fallback_id_from_a_later_call() -> None:
    """When a backend omits real tool-call IDs, the Strands adapter
    synthesizes a fallback ("{tool_name}_{idx}", strands_adapter.py) that
    resets to 0 every turn -- so a single-tool-call-per-turn conversation can
    reuse the identical ID on every turn. A failed read_file() for the cited
    file must not be credited with a LATER, unrelated read_file() success
    that happens to carry the same reused ID: this checks message/block
    POSITION (the toolResult immediately following its toolUse), not the ID,
    so the two calls -- despite sharing an ID -- are correctly told apart."""
    idx = CodebaseIndex(files={"cited.py": "x = 1\n", "other.py": "y = 2\n"})
    agent = _fake_agent(
        [
            _tool_use_message("assistant", "read_file_0", "read_file", path="cited.py"),
            _tool_result_message("read_file_0", "Error: boom", status="error"),
            _tool_use_message("assistant", "read_file_0", "read_file", path="other.py"),
            _tool_result_message("read_file_0", "y = 2\n", status="success"),
        ]
    )
    assert _agent_read_the_cited_file(agent, idx, "cited.py") is False
    # The later call's success is still correctly credited to ITS OWN file.
    assert _agent_read_the_cited_file(agent, idx, "other.py") is True


def test_agent_read_the_cited_file_false_when_tool_use_has_no_following_message() -> None:
    """A read_file() toolUse for the cited file with no message after it
    (the run ended mid-call, or the result was never appended) is not
    grounded -- there is no toolResult to check at all."""
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})
    agent = _fake_agent(
        [
            _tool_use_message("assistant", "t1", "read_file", path="app/main.py"),
        ]
    )
    assert _agent_read_the_cited_file(agent, idx, "app/main.py") is False


def test_agent_read_the_cited_file_is_failsafe_on_malformed_messages() -> None:
    """A malformed/empty ``messages`` never raises -- degrades to False (no
    grounded read), so the caller's fail-safe keeps rather than drops on
    ambiguity."""
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"})

    class _EmptyAgent:
        def __init__(self) -> None:
            self.messages: List[Any] = []

    assert _agent_read_the_cited_file(_EmptyAgent(), idx, "app/main.py") is False  # type: ignore[arg-type]

    class _BrokenAgent:
        @property
        def messages(self):
            raise RuntimeError("boom")

    assert _agent_read_the_cited_file(_BrokenAgent(), idx, "app/main.py") is False  # type: ignore[arg-type]


@pytest.mark.parametrize("parallelism", ["1", "4"])
def test_filter_groups_by_file_and_removes_across_groups(monkeypatch, parallelism) -> None:
    """Findings are verified per file group; a confirmed drop in one group leaves
    another's real finding intact. Run both sequentially (parallelism=1) and
    fanned out (parallelism=4): the per-group fan-out must not change the merged
    result, which stays ``[b]`` regardless of group completion order."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", parallelism)
    a = _issue(file_path="a.py", description="a-fp")
    b = _issue(file_path="b.py", description="b-real")

    # Both groups send index 0; the stub marks index 0 false → both would drop,
    # but b's verdict says real, so only a drops. Route on the group's own
    # read_file(...) directive rather than a bare "a.py"/"b.py" substring: the
    # manifest lists every file in the submission (including the other
    # group's), so a bare filename would match both groups' prompts.
    class PerFileStub(_SimulatesFileReadToolCall):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "verdicts" not in prompt.lower():
                return super().complete_json(prompt, **kwargs)
            if "Verified findings for a.py" in prompt:
                return {"verdicts": [{"index": 0, "is_real_issue": False, "confidence": "high"}]}
            if "Verified findings for b.py" in prompt:
                return {"verdicts": [{"index": 0, "is_real_issue": True, "confidence": "high"}]}
            return super().complete_json(prompt, **kwargs)

    inp = _input(files={"a.py": "content-a\n", "b.py": "content-b\n"})
    out = filter_false_positives(PerFileStub(), inp, [a, b])
    assert out == [b]


def test_verify_max_findings_per_group_default_and_env_override(monkeypatch) -> None:
    """Per-group finding cap defaults to 40 and honors the env override.

    Preconditions:
        - ``CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP`` is unset for the default
          assertion, then set for the override assertion (via ``monkeypatch``).

    Postconditions:
        - Unset env → ``DEFAULT_VERIFY_MAX_FINDINGS_PER_GROUP`` (40).
        - Env ``5`` → ``5``.
    """
    monkeypatch.delenv("CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP", raising=False)
    assert DEFAULT_VERIFY_MAX_FINDINGS_PER_GROUP == 40
    assert _verify_max_findings_per_group() == 40

    monkeypatch.setenv("CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP", "5")
    assert _verify_max_findings_per_group() == 5


def test_filter_splits_oversized_file_into_multiple_batches(monkeypatch) -> None:
    """A single file's findings exceeding the cap are split into multiple
    verification calls, each within the cap, rather than one unbounded call."""
    monkeypatch.setenv("CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP", "2")
    issues = [_issue(description=f"finding-{i}") for i in range(5)]
    call_sizes: List[int] = []
    lock = threading.Lock()

    class CountingStub(_SimulatesFileReadToolCall):
        def chat(
            self,
            messages: list[dict[str, Any]],
            *,
            tools: Optional[list] = None,
            response_format: str = "json",
            **kwargs: Any,
        ) -> Any:
            first_text = _first_user_text_from_chat_messages(messages)
            if (
                _is_fpf_reasoning_prompt(first_text)
                and tools
                and _chat_tool_result_count(messages) == 0
            ):
                n = first_text.count("--- Finding index")
                with lock:
                    call_sizes.append(n)
            return super().chat(
                messages,
                tools=tools,
                response_format=response_format,
                **kwargs,
            )

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "verdicts" not in prompt.lower():
                return super().complete_json(prompt, **kwargs)
            n = prompt.count("--- Finding index") or call_sizes[-1]
            return {
                "verdicts": [
                    {"index": i, "is_real_issue": True, "confidence": "high"} for i in range(n)
                ]
            }

    out = filter_false_positives(CountingStub(), _input(), issues)
    assert out == issues  # every finding verified as real, none dropped
    assert len(call_sizes) == 3  # ceil(5 / 2)
    assert all(size <= 2 for size in call_sizes)
    assert sorted(call_sizes) == [1, 2, 2]


def test_filter_merges_verdicts_across_split_batches(monkeypatch) -> None:
    """Verdicts merge back onto the correct *original* findings across a split:
    a drop confirmed at within-batch index 0 in two different batches removes
    two distinct original findings, not the same one twice."""
    monkeypatch.setenv("CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP", "2")
    issues = [_issue(description=f"finding-{i}") for i in range(4)]

    class AlwaysDropFirstStub(_SimulatesFileReadToolCall):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "verdicts" not in prompt.lower():
                return super().complete_json(prompt, **kwargs)
            return {"verdicts": [{"index": 0, "is_real_issue": False, "confidence": "high"}]}

    out = filter_false_positives(AlwaysDropFirstStub(), _input(), issues)
    # Batch 1 = [issues[0], issues[1]] -> drops issues[0]; batch 2 =
    # [issues[2], issues[3]] -> drops issues[2]. If the split incorrectly
    # mapped every batch's index 0 back to the whole list's index 0, this
    # would instead drop issues[0] twice (a no-op the second time) and keep
    # issues[2].
    assert out == [issues[1], issues[3]]


def test_verify_timeout_seconds_default_and_env_override(monkeypatch) -> None:
    """Per-group verify timeout defaults to 60 minutes and honors the env override.

    Preconditions:
        - ``CODE_REVIEW_VERIFY_TIMEOUT_SECONDS`` is unset for the default assertion,
          then set for the override assertion (via ``monkeypatch``).

    Postconditions:
        - Unset env → ``DEFAULT_VERIFY_TIMEOUT_SECONDS`` (3600).
        - Env ``90`` → ``90``.
    """
    monkeypatch.delenv("CODE_REVIEW_VERIFY_TIMEOUT_SECONDS", raising=False)
    assert DEFAULT_VERIFY_TIMEOUT_SECONDS == 3600
    assert _verify_timeout_seconds() == 3600

    monkeypatch.setenv("CODE_REVIEW_VERIFY_TIMEOUT_SECONDS", "90")
    assert _verify_timeout_seconds() == 90


def test_filter_timeout_keeps_group_findings_without_hanging(monkeypatch) -> None:
    """A verification call that hangs past the per-group timeout is treated as a
    failure for its group only (fail-safe: kept, not dropped) while other groups'
    verdicts still apply — and the call returns promptly instead of blocking on
    the hung call for its full duration."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "2")
    monkeypatch.setenv("CODE_REVIEW_VERIFY_TIMEOUT_SECONDS", "1")

    a = _issue(file_path="a.py", description="a-fp")
    b = _issue(file_path="b.py", description="b-real")

    # Route on the group's own read_file(...) directive (see PerFileStub above).
    class SlowStub(_SimulatesFileReadToolCall):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "verdicts" not in prompt.lower():
                return super().complete_json(prompt, **kwargs)
            if 'read_file("a.py")' in prompt:
                time.sleep(3)  # exceeds the 1s timeout set above
                return {"verdicts": [{"index": 0, "is_real_issue": False, "confidence": "high"}]}
            if 'read_file("b.py")' in prompt:
                return {"verdicts": [{"index": 0, "is_real_issue": True, "confidence": "high"}]}
            return super().complete_json(prompt, **kwargs)

    inp = _input(files={"a.py": "content-a\n", "b.py": "content-b\n"})
    start = time.monotonic()
    out = filter_false_positives(SlowStub(), inp, [a, b])
    elapsed = time.monotonic() - start

    # a's group timed out and was kept (not dropped); b's real finding stays too.
    assert out == [a, b]
    # Returned close to the 1s timeout, not the 3s hang.
    assert elapsed < 2.5


def test_filter_empty_issue_list() -> None:
    """An empty finding list returns empty without invoking the verifier."""
    assert filter_false_positives(_RaisingStub(), _input(), []) == []


def test_verify_group_propagates_trace_id_into_worker_threads(monkeypatch) -> None:
    """A trace_id bound in the parent thread (shared.observability.bind_trace_id)
    is visible inside each verification worker thread. The fan-out now goes
    through parallel_map (propagate_context=True by default), closing the gap
    the old hand-rolled ThreadPoolExecutor left — it never copied context into
    its worker threads, so trace_id/LLM attribution was silently dropped."""
    from shared.observability import bind_trace_id, current_trace_id

    # High enough (relative to 3 groups) to force the parallel_map branch
    # (workers > 1), not the sequential fast path, which runs in-thread and
    # would prove nothing about context propagation.
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "4")
    seen_trace_ids: List[str] = []
    lock = threading.Lock()

    class TraceCapturingStub(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "verdicts" in prompt.lower():
                with lock:
                    seen_trace_ids.append(current_trace_id())
                return {"verdicts": [{"index": 0, "is_real_issue": True, "confidence": "high"}]}
            return super().complete_json(prompt, **kwargs)

    a = _issue(file_path="a.py")
    b = _issue(file_path="b.py")
    c = _issue(file_path="c.py")
    inp = _input(files={"a.py": "x=1\n", "b.py": "y=2\n", "c.py": "z=3\n"})
    stub = TraceCapturingStub()

    with bind_trace_id("trace-abc123"):
        out = filter_false_positives(stub, inp, [a, b, c])

    assert out == [a, b, c]  # nothing dropped; this test targets propagation only
    assert len(seen_trace_ids) == 3
    assert all(tid == "trace-abc123" for tid in seen_trace_ids)


# --------------------------------------------------------------------------- repo reader


class _FakeReader:
    """A minimal duck-typed RepoReader over an in-memory {path: content} map."""

    def __init__(self, files: Dict[str, str]):
        self._files = files

    def list_files(self) -> List[str]:
        return list(self._files)

    def read_file(self, path: str) -> Optional[str]:
        return self._files.get((path or "").strip())


class _BoomReader:
    """A reader whose every method raises, to exercise the fail-safe fall-through."""

    def list_files(self) -> List[str]:
        raise RuntimeError("tree boom")

    def read_file(self, path: str) -> Optional[str]:
        raise RuntimeError("read boom")


class _PartialFailReader:
    """A reader whose read_file fails for specific paths, succeeds for the rest.

    Models one file's fetch failing mid-scan (e.g. a GitHub-backed reader
    erroring on a single path) without aborting the whole repo half of
    ``find_references`` -- distinct from ``_BoomReader``'s total ``list_files``
    failure, which never gets far enough to scan any file.
    """

    def __init__(
        self, files: Dict[str, str], fail_paths: Iterable[str], *, raise_error: bool = True
    ):
        self._files = files
        self._fail_paths = set(fail_paths)
        self._raise_error = raise_error

    def list_files(self) -> List[str]:
        return list(self._files)

    def read_file(self, path: str) -> Optional[str]:
        key = (path or "").strip()
        if key in self._fail_paths:
            if self._raise_error:
                raise RuntimeError(f"read boom: {key}")
            return None
        return self._files.get(key)


class _FlakyReader:
    """A reader whose read_file returns ``None`` after the first successful read per path.

    ``find_references`` reads a matched repo file twice: once in
    ``_search_repo_references`` to find the hit, and again in
    ``_format_reference_hit`` (via ``CodebaseIndex._read``) to build the
    excerpt. This models a reader that can serve a file once but not again
    (e.g. a transient or rate-limited fetch), exercising the locator-only
    fallback when that second read fails.
    """

    def __init__(self, files: Dict[str, str]):
        self._files = files
        self._served: set = set()

    def list_files(self) -> List[str]:
        return list(self._files)

    def read_file(self, path: str) -> Optional[str]:
        key = (path or "").strip()
        if key in self._served:
            return None
        self._served.add(key)
        return self._files.get(key)


def test_index_list_files_appends_reader_paths_deduped() -> None:
    """``list_files`` lists submission paths first, then reader paths, deduped."""
    idx = CodebaseIndex(
        files={"app/main.py": "x = 1\n"},
        repo_reader=_FakeReader({"app/main.py": "OTHER", "pkg/models.py": "class M: ..."}),
    )
    listed = idx.list_files()
    assert listed[0] == "app/main.py"  # submission first
    assert "pkg/models.py" in listed
    assert listed.count("app/main.py") == 1  # submission wins the dedupe


def test_index_read_file_falls_through_to_reader() -> None:
    """A path absent from the submission is read from the repo reader."""
    idx = CodebaseIndex(
        files={"app/main.py": "x = 1\n"},
        repo_reader=_FakeReader({"pkg/models.py": "class M:\n    pass\n"}),
    )
    assert idx.read_file("pkg/models.py") == "class M:\n    pass\n"
    # Submission files still win over the reader for a shared path.
    assert idx.read_file("app/main.py") == "x = 1\n"
    # A path in neither still errors.
    assert idx.read_file("nowhere.py").startswith("Error")


def test_reader_existing_empty_file_is_present() -> None:
    """An existing zero-byte file (e.g. a package __init__.py) resolves as present,
    not absent — so a 'must create __init__.py' finding can be refuted."""
    idx = CodebaseIndex(
        files={"app/main.py": "x = 1\n"},
        repo_reader=_FakeReader({"pkg/__init__.py": ""}),
    )
    # read_file returns the empty content (present), not an Error string.
    assert idx.read_file("pkg/__init__.py") == ""
    # resolve_path treats the empty existing file as resolvable.
    assert idx.resolve_path("pkg/__init__.py") == "pkg/__init__.py"


def test_resolve_path_uses_reader() -> None:
    """``resolve_path`` returns the cited path when only the reader can read it."""
    idx = CodebaseIndex(
        files={"app/main.py": "x = 1\n"},
        repo_reader=_FakeReader({"pkg/models.py": "class M: ..."}),
    )
    assert idx.resolve_path("pkg/models.py") == "pkg/models.py"
    assert idx.resolve_path("still/absent.py") is None


def test_reader_errors_are_failsafe() -> None:
    """A reader that raises never breaks the index: reads/lists degrade gracefully."""
    idx = CodebaseIndex(files={"app/main.py": "x = 1\n"}, repo_reader=_BoomReader())
    # list_files degrades to just the submission's paths.
    assert idx.list_files() == ["app/main.py"]
    # read_file for an absent path degrades to the not-found error (finding kept).
    assert idx.read_file("pkg/models.py").startswith("Error")
    assert idx.resolve_path("pkg/models.py") is None


def test_filter_drops_finding_for_existing_repo_file() -> None:
    """With a reader, a finding citing an existing (unchanged) repo file is
    verifiable and droppable — the file is absent from the diff but present in
    the repo, so the verifier can confirm the false positive."""
    # The finding cites pkg/models.py, which is NOT in the submission's files.
    ghost = _issue(file_path="pkg/models.py", description="pkg/models.py must be created")
    reader = _FakeReader({"pkg/models.py": "class Model:\n    pass\n"})
    stub = _VerdictStub(verdicts=[{"index": 0, "is_real_issue": False, "confidence": "high"}])
    out = filter_false_positives(
        stub,
        _input(files={"app/main.py": "from pkg.models import Model\n"}),
        [ghost],
        repo_reader=reader,
    )
    assert out == []  # confirmed existing → dropped (not skipped as unresolved)


# --------------------------------------------------------------------------- coordinator integration

_CHUNK_ISSUE = {
    "severity": "high",
    "category": "logic",
    "file_path": "app/main.py",
    "line": 1,
    "description": "foo undefined",
    "suggestion": "define foo",
}


def test_run_coordinator_drops_false_positive_and_flips_to_approved() -> None:
    """A chunk's only blocking finding, confirmed a false positive, is removed and
    the deterministic gate then approves — the developer is not handed phantom work."""
    stub = _VerdictStub(
        verdicts=[
            {
                "index": 0,
                "is_real_issue": False,
                "confidence": "high",
                "reasoning": "defined in util.py",
            }
        ],
        chunk_issues=[_CHUNK_ISSUE],
    )
    out = run_coordinator(stub, _input(files={"app/main.py": "def bar():\n    return foo()\n"}))
    assert out.approved is True
    assert out.issues == []


def test_run_coordinator_keeps_confirmed_issue_and_rejects() -> None:
    """A finding the verifier confirms is real survives and the review still rejects."""
    stub = _VerdictStub(
        verdicts=[{"index": 0, "is_real_issue": True, "confidence": "high"}],
        chunk_issues=[_CHUNK_ISSUE],
    )
    out = run_coordinator(stub, _input(files={"app/main.py": "def bar():\n    return foo()\n"}))
    assert out.approved is False
    assert any(i.description == "foo undefined" for i in out.issues)


def test_run_coordinator_disabled_filter_keeps_issue(monkeypatch) -> None:
    """With the filter disabled, the false-positive finding is NOT removed."""
    monkeypatch.setenv("CODE_REVIEW_FALSE_POSITIVE_FILTER", "0")
    stub = _VerdictStub(
        verdicts=[{"index": 0, "is_real_issue": False, "confidence": "high"}],
        chunk_issues=[_CHUNK_ISSUE],
    )
    out = run_coordinator(stub, _input(files={"app/main.py": "def bar():\n    return foo()\n"}))
    assert out.approved is False
    assert any(i.description == "foo undefined" for i in out.issues)
