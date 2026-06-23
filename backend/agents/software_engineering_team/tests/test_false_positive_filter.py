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

from typing import Any, Dict, List, Optional

import pytest
from code_review_agent.coordinator import run_coordinator
from code_review_agent.false_positive_filter import (
    _MANIFEST_LIMIT,
    CodebaseIndex,
    _build_group_prompt,
    _build_tools,
    _code_fence_for,
    _coerce_verdict,
    _parse_verdicts,
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


class _VerdictStub(DummyLLMClient):
    """Returns canned verdicts for the verification call; rejects on chunk review.

    ``complete_json`` branches on the prompt: the verification user prompt
    contains the anchor "verdicts" (the contract asks for a ``verdicts`` array),
    so the stub can serve both the chunk reviewer and the verifier from one
    injected client in an end-to-end coordinator run.
    """

    def __init__(self, verdicts: List[Dict[str, Any]], chunk_issues: Optional[List[Dict]] = None):
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
    """Returns non-JSON text on the verification call (unparsable → keep)."""

    def complete(self, prompt: str, **kwargs: Any) -> str:  # type: ignore[override]
        return "not json at all"

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:  # type: ignore[override]
        if "verdicts" in prompt.lower():
            return "not a json object"
        return super().complete_json(prompt, **kwargs)


@pytest.fixture(autouse=True)
def _enable_filter(monkeypatch):
    """The filter is default-on; make tests independent of the ambient env."""
    monkeypatch.delenv("CODE_REVIEW_FALSE_POSITIVE_FILTER", raising=False)
    yield


# --------------------------------------------------------------------------- CodebaseIndex


def test_index_from_files_keeps_nonblank() -> None:
    """``from_input`` keeps only files with non-blank content (blank/empty dropped)."""
    idx = CodebaseIndex.from_input(_input(files={"a.py": "x = 1\n", "b.py": "   ", "c.py": ""}))
    assert set(idx.files) == {"a.py"}


def test_index_from_legacy_code_parses_headers() -> None:
    """Legacy ``code`` with ``### path ###`` headers splits into path-addressable files."""
    code = "### app/main.py ###\ndef foo(): pass\n\n### app/util.py ###\ndef bar(): pass"
    idx = CodebaseIndex.from_input(CodeReviewInput(code=code, task_description="t"))
    assert set(idx.files) == {"app/main.py", "app/util.py"}
    assert "def foo" in idx.files["app/main.py"]


def test_index_legacy_code_without_headers_has_no_readable_files() -> None:
    """Headerless legacy code yields no path-addressable files (filter then keeps all)."""
    idx = CodebaseIndex.from_input(
        CodeReviewInput(code="just some loose code", task_description="t")
    )
    assert idx.files == {}


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


def test_search_rejects_nonpositive_max() -> None:
    """``search`` asserts on a non-positive ``max_matches`` (precondition guard)."""
    with pytest.raises(AssertionError):
        CodebaseIndex(files={"a.py": "x"}).search("x", max_matches=0)


# --------------------------------------------------------------------------- tools


def test_build_tools_delegate_to_index() -> None:
    """``_build_tools`` returns read_file/list_files/search_codebase tools that delegate to the index."""
    idx = CodebaseIndex(files={"app/main.py": "def foo(): pass\n"}, existing_codebase="old")
    read_file, list_files, search_codebase = _build_tools(idx)
    assert {read_file.tool_name, list_files.tool_name, search_codebase.tool_name} == {
        "read_file",
        "list_files",
        "search_codebase",
    }
    assert read_file("app/main.py") == "def foo(): pass\n"
    listed = list_files()
    assert "app/main.py" in listed and CodebaseIndex.EXISTING_CODEBASE_PATH in listed
    assert "app/main.py:1: def foo(): pass" in search_codebase("foo")
    assert "No matches" in search_codebase("zzz-not-there")


def test_list_files_tool_handles_empty_index() -> None:
    """The list_files tool returns a placeholder string for an empty index."""
    _, list_files, _ = _build_tools(CodebaseIndex(files={}))
    assert list_files() == "(no files available)"


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


# --------------------------------------------------------------------------- prompt


def test_group_prompt_has_anchor_indices_and_truncation_note() -> None:
    """``_build_group_prompt`` emits per-finding anchor indices, the task description, and the inline-truncation note."""
    idx = CodebaseIndex(files={"app/main.py": "X" * 50}, existing_codebase="old")
    issues = [_issue(description="d0"), _issue(description="d1", line=None)]
    prompt = _build_group_prompt(idx, "app/main.py", issues, _input(), max_inline_chars=10)
    assert "verdicts" in prompt.lower()
    assert "Finding index 0" in prompt and "Finding index 1" in prompt
    assert "wire up foo" in prompt  # task description
    assert "first 10 characters" in prompt  # inline truncation note


def test_group_prompt_truncates_large_manifest() -> None:
    """A submission with more files than the manifest limit defers the rest to list_files()."""
    files = {f"f{i:04d}.py": "x = 1\n" for i in range(_MANIFEST_LIMIT + 5)}
    idx = CodebaseIndex(files=files)
    prompt = _build_group_prompt(idx, "f0000.py", [_issue(file_path="f0000.py")], _input(), 1000)
    assert "and 5 more (call list_files())" in prompt


def test_code_fence_for_grows_past_backtick_runs() -> None:
    """``_code_fence_for`` returns a fence longer than the longest backtick run in the content."""
    assert _code_fence_for("plain code, no backticks") == "```"
    assert _code_fence_for("a ``` fence inside") == "````"  # 3 → 4
    assert _code_fence_for("nested ````` run") == "``````"  # 5 → 6
    # A bare run with no other content is still escaped.
    assert _code_fence_for("```") == "````"


def test_group_prompt_uses_safe_fence_for_backtick_content() -> None:
    """A file body containing a ``` fence is wrapped in a longer fence so it cannot close early."""
    idx = CodebaseIndex(files={"app/doc.md": "before\n```python\nx = 1\n```\nafter\n"})
    prompt = _build_group_prompt(
        idx, "app/doc.md", [_issue(file_path="app/doc.md")], _input(), 1000
    )
    # The wrapping fence is four backticks (one longer than the body's run); the
    # body's own ``` survives intact between them.
    assert "````" in prompt
    assert "```python" in prompt


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
    inp = CodeReviewInput(code="loose code with no headers", task_description="t")
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


def test_filter_keeps_on_setup_exception() -> None:
    """A failure in the verification *setup* (before the per-group loop) must be
    caught by the fail-safe guard and keep all findings — not crash the review."""

    class BoomCtxStub(DummyLLMClient):
        # context sizing (compute_code_review_map_chunk_chars) calls this during
        # setup, after the index is built — a perfect injection point.
        def get_max_context_tokens(self) -> int:  # type: ignore[override]
            raise RuntimeError("context sizing boom")

    issues = [_issue()]
    out = filter_false_positives(BoomCtxStub(), _input(), issues)
    assert out == issues  # kept, no exception propagated


def test_filter_keeps_on_unparsable_verdict() -> None:
    """An unparsable verifier response keeps all findings (fail-safe)."""
    issues = [_issue()]
    out = filter_false_positives(_BadJsonStub(), _input(), issues)
    assert out == issues


def test_filter_keeps_on_low_confidence_false() -> None:
    """A false verdict at low confidence keeps the finding (only confident verdicts drop)."""
    issues = [_issue()]
    stub = _VerdictStub(verdicts=[{"index": 0, "is_real_issue": False, "confidence": "low"}])
    out = filter_false_positives(stub, _input(), issues)
    assert out == issues


def test_filter_groups_by_file_and_removes_across_groups() -> None:
    """Findings are verified per file group; a confirmed drop in one group leaves another's real finding intact."""
    a = _issue(file_path="a.py", description="a-fp")
    b = _issue(file_path="b.py", description="b-real")

    # Both groups send index 0; the stub marks index 0 false → both would drop,
    # but b's verdict says real, so only a drops. Use a per-file stub.
    class PerFileStub(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            low = prompt.lower()
            # The manifest lists every file in every prompt; the "Full content of
            # `<path>`" line uniquely names the group's primary file.
            if "full content of `a.py`" in low:
                return {"verdicts": [{"index": 0, "is_real_issue": False, "confidence": "high"}]}
            if "full content of `b.py`" in low:
                return {"verdicts": [{"index": 0, "is_real_issue": True, "confidence": "high"}]}
            return super().complete_json(prompt, **kwargs)

    inp = _input(files={"a.py": "x=1\n", "b.py": "y=2\n"})
    out = filter_false_positives(PerFileStub(), inp, [a, b])
    assert out == [b]


def test_filter_empty_issue_list() -> None:
    """An empty finding list returns empty without invoking the verifier."""
    assert filter_false_positives(_RaisingStub(), _input(), []) == []


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
