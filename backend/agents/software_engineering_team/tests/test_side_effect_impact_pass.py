"""Tests for the side-effect / blast-radius pass.

This pass is purely additive (it can only ADD findings on top of what the map
phase, false-positive filter, and architecture-consistency pass already
produced) and fail-safe (any setup or LLM failure yields no additional
findings, never an exception). The standalone module remains the Temporal
activity path; the in-process coordinator routes architecture + side-effect
through ``merged_architecture_side_effect_pass`` instead.

Style mirrors ``test_architecture_consistency_pass.py``: the LLM seam is
exercised with ``DummyLLMClient`` subclasses that pattern-match on the user
prompt (never the system prompt). Direct unit tests of
``find_side_effect_impact_issues`` match this pass's prompt anchor; end-to-end
``run_coordinator`` tests match the merged-pass prompt anchor so one scripted
client can serve the chunk-review call and the merged pass's side-effect
findings in a single run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from code_review_agent.coordinator import run_coordinator
from code_review_agent.false_positive_filter import (
    _MAX_DUPLICATE_TOOL_CALLS,
    _MAX_TOTAL_TOOL_CALLS,
    CodebaseIndex,
)
from code_review_agent.models import CodeReviewInput, CodeReviewIssue
from code_review_agent.repo_reader import DiskRepoReader
from code_review_agent.side_effect_impact_pass import (
    _build_prompt,
    _build_side_effect_tools,
    _coerce_finding,
    _is_changed_file,
    _parse_findings,
    _search_repository,
    _validate_finding_line,
    _validate_findings,
    find_side_effect_impact_issues,
)
from tests.submission_pass_two_call_client import (
    SubmissionPassTwoCallClient,
    wire_run_agent_via_reasoning_with_raw,
)

from llm_service import LLMJsonParseError
from llm_service.clients.dummy import DummyLLMClient

pytest_plugins = ["tests.submission_pass_two_call_client"]

# Unique anchor in this pass's user prompt (never the system prompt), distinct
# from architecture_consistency_pass's own anchor so a DummyLLMClient subclass
# can route between the two passes' calls without collision.
_SIDE_EFFECT_PASS_ANCHOR = "Summarize side-effect-impact findings in structured prose"
_MERGED_PASS_ANCHOR = "Merged submission pass:"
_ARCH_PASS_ANCHOR = "Summarize architecture-consistency findings in structured prose"


def _input(files: Optional[Dict[str, str]] = None) -> CodeReviewInput:
    return CodeReviewInput(
        files=files if files is not None else {"app/main.py": "def bar():\n    return 1\n"},
        task_description="wire up bar",
    )


class _FakeReader:
    """A minimal duck-typed RepoReader over an in-memory {path: content} map."""

    def __init__(self, files: Dict[str, str]):
        self._files = files

    def list_files(self):
        return list(self._files)

    def read_file(self, path: str):
        return self._files.get((path or "").strip())


# --------------------------------------------------------------------------- helpers


def _tool_by_name(tools, name: str):
    """Return the tool whose ``tool_name`` matches ``name``.

    Raises:
        ValueError: when no tool with that name is present.
    """
    for tool in tools:
        if getattr(tool, "tool_name", None) == name:
            return tool
    raise ValueError(f"Tool {name!r} not found")


def test_build_prompt_includes_changed_files() -> None:
    """User prompt inlines submission file paths and bodies."""
    index = CodebaseIndex.from_input(_input())
    prompt = _build_prompt(index)
    assert "app/main.py" in prompt
    assert "def bar():" in prompt


def test_build_prompt_inlines_all_changed_files_in_full() -> None:
    """Every changed file's full content reaches the prompt."""
    file_a_content = "x" * 50
    file_b_content = "y" * 50
    files = {"a.py": file_a_content, "b.py": file_b_content}
    index = CodebaseIndex.from_input(_input(files=files))
    prompt = _build_prompt(index)
    assert file_a_content in prompt
    assert file_b_content in prompt
    assert "more changed file(s) not shown above" not in prompt
    assert "Only the first" not in prompt


def test_build_prompt_mentions_search_repository_tool() -> None:
    index = CodebaseIndex.from_input(_input())
    prompt = _build_prompt(index)
    assert "search_repository" in prompt


def test_build_prompt_renders_replaced_content_section_when_present() -> None:
    """A path with a ``replaced_content`` entry gets its before-image section."""
    files = {"app/main.py": "def bar():\n    return 2\n"}
    index = CodebaseIndex.from_input(_input(files=files))
    prompt = _build_prompt(index, replaced_content={"app/main.py": "def bar():\n    return 1\n"})
    assert "Replaced (pre-change) content" in prompt
    assert "def bar():\n    return 1\n" in prompt


def test_build_prompt_omits_replaced_content_section_for_path_without_one() -> None:
    """A changed path absent from ``replaced_content`` renders no such section."""
    files = {"a.py": "aaa", "b.py": "bbb"}
    index = CodebaseIndex.from_input(_input(files=files))
    prompt = _build_prompt(index, replaced_content={"a.py": "old-a"})
    assert "Replaced (pre-change) content" in prompt
    assert "old-a" in prompt
    # b.py has no replaced_content entry: only one section should appear.
    assert prompt.count("Replaced (pre-change) content") == 1


def test_build_prompt_omits_replaced_content_section_when_absent() -> None:
    """Default (``replaced_content=None``) renders exactly as today."""
    index = CodebaseIndex.from_input(_input())
    prompt = _build_prompt(index)
    assert "Replaced (pre-change) content" not in prompt


def test_build_prompt_ignores_empty_replaced_content_dict() -> None:
    """An empty ``replaced_content`` mapping behaves like ``None``."""
    index = CodebaseIndex.from_input(_input())
    prompt = _build_prompt(index, replaced_content={})
    assert "Replaced (pre-change) content" not in prompt


