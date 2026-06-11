"""Tests for Code Review Coordinator.

Pure-function tests (``parse_code_into_file_blocks``, the splitter and
chunker) stay LLM-free. The LLM-integration tests use ``DummyLLMClient``
subclasses now that ``ChunkReviewAgent`` is Strands-backed and bypasses
``llm.complete_json`` in favor of the ``chat_json_round`` +
structured-output flow.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

import pytest
from code_review_agent.coordinator import (
    MIN_SPLIT_SEGMENT_CHARS,
    _anchor_line,
    _issues_from_chunk_output,
    _segment_range_label,
    build_review_chunks,
    parse_code_into_file_blocks,
    run_coordinator,
    split_block_into_segments,
)
from code_review_agent.models import (
    CodeReviewInput,
    CodeReviewOutput,
    CodeReviewUnavailableError,
    FileSegment,
    ReviewChunk,
)
from pydantic import ValidationError

from llm_service import LLMRateLimitError, LLMSemanticExhaustionError
from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.shared.context_sizing import compute_code_review_map_chunk_chars

# ---------------------------------------------------------------------------
# Pure-function tests
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


def test_parse_code_preserves_preamble_before_first_header() -> None:
    """Content before the first header must become a headerless block, not be
    silently dropped (full-coverage postcondition)."""
    code = "import os\nhelper()\n\n### app/main.py ###\ndef foo(): pass"
    blocks = parse_code_into_file_blocks(code)
    assert blocks[0] == ("", "import os\nhelper()")
    assert blocks[1][0] == "app/main.py"


def test_parse_code_ignores_header_like_source_lines() -> None:
    """Header-like fragments inside source — markdown headings without the
    trailing marker, '###' starting a comment, mid-line strings — are content,
    not file headers, and a header can never span multiple source lines."""
    code = (
        'md = """\n'
        "### Title\n"
        "Body text\n"
        '"""\n'
        "\n"
        "### app/x.py ###\n"
        'banner = "### not a header ###"\n'
        "## ### also not ###\n"
        "code = 1"
    )
    blocks = parse_code_into_file_blocks(code)
    assert [b[0] for b in blocks] == ["", "app/x.py"]
    # The markdown body survives intact in the headerless preamble block.
    assert "### Title" in blocks[0][1]
    assert "Body text" in blocks[0][1]
    # Mid-line and non-line-start "###" sequences stay in the file's content.
    assert 'banner = "### not a header ###"' in blocks[1][1]
    assert "## ### also not ###" in blocks[1][1]
    assert "code = 1" in blocks[1][1]


# ---------------------------------------------------------------------------
# CodeReviewInput boundary validation
# ---------------------------------------------------------------------------


def test_input_without_any_code_source_raises() -> None:
    with pytest.raises(ValidationError):
        CodeReviewInput(task_description="t")


def test_input_with_empty_files_dict_raises() -> None:
    """files={} (e.g. a glob miss) is a caller bug, not an empty review."""
    with pytest.raises(ValidationError):
        CodeReviewInput(files={}, task_description="t")


def test_input_with_explicit_empty_code_is_valid() -> None:
    assert CodeReviewInput(code="", task_description="t").code == ""


# ---------------------------------------------------------------------------
# run_coordinator — LLM-integration tests
# ---------------------------------------------------------------------------


class _ScriptedClient(DummyLLMClient):
    """Returns a different canned response on each ``complete_json`` call.

    Used to simulate the coordinator dispatching to multiple chunks and
    each chunk getting its own LLM response. Thread-safe: map calls may run
    in parallel.
    """

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0
        self._lock = threading.Lock()

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
        with self._lock:
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


def test_run_coordinator_keeps_same_description_on_different_lines() -> None:
    """Two findings sharing file_path + description but on different lines are
    distinct (line anchors inline comments), so dedup must keep both."""
    code = "### app/main.py ###\n" + "\n".join(f"x{i} = {i}" for i in range(100))

    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "medium",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "line": 10,
                        "description": "duplicate string literal",
                        "suggestion": "extract a constant",
                    },
                    {
                        "severity": "medium",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "line": 80,
                        "description": "duplicate string literal",
                        "suggestion": "extract a constant",
                    },
                ],
                "summary": "Two occurrences.",
            }
        ]
    )

    result = run_coordinator(
        client,
        CodeReviewInput(code=code, task_description="Add feature", language="python"),
    )

    assert sorted(i.line for i in result.issues) == [10, 80]


