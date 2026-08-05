"""Unit tests for DbcCommentsAgent's local, self-contained chunking module
(``technical_writers/dbc_comments_agent/chunking.py``), decoupled from the
agent-level integration tests in ``test_dbc_chunking.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from software_engineering_team.technical_writers.dbc_comments_agent.chunking import (
    DbcChunk,
    DbcFileSegment,
    build_dbc_chunks,
)


def test_whole_file_segment_is_not_partial() -> None:
    seg = DbcFileSegment(path="a.py", content="def f():\n    pass\n", start_line=1, total_lines=2)
    assert seg.is_partial is False
    assert seg.prompt_content == seg.content


def test_partial_segment_renders_with_line_number_prefixes() -> None:
    seg = DbcFileSegment(path="a.py", content="line5\nline6\n", start_line=5, total_lines=10)
    assert seg.is_partial is True
    assert seg.prompt_content == "5: line5\n6: line6"


def test_segment_validator_rejects_start_line_below_one() -> None:
    with pytest.raises(ValidationError):
        DbcFileSegment(path="a.py", content="x\n", start_line=0, total_lines=1)


def test_segment_validator_rejects_total_lines_below_line_count() -> None:
    with pytest.raises(ValidationError):
        DbcFileSegment(path="a.py", content="a\nb\nc\n", start_line=1, total_lines=1)


def test_segment_validator_rejects_end_line_past_total() -> None:
    with pytest.raises(ValidationError):
        DbcFileSegment(path="a.py", content="a\nb\n", start_line=5, total_lines=5)


def test_chunk_validator_rejects_duplicate_path_segments() -> None:
    seg = DbcFileSegment(path="a.py", content="x\n", start_line=1, total_lines=1)
    with pytest.raises(ValidationError):
        DbcChunk(segments=[seg, seg.model_copy()])


def test_chunk_content_renders_headered_and_headerless_segments() -> None:
    headered = DbcFileSegment(path="a.py", content="x\n", start_line=1, total_lines=1)
    headerless = DbcFileSegment(path="", content="y\n", start_line=1, total_lines=1)
    chunk = DbcChunk(segments=[headered, headerless])
    assert chunk.content == "### a.py ###\nx\n\n\ny\n"


def test_build_dbc_chunks_covers_every_block_exactly_once() -> None:
    blocks = [("a.py", "def a(): pass\n"), ("b.py", "def b(): pass\n")]
    chunks = build_dbc_chunks(blocks, max_chars=10_000)
    assert len(chunks) == 1
    paths = {seg.path for chunk in chunks for seg in chunk.segments}
    assert paths == {"a.py", "b.py"}


def test_build_dbc_chunks_never_puts_two_segments_of_same_path_in_one_chunk() -> None:
    # A single oversized file gets split into multiple segments; each
    # oversized segment is placed in its own chunk (never combined with a
    # sibling segment of the same path).
    big_content = "\n".join(f"line {i}" for i in range(2000)) + "\n"
    blocks = [("big.py", big_content)]
    chunks = build_dbc_chunks(blocks, max_chars=500)
    assert len(chunks) > 1
    for chunk in chunks:
        paths = [seg.path for seg in chunk.segments]
        assert len(paths) == len(set(paths))
    # Every segment's rendered content, reassembled, reproduces the original.
    reassembled = "".join(seg.content for chunk in chunks for seg in chunk.segments)
    assert reassembled == big_content


def test_build_dbc_chunks_splits_across_multiple_chunks_when_over_budget() -> None:
    blocks = [(f"file{i}.py", f"def f{i}(): pass\n" * 50) for i in range(20)]
    chunks = build_dbc_chunks(blocks, max_chars=2_000)
    assert len(chunks) > 1
    covered_paths = {seg.path for chunk in chunks for seg in chunk.segments}
    assert covered_paths == {f"file{i}.py" for i in range(20)}
    for chunk in chunks:
        assert len(chunk.content) <= 2_000 or len(chunk.segments) == 1


def test_build_dbc_chunks_single_line_over_budget_is_placed_alone() -> None:
    huge_line = "x" * 5_000
    blocks = [("huge.py", huge_line)]
    chunks = build_dbc_chunks(blocks, max_chars=100)
    assert len(chunks) == 1
    assert chunks[0].segments[0].content == huge_line


def test_build_dbc_chunks_headerless_block_round_trips() -> None:
    blocks = [("", "def f(): pass\n")]
    chunks = build_dbc_chunks(blocks, max_chars=10_000)
    assert len(chunks) == 1
    assert chunks[0].content == "def f(): pass\n"