def test_build_prompt_renders_replaced_content_only_for_batch_paths_that_have_one() -> None:
    """Per-path ``replaced_content`` gating holds when a batch spans multiple
    files and only some of them have a before-image entry."""
    files = {
        "app/main.py": "def bar():\n    return 2\n",
        "app/util.py": "def helper():\n    return 3\n",
    }
    index = CodebaseIndex.from_input(_input(files=files))
    prompt = _build_prompt(
        index,
        content_items=list(files.items()),
        batch_index=1,
        total_batches=2,
        replaced_content={"app/main.py": "def bar():\n    return 1\n"},
    )
    assert prompt.count("Replaced (pre-change) content") == 1
    assert "app/main.py — Replaced (pre-change) content" in prompt
    assert "app/util.py — Replaced (pre-change) content" not in prompt


# --------------------------------------------------------------------------- repo-wide search


def test_search_repository_returns_empty_with_no_reader() -> None:
    """Without a repo reader, search returns no matches and is not truncated."""
    index = CodebaseIndex(files={"app/main.py": "code"})
    assert _search_repository(index, "bar") == ([], False)


def test_search_repository_returns_empty_for_blank_query() -> None:
    index = CodebaseIndex(
        files={"app/main.py": "code"}, repo_reader=_FakeReader({"app/caller.py": "bar()"})
    )
    assert _search_repository(index, "") == ([], False)
    assert _search_repository(index, "   ") == ([], False)


def test_search_repository_finds_matches_in_repo_reader_files() -> None:
    """_search_repository returns line-numbered matches from repo_reader files outside the submission."""
    index = CodebaseIndex(
        files={"app/main.py": "def bar():\n    return 1\n"},
        repo_reader=_FakeReader({"app/caller.py": "from app.main import bar\n\nresult = bar()\n"}),
    )
    matches, truncated = _search_repository(index, "bar(")
    assert matches == [("app/caller.py", 3, "result = bar()")]
    assert truncated is False


def test_search_repository_skips_files_already_in_the_submission() -> None:
    """A path the repo reader also lists but that is already one of the
    submission's own files is skipped -- it is already reachable via
    search_codebase, so scanning it again here would be redundant work."""
    index = CodebaseIndex(
        files={"app/main.py": "needle here"},
        repo_reader=_FakeReader({"app/main.py": "needle here", "app/other.py": "needle there"}),
    )
    matches, truncated = _search_repository(index, "needle")
    assert matches == [("app/other.py", 1, "needle there")]
    assert truncated is False


def test_search_repository_respects_max_matches() -> None:
    index = CodebaseIndex(
        files={},
        repo_reader=_FakeReader({f"f{i}.py": "needle\n" for i in range(5)}),
    )
    matches, truncated = _search_repository(index, "needle", max_matches=2)
    assert len(matches) == 2
    assert truncated is True


def test_search_repository_respects_max_files_scanned() -> None:
    index = CodebaseIndex(
        files={},
        repo_reader=_FakeReader({f"f{i}.py": "needle\n" for i in range(5)}),
    )
    matches, truncated = _search_repository(index, "needle", max_files_scanned=2)
    assert len(matches) == 2
    assert truncated is True


def test_search_repository_not_truncated_when_scan_covers_every_candidate() -> None:
    """``truncated`` is ``False`` when the scan finishes the whole candidate
    list on its own, without hitting either cap."""
    index = CodebaseIndex(
        files={},
        repo_reader=_FakeReader({f"f{i}.py": "no match" for i in range(3)}),
    )
    matches, truncated = _search_repository(index, "needle", max_files_scanned=10)
    assert matches == []
    assert truncated is False


def test_search_repository_default_limit_stays_conservative_for_unknown_readers() -> None:
    """A duck-typed reader (not a known-cheap ``DiskRepoReader``) still gets the
    conservative default cap when the caller doesn't override it -- e.g. a
    ``GitHubRepoReader``, whose per-file cost is real and must stay bounded."""
    # Insertion order controls scan order for this in-memory fake; put the only
    # matching file after the conservative default cap so it is missed.
    files = {f"f{i}.py": "no match" for i in range(45)}
    files["z_match.py"] = "needle\n"
    index = CodebaseIndex(files={}, repo_reader=_FakeReader(files))
    matches, truncated = _search_repository(index, "needle")
    assert matches == []
    assert truncated is True


def test_search_repository_disk_reader_scans_beyond_the_conservative_cap(
    tmp_path: Path,
) -> None:
    """Regression test: a real ``DiskRepoReader`` (the SE-pipeline path) must not
    be limited to the GitHub-budget-driven default cap. ``DiskRepoReader.list_files()``
    returns paths in fixed alphabetical order, so with the old flat
    ``_REPO_SEARCH_FILE_SCAN_LIMIT`` applied uniformly, a needle placed in a
    file that sorts after the cap would be silently unreachable on every call --
    this asserts it IS found, proving the disk-specific higher cap is in effect."""
    for i in range(45):
        (tmp_path / f"a_{i:03d}.py").write_text("no match\n")
    (tmp_path / "z_caller.py").write_text("result = bar()\n")
    index = CodebaseIndex(files={}, repo_reader=DiskRepoReader(str(tmp_path)))
    matches, truncated = _search_repository(index, "bar(")
    assert matches == [("z_caller.py", 1, "result = bar()")]
    assert truncated is False


