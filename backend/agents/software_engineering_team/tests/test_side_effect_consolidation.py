"""Tests for side-effect finding consolidation (same-function and shared-reference grouping)."""

from __future__ import annotations

from code_review_agent.false_positive_filter import CodebaseIndex
from code_review_agent.models import CodeReviewIssue
from code_review_agent.side_effect_consolidation import consolidate_side_effect_issues


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
        _issue(line=2, description="foo mutates shared state"),
        _issue(line=4, description="foo's return type changed"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert result[0].category == "side-effects"
    assert "foo mutates shared state" in result[0].description
    assert "foo's return type changed" in result[0].description
    assert result[0].start_line == 2
    assert result[0].line == 4


def test_findings_in_different_functions_do_not_merge() -> None:
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


def test_non_python_files_group_by_heuristic_start_line() -> None:
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
    assert len(result) == 1


# --------------------------------------------------------------------------- passthrough / non-merge cases


def test_non_side_effect_issues_pass_through_unchanged() -> None:
    index = _index({"app/foo.py": "def foo():\n    return 1\n"})
    doc_issue = _issue(category="documentation", description="stale docstring")
    other_issue = _issue(category="logic", description="unrelated logic issue")
    result = consolidate_side_effect_issues([doc_issue, other_issue], index)
    assert result == [doc_issue, other_issue]


def test_single_side_effect_issue_passes_through_unchanged() -> None:
    index = _index({"app/foo.py": "def foo():\n    return 1\n"})
    issue = _issue(line=2, description="lone finding")
    result = consolidate_side_effect_issues([issue], index)
    assert result == [issue]


def test_unresolvable_file_does_not_crash_and_leaves_issues_separate() -> None:
    index = _index({})
    issues = [
        _issue(file_path="missing.py", line=2, description="a"),
        _issue(file_path="missing.py", line=5, description="b"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 2


# --------------------------------------------------------------------------- merge field semantics


def test_severity_elevates_to_highest_in_group() -> None:
    content = "def foo():\n    x = 1\n    return x\n"
    index = _index({"app/foo.py": content})
    issues = [
        _issue(line=2, severity="low", description="minor note"),
        _issue(line=3, severity="critical", description="critical break"),
    ]
    result = consolidate_side_effect_issues(issues, index)
    assert len(result) == 1
    assert result[0].severity == "critical"


def test_pre_existing_true_only_when_all_members_are() -> None:
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


def test_majority_file_wins_for_multi_file_group() -> None:
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
