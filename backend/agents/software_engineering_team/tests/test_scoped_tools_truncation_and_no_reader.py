"""Cross-builder truncation/no-reader tests for the code-review scoped tools.

``test_scoped_tools_bounded_content.py`` proves the scoped tools' size/count
caps (``read_lines``'s span cap, ``find_references``'s hit cap, ``list_files``'s
repo-reader listing cap) survive being wired through each production
tool-builder call site. This module covers the other half of the same
"partial results must not look complete" contract: the *messaging* the tools
attach when a search is incomplete.

Two behaviors, each already unit-tested once against its base implementation
(``CodebaseIndex.find_references`` in ``test_false_positive_filter.py``;
``_search_repository``/its tool in ``test_side_effect_impact_pass.py``), are
re-checked here through every builder that exposes them:

    - **No-reader note.** When no repository reader is attached, a tool that
      searches beyond the submission says so explicitly instead of silently
      treating the submission as the whole codebase.
    - **Truncation flag.** When a reader is attached but the scan did not
      cover every candidate (the submission alone filled the match cap, or
      the repo-side scan hit its own file/listing cap), the result is flagged
      as truncated -- with or without hits -- rather than read as exhaustive.

``find_references`` is part of ``false_positive_filter._build_tools``, which
every one of the five builders composes, so it is checked across all five.
``search_repository`` is a side-effect-pass-only tool (the epic explicitly
calls out its complementarity), present only in the side-effect builder and
the merged pass's ``side_on=True`` variant, so it is checked across those two.
Purely offline: no LLM client is constructed anywhere in this file.
"""

from __future__ import annotations

import os
from typing import Callable, List

import code_review_agent.architecture_consistency_pass as arch_mod
import code_review_agent.false_positive_filter as fp_mod
import code_review_agent.merged_architecture_side_effect_pass as merged_mod
import code_review_agent.side_effect_impact_pass as side_mod
import pytest
from code_review_agent.false_positive_filter import _SEARCH_MATCH_LIMIT, CodebaseIndex
from code_review_agent.repo_reader import DiskRepoReader

# --------------------------------------------------------------------------- helpers

_NO_REPO = "No repository access is available beyond this submission."


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


# Every builder that exposes find_references, normalized to a single
# ``(index) -> tools`` signature -- the same five call sites covered by
# test_scoped_tools_bounded_content.py.
FIND_REFS_BUILDERS: List[tuple] = [
    ("fp", lambda index: fp_mod._build_tools(index)),
    ("arch", lambda index: arch_mod._build_tools(index)),
    ("side_effect", lambda index: side_mod.build_side_effect_tools(index)),
    ("merged_side_off", lambda index: merged_mod._build_merged_pass_tools(index, side_on=False)),
    ("merged_side_on", lambda index: merged_mod._build_merged_pass_tools(index, side_on=True)),
]
_FIND_REFS_IDS = [label for label, _ in FIND_REFS_BUILDERS]

# Only the two builders that actually include search_repository: the
# side-effect pass itself, and the merged pass's side_on=True variant (which
# composes build_side_effect_tools internally). merged_side_off never
# includes this tool at all.
SEARCH_REPO_BUILDERS: List[tuple] = [
    ("side_effect", lambda index: side_mod.build_side_effect_tools(index)),
    ("merged_side_on", lambda index: merged_mod._build_merged_pass_tools(index, side_on=True)),
]
_SEARCH_REPO_IDS = [label for label, _ in SEARCH_REPO_BUILDERS]


# --------------------------------------------------------------------------- find_references


@pytest.mark.parametrize("label, build_tools", FIND_REFS_BUILDERS, ids=_FIND_REFS_IDS)
def test_find_references_tool_reports_no_reader_across_builders(
    label: str, build_tools: Callable[[CodebaseIndex], list]
) -> None:
    """Without a repo reader, every builder's find_references tool says so
    explicitly -- through that builder's own tool wiring, not just the
    CodebaseIndex method directly."""
    index = CodebaseIndex(files={"a.py": "def foo():\n    pass\n"})
    find_references = _tool_by_name(build_tools(index), "find_references")

    result = find_references("foo")
    assert _NO_REPO in result


@pytest.mark.parametrize("label, build_tools", FIND_REFS_BUILDERS, ids=_FIND_REFS_IDS)
def test_find_references_tool_flags_truncated_scan_across_builders(
    label: str, build_tools: Callable[[CodebaseIndex], list], tmp_path
) -> None:
    """When the submission alone fills max_matches and a reader is attached,
    every builder's find_references tool flags the scan as truncated rather
    than silently skipping the repository half."""
    path = "app/big.txt"
    body = "\n".join(f"foo() call site {i}" for i in range(1, _SEARCH_MATCH_LIMIT + 1)) + "\n"
    index = CodebaseIndex(
        files={path: body},
        repo_reader=DiskRepoReader(str(tmp_path), max_listed_files=3),
    )
    _write(tmp_path, "other.py", "foo()\n")
    find_references = _tool_by_name(build_tools(index), "find_references")

    result = find_references("foo")
    assert "Scan truncated" in result


# --------------------------------------------------------------------------- search_repository


@pytest.mark.parametrize("label, build_tools", SEARCH_REPO_BUILDERS, ids=_SEARCH_REPO_IDS)
def test_search_repository_tool_reports_no_reader_across_builders(
    label: str, build_tools: Callable[[CodebaseIndex], list]
) -> None:
    """Without a repo reader, search_repository reports no repository access
    is available, through both builders that expose the tool."""
    index = CodebaseIndex(files={"a.py": "def bar():\n    pass\n"})
    search_repository = _tool_by_name(build_tools(index), "search_repository")

    assert search_repository("bar") == _NO_REPO


@pytest.mark.parametrize("label, build_tools", SEARCH_REPO_BUILDERS, ids=_SEARCH_REPO_IDS)
def test_search_repository_tool_flags_truncated_scan_across_builders(
    label: str, build_tools: Callable[[CodebaseIndex], list], tmp_path
) -> None:
    """A repo whose file listing itself was truncated (more files than the
    reader's own cap) must flag search_repository's result as truncated even
    though matches were found -- a partial hit list must not read as
    complete, through both builders that expose the tool."""
    for i in range(10):
        _write(tmp_path, f"f{i}.py", "needle\n")
    reader = DiskRepoReader(str(tmp_path), max_listed_files=3)
    index = CodebaseIndex(files={}, repo_reader=reader)
    search_repository = _tool_by_name(build_tools(index), "search_repository")

    result = search_repository("needle")
    assert "needle" in result.lower()
    assert "truncated" in result.lower()
