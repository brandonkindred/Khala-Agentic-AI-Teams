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
    MIN_SPLIT_SEGMENT_CHARS,
    _is_pre_numbered,
    build_chunks,
    build_review_chunks,
    parse_code_into_file_blocks,
    run_coordinator,
    split_block_into_segments,
)
from code_review_agent.models import CodeReviewInput, CodeReviewOutput, FileSegment, ReviewChunk

from llm_service import LLMSemanticExhaustionError
from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.shared.context_sizing import compute_code_review_map_chunk_chars

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


def test_run_coordinator_keeps_same_description_on_different_lines() -> None:
    """Two findings sharing file_path + description but on different lines are
    distinct (line anchors inline comments), so dedup must keep both."""
    code = "### app/main.py ###\n" + ("x" * 20_000)

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
    code = "### app/main.py ###\n" + ("x" * 20_000)

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
    assert (seg.start_line, seg.part_index, seg.part_count) == (1, 1, 1)
    assert seg.total_lines == 10
    assert seg.is_partial is False
    assert seg.line_offset == 0


def test_split_reassembles_content_exactly_and_tracks_lines() -> None:
    content = _numbered_file(300)  # ~12.3K chars
    segments = split_block_into_segments("a.py", content, max_chars=4_000)
    assert len(segments) > 1
    assert "".join(s.content for s in segments) == content
    assert all(len(s.content) <= 4_000 for s in segments)
    # part bookkeeping is consistent
    assert [s.part_index for s in segments] == list(range(1, len(segments) + 1))
    assert all(s.part_count == len(segments) for s in segments)
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
# _is_pre_numbered
# ---------------------------------------------------------------------------


def test_pre_numbered_detection_positive_and_negative() -> None:
    hunk = "\n".join(f"{i}: some_code()" for i in range(4000, 4010))
    assert _is_pre_numbered(hunk) is True
    assert _is_pre_numbered("def foo():\n    return 1\n\nx = 2\ny = 3") is False
    # A short fully-numbered hunk still counts
    assert _is_pre_numbered("12: a\n13: b") is True
    # Mostly un-numbered lines do not
    assert _is_pre_numbered("1: a\nplain\nplain\nplain\nplain") is False


def test_split_flags_pre_numbered_segments_with_zero_offset() -> None:
    hunk = "\n".join(f"{4000 + i}: code_{i}()".ljust(40, " ") for i in range(300))
    segments = split_block_into_segments("pr.py", hunk, max_chars=4_000)
    assert len(segments) > 1
    assert all(s.pre_numbered for s in segments)
    assert all(s.line_offset == 0 for s in segments)


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


def test_empty_input_short_circuits() -> None:
    for input_data in (
        CodeReviewInput(code="", task_description="t"),
        CodeReviewInput(files={}, task_description="t"),
        CodeReviewInput(files={"a.py": "   "}, task_description="t"),
    ):
        result = run_coordinator(DummyLLMClient(), input_data)
        assert result.approved is True
        assert result.issues == []
        assert result.summary == "No code to review."


# ---------------------------------------------------------------------------
# Failure degradation
# ---------------------------------------------------------------------------