def test_search_repository_reports_truncated_when_disk_reader_listing_itself_is_capped(
    tmp_path: Path,
) -> None:
    """Regression test: for a DiskRepoReader, max_files_scanned is set equal to
    the reader's own listing cap, so a repository with MORE paths than that cap
    would have every returned (already-truncated) path scanned without the
    per-file-scan cap ever tripping -- the only way to detect this is asking the
    reader itself whether its listing was capped."""
    for i in range(10):
        (tmp_path / f"f{i}.py").write_text("no match\n")
    reader = DiskRepoReader(str(tmp_path), max_listed_files=4)
    index = CodebaseIndex(files={}, repo_reader=reader)
    matches, truncated = _search_repository(index, "needle")
    assert matches == []
    assert truncated is True


def test_search_repository_fails_safe_on_reader_error() -> None:
    """A list_files failure must report truncated=True so callers do not treat
    an empty result as proof the substring is absent from the repository."""

    class _RaisingReader:
        def list_files(self):
            raise RuntimeError("boom")

        def read_file(self, path: str):
            raise RuntimeError("boom")

    index = CodebaseIndex(files={}, repo_reader=_RaisingReader())
    assert _search_repository(index, "bar") == ([], True)


def test_search_repository_tool_flags_truncated_when_list_files_fails() -> None:
    """When list_files raises, the tool must warn that the scan was truncated
    rather than claiming an exhaustive 'no matches in the rest of the repository'."""

    class _RaisingReader:
        def list_files(self):
            raise RuntimeError("boom")

        def read_file(self, path: str):
            raise RuntimeError("boom")

    index = CodebaseIndex(files={"app/main.py": "code"}, repo_reader=_RaisingReader())
    search_repository = _build_side_effect_tools(index)[-1]
    result = search_repository("bar(")
    assert "No matches for" in result
    assert "truncated" in result.lower()
    assert "in the rest of the repository" not in result


def test_search_repository_skips_a_single_unreadable_file() -> None:
    """One file raising on read must not abort the scan of the rest, but the
    scan must report itself as incomplete rather than an exhaustive negative --
    the raising file's content was never actually inspected."""

    class _PartlyRaisingReader:
        def list_files(self):
            return ["bad.py", "good.py"]

        def read_file(self, path: str):
            if path == "bad.py":
                raise RuntimeError("boom")
            return "needle\n"

    index = CodebaseIndex(files={}, repo_reader=_PartlyRaisingReader())
    matches, truncated = _search_repository(index, "needle")
    assert matches == [("good.py", 1, "needle")]
    assert truncated is True


class _PartlyUnreadableReader:
    """Lists every path but returns None for a configured unreadable subset."""

    def __init__(self, files: Dict[str, str], unreadable: set[str]):
        self._files = files
        self._unreadable = unreadable

    def list_files(self):
        return list(self._files)

    def read_file(self, path: str):
        if path in self._unreadable:
            return None
        return self._files.get((path or "").strip())


def test_search_repository_skips_files_the_reader_cannot_read() -> None:
    """A path the reader lists but returns None for (fail-safe RepoReader
    contract) is skipped rather than crashing the scan, but -- same as the
    raising case -- must mark the scan truncated since that file's content
    was never actually inspected (e.g. a shared GitHubRepoReader fetch budget
    already exhausted by an earlier pass would surface exactly this way)."""
    index = CodebaseIndex(
        files={},
        repo_reader=_PartlyUnreadableReader(
            {"present.py": "needle here", "missing.py": ""},
            unreadable={"missing.py"},
        ),
    )
    matches, truncated = _search_repository(index, "needle")
    assert matches == [("present.py", 1, "needle here")]
    assert truncated is True


# --------------------------------------------------------------------------- tools


def test_build_side_effect_tools_includes_search_repository() -> None:
    index = CodebaseIndex(files={"app/main.py": "def bar(): pass\n"})
    tools = _build_side_effect_tools(index)
    names = {getattr(t, "tool_name", "") for t in tools}
    assert names == {
        "read_file",
        "read_lines",
        "read_function",
        "list_files",
        "search_codebase",
        "find_function_at_line",
        "find_references",
        "search_repository",
    }


def test_search_repository_tool_reports_no_reader() -> None:
    """search_repository tool reports that no repository access is available."""
    index = CodebaseIndex(files={"app/main.py": "code"})
    search_repository = _tool_by_name(_build_side_effect_tools(index), "search_repository")
    assert "No repository access" in search_repository("bar")
    doc = " ".join((search_repository.__doc__ or "").split()).lower()
    assert "fall back" not in doc
    assert "no repository access is available beyond the submission" in doc


def test_search_repository_tool_reports_no_matches() -> None:
    index = CodebaseIndex(
        files={"app/main.py": "code"}, repo_reader=_FakeReader({"app/caller.py": "unrelated"})
    )
    search_repository = _tool_by_name(_build_side_effect_tools(index), "search_repository")
    assert "No matches" in search_repository("bar(")


def test_search_repository_tool_finds_matches() -> None:
    """search_repository tool returns path:line matches from the rest of the repository."""
    index = CodebaseIndex(
        files={"app/main.py": "def bar(): pass\n"},
        repo_reader=_FakeReader({"app/caller.py": "result = bar()\n"}),
    )
    search_repository = _tool_by_name(_build_side_effect_tools(index), "search_repository")
    assert "app/caller.py:1: result = bar()" in search_repository("bar(")


def test_search_repository_tool_flags_truncated_scan_with_no_matches() -> None:
    """A truncated no-match scan must not read as an exhaustive negative result --
    the model needs to know the repository was larger than what got scanned."""
    files = {f"f{i}.py": "no match" for i in range(45)}
    index = CodebaseIndex(files={}, repo_reader=_FakeReader(files))
    search_repository = _tool_by_name(_build_side_effect_tools(index), "search_repository")
    result = search_repository("needle")
    assert "No matches for" in result
    assert "truncated" in result.lower()


