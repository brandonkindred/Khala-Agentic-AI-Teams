"""Tests for side-effect finding consolidation (same-function and shared-reference grouping)."""

from __future__ import annotations

import pytest
from code_review_agent.false_positive_filter import CodebaseIndex
from code_review_agent.models import CodeReviewInput, CodeReviewIssue
from code_review_agent.side_effect_consolidation import (
    consolidate_side_effect_issues,
    effective_replaced_content,
)


def _issue(**kwargs) -> CodeReviewIssue:
    defaults = dict(
        severity="high",
        category="side-effects",
        file_path="app/foo.py",
        line=3,
        description="finding",
        suggestion="",
        pre_existing=False,
    )
    defaults.update(kwargs)
    return CodeReviewIssue(**defaults)


def _index(files: dict) -> CodebaseIndex:
    return CodebaseIndex(files=files)


# --------------------------------------------------------------------------- same-function grouping


def test_two_findings_in_same_python_function_merge() -> None:
    """Two ``side-effects`` findings in the same function merge into one with combined text and span."""
    content = "\n".join(
        [
            "def foo():",  # 1
            "    x = 1",  # 2
            "    y = 2",  # 3
            "    return x + y",  # 4
            "",
        ]
    )
    index = _index({"app/foo.py": content})
    issues = [
        _issue(
            line=2,
            description="foo mutates shared state",
            suggestion="remove global mutation",
        ),
        _issue(
            line=4,
            description="foo's return type changed",
            suggestion="restore return type",
        ),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert result[0].category == "side-effects"
    assert "foo mutates shared state" in result[0].description
    assert "foo's return type changed" in result[0].description
    assert "remove global mutation" in result[0].suggestion
    assert "restore return type" in result[0].suggestion
    assert result[0].start_line == 2
    assert result[0].line == 4


def test_findings_in_different_functions_do_not_merge() -> None:
    """Findings in distinct top-level functions stay separate (no shared construct or reference)."""
    content = "\n".join(
        [
            "def foo():",  # 1
            "    return 1",  # 2
            "",
            "def bar():",  # 4
            "    return 2",  # 5
            "",
        ]
    )
    index = _index({"app/foo.py": content})
    issues = [
        _issue(line=2, description="foo issue"),
        _issue(line=5, description="bar issue"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 2


# --------------------------------------------------------------------------- shared-reference grouping


def test_finding_referencing_another_functions_finding_merges() -> None:
    """A caller finding that cites another finding's ``path:line`` merges with that finding."""
    content = "\n".join(
        [
            "def foo():",  # 1
            "    return 1",  # 2
            "",
            "def bar():",  # 4
            "    return foo() + 1",  # 5
            "",
        ]
    )
    index = _index({"app/foo.py": content})
    issues = [
        _issue(line=2, description="foo's return value changed"),
        _issue(line=5, description="bar breaks because of app/foo.py:2 (foo's old contract)"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert "foo's return value changed" in result[0].description
    assert "bar breaks" in result[0].description


def test_transitive_reference_chain_merges_three() -> None:
    """A→B and B→C reference links transitively merge all three findings into one group."""
    content = "\n".join(
        [
            "def foo():",  # 1
            "    return 1",  # 2
            "",
            "def bar():",  # 4
            "    return foo()",  # 5
            "",
            "def baz():",  # 7
            "    return bar()",  # 8
            "",
        ]
    )
    index = _index({"app/foo.py": content})
    issues = [
        _issue(line=2, description="foo changed"),
        _issue(line=5, description="bar calls app/foo.py:2 and now misbehaves"),
        _issue(line=8, description="baz calls app/foo.py:5 (bar) and inherits the break"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert result[0].description.count("Consolidated 3") == 1


# --------------------------------------------------------------------------- non-python fallback


def test_non_python_files_do_not_group_by_heuristic() -> None:
    """Non-Python findings stay separate: column-0 heuristics would false-merge indented methods."""
    # Top-level TS function where the heuristic *would* have grouped — still
    # left alone so class-based languages with indented methods are safe.
    content = "\n".join(
        [
            "function foo() {",  # 1
            "  const x = 1;",  # 2
            "  return x;",  # 3
            "}",  # 4
            "",
        ]
    )
    index = _index({"app/foo.ts": content})
    issues = [
        _issue(file_path="app/foo.ts", line=2, description="ts finding one"),
        _issue(file_path="app/foo.ts", line=3, description="ts finding two"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 2
    assert result[0].description == "ts finding one"
    assert result[1].description == "ts finding two"


def test_java_style_indented_methods_do_not_false_merge() -> None:
    """Findings in distinct indented methods of one class must not share a group key."""
    content = "\n".join(
        [
            "public class Widget {",  # 1
            "    public void draw() {",  # 2
            "        int x = 1;",  # 3
            "    }",  # 4
            "    public void erase() {",  # 5
            "        int y = 2;",  # 6
            "    }",  # 7
            "}",  # 8
            "",
        ]
    )
    index = _index({"Widget.java": content})
    issues = [
        _issue(file_path="Widget.java", line=3, description="draw side effect"),
        _issue(file_path="Widget.java", line=6, description="erase side effect"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 2


def test_basename_citation_matches_canonical_path() -> None:
    """A prose citation of a basename resolves to the same key as the canonical finding path."""
    content = "\n".join(
        [
            "def foo():",  # 1
            "    return 1",  # 2
            "",
            "def bar():",  # 4
            "    return foo()",  # 5
            "",
        ]
    )
    index = _index({"app/foo.py": content})
    issues = [
        _issue(
            file_path="app/foo.py",
            line=2,
            description="foo's contract changed",
        ),
        _issue(
            file_path="app/foo.py",
            line=5,
            description="bar breaks because of foo.py:2 (old contract)",
        ),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert "foo's contract changed" in result[0].description
    assert "bar breaks" in result[0].description


def test_aliased_file_paths_group_under_canonical_key() -> None:
    """Findings citing the same file via basename vs full path share a construct key."""
    content = "\n".join(
        [
            "def foo():",  # 1
            "    x = 1",  # 2
            "    return x",  # 3
            "",
        ]
    )
    index = _index({"app/foo.py": content})
    issues = [
        _issue(file_path="app/foo.py", line=2, description="full-path finding"),
        _issue(file_path="foo.py", line=3, description="basename finding"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert result[0].file_path == "app/foo.py"
    assert result[0].start_line == 2
    assert result[0].line == 3


def test_aliased_path_tie_publishes_canonical_span() -> None:
    """Basename-first tie still votes as the resolved file and spans all aliased lines."""
    # Tall function so an early alias (line 2) and a later canonical finding
    # (line 20) merge; without resolve_path voting the alias would win the
    # 1–1 tie and drop line 20 from the published range.
    body = ["def foo():"] + [f"    x{i} = {i}" for i in range(1, 20)] + ["    return x1", ""]
    content = "\n".join(body)
    index = _index({"app/foo.py": content})
    issues = [
        _issue(file_path="foo.py", line=2, description="basename map finding"),
        _issue(file_path="app/foo.py", line=20, description="canonical additive finding"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert result[0].file_path == "app/foo.py"
    assert result[0].start_line == 2
    assert result[0].line == 20


# --------------------------------------------------------------------------- passthrough / non-merge cases


def test_non_side_effect_issues_pass_through_unchanged() -> None:
    """Non-``side-effects`` categories are returned unchanged and never enter a merge group."""
    index = _index({"app/foo.py": "def foo():\n    return 1\n"})
    doc_issue = _issue(category="documentation", description="stale docstring")
    other_issue = _issue(category="logic", description="unrelated logic issue")
    result = consolidate_side_effect_issues([doc_issue, other_issue], index)
    assert result == [doc_issue, other_issue]


def test_single_side_effect_issue_passes_through_unchanged() -> None:
    """A singleton ``side-effects`` finding has nothing to merge with and is returned as-is."""
    index = _index({"app/foo.py": "def foo():\n    return 1\n"})
    issue = _issue(line=2, description="lone finding")
    result = consolidate_side_effect_issues([issue], index)
    assert result == [issue]


def test_unresolvable_file_does_not_crash_and_leaves_issues_separate() -> None:
    """Findings whose file is missing from the index stay separate rather than crashing or merging."""
    index = _index({})
    issues = [
        _issue(file_path="missing.py", line=2, description="a"),
        _issue(file_path="missing.py", line=5, description="b"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 2


# --------------------------------------------------------------------------- merge field semantics


def test_severity_elevates_to_highest_in_group() -> None:
    """Merged severity is the highest among members (critical wins over low)."""
    content = "def foo():\n    x = 1\n    return x\n"
    index = _index({"app/foo.py": content})
    issues = [
        _issue(line=2, severity="low", description="minor note"),
        _issue(line=3, severity="critical", description="critical break"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert result[0].severity == "critical"


def test_merge_preserves_title_from_highest_severity_member() -> None:
    """Non-merged fields such as ``title`` are kept from the highest-severity member."""
    content = "def foo():\n    x = 1\n    return x\n"
    index = _index({"app/foo.py": content})
    issues = [
        _issue(line=2, severity="low", title="low title", description="minor note"),
        _issue(line=3, severity="critical", title="critical title", description="critical break"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert result[0].title == "critical title"


@pytest.mark.parametrize(
    ("severities", "expected"),
    [
        (["low", "medium"], "medium"),
        (["medium", "high"], "high"),
        (["info", "low"], "low"),
        (["high", "critical"], "critical"),
    ],
)
def test_severity_elevates_across_all_adjacent_ranks(severities, expected) -> None:
    """Severity elevation holds for every adjacent rank pair on the severity ladder."""
    content = "def foo():\n    x = 1\n    return x\n"
    index = _index({"app/foo.py": content})
    issues = [
        _issue(line=2, severity=severities[0], description="a"),
        _issue(line=3, severity=severities[1], description="b"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert result[0].severity == expected


def test_pre_existing_true_only_when_all_members_are() -> None:
    """A consolidated group is marked pre_existing only if every member in the group is pre_existing; a single non-pre-existing member forces the merged issue to False."""
    content = "def foo():\n    x = 1\n    return x\n"
    index = _index({"app/foo.py": content})
    issues = [
        _issue(line=2, pre_existing=True, description="a"),
        _issue(line=3, pre_existing=False, description="b"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert result[0].pre_existing is False

    issues_all_pre_existing = [
        _issue(line=2, pre_existing=True, description="a"),
        _issue(line=3, pre_existing=True, description="b"),
    ]
    result2 = consolidate_side_effect_issues(issues_all_pre_existing, index)
    assert result2[0].pre_existing is True


def test_merge_preserves_earliest_start_of_multi_line_members() -> None:
    """Merged ``start_line`` uses each member's effective start so multi-line ranges keep the earliest bound."""
    content = "\n".join(
        [
            "def foo():",  # 1
            "    a = 1",  # 2
            "    b = 2",  # 3
            "    c = 3",  # 4
            "    d = 4",  # 5
            "    e = 5",  # 6
            "    f = 6",  # 7
            "    return a",  # 8
            "",
        ]
    )
    index = _index({"app/foo.py": content})
    issues = [
        _issue(line=4, start_line=2, description="first multi-line symptom"),
        _issue(line=8, start_line=6, description="second multi-line symptom"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert result[0].start_line == 2
    assert result[0].line == 8


def test_construct_resolution_normalizes_pre_numbered_hunks() -> None:
    """Annotated ``N: `` hunks remapped before construct lookup so cross-function references still merge."""
    # Mirrors the PR-review path's render_annotated_hunks output: every line
    # prefixed with its original file line number.
    content = "\n".join(
        [
            "100: def foo():",
            "101:     return 1",
            "102: ",
            "103: def bar():",
            "104:     return foo() + 1",
        ]
    )
    index = _index({"app/foo.py": content})
    issues = [
        _issue(line=101, description="foo's return value changed"),
        _issue(line=104, description="bar breaks because of app/foo.py:101 (foo's old contract)"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1


def test_cross_hunk_indented_continuation_does_not_false_merge() -> None:
    """Findings across a ``...`` gap must not share a construct key invented by joining hunks."""
    content = "\n".join(
        [
            "10: def first():",
            "11:     return 1",
            "...",
            "100:     changed()",
        ]
    )
    index = _index({"app/foo.py": content})
    issues = [
        _issue(line=11, description="first return changed"),
        _issue(line=100, description="unrelated mid-function change"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 2
    assert result[0].description == "first return changed"
    assert result[1].description == "unrelated mid-function change"


def test_ellipsis_stub_bodies_still_group_in_full_file_source() -> None:
    """Indented Ellipsis in real Python stubs is not treated as an annotated hunk gap."""
    content = "\n".join(
        [
            "def foo():",  # 1
            "    ...",  # 2
            "    return 1",  # 3
            "",
        ]
    )
    index = _index({"app/foo.py": content})
    issues = [
        _issue(line=2, description="ellipsis body changed"),
        _issue(line=3, description="return shape changed"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert "ellipsis body changed" in result[0].description
    assert "return shape changed" in result[0].description


def test_merge_keeps_distinct_caller_citations() -> None:
    """Near-identical descriptions that cite different callers all survive consolidation."""
    content = "def foo():\n    return 1\n"
    index = _index({"app/foo.py": content})
    issues = [
        _issue(
            line=2,
            description="caller at app/a.py:10 assumes the old return shape and will break",
        ),
        _issue(
            line=2,
            description="caller at app/b.py:20 assumes the old return shape and will break",
        ),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert "app/a.py:10" in result[0].description
    assert "app/b.py:20" in result[0].description


def test_majority_file_wins_for_multi_file_group() -> None:
    """When a merged group spans files, the published finding's path is the majority-file path."""
    foo_content = "def foo():\n    return 1\n"
    other_content = "def other():\n    return foo() + bar()\n"
    index = _index({"app/foo.py": foo_content, "app/other.py": other_content})
    issues = [
        _issue(file_path="app/other.py", line=2, description="other references app/foo.py:2"),
        _issue(file_path="app/foo.py", line=2, description="foo's contract changed"),
        _issue(file_path="app/foo.py", line=2, description="foo also breaks a second caller"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert result[0].file_path == "app/foo.py"


def test_unanchored_citation_merge_keeps_known_file_anchor() -> None:
    """Blank-path members that citation-group with an anchored finding must not wipe the path."""
    content = "def foo():\n    return 1\n"
    index = _index({"app/foo.py": content})
    # Unanchored map-phase-style finding listed first (tie/earliest would
    # otherwise let "" win majority), citing the anchored construct.
    issues = [
        _issue(
            file_path="",
            line=None,
            description="blast radius via caller of app/foo.py:2",
        ),
        _issue(
            file_path="app/foo.py",
            line=2,
            description="foo's contract changed",
        ),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert result[0].file_path == "app/foo.py"
    assert result[0].line == 2
    assert "foo's contract changed" in result[0].description
    assert "blast radius" in result[0].description


# --------------------------------------------------------------------------- effective_replaced_content


def test_effective_replaced_content_passes_through_when_mutation_on() -> None:
    replaced = {"app/foo.py": "def foo():\n    return 0\n"}
    input_data = CodeReviewInput(
        files={"app/foo.py": "def foo():\n    return 1\n"},
        task_description="change return value",
        replaced_content=replaced,
    )
    assert effective_replaced_content(input_data, mutation_on=True) == replaced


def test_effective_replaced_content_hidden_when_mutation_off() -> None:
    """The before-image is hidden entirely when the toggle is off, regardless
    of whether ``replaced_content`` is populated."""
    input_data = CodeReviewInput(
        files={"app/foo.py": "def foo():\n    return 1\n"},
        task_description="change return value",
        replaced_content={"app/foo.py": "def foo():\n    return 0\n"},
    )
    assert effective_replaced_content(input_data, mutation_on=False) is None


def test_effective_replaced_content_none_when_absent_regardless_of_toggle() -> None:
    input_data = CodeReviewInput(
        files={"app/foo.py": "def foo():\n    return 1\n"},
        task_description="change return value",
    )
    assert effective_replaced_content(input_data, mutation_on=True) is None
    assert effective_replaced_content(input_data, mutation_on=False) is None
