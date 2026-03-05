"""Tests for spec chunking utilities."""

import pytest

from planning_team.spec_chunking import chunk_spec_by_sections, chunk_spec_by_size


def test_chunk_spec_by_size_empty() -> None:
    """Empty or whitespace spec returns empty list."""
    assert chunk_spec_by_size("") == []
    assert chunk_spec_by_size("   \n  ") == []


def test_chunk_spec_by_size_small_spec() -> None:
    """Spec smaller than max_chars returns single chunk."""
    spec = "Short spec content."
    assert chunk_spec_by_size(spec, max_chars=100) == [spec]


def test_chunk_spec_by_size_splits_large_spec() -> None:
    """Large spec is split into multiple chunks."""
    spec = "x" * 25000
    chunks = chunk_spec_by_size(spec, max_chars=10000, overlap=0)
    assert len(chunks) >= 2
    assert sum(len(c) for c in chunks) >= 25000
    assert all(len(c) <= 10000 for c in chunks)


def test_chunk_spec_by_size_overlap() -> None:
    """Overlap causes last N chars of chunk N to appear at start of chunk N+1."""
    spec = "a" * 5000 + "b" * 5000 + "c" * 5000  # 15K
    chunks = chunk_spec_by_size(spec, max_chars=6000, overlap=500)
    assert len(chunks) >= 2
    # Overlap means chunk boundaries shift; total chars across chunks exceeds len(spec)
    total_with_overlap = sum(len(c) for c in chunks)
    assert total_with_overlap > len(spec)


def test_chunk_spec_by_size_exact_boundary() -> None:
    """Spec exactly at max_chars returns single chunk."""
    spec = "x" * 12000
    chunks = chunk_spec_by_size(spec, max_chars=12000)
    assert len(chunks) == 1
    assert len(chunks[0]) == 12000


def test_chunk_spec_by_sections_empty() -> None:
    """Empty spec returns empty list."""
    assert chunk_spec_by_sections("") == []
    assert chunk_spec_by_sections("   \n  ") == []


def test_chunk_spec_by_sections_small_spec() -> None:
    """Spec smaller than max_chars returns single chunk with empty title."""
    spec = "Intro content without headers."
    result = chunk_spec_by_sections(spec, max_chars=100)
    assert len(result) == 1
    assert result[0][0] == ""
    assert result[0][1] == spec


def test_chunk_spec_by_sections_splits_by_headers() -> None:
    """Spec with ## headers is split by sections."""
    spec = """## Overview
Overview content here.

## Features
Features content.

## API
API content.
"""
    # Use small max_chars to force multiple chunks so section titles appear
    result = chunk_spec_by_sections(spec, max_chars=50)
    assert len(result) >= 1
    titles = [r[0] for r in result]
    assert "Overview" in titles or any("Overview" in t for t in titles)


def test_chunk_spec_by_sections_large_section_sub_splits() -> None:
    """Section exceeding max_chars is sub-split by size."""
    spec = """## Big Section
""" + "x" * 15000
    result = chunk_spec_by_sections(spec, max_chars=5000)
    assert len(result) >= 2
    # Should have "Big Section (part 1)", "Big Section (part 2)", etc.
    assert any("part" in r[0] for r in result)


def test_chunk_spec_by_sections_preserves_content() -> None:
    """All content is preserved across chunks."""
    spec = "## A\nContent A\n\n## B\nContent B"
    result = chunk_spec_by_sections(spec, max_chars=1000)
    combined = "\n\n".join(r[1] for r in result)
    assert "Content A" in combined
    assert "Content B" in combined
