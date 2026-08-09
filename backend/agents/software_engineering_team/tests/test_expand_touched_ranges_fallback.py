"""Heuristic / capped-context fallback for ``expand_touched_ranges`` (#5420)."""

from __future__ import annotations

from software_engineering_team.code_review_agent.change_surface import (
    DEFAULT_EXPANSION_CONTEXT_LINES,
    LineRange,
    expand_touched_ranges,
)


def test_non_python_uses_heuristic_start_capped() -> None:
    content = "function f() {\n  return 1;\n}\n"
    ranges = expand_touched_ranges(content, {2}, path="f.ts")
    assert ranges == (LineRange(1, 3),)
    assert ranges[0].end_line - ranges[0].start_line + 1 <= DEFAULT_EXPANSION_CONTEXT_LINES
    # Non-positive touched lines are ignored on the fallback path too.
    assert expand_touched_ranges(content, {0, -3, 2}, path="f.ts") == (LineRange(1, 3),)


def test_no_heuristic_uses_centered_context_window() -> None:
    # All indented — no column-0 construct start for the heuristic.
    content = "    const a = 1;\n    const b = 2;\n    const c = 3;\n"
    ranges = expand_touched_ranges(content, {2}, path="snippet.ts")
    assert ranges == (LineRange(1, 3),)


def test_fallback_never_returns_whole_large_file() -> None:
    lines = [f"    x{i} = {i}" for i in range(1, 101)]
    content = "\n".join(lines) + "\n"
    ranges = expand_touched_ranges(content, {50}, path="big.ts")
    assert len(ranges) == 1
    r = ranges[0]
    assert r.end_line - r.start_line + 1 <= DEFAULT_EXPANSION_CONTEXT_LINES
    assert (r.start_line, r.end_line) != (1, 100)


def test_module_level_python_uses_capped_fallback() -> None:
    content = "def outer():\n    return 1\n\nx = 1\n"
    ranges = expand_touched_ranges(content, {4}, path="mod.py")
    assert ranges == (LineRange(4, 4),)


def test_unparseable_python_uses_capped_fallback() -> None:
    ranges = expand_touched_ranges("def broken(\n", {1}, path="broken.py")
    assert ranges == (LineRange(1, 1),)


def test_empty_path_non_python_uses_fallback() -> None:
    content = "function f() {\n  return 1;\n}\n"
    ranges = expand_touched_ranges(content, {2}, path="")
    assert ranges == (LineRange(1, 3),)


def test_heuristic_start_far_above_touched_uses_window() -> None:
    # Column-0 at line 1, touched line beyond CAP distance → centered window.
    head = "export const ROOT = 1;\n"
    pad = "\n".join(f"    // pad {i}" for i in range(2, 40))
    content = head + pad + "\n    const target = 1;\n"
    # Line numbers: 1 = ROOT, 2..39 = pads, 40 = target
    ranges = expand_touched_ranges(content, {40}, path="far.ts")
    assert len(ranges) == 1
    r = ranges[0]
    assert r.start_line <= 40 <= r.end_line
    assert r.end_line - r.start_line + 1 <= DEFAULT_EXPANSION_CONTEXT_LINES
    assert r.start_line > 1  # must not stretch from line 1 through CAP only at start