def test_search_repository_tool_flags_truncated_scan_with_matches() -> None:
    """A truncated scan that DID find matches still warns there may be more."""
    files = {f"f{i}.py": "needle\n" for i in range(45)}
    index = CodebaseIndex(files={}, repo_reader=_FakeReader(files))
    search_repository = _tool_by_name(_build_side_effect_tools(index), "search_repository")
    result = search_repository("needle")
    assert "f0.py:1: needle" in result
    assert "truncated" in result.lower()


def test_search_repository_shares_the_run_level_duplicate_call_budget() -> None:
    """search_repository is built outside ``false_positive_filter._build_tools``,
    but must still share that one call tracker -- a repeated identical
    search_repository call gets the same "already called" note as the seven
    base tools, not silent unlimited repetition."""
    index = CodebaseIndex(
        files={"app/main.py": "def bar(): pass\n"},
        repo_reader=_FakeReader({"app/caller.py": "result = bar()\n"}),
    )
    search_repository = _tool_by_name(_build_side_effect_tools(index), "search_repository")
    for _ in range(_MAX_DUPLICATE_TOOL_CALLS):
        assert "already called" not in search_repository("bar(")
    result = search_repository("bar(")
    assert "app/caller.py:1: result = bar()" in result
    assert "already called search_repository" in result


def test_search_repository_shares_the_run_level_total_call_budget_with_base_tools() -> None:
    """Calls to the seven base tools and to search_repository count against
    ONE shared total-call budget -- exhausting it via the base tools must
    also short-circuit search_repository, proving the run-level cap covers
    this pass's whole tool set, not just the seven tools built by
    ``_build_tools``."""
    index = CodebaseIndex(
        files={"app/main.py": "def bar(): pass\n"},
        repo_reader=_FakeReader({"app/caller.py": "result = bar()\n"}),
    )
    tools = _build_side_effect_tools(index)
    list_files = _tool_by_name(tools, "list_files")
    search_repository = _tool_by_name(tools, "search_repository")
    for _ in range(_MAX_TOTAL_TOOL_CALLS):
        list_files()
    result = search_repository("bar(")
    assert "tool call budget" in result
    assert "exhausted" in result
    assert "app/caller.py" not in result


# --------------------------------------------------------------------------- line bounds


def test_validate_finding_line_keeps_in_range_line() -> None:
    """In-range line citations survive validation unchanged."""
    index = CodebaseIndex.from_input(_input(files={"a.py": "one\ntwo\nthree\n"}))
    assert _validate_finding_line(index, "a.py", 2) == 2


def test_validate_finding_line_drops_out_of_range_line() -> None:
    index = CodebaseIndex.from_input(_input(files={"a.py": "one\ntwo\nthree\n"}))
    assert _validate_finding_line(index, "a.py", 9999) is None


def test_validate_finding_line_drops_when_file_unresolved() -> None:
    index = CodebaseIndex.from_input(_input(files={"a.py": "one\ntwo\n"}))
    assert _validate_finding_line(index, "does/not/exist.py", 1) is None


def test_validate_finding_line_survives_file_content_starting_with_error() -> None:
    """A real file whose content starts with "Error:" must not be treated as an unreadable file."""
    index = CodebaseIndex.from_input(_input(files={"a.py": "Error: not a real failure\ntwo\n"}))
    assert _validate_finding_line(index, "a.py", 2) == 2
    assert _validate_finding_line(index, "a.py", 9999) is None


def test_validate_finding_line_trusts_pre_numbered_citation_as_is() -> None:
    index = CodebaseIndex.from_input(
        CodeReviewInput(
            files={"a.py": "4242: one\n4243: two\n"},
            task_description="t",
            pre_numbered=True,
        )
    )
    assert _validate_finding_line(index, "a.py", 4242, pre_numbered=True) == 4242
    assert _validate_finding_line(index, "a.py", None, pre_numbered=True) is None


def test_is_changed_file_true_only_for_submission_files() -> None:
    index = CodebaseIndex(
        files={"app/main.py": "code"},
        repo_reader=_FakeReader({"app/existing_helper.py": "helper code"}),
    )
    assert _is_changed_file(index, "app/main.py") is True
    assert _is_changed_file(index, "app/existing_helper.py") is False
    assert _is_changed_file(index, "") is False


def test_validate_findings_normalizes_a_changed_file_alias_to_its_real_key() -> None:
    """A finding anchored by a basename/suffix alias of a changed file is kept
    AND its ``file_path`` is normalized to the submission's real key."""
    index = CodebaseIndex(files={"app/main.py": "def bar():\n    return 1\n"})
    finding = CodeReviewIssue(
        category="side-effects", description="d1", file_path="main.py", line=1
    )
    validated = _validate_findings(index, [finding])
    assert validated[0].file_path == "app/main.py"
    assert validated[0].line == 1


def test_validate_findings_blanks_file_path_anchored_outside_the_diff() -> None:
    """A caller-impact finding that cites the OUT-OF-DIFF caller file it found
    the break in (rather than the changed function's own file) cannot become a
    useful PR comment -- that file is not part of the diff. Kept, but degraded
    to a submission-wide finding."""
    index = CodebaseIndex(
        files={"app/bar.py": "code"},
        repo_reader=_FakeReader({"app/caller.py": "bar()\n"}),
    )
    outside_diff = CodeReviewIssue(
        category="side-effects",
        description="app/caller.py assumes the old return shape",
        file_path="app/caller.py",
        line=1,
    )
    validated = _validate_findings(index, [outside_diff])
    assert validated[0].file_path == ""
    assert validated[0].line is None
    assert validated[0].description == "app/caller.py assumes the old return shape"