def test_run_coordinator_drops_unanchored_twin_of_anchored_finding() -> None:
    """An unanchored (line=None) finding that duplicates an anchored one (same
    file_path + description) is dropped, so the issue is reported once (inline),
    not twice (once in the body, once inline)."""
    code = "### app/main.py ###\n" + "\n".join(f"x{i} = {i}" for i in range(50))

    client = _ScriptedClient(
        [
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "description": "missing null check",
                        "suggestion": "guard it",
                    },
                    {
                        "severity": "high",
                        "category": "logic",
                        "file_path": "app/main.py",
                        "line": 12,
                        "description": "missing null check",
                        "suggestion": "guard it",
                    },
                ],
                "summary": "One issue, reported twice.",
            }
        ]
    )

    result = run_coordinator(
        client,
        CodeReviewInput(code=code, task_description="Add feature", language="python"),
    )

    assert len(result.issues) == 1
    assert result.issues[0].line == 12


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


# ---------------------------------------------------------------------------
# split_block_into_segments
# ---------------------------------------------------------------------------


def _numbered_file(n_lines: int, width: int = 40) -> str:
    """A deterministic multi-line file where every line is identifiable."""
    return "\n".join(f"line {i:05d} ".ljust(width, "x") for i in range(1, n_lines + 1))


def test_split_within_budget_returns_single_whole_segment() -> None:
    content = _numbered_file(10)
    segments = split_block_into_segments("a.py", content, max_chars=10_000)
    assert len(segments) == 1
    seg = segments[0]
    assert seg.content == content
    assert seg.start_line == 1
    assert seg.total_lines == 10
    assert seg.is_partial is False
    assert seg.line_offset == 0


def test_split_reassembles_content_exactly_and_tracks_lines() -> None:
    content = _numbered_file(300)  # ~12.3K chars
    segments = split_block_into_segments("a.py", content, max_chars=4_000)
    assert len(segments) > 1
    assert "".join(s.content for s in segments) == content
    assert all(len(s.content) <= 4_000 for s in segments)
    # line bookkeeping is contiguous: each segment starts right after the previous
    assert segments[0].start_line == 1
    for prev, cur in zip(segments, segments[1:]):
        assert cur.start_line == prev.end_line + 1
    assert segments[-1].end_line == 300
    assert all(s.total_lines == 300 for s in segments)
    # the line at each boundary really is the one the bookkeeping claims
    for seg in segments[1:]:
        assert seg.content.splitlines()[0].startswith(f"line {seg.start_line:05d}")


def test_split_never_breaks_a_line_even_when_oversized() -> None:
    content = "x" * 25_000  # one giant line
    segments = split_block_into_segments("big.js", content, max_chars=8_000)
    assert len(segments) == 1
    assert segments[0].content == content


# ---------------------------------------------------------------------------
# pre_numbered: explicit producer flag (never sniffed from content)
# ---------------------------------------------------------------------------


def test_split_flags_pre_numbered_segments_with_zero_offset() -> None:
    hunk = "\n".join(f"{4000 + i}: code_{i}()".ljust(40, " ") for i in range(300))
    segments = split_block_into_segments("pr.py", hunk, max_chars=4_000, pre_numbered=True)
    assert len(segments) > 1
    assert all(s.pre_numbered for s in segments)
    assert all(s.line_offset == 0 for s in segments)


def test_int_keyed_mapping_content_is_not_treated_as_pre_numbered() -> None:
    """A dict-literal file whose lines look like ``N: value`` must NOT be
    treated as pre-numbered unless the producer declared it: re-anchoring
    stays positional."""
    content = "STATUS = {\n" + "\n".join(f"    {i}: 'v{i}'," for i in range(1, 300)) + "\n}"
    segments = split_block_into_segments("status.py", content, max_chars=2_000)
    assert len(segments) > 1
    assert all(not s.pre_numbered for s in segments)
    assert all(s.line_offset == s.start_line - 1 for s in segments)


