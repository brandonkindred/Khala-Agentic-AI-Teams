"""Cross-builder bounded-content tests for the code-review scoped tools.

The false-positive verifier, architecture-consistency pass, side-effect-impact
pass, and merged architecture/side-effect pass each build their own tool list
via a different call site, but every one of those lists composes the same
underlying ``false_positive_filter._build_tools`` base. The size/count caps on
that base (``read_lines``'s span cap, ``find_references``'s hit cap, and the
repo-reader listing cap behind ``list_files``) are already unit-tested once
against the base builder directly. This module instead proves the caps survive
being wired through *each* builder's own production call site, so a future
change that drops a cap for one pass (but not the others) fails here rather
than shipping unnoticed. Purely offline: no LLM client is constructed.
"""

from __future__ import annotations

import os
from typing import Callable, List

import code_review_agent.architecture_consistency_pass as arch_mod
import code_review_agent.false_positive_filter as fp_mod
import code_review_agent.merged_architecture_side_effect_pass as merged_mod
import code_review_agent.side_effect_impact_pass as side_mod
import pytest
from code_review_agent.false_positive_filter import (
    _READ_LINES_MAX_SPAN,
    _SEARCH_MATCH_LIMIT,
    CodebaseIndex,
)
from code_review_agent.repo_reader import DiskRepoReader

# --------------------------------------------------------------------------- helpers


def _tool_by_name(tools: List[object], name: str):
    """Return the tool whose ``tool_name`` matches ``name``.

    Raises:
        ValueError: when no tool with that name is present.
    """
    for tool in tools:
        if getattr(tool, "tool_name", None) == name:
            return tool
    raise ValueError(f"Tool {name!r} not found")


def _write(root, rel: str, content: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


# Every one of the four production call sites that assembles a tool list for a
# code-review pass, normalized to a single ``(index) -> tools`` signature so
# the bounded-content tests below can run identically against each.
BUILDERS: List[tuple] = [
    ("fp", lambda index: fp_mod._build_tools(index)),
    ("arch", lambda index: arch_mod._build_tools(index)),
    ("side_effect", lambda index: side_mod.build_side_effect_tools(index)),
    ("merged_side_off", lambda index: merged_mod._build_merged_pass_tools(index, side_on=False)),
    ("merged_side_on", lambda index: merged_mod._build_merged_pass_tools(index, side_on=True)),
]
_BUILDER_IDS = [label for label, _ in BUILDERS]


# --------------------------------------------------------------------------- read_lines


@pytest.mark.parametrize("label, build_tools", BUILDERS, ids=_BUILDER_IDS)
def test_read_lines_tool_enforces_bounded_span_across_builders(
    label: str, build_tools: Callable[[CodebaseIndex], list]
) -> None:
    """Every builder's read_lines tool refuses a span over _READ_LINES_MAX_SPAN
    and returns exactly _READ_LINES_MAX_SPAN numbered lines at the cap -- proving
    the shared span cap survives that builder's own tool wiring, not just the
    false-positive filter's base builder."""
    body = "\n".join(f"line-{i}" for i in range(1, _READ_LINES_MAX_SPAN + 5)) + "\n"
    index = CodebaseIndex(files={"big.py": body})
    read_lines = _tool_by_name(build_tools(index), "read_lines")

    oversize = read_lines("big.py", 1, _READ_LINES_MAX_SPAN + 1)
    assert f"maximum is {_READ_LINES_MAX_SPAN}" in oversize

    at_cap = read_lines("big.py", 1, _READ_LINES_MAX_SPAN)
    body_lines = at_cap.splitlines()[1:]
    assert len(body_lines) == _READ_LINES_MAX_SPAN


# --------------------------------------------------------------------------- find_references


@pytest.mark.parametrize("label, build_tools", BUILDERS, ids=_BUILDER_IDS)
def test_find_references_tool_caps_hit_count_across_builders(
    label: str, build_tools: Callable[[CodebaseIndex], list]
) -> None:
    """find_references never returns more than _SEARCH_MATCH_LIMIT hits, even
    when a symbol occurs far more often than that -- through each builder's own
    tool, not just the false-positive filter's base builder."""
    path = "app/big.txt"
    occurrences = _SEARCH_MATCH_LIMIT + 10
    body = "\n".join(f"foo() call site {i}" for i in range(1, occurrences + 1)) + "\n"
    index = CodebaseIndex(files={path: body})
    find_references = _tool_by_name(build_tools(index), "find_references")

    result = find_references("foo")
    assert result.count(f"{path}:") == _SEARCH_MATCH_LIMIT


# --------------------------------------------------------------------------- list_files


@pytest.mark.parametrize("label, build_tools", BUILDERS, ids=_BUILDER_IDS)
def test_list_files_tool_respects_repo_reader_listing_cap_across_builders(
    label: str, build_tools: Callable[[CodebaseIndex], list], tmp_path
) -> None:
    """list_files stays capped at the attached DiskRepoReader's max_listed_files
    through each builder's own tool, proving no builder's wiring bypasses the
    repo reader's listing cap."""
    for i in range(10):
        _write(tmp_path, f"f{i}.py", "x")
    reader = DiskRepoReader(str(tmp_path), max_listed_files=3)
    index = CodebaseIndex(files={}, repo_reader=reader)
    list_files = _tool_by_name(build_tools(index), "list_files")

    result = list_files()
    assert len(result.splitlines()) == 3