# --------------------------------------------------------------------------- parsing


def test_coerce_finding_accepts_side_effects_category() -> None:
    finding = _coerce_finding(
        {
            "severity": "high",
            "category": "side-effects",
            "file_path": "app/main.py",
            "description": "bar() no longer raises ValueError; app/caller.py still catches it",
            "suggestion": "update app/caller.py's except clause",
        }
    )
    assert finding is not None
    assert finding.category == "side-effects"
    assert finding.severity == "high"


def test_coerce_finding_accepts_documentation_category() -> None:
    """A docstring/comment-vs-implementation mismatch is a documentation-accuracy
    finding, not a side effect: the pass emits it under the ``documentation``
    category and ``_coerce_finding`` accepts it alongside ``side-effects``."""
    finding = _coerce_finding(
        {
            "severity": "medium",
            "category": "documentation",
            "file_path": "app/main.py",
            "description": "bar()'s docstring says it returns a list, but it returns a dict",
            "suggestion": "update bar()'s docstring to describe the dict it actually returns",
        }
    )
    assert finding is not None
    assert finding.category == "documentation"
    assert finding.severity == "medium"


def test_coerce_finding_carries_through_pre_existing_tag() -> None:
    """The model's optional pre_existing tag (used by the PR-review whole-file
    path to route a doc/impl-mismatch finding in untouched code to a
    human-review proposal instead of a blocking PR comment) survives
    conversion, tolerates string encodings, and defaults False when absent --
    mirrors chunking._issues_from_chunk_output's identical convention."""
    tagged_true = _coerce_finding(
        {"category": "side-effects", "description": "d1", "pre_existing": True}
    )
    tagged_str = _coerce_finding(
        {"category": "side-effects", "description": "d2", "pre_existing": "true"}
    )
    tagged_false_str = _coerce_finding(
        {"category": "side-effects", "description": "d3", "pre_existing": "false"}
    )
    untagged = _coerce_finding({"category": "side-effects", "description": "d4"})
    assert [f.pre_existing for f in (tagged_true, tagged_str, tagged_false_str, untagged)] == [
        True,
        True,
        False,
        False,
    ]


@pytest.mark.parametrize(
    "item",
    [
        "not-a-dict",
        {"category": "architecture", "description": "wrong category for this pass"},
        {"category": "side-effects", "description": ""},
        {"category": "", "description": "no category at all"},
        {
            "category": "side-effects",
            "description": "no behavior change found",
            "suggestion": "No changes needed.",
        },
    ],
)
def test_coerce_finding_rejects_invalid_items(item: object) -> None:
    assert _coerce_finding(item) is None


def test_coerce_finding_coerces_line_and_unknown_severity() -> None:
    finding = _coerce_finding(
        {
            "severity": "not-a-real-severity",
            "category": "side-effects",
            "description": "d",
            "line": "42",
        }
    )
    assert finding is not None
    assert finding.severity == "medium"
    assert finding.line == 42


def test_parse_findings_handles_off_contract_replies() -> None:
    assert _parse_findings("not-a-dict") == []
    assert _parse_findings({}) == []
    assert _parse_findings({"findings": "not-a-list"}) == []
    parsed = _parse_findings(
        {
            "findings": [
                {"category": "side-effects", "description": "real"},
                {"category": "bogus", "description": "dropped"},
            ]
        }
    )
    assert len(parsed) == 1
    assert parsed[0].description == "real"


# --------------------------------------------------------------------------- gating / fail-safe


def test_returns_empty_when_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS", "false")
    result = find_side_effect_impact_issues(DummyLLMClient(), _input())
    assert result == []


def test_replaced_content_reaches_prompt_when_mutation_analysis_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (``CODE_REVIEW_MUTATION_ANALYSIS`` unset): a before-image supplied
    on ``CodeReviewInput.replaced_content`` reaches the user prompt as a
    "Replaced (pre-change) content" section."""
    monkeypatch.delenv("CODE_REVIEW_MUTATION_ANALYSIS", raising=False)

    class _Capture(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"findings": []}

    client = _Capture()
    find_side_effect_impact_issues(
        client,
        CodeReviewInput(
            files={"app/main.py": "def bar():\n    return 2\n"},
            task_description="wire up bar",
            replaced_content={"app/main.py": "def bar():\n    return 1\n"},
        ),
    )
    assert "Replaced (pre-change) content" in client.latest_reasoning_prompt()
    assert "def bar():\n    return 1\n" in client.latest_reasoning_prompt()


def test_replaced_content_hidden_from_prompt_when_mutation_analysis_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CODE_REVIEW_MUTATION_ANALYSIS=false`` must hide the before-image from
    the model entirely, not merely leave it unused -- so the disabled toggle's
    behavior matches the pass's pre-mutation-analysis behavior exactly."""
    monkeypatch.setenv("CODE_REVIEW_MUTATION_ANALYSIS", "false")

    class _Capture(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"findings": []}

    client = _Capture()
    find_side_effect_impact_issues(
        client,
        CodeReviewInput(
            files={"app/main.py": "def bar():\n    return 2\n"},
            task_description="wire up bar",
            replaced_content={"app/main.py": "def bar():\n    return 1\n"},
        ),
    )
    assert "Replaced (pre-change) content" not in client.latest_reasoning_prompt()
    assert "def bar():\n    return 1\n" not in client.latest_reasoning_prompt()