def test_segment_range_label_uses_embedded_numbers_for_pre_numbered() -> None:
    """Unreviewed-range reporting must cite the embedded original lines for
    pre-numbered hunks, not the positional 1-based indices."""
    hunk = "\n".join(f"{4000 + i}: code_{i}()" for i in range(51))
    seg = split_block_into_segments("src/feature.py", hunk, 100_000, pre_numbered=True)[0]
    assert _segment_range_label(seg) == "src/feature.py (original lines 4000-4050)"
    plain = FileSegment(path="a.py", content="x = 1\ny = 2", start_line=5, total_lines=20)
    assert _segment_range_label(plain) == "a.py (lines 5-6 of 20)"


# ---------------------------------------------------------------------------
# build_review_chunks — no-file-dropped property
# ---------------------------------------------------------------------------


def _coverage_by_path(chunks: list) -> dict:
    by_path: dict = {}
    for chunk in chunks:
        for seg in chunk.segments:
            by_path.setdefault(seg.path, []).append(seg)
    return by_path


def test_build_review_chunks_no_file_dropped_property() -> None:
    """Mixed input — small files, one 3×-cap file, one headerless block — must be
    covered exactly once, with every rendered chunk within the cap."""
    cap = 10_000
    big = _numbered_file(750)  # ~3× the cap
    blocks = [
        ("small_a.py", "def a(): pass"),
        ("big.py", big),
        ("small_b.py", "def b(): pass"),
        ("", "headerless = True"),
    ]
    chunks = build_review_chunks(blocks, max_chars=cap)

    covered = _coverage_by_path(chunks)
    assert set(covered) == {"small_a.py", "big.py", "small_b.py", ""}
    for path, content in blocks:
        segs = covered[path]
        # exactly-once coverage: concatenation reproduces the block, contiguously
        assert "".join(s.content for s in segs) == content
        assert segs[0].start_line == 1
        for prev, cur in zip(segs, segs[1:]):
            assert cur.start_line == prev.end_line + 1

    for chunk in chunks:
        assert len(chunk.content) <= cap
        paths = [s.path for s in chunk.segments]
        assert len(paths) == len(set(paths)), "a chunk may not hold two segments of one path"


def test_build_review_chunks_oversized_single_line_sits_alone() -> None:
    blocks = [("a.py", "x" * 30_000), ("b.py", "def b(): pass")]
    chunks = build_review_chunks(blocks, max_chars=10_000)
    oversized = [c for c in chunks if any(s.path == "a.py" for s in c.segments)]
    assert len(oversized) == 1
    assert [s.path for s in oversized[0].segments] == ["a.py"]


def test_review_chunk_paths_label_marks_partial_segments() -> None:
    big = _numbered_file(750)
    chunks = build_review_chunks([("big.py", big)], max_chars=10_000)
    assert len(chunks) > 1
    first = chunks[0]
    assert first.paths_label.startswith("big.py (lines 1-")
    assert f"of {750})" in first.paths_label.replace("of 750)", f"of {750})")
    whole = ReviewChunk(segments=[FileSegment(path="a.py", content="x = 1", total_lines=1)])
    assert whole.paths_label == "a.py"


# ---------------------------------------------------------------------------
# files= input path
# ---------------------------------------------------------------------------


def test_files_dict_input_matches_code_input() -> None:
    """`files=` must produce the same review as the equivalent `code=` blob."""
    files = {
        "app/main.py": "def main(): pass",
        "app/util.py": "def util(): pass",
    }
    code = "\n\n".join(f"### {path} ###\n{content}" for path, content in files.items())
    responses = [
        {
            "approved": False,
            "issues": [
                {
                    "severity": "high",
                    "category": "logic",
                    "file_path": "app/main.py",
                    "line": 1,
                    "description": "main() is empty",
                    "suggestion": "implement it",
                }
            ],
            "summary": "Needs work.",
        }
    ]

    via_files = run_coordinator(
        _ScriptedClient(list(responses)),
        CodeReviewInput(files=files, task_description="t", language="python"),
    )
    via_code = run_coordinator(
        _ScriptedClient(list(responses)),
        CodeReviewInput(code=code, task_description="t", language="python"),
    )

    assert via_files.model_dump() == via_code.model_dump()
    assert via_files.approved is False
    assert via_files.issues[0].line == 1