class _SelectiveRaiser(DummyLLMClient):
    """Raises for prompts containing a marker; otherwise delegates to Dummy.

    Records every prompt so tests can count map calls.
    """

    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker
        self.prompts: List[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.prompts.append(prompt)
        if self.marker in prompt:
            raise LLMSemanticExhaustionError("LLM returned reasoning only (no content)")
        return super().complete_json(prompt, **kwargs)


def test_failing_multi_segment_chunk_bisects_and_recovers() -> None:
    """A chunk whose combined review fails is bisected per segment; both halves
    succeed individually, so nothing is degraded and review completes."""

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


def test_terminal_failure_degrades_to_info_finding_without_blocking() -> None:
    """One small file keeps failing (below the bisect floor) while another chunk
    succeeds: the failed lines surface as an info finding and approval is kept."""
    llm_probe = DummyLLMClient()
    cap = compute_code_review_map_chunk_chars(llm_probe)
    filler_size = cap - 2_000  # forces the two files into separate chunks
    files = {
        "bad.py": "FAILME = True\n" + ("x = 1\n" * 50),
        "good.py": "ok = 1\n".ljust(filler_size, "#"),
    }
    assert len(files["bad.py"]) < 2 * MIN_SPLIT_SEGMENT_CHARS

    client = _SelectiveRaiser("FAILME")
    result = run_coordinator(
        client,
        CodeReviewInput(files=files, task_description="t", language="python"),
    )

    info = [i for i in result.issues if i.severity == "info"]
    assert len(info) == 1
    assert info[0].file_path == "bad.py"
    assert "NOT reviewed" in info[0].description
    assert "lines 1-51" in info[0].description
    # The successful chunk approved; the degraded one must not block.
    assert result.approved is True


def test_all_chunks_failed_fails_closed() -> None:
    client = _SelectiveRaiser("def")
    result = run_coordinator(
        client,
        CodeReviewInput(
            files={"only.py": "def only(): pass"},
            task_description="t",
            language="python",
        ),
    )
    assert result.approved is False
    high = [i for i in result.issues if i.severity == "high"]
    assert len(high) == 1
    assert "could not run" in high[0].description
    info = [i for i in result.issues if i.severity == "info"]
    assert len(info) == 1
    assert info[0].file_path == "only.py"


def test_large_failing_file_bisects_by_lines_then_degrades_per_range() -> None:
    """A single big segment that keeps failing bisects by lines until the
    floor, then reports one info finding per un-reviewed line range."""
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
    result = run_coordinator(
        client,
        CodeReviewInput(files={"big.py": content}, task_description="t", language="python"),
    )

    assert result.approved is False  # zero successes → fail closed
    info = [i for i in result.issues if i.severity == "info"]
    assert len(info) >= 2  # the bisected halves degraded separately
    assert all(i.file_path == "big.py" for i in info)
    # Together the info findings name the whole file's line range exactly once.
    ranges = sorted(
        tuple(map(int, i.description.split("(lines ")[1].split(" of")[0].split("-"))) for i in info
    )
    assert ranges[0][0] == 1
    assert ranges[-1][1] == n_lines
    for (_, prev_end), (next_start, _) in zip(ranges, ranges[1:]):
        assert next_start == prev_end + 1


def test_failing_single_giant_line_degrades_without_bisecting() -> None:
    """A single line above the bisect floor cannot split further (line
    boundaries are never broken); it must degrade cleanly, not recurse."""
    client = _SelectiveRaiser("FAILME")
    result = run_coordinator(
        client,
        CodeReviewInput(
            files={"min.js": "FAILME;" + ("x" * 20_000)},
            task_description="t",
            language="typescript",
        ),
    )
    assert result.approved is False  # sole chunk failed → fail closed
    info = [i for i in result.issues if i.severity == "info"]
    assert len(info) == 1
    assert info[0].file_path == "min.js"
    assert len(client.prompts) == 1  # no bisect retries possible


def test_headerless_code_reviews_as_single_unnamed_block() -> None:
    client = _ScriptedClient([{"approved": True, "issues": [], "summary": "fine"}])
    result = run_coordinator(
        client, CodeReviewInput(code="x = compute()\ny = x + 1", task_description="t")
    )
    assert result.approved is True
    assert result.summary == "fine"


def test_normalize_issue_path_blank_and_suffix_cases() -> None:
    from code_review_agent.coordinator import _issues_from_chunk_output, _normalize_issue_path

    seg = FileSegment(path="a.py", content="x = 1", start_line=501, total_lines=900)
    chunk = ReviewChunk(segments=[seg])
    assert _normalize_issue_path("", chunk) == "a.py"
    assert _normalize_issue_path("a.py (lines 501-505 of 900)", chunk) == "a.py"
    two = ReviewChunk(segments=[seg, FileSegment(path="b.py", content="y = 2", total_lines=1)])
    assert _normalize_issue_path("", two) == ""
    # Non-dict issue entries are skipped defensively.
    assert _issues_from_chunk_output(chunk, ["not-a-dict"]) == []


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


def test_coordinator_multi_chunk_leaves_notes_empty() -> None:
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
                "spec_compliance_notes": "notes",
                "suggested_commit_message": "msg",
            }
        ]
    )
    result = run_coordinator(client, CodeReviewInput(files=files, task_description="t"))
    assert result.approved is True
    assert result.spec_compliance_notes == ""
    assert result.suggested_commit_message == ""


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
