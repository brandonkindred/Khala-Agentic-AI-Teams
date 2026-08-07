"""Assembly tests for ``build_change_surface_from_patches``."""

from __future__ import annotations

from collections import OrderedDict

from software_engineering_team.code_review_agent.change_surface import (
    LineRange,
    _merge_line_ranges,
    _pre_number_ranges,
)


def test_merge_line_ranges_overlaps_and_adjacent() -> None:
    ranges = (
        LineRange(6, 7),
        LineRange(1, 2),
        LineRange(3, 4),  # adjacent to 1-2 → merge to 1-4
        LineRange(6, 9),  # overlaps 6-7 → 6-9
    )
    assert _merge_line_ranges(ranges) == (LineRange(1, 4), LineRange(6, 9))


def test_merge_line_ranges_empty() -> None:
    assert _merge_line_ranges(()) == ()
    assert _merge_line_ranges([]) == ()


def test_pre_number_ranges_single_span() -> None:
    content = "a\nb\nc\n"
    body = _pre_number_ranges(content, (LineRange(2, 3),))
    assert body == "2: b\n3: c"


def test_pre_number_ranges_inserts_gap_marker() -> None:
    content = "a\nb\nc\nd\ne\n"
    body = _pre_number_ranges(content, (LineRange(1, 1), LineRange(4, 5)))
    assert body == "1: a\n...\n4: d\n5: e"