def test_explicit_empty_code_short_circuits() -> None:
    result = run_coordinator(DummyLLMClient(), CodeReviewInput(code="", task_description="t"))
    assert result.approved is True
    assert result.issues == []
    assert result.summary == "No code to review."


def test_blank_file_content_is_named_by_info_finding() -> None:
    """An empty/whitespace-only file is skipped from review but never silently:
    it gets a non-blocking info finding naming it."""
    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(
            files={"pkg/__init__.py": "", "pkg/api.py": "def api(): pass"},
            task_description="t",
            language="python",
        ),
    )
    assert result.approved is True
    info = [i for i in result.issues if i.severity == "info"]
    assert [i.file_path for i in info] == ["pkg/__init__.py"]
    assert "empty" in info[0].description


def test_code_mode_blank_block_is_named_by_info_finding() -> None:
    """A ``### path ###`` header whose block is blank is reported, not dropped."""
    code = "### empty.py ###\n   \n\n### real.py ###\nx = 1"
    result = run_coordinator(
        _ScriptedClient([{"approved": True, "issues": [], "summary": "ok"}]),
        CodeReviewInput(code=code, task_description="t"),
    )
    info = [i for i in result.issues if i.severity == "info"]
    assert [i.file_path for i in info] == ["empty.py"]


def test_all_files_blank_short_circuits_with_info_findings() -> None:
    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(files={"a.py": "   "}, task_description="t"),
    )
    assert result.approved is True
    assert result.summary == "No code to review."
    assert [i.file_path for i in result.issues] == ["a.py"]
    assert result.issues[0].severity == "info"


# ---------------------------------------------------------------------------
# Failure recovery: retry, bisect, fail loudly
# ---------------------------------------------------------------------------