def test_reasoning_system_prompt_reflects_mutation_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The system prompt handed to the runner must carry (or omit) the
    mutation-vs-replaced-code contract sub-check per ``CODE_REVIEW_MUTATION_ANALYSIS``."""
    import code_review_agent.side_effect_impact_pass as pass_mod

    captured: Dict[str, Any] = {}

    def _fake_run_submission_pass(llm: Any, **kwargs: Any) -> list:
        captured["reasoning_system_prompt"] = kwargs["reasoning_system_prompt"]
        return []

    monkeypatch.setattr(pass_mod, "run_submission_pass", _fake_run_submission_pass)

    monkeypatch.delenv("CODE_REVIEW_MUTATION_ANALYSIS", raising=False)
    find_side_effect_impact_issues(DummyLLMClient(), _input())
    assert "mutation-vs-replaced-code" in captured["reasoning_system_prompt"]

    captured.clear()
    monkeypatch.setenv("CODE_REVIEW_MUTATION_ANALYSIS", "false")
    find_side_effect_impact_issues(DummyLLMClient(), _input())
    assert "mutation-vs-replaced-code" not in captured["reasoning_system_prompt"]


class _MutationFindingClient(SubmissionPassTwoCallClient):
    """Returns a mutation-contract finding only when the pass actually showed
    the model a before-image (i.e. the "Replaced (pre-change) content"
    section reached the prompt alongside this pass's anchor). Used by both
    ``test_fires_mutation_finding_when_before_image_present`` and
    ``test_no_speculative_finding_without_before_image`` so the pair proves
    the *same* finding path fires with a before-image and goes silent
    without one, rather than asserting two unrelated behaviors."""

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        reasoning_prompt = self.latest_reasoning_prompt()
        if _SIDE_EFFECT_PASS_ANCHOR in reasoning_prompt and "Replaced (pre-change) content" in reasoning_prompt:
            return {
                "findings": [
                    {
                        "severity": "high",
                        "category": "side-effects",
                        "file_path": "app/main.py",
                        "description": (
                            "bar() now returns 2 instead of the shown before-image's 1; "
                            "app/caller.py still expects the old contract"
                        ),
                        "suggestion": "update app/caller.py for the new return value",
                    }
                ]
            }
        return {"findings": []}


def test_fires_mutation_finding_when_before_image_present() -> None:
    """A mutation-contract finding is produced when the submission carries a
    before-image for the changed file."""
    result = find_side_effect_impact_issues(
        _MutationFindingClient(),
        CodeReviewInput(
            files={"app/main.py": "def bar():\n    return 2\n"},
            task_description="wire up bar",
            replaced_content={"app/main.py": "def bar():\n    return 1\n"},
        ),
    )
    assert len(result) == 1
    assert result[0].category == "side-effects"
    assert "app/caller.py" in result[0].description


def test_no_speculative_finding_without_before_image() -> None:
    """The identical scripted reply logic produces no finding when there is
    no before-image to react to -- the mutation sub-check cannot speculate
    about a prior version it was never shown."""
    result = find_side_effect_impact_issues(
        _MutationFindingClient(),
        CodeReviewInput(
            files={"app/main.py": "def bar():\n    return 2\n"},
            task_description="wire up bar",
        ),
    )
    assert result == []


def test_returns_empty_when_submission_has_no_readable_files() -> None:
    result = find_side_effect_impact_issues(DummyLLMClient(), _input(files={"empty.py": "   "}))
    assert result == []


def test_fails_safe_on_llm_error() -> None:
    class _Raiser(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("boom")

    result = find_side_effect_impact_issues(_Raiser(), _input())
    assert result == []


def test_fails_safe_on_unparsable_reply() -> None:
    class _Gibberish(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            raise LLMJsonParseError("not even a dict-shaped reply")

    result = find_side_effect_impact_issues(_Gibberish(), _input())
    assert result == []


def test_returns_empty_for_non_code_review_profile() -> None:
    from code_review_agent.profiles import ReviewProfile

    class _FailIfAskedClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            assert _SIDE_EFFECT_PASS_ANCHOR not in self.latest_reasoning_prompt(), "side-effect pass should not run"
            assert _MERGED_PASS_ANCHOR not in self.latest_reasoning_prompt(), "merged pass should not run"
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_side_effect_impact_issues(
        _FailIfAskedClient(),
        CodeReviewInput(
            files={"app/main.py": "def bar():\n    return 1\n"},
            task_description="wire up bar",
            profile=ReviewProfile.ACCEPTANCE,
        ),
    )
    assert result == []


def test_returns_empty_when_pre_numbered() -> None:
    """Hunk-fallback mode: ``index.files`` holds partial excerpts, not full files.

    Preconditions: none.
    Postconditions: no LLM call is made and no findings are returned, since
        this pass cannot verify a finding against content it never fully saw.
    """

    class _FailIfAskedClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            assert _SIDE_EFFECT_PASS_ANCHOR not in self.latest_reasoning_prompt(), "side-effect pass should not run"
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_side_effect_impact_issues(
        _FailIfAskedClient(),
        CodeReviewInput(
            files={"app/main.py": "def bar():\n    return 1\n"},
            task_description="wire up bar",
            pre_numbered=True,
        ),
    )
    assert result == []


def test_runs_when_pre_numbered_with_full_content_supplied() -> None:
    """A caller that supplies ``full_content`` alongside ``pre_numbered=True`` has
    given this pass real full bodies (via ``CodebaseIndex.from_input``'s overlay) --
    the pass must run its normal caller-impact analysis instead of treating the
    submission as unverifiable hunk-fallback mode."""

    class _FindingsClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _SIDE_EFFECT_PASS_ANCHOR in self.latest_reasoning_prompt():
                assert (
                    "def bar():" in self.latest_reasoning_prompt()
                )  # full_content reached the prompt, not "1: def bar():"
                return {
                    "findings": [
                        {
                            "severity": "high",
                            "category": "side-effects",
                            "file_path": "app/main.py",
                            "description": "bar() behavior changed",
                            "suggestion": "check callers",
                        }
                    ]
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    full = "def bar():\n    return 1\n"
    result = find_side_effect_impact_issues(
        _FindingsClient(),
        CodeReviewInput(
            files={"app/main.py": "1: def bar():\n2:     return 1\n"},
            pre_numbered=True,
            full_content={"app/main.py": full},
            task_description="wire up bar",
        ),
    )
    assert len(result) == 1
    assert result[0].category == "side-effects"


def test_stays_disabled_when_full_content_covers_only_some_paths() -> None:
    """``full_content`` that covers only SOME of the submission's changed paths
    must NOT re-enable this pass: overlaying just the covered subset would leave
    the rest as bounded ``N: ``-prefixed excerpts, and reasoning about those as
    if they were complete files is exactly the "flag from a guess" failure mode
    this pass's ``pre_numbered`` guard exists to prevent."""

    class _FailIfAskedClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            assert _SIDE_EFFECT_PASS_ANCHOR not in self.latest_reasoning_prompt(), "pass should stay disabled"
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_side_effect_impact_issues(
        _FailIfAskedClient(),
        CodeReviewInput(
            files={
                "app/main.py": "1: def bar():\n2:     return 1\n",
                "app/util.py": "1: def helper():\n2:     return 2\n",
            },
            pre_numbered=True,
            # Covers only app/main.py, not app/util.py -- partial coverage.
            full_content={"app/main.py": "def bar():\n    return 1\n"},
            task_description="wire up bar",
        ),
    )
    assert result == []


# --------------------------------------------------------------------------- happy path


def test_finds_and_returns_new_findings() -> None:
    class _FindingsClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _SIDE_EFFECT_PASS_ANCHOR in self.latest_reasoning_prompt():
                return {
                    "findings": [
                        {
                            "severity": "high",
                            "category": "side-effects",
                            "file_path": "app/main.py",
                            "description": (
                                "bar() no longer raises ValueError on empty input; "
                                "app/caller.py still catches it and would now hang"
                            ),
                            "suggestion": "update app/caller.py to handle the new return value",
                        }
                    ]
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_side_effect_impact_issues(_FindingsClient(), _input())
    assert len(result) == 1
    assert result[0].category == "side-effects"
    assert "app/caller.py" in result[0].description


@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param("fenced", id="fenced"),
        pytest.param("prose", id="prose-prefixed"),
    ],
)
def test_recovers_fenced_and_prose_wrapped_reply(
    monkeypatch: pytest.MonkeyPatch, wrap: str
) -> None:
    """A formatting reply wrapped in a ```json fence or prefixed with prose still
    parses: the pass routes it through the canonical recovery ladder rather than
    a bare ``json.loads`` that would raise on the fence/prose."""
    import code_review_agent.submission_pass_runner as runner_mod

    payload = {
        "findings": [
            {
                "severity": "high",
                "category": "side-effects",
                "file_path": "app/main.py",
                "description": "bar() behavior changed and app/caller.py would hang",
                "suggestion": "update app/caller.py",
            }
        ]
    }
    inner = json.dumps(payload)
    raw = f"```json\n{inner}\n```" if wrap == "fenced" else f"Sure, here you go: {inner}"
    wire_run_agent_via_reasoning_with_raw(monkeypatch, runner_mod, raw)

    result = find_side_effect_impact_issues(DummyLLMClient(), _input())
    assert len(result) == 1
    assert result[0].category == "side-effects"


def test_finds_and_returns_new_findings_drops_hallucinated_line() -> None:
    class _FindingsClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _SIDE_EFFECT_PASS_ANCHOR in self.latest_reasoning_prompt():
                return {
                    "findings": [
                        {
                            "severity": "high",
                            "category": "side-effects",
                            "file_path": "app/main.py",
                            "line": 9999,
                            "description": "behavior change",
                            "suggestion": "fix it",
                        }
                    ]
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_side_effect_impact_issues(
        _FindingsClient(), _input(files={"app/main.py": "def bar():\n    return 1\n"})
    )
    assert len(result) == 1
    assert result[0].line is None  # line 9999 doesn't exist in a 2-line file


# --------------------------------------------------------------------------- caller-impact fixture


def test_finds_caller_impact_across_the_repository() -> None:
    """End-to-end: the pass is given a changed function and, via
    ``search_repository``, an out-of-diff caller whose usage the new behavior
    breaks. This exercises the actual reason this pass exists -- something no
    other pass or the per-chunk map phase can do."""

    caller_content = "from app.main import bar\n\ndef use_bar():\n    return bar() + 1\n"

    class _FindingsClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _SIDE_EFFECT_PASS_ANCHOR in self.latest_reasoning_prompt():
                assert "def bar():" in self.latest_reasoning_prompt()  # the changed function reached the prompt
                return {
                    "findings": [
                        {
                            "severity": "critical",
                            "category": "side-effects",
                            "file_path": "app/main.py",
                            "description": (
                                "bar() now returns a string instead of an int; "
                                "app/caller.py:4 does `bar() + 1`, which will raise TypeError"
                            ),
                            "suggestion": "either keep bar() returning an int, or update the caller",
                        }
                    ]
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_side_effect_impact_issues(
        _FindingsClient(),
        _input(files={"app/main.py": "def bar():\n    return 'one'\n"}),
        repo_reader=_FakeReader({"app/caller.py": caller_content}),
    )
    assert len(result) == 1
    assert "TypeError" in result[0].description


# --------------------------------------------------------------------------- batching / reactive recovery


def test_single_call_for_multi_file_submission() -> None:
    """Several small files are reviewed in one LLM call when no overflow occurs."""
    prompts: list = []

    class _Client(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _SIDE_EFFECT_PASS_ANCHOR in self.latest_reasoning_prompt():
                prompts.append(self.latest_reasoning_prompt())
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {
        "a.py": "def a():\n    return 1\n",
        "b.py": "def b():\n    return 2\n",
        "c.py": "def c():\n    return 3\n",
    }
    find_side_effect_impact_issues(_Client(), _input(files=files))
    assert len(prompts) == 1
    for path in files:
        assert f"### {path} ###" in prompts[0]


def test_reactive_recovery_bisects_overflowing_batch_through_public_entry_point() -> None:
    """The pass benefits from the shared runner's reactive bisect recovery."""
    from strands.types.exceptions import ContextWindowOverflowException

    call_count = {"n": 0}

    class _Client(SubmissionPassTwoCallClient):
        def complete(self, prompt: str, **kwargs: Any) -> str:
            if _SIDE_EFFECT_PASS_ANCHOR in prompt:
                call_count["n"] += 1
                if "### a.py ###" in prompt and "### b.py ###" in prompt:
                    raise ContextWindowOverflowException("combined batch too large")
            return super().complete(prompt, **kwargs)

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _SIDE_EFFECT_PASS_ANCHOR in self.latest_reasoning_prompt():
                for path in ("a.py", "b.py"):
                    if f"### {path} ###" in self.latest_reasoning_prompt():
                        return {
                            "findings": [
                                {
                                    "severity": "medium",
                                    "category": "side-effects",
                                    "file_path": path,
                                    "description": f"finding for {path}",
                                    "suggestion": "n/a",
                                }
                            ]
                        }
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {"a.py": "x = 1\n", "b.py": "y = 2\n"}
    result = find_side_effect_impact_issues(_Client(), _input(files=files))

    assert {f.description for f in result} == {"finding for a.py", "finding for b.py"}
    assert call_count["n"] > 1


# --------------------------------------------------------------------------- coordinator integration


def test_coordinator_runs_pass_once_per_submission_not_per_chunk() -> None:
    """Merged pass runs once per submission; standalone arch/side-effect passes do not."""
    calls = {"merged_pass": 0, "arch_pass": 0, "side_effect_pass": 0, "chunk_review": 0}

    class _CountingClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                calls["merged_pass"] += 1
                return {"architecture_findings": [], "side_effect_findings": []}
            if _ARCH_PASS_ANCHOR in self.latest_reasoning_prompt():
                calls["arch_pass"] += 1
                return {"findings": []}
            if _SIDE_EFFECT_PASS_ANCHOR in self.latest_reasoning_prompt():
                calls["side_effect_pass"] += 1
                return {"findings": []}
            calls["chunk_review"] += 1
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {"a.py": "def a():\n    return 1\n", "b.py": "def b():\n    return 2\n"}
    run_coordinator(_CountingClient(), CodeReviewInput(files=files))

    assert calls["merged_pass"] == 1
    assert calls["arch_pass"] == 0
    assert calls["side_effect_pass"] == 0


def test_coordinator_merges_side_effect_findings_into_final_output() -> None:
    """A genuine caller-breaking side effect surfaces under ``side-effects`` and a
    docstring/implementation mismatch surfaces under ``documentation`` -- both fold
    into the final output, and neither blocks approval on its own at medium/low."""

    class _FindingsClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            assert _SIDE_EFFECT_PASS_ANCHOR not in self.latest_reasoning_prompt(), (
                "standalone side-effect pass should not run when merged pass is enabled"
            )
            assert _ARCH_PASS_ANCHOR not in self.latest_reasoning_prompt(), (
                "standalone architecture pass should not run when merged pass is enabled"
            )
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                return {
                    "architecture_findings": [],
                    "side_effect_findings": [
                        {
                            "severity": "medium",
                            "category": "side-effects",
                            "file_path": "app/main.py",
                            "description": (
                                "bar() now returns None instead of an int; "
                                "app/caller.py line 4 does `bar() + 1` and will raise TypeError"
                            ),
                            "suggestion": "update app/caller.py to handle bar()'s new return value",
                            "pre_existing": False,
                        },
                        {
                            "severity": "low",
                            "category": "documentation",
                            "file_path": "app/main.py",
                            "description": "bar()'s docstring claims it returns an int, but it "
                            "returns None",
                            "suggestion": "correct bar()'s docstring to match its implementation",
                            "pre_existing": False,
                        },
                    ],
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = run_coordinator(
        _FindingsClient(),
        CodeReviewInput(files={"app/main.py": "def bar():\n    return None\n"}),
    )
    assert result.approved  # medium/low findings never block approval alone
    assert any(i.category == "side-effects" and "TypeError" in i.description for i in result.issues)
    assert any(
        i.category == "documentation" and "docstring" in i.description for i in result.issues
    )


def test_coordinator_skips_pass_for_non_default_profile() -> None:
    from code_review_agent.profiles import ReviewProfile

    class _FailIfAskedClient(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            assert _SIDE_EFFECT_PASS_ANCHOR not in self.latest_reasoning_prompt(), "side-effect pass should not run"
            assert _MERGED_PASS_ANCHOR not in self.latest_reasoning_prompt(), "merged pass should not run"
            return {
                "index": 0,
                "is_real_issue": True,
                "confidence": "high",
                "reasoning": "n/a",
                "verdicts": [],
                "approved": True,
                "issues": [],
                "summary": "ok",
                "spec_compliance_notes": "",
            }

    run_coordinator(
        _FailIfAskedClient(),
        CodeReviewInput(
            files={"app/main.py": "def bar():\n    return 1\n"},
            task_description="verify",
            acceptance_criteria=["bar returns 1"],
            profile=ReviewProfile.ACCEPTANCE,
        ),
    )