class _SelectiveRaiser(DummyLLMClient):
    """Raises for prompts containing a marker; otherwise delegates to Dummy.

    Records every prompt so tests can count map calls.
    """

    def __init__(self, marker: str, exc: Optional[Exception] = None) -> None:
        super().__init__()
        self.marker = marker
        self.exc = exc or LLMSemanticExhaustionError("LLM returned reasoning only (no content)")
        self.prompts: List[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.prompts.append(prompt)
        if self.marker in prompt:
            raise self.exc
        return super().complete_json(prompt, **kwargs)


class _FailNTimes(DummyLLMClient):
    """Fails the first ``n`` calls, then succeeds."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.remaining = n
        self.prompts: List[str] = []
        self._lock = threading.Lock()

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            self.prompts.append(prompt)
            if self.remaining > 0:
                self.remaining -= 1
                raise LLMSemanticExhaustionError("transient")
        return super().complete_json(prompt, **kwargs)


def test_failing_multi_segment_chunk_bisects_and_recovers() -> None:
    """A chunk whose combined review fails is bisected per segment; both halves
    succeed individually, so the review completes normally."""

    class _FailOnCombined(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            self.calls += 1
            # Both files in one prompt → fail; single file → succeed.
            if "### a.py ###" in prompt and "### b.py ###" in prompt:
                raise LLMSemanticExhaustionError("no content")
            return super().complete_json(prompt, **kwargs)

    client = _FailOnCombined()
    result = run_coordinator(
        client,
        CodeReviewInput(
            files={"a.py": "def a(): pass", "b.py": "def b(): pass"},
            task_description="t",
            language="python",
        ),
    )
    assert client.calls == 3  # combined fail + two single-file successes
    assert result.approved is True
    assert all(i.severity != "info" for i in result.issues)


def test_transient_failure_recovers_via_same_input_retry() -> None:
    """A chunk too small to bisect gets one same-input retry, so a one-off
    transient error never costs the review."""
    client = _FailNTimes(1)
    result = run_coordinator(
        client,
        CodeReviewInput(
            files={"only.py": "def only(): pass"},
            task_description="t",
            language="python",
        ),
    )
    assert result.approved is True
    assert len(client.prompts) == 2  # initial failure + successful retry


def test_persistent_small_chunk_failure_raises_unavailable() -> None:
    """A small chunk that fails its initial call and its retry has no verdict:
    the run must raise, never render approved/rejected on unreviewed code."""
    client = _SelectiveRaiser("def only")
    with pytest.raises(CodeReviewUnavailableError) as excinfo:
        run_coordinator(
            client,
            CodeReviewInput(
                files={"only.py": "def only(): pass"},
                task_description="t",
                language="python",
            ),
        )
    assert len(client.prompts) == 2  # initial + one same-input retry
    assert any("only.py" in r for r in excinfo.value.unreviewed)


def test_partial_terminal_failure_raises_instead_of_failing_open(monkeypatch) -> None:
    """One chunk keeps failing while another succeeds: the run raises rather
    than approving partially reviewed code."""
    monkeypatch.setenv("CODE_REVIEW_MAP_PARALLELISM", "1")
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)
    filler_size = cap - 2_000  # forces the two files into separate chunks
    files = {
        "bad.py": "FAILME = True\n" + ("x = 1\n" * 50),
        "good.py": "ok = 1\n".ljust(filler_size, "#"),
    }
    assert len(files["bad.py"]) < 2 * MIN_SPLIT_SEGMENT_CHARS

    client = _SelectiveRaiser("FAILME")
    with pytest.raises(CodeReviewUnavailableError) as excinfo:
        run_coordinator(
            client,
            CodeReviewInput(files=files, task_description="t", language="python"),
        )
    assert any("bad.py" in r for r in excinfo.value.unreviewed)


def test_infra_failure_fails_fast_without_retry_or_bisect() -> None:
    """Rate-limit/unreachable/auth failures can't be fixed by smaller chunks:
    exactly one map call, then CodeReviewUnavailableError."""
    client = _SelectiveRaiser("def", exc=LLMRateLimitError("429"))
    with pytest.raises(CodeReviewUnavailableError):
        run_coordinator(
            client,
            CodeReviewInput(
                files={"only.py": "def only(): pass"},
                task_description="t",
                language="python",
            ),
        )
    assert len(client.prompts) == 1


def test_infra_failure_is_detected_through_exception_chain() -> None:
    """Strands may wrap the client error; classification must walk the chain."""
    wrapped = RuntimeError("agent invocation failed")
    wrapped.__cause__ = LLMRateLimitError("429")
    client = _SelectiveRaiser("def", exc=wrapped)
    with pytest.raises(CodeReviewUnavailableError):
        run_coordinator(
            client,
            CodeReviewInput(
                files={"only.py": "def only(): pass"},
                task_description="t",
                language="python",
            ),
        )
    assert len(client.prompts) == 1


def test_large_failing_file_bisects_then_raises_with_ranges() -> None:
    """A single big segment that keeps failing bisects by lines until the
    floor, then the run raises naming an unreviewed range."""
    n_lines = 425
    content = "\n".join(f"FAILME {i:05d}".ljust(40, "x") for i in range(1, n_lines + 1))
    # One chunk (below the map cap) but above the bisect floor, and every
    # half still carries the failure marker.
    assert (
        2 * MIN_SPLIT_SEGMENT_CHARS
        <= len(content)
        < compute_code_review_map_chunk_chars(DummyLLMClient())
    )
    client = _SelectiveRaiser("FAILME")
    with pytest.raises(CodeReviewUnavailableError) as excinfo:
        run_coordinator(
            client,
            CodeReviewInput(files={"big.py": content}, task_description="t", language="python"),
        )
    assert len(client.prompts) >= 2  # at least one bisect retry happened
    assert any("big.py" in r for r in excinfo.value.unreviewed)


def test_failing_single_giant_line_retries_once_then_raises() -> None:
    """A single line above the bisect floor cannot split (line boundaries are
    never broken); it gets the same-input retry, then the run raises."""
    client = _SelectiveRaiser("FAILME")
    with pytest.raises(CodeReviewUnavailableError):
        run_coordinator(
            client,
            CodeReviewInput(
                files={"min.js": "FAILME;" + ("x" * 20_000)},
                task_description="t",
                language="typescript",
            ),
        )
    assert len(client.prompts) == 2  # initial + same-input retry; no bisect possible


def test_parallel_map_failure_cancels_and_raises() -> None:
    """With multiple chunks reviewed concurrently, a terminal failure must
    surface as CodeReviewUnavailableError (pending work is cancelled, no
    partial verdict is rendered)."""
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)
    files = {
        "a.py": "FAILME = 1\n".ljust(cap - 2_000, "#"),
        "b.py": "FAILME = 2\n".ljust(cap - 2_000, "#"),
        "c.py": "FAILME = 3\n".ljust(cap - 2_000, "#"),
    }
    client = _SelectiveRaiser("FAILME", exc=LLMRateLimitError("429"))
    with pytest.raises(CodeReviewUnavailableError):
        run_coordinator(
            client,
            CodeReviewInput(files=files, task_description="t", language="python"),
        )


@pytest.mark.parametrize("fail_first", [True, False])
def test_parallel_map_failure_does_not_wait_for_inflight_reviews(fail_first: bool) -> None:
    """A fast infra failure must propagate immediately regardless of where the
    failing chunk sits in submission order: completions are observed as they
    happen (never joined in submission order), pending chunks are cancelled,
    and in-flight reviews are left to finish in the background — so the
    failure is never blocked behind another chunk's model timeout."""
    release = threading.Event()

    class _OneFailsOneBlocks(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.slow_finished = False

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if "FAILME" in prompt:
                raise LLMRateLimitError("429")
            release.wait(timeout=10)
            self.slow_finished = True
            return super().complete_json(prompt, **kwargs)

    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)
    contents = {
        "fast_fail.py": "FAILME = 1\n".ljust(cap - 2_000, "#"),
        "slow.py": "ok = 1\n".ljust(cap - 2_000, "#"),
    }
    order = ["fast_fail.py", "slow.py"] if fail_first else ["slow.py", "fast_fail.py"]
    files = {name: contents[name] for name in order}
    client = _OneFailsOneBlocks()
    try:
        with pytest.raises(CodeReviewUnavailableError):
            run_coordinator(
                client,
                CodeReviewInput(files=files, task_description="t", language="python"),
            )
        # The failure surfaced while the other chunk's review was still
        # in flight — the coordinator did not block waiting for it.
        assert client.slow_finished is False
    finally:
        release.set()


def test_headerless_code_reviews_as_single_unnamed_block() -> None:
    client = _ScriptedClient([{"approved": True, "issues": [], "summary": "fine"}])
    result = run_coordinator(
        client, CodeReviewInput(code="x = compute()\ny = x + 1", task_description="t")
    )
    assert result.approved is True
    assert result.summary == "fine"


# ---------------------------------------------------------------------------
# Issue normalization: paths, sanitization, anchoring
# ---------------------------------------------------------------------------


def test_normalize_issue_path_blank_and_suffix_cases() -> None:
    from code_review_agent.coordinator import _normalize_issue_path

    seg = FileSegment(path="a.py", content="x = 1", start_line=501, total_lines=900)
    chunk = ReviewChunk(segments=[seg])
    assert _normalize_issue_path("", chunk) == "a.py"
    assert _normalize_issue_path("a.py (lines 501-505 of 900)", chunk) == "a.py"
    two = ReviewChunk(segments=[seg, FileSegment(path="b.py", content="y = 2", total_lines=1)])
    assert _normalize_issue_path("", two) == ""
    # Non-dict issue entries are skipped defensively.
    assert _issues_from_chunk_output(chunk, ["not-a-dict"]) == []


def test_blank_path_in_multi_segment_chunk_stays_blank() -> None:
    """A blank path in a multi-segment chunk must never be replaced with a
    fabricated multi-file label (which would defeat offset lookup and feed
    garbage paths downstream)."""
    tail = FileSegment(path="big.py", content="x = 1\ny = 2", start_line=801, total_lines=802)
    other = FileSegment(path="small.py", content="z = 3", total_lines=1)
    chunk = ReviewChunk(segments=[tail, other])
    issues = _issues_from_chunk_output(
        chunk, [{"description": "problem", "line": 1, "severity": "high"}]
    )
    assert len(issues) == 1
    assert issues[0].file_path == ""
    assert issues[0].line == 1  # unknown segment → anchored as-is, never shifted


def test_malformed_severity_and_category_are_sanitized_not_crashing() -> None:
    """LLM output is untrusted boundary input: null/numeric severity must be
    coerced, never allowed to raise out of the coordinator."""
    seg = FileSegment(path="a.py", content="x = 1\ny = 2\nz = 3", total_lines=3)
    chunk = ReviewChunk(segments=[seg])
    issues = _issues_from_chunk_output(
        chunk,
        [
            {"description": "bad sev", "severity": None, "category": None, "line": 2},
            {"description": "numeric sev", "severity": 3, "line": 1},
            {"description": "unknown sev", "severity": "blocker", "line": 3},
            {"severity": "high"},  # no description → skipped
        ],
    )
    assert [i.severity for i in issues] == ["high", "high", "high"]
    assert issues[0].category == "general"


def test_anchor_line_echo_detection_and_bounds() -> None:
    """Snippet-relative numbers are shifted; echoed absolute numbers are kept;
    numbers beyond both ranges are dropped rather than mis-anchored."""
    seg = FileSegment(
        path="big.py",
        content="\n".join(f"l{i}" for i in range(100)),  # 100 lines
        start_line=501,
        total_lines=900,
    )
    assert _anchor_line(5, seg) == 505  # snippet-relative → shifted
    assert _anchor_line(550, seg) == 550  # inside [501, 600] → echoed absolute, kept
    assert _anchor_line(730, seg) is None  # beyond snippet and segment → dropped
    assert _anchor_line(None, seg) is None
    assert _anchor_line(7, None) == 7  # unknown segment → as-is
    pre = FileSegment(path="pr.py", content="4000: a\n4001: b", pre_numbered=True, total_lines=2)
    assert _anchor_line(4001, pre) == 4001


# ---------------------------------------------------------------------------
# Reconcile safety-net port (coordinator level)
# ---------------------------------------------------------------------------


def test_coordinator_synthesizes_issue_on_zero_issue_reject_with_summary() -> None:
    client = _ScriptedClient(
        [{"approved": False, "issues": [], "summary": "Missing input validation throughout."}]
    )
    result = run_coordinator(
        client,
        CodeReviewInput(code="### a.py ###\nx = 1", task_description="t", language="python"),
    )
    assert result.approved is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == "high"
    assert "Missing input validation" in result.issues[0].description


def test_coordinator_single_chunk_propagates_notes_and_commit_message() -> None:
    client = _ScriptedClient(
        [
            {
                "approved": True,
                "issues": [],
                "summary": "All good.",
                "spec_compliance_notes": "Meets all acceptance criteria.",
                "suggested_commit_message": "feat: add a()",
            }
        ]
    )
    result = run_coordinator(
        client,
        CodeReviewInput(code="### a.py ###\ndef a(): pass", task_description="t"),
    )
    assert result.spec_compliance_notes == "Meets all acceptance criteria."
    assert result.suggested_commit_message == "feat: add a()"


def test_coordinator_multi_chunk_joins_notes_and_drops_commit_message() -> None:
    """Spec notes are joinable per-chunk observations and must survive
    multi-chunk reviews; a commit message synthesized from a fraction of the
    change is misleading and is dropped."""
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)
    files = {
        "a.py": "a = 1\n".ljust(cap - 1_000, "#"),
        "b.py": "b = 2\n".ljust(cap - 1_000, "#"),
    }
    client = _ScriptedClient(
        [
            {
                "approved": True,
                "issues": [],
                "summary": "ok",
                "spec_compliance_notes": "chunk notes",
                "suggested_commit_message": "msg",
            }
        ]
    )
    result = run_coordinator(client, CodeReviewInput(files=files, task_description="t"))
    assert result.approved is True
    assert result.spec_compliance_notes == "chunk notes\n\nchunk notes"
    assert result.suggested_commit_message == ""


def test_single_chunk_keeps_notes_but_drops_commit_message_after_bisection() -> None:
    """A logically-single-chunk review that recovers via bisection keeps its
    spec notes (joined), but drops the commit message: each half's message was
    written having seen only part of the change, so forwarding one would
    present a partial view as covering the whole submission."""

    class _FailCombinedWithNotes(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            self.calls += 1
            if "### a.py ###" in prompt and "### b.py ###" in prompt:
                raise LLMSemanticExhaustionError("no content")
            return {
                "approved": True,
                "issues": [],
                "summary": "ok",
                "spec_compliance_notes": "half notes",
                "suggested_commit_message": "feat: half",
            }

    result = run_coordinator(
        _FailCombinedWithNotes(),
        CodeReviewInput(
            files={"a.py": "def a(): pass", "b.py": "def b(): pass"},
            task_description="t",
            language="python",
        ),
    )
    assert result.spec_compliance_notes == "half notes\n\nhalf notes"
    # Two sub-reviews → neither commit message saw the whole change.
    assert result.suggested_commit_message == ""


# ---------------------------------------------------------------------------
# Language threading
# ---------------------------------------------------------------------------


def test_language_is_threaded_into_every_chunk_prompt() -> None:
    """The caller-declared language must reach the chunk prompt — never be
    re-guessed from the first 500 chars of the chunk."""

    class _Recorder(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.prompts: List[str] = []

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            self.prompts.append(prompt)
            return super().complete_json(prompt, **kwargs)

    client = _Recorder()
    # No "def " anywhere: the old heuristic would have guessed typescript.
    run_coordinator(
        client,
        CodeReviewInput(
            files={"config.py": "TIMEOUT = 30\nRETRIES = 2"},
            task_description="t",
            language="python",
        ),
    )
    assert client.prompts
    assert all("**Language:** python" in p for p in client.prompts)


# ---------------------------------------------------------------------------
# End-to-end property: large synthetic input through CodeReviewAgent.run
# ---------------------------------------------------------------------------


class _RecordingClient(DummyLLMClient):
    """Delegates to Dummy but records every prompt."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: List[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.prompts.append(prompt)
        return super().complete_json(prompt, **kwargs)


def test_large_synthetic_input_is_fully_covered_with_bounded_prompts() -> None:
    from code_review_agent.agent import CodeReviewAgent

    client = _RecordingClient()
    cap = compute_code_review_map_chunk_chars(client)
    files = {f"app/mod_{i}.py": _numbered_file(2_500) for i in range(5)}  # ~500K chars total

    agent = CodeReviewAgent(llm_client=client)
    result = agent.run(CodeReviewInput(files=files, task_description="t", language="python"))

    assert isinstance(result, CodeReviewOutput)
    assert len(client.prompts) > 1
    # Every file appears in at least one map prompt.
    for path in files:
        assert any(f"### {path} ###" in p for p in client.prompts)
    # Every prompt is bounded: chunk cap plus the fixed instruction overhead.
    assert all(len(p) <= cap + 2_000 for p in client.prompts)


# ---------------------------------------------------------------------------
# Progress callback reporting
# ---------------------------------------------------------------------------


def test_coordinator_reports_per_chunk_progress() -> None:
    """With 2 chunks the coordinator reports one 'chunk i/2 reviewed' per
    completion (fractions inside (0.10, 0.90], non-decreasing even with
    parallel workers), then finalizing and done at 1.0."""
    big_file_1 = "### app/main.py ###\n" + ("a" * 25_000)
    big_file_2 = "### app/util.py ###\n" + ("b" * 25_000)
    code = big_file_1 + "\n\n" + big_file_2

    calls: list = []

    def _cb(step: str, detail: str, fraction: float) -> None:
        calls.append((step, detail, fraction))

    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(code=code, task_description="Add feature", language="python"),
        progress_callback=_cb,
    )
    assert isinstance(result, CodeReviewOutput)

    steps = [c[0] for c in calls]
    assert steps[0] == "preparing"
    assert "finalizing" in steps
    assert steps[-1] == "done"

    reviewing = [c for c in calls if c[0] == "reviewing"]
    assert any("chunk 1/2 reviewed" in c[1] for c in reviewing)
    assert any("chunk 2/2 reviewed" in c[1] for c in reviewing)
    assert all(0.10 < c[2] <= 0.90 for c in reviewing), reviewing

    fractions = [c[2] for c in calls]
    assert fractions == sorted(fractions), "fractions must be non-decreasing"
    assert fractions[-1] == 1.0
    assert "approved=" in calls[-1][1]


def test_empty_input_still_reports_done() -> None:
    """The done/1.0 postcondition holds on the empty-input short-circuit too."""
    calls: list = []
    result = run_coordinator(
        DummyLLMClient(),
        CodeReviewInput(code="", task_description="t"),
        progress_callback=lambda s, d, f: calls.append((s, d, f)),
    )
    assert result.approved is True
    assert calls[-1][0] == "done"
    assert calls[-1][2] == 1.0
