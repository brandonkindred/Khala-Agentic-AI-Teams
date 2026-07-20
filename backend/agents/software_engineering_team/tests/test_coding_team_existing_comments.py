"""Tests for matching code-review findings against existing PR comments (pure, no network/LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from software_engineering_team.github_source.client import (
    KHALA_COMMENT_MARKER,
    IssueComment,
    ReviewComment,
)
from software_engineering_team.github_source.existing_comments import (
    ExistingComment,
    build_existing_comments,
    match_existing_comment,
    partition_issues_by_existing_comments,
)


@dataclass
class _Issue:
    """Duck-typed stand-in for CodeReviewIssue."""

    severity: str = "high"
    category: str = "logic"
    file_path: str = "a.py"
    line: Optional[int] = 3
    description: str = "SQL injection risk in the query builder"
    suggestion: str = "use parameterized queries"


def _khala_body(text: str) -> str:
    return f"{text}\n\n{KHALA_COMMENT_MARKER}"


# ---------------------------------------------------------------------------
# build_existing_comments
# ---------------------------------------------------------------------------


def test_build_existing_comments_marks_resolved_review_comments() -> None:
    review_comments = [
        ReviewComment(id=1, path="a.py", line=3, body="resolved one", html_url="https://x/1"),
        ReviewComment(id=2, path="a.py", line=5, body="still open", html_url="https://x/2"),
    ]
    out = build_existing_comments(review_comments, resolved_ids={1}, issue_comments=[])
    assert [(c.path, c.line, c.resolved) for c in out] == [
        ("a.py", 3, True),
        ("a.py", 5, False),
    ]


def test_build_existing_comments_file_level_has_no_line() -> None:
    review_comments = [
        ReviewComment(id=1, path="a.py", line=None, body="b", html_url="https://x/1")
    ]
    out = build_existing_comments(review_comments, resolved_ids=set(), issue_comments=[])
    assert out[0].line is None
    assert out[0].resolved is False


def test_build_existing_comments_only_khala_marked_issue_comments_included() -> None:
    issue_comments = [
        IssueComment(
            id=1,
            body=_khala_body("`a.py:3` — **[HIGH] logic** — desc"),
            html_url="https://x/1",
        ),
        IssueComment(id=2, body="a human's comment, not marked", html_url="https://x/2"),
    ]
    out = build_existing_comments([], resolved_ids=set(), issue_comments=issue_comments)
    assert len(out) == 1
    assert out[0].path == "a.py"
    assert out[0].line == 3
    assert out[0].resolved is False


def test_build_existing_comments_issue_comment_without_location_has_none_path() -> None:
    issue_comments = [
        IssueComment(id=1, body=_khala_body("Coding team started job x"), html_url="https://x/1")
    ]
    out = build_existing_comments([], resolved_ids=set(), issue_comments=issue_comments)
    assert out[0].path is None
    assert out[0].line is None


def test_build_existing_comments_file_level_standalone_location() -> None:
    issue_comments = [
        IssueComment(
            id=1,
            body=_khala_body("`a.py` — **[LOW] general** — minor thing"),
            html_url="https://x/1",
        )
    ]
    out = build_existing_comments([], resolved_ids=set(), issue_comments=issue_comments)
    assert out[0].path == "a.py"
    assert out[0].line is None


# ---------------------------------------------------------------------------
# match_existing_comment
# ---------------------------------------------------------------------------


def test_match_existing_comment_same_location_and_similar_text() -> None:
    issue = _Issue(file_path="a.py", line=3, description="SQL injection risk in the query builder")
    existing = [
        ExistingComment(
            path="a.py",
            line=3,
            body="**[HIGH] logic** — SQL injection risk in the query builder",
            html_url="https://x/1",
            resolved=False,
        )
    ]
    match = match_existing_comment(issue, existing)
    assert match is not None
    assert match.html_url == "https://x/1"


def test_match_existing_comment_same_location_different_issue_no_match() -> None:
    issue = _Issue(file_path="a.py", line=3, description="SQL injection risk in the query builder")
    existing = [
        ExistingComment(
            path="a.py",
            line=3,
            body="**[LOW] naming** — rename this variable",
            html_url="https://x/1",
            resolved=False,
        )
    ]
    assert match_existing_comment(issue, existing) is None


def test_match_existing_comment_different_line_no_match() -> None:
    issue = _Issue(file_path="a.py", line=3, description="SQL injection risk in the query builder")
    existing = [
        ExistingComment(
            path="a.py",
            line=99,
            body="SQL injection risk in the query builder",
            html_url="https://x/1",
            resolved=False,
        )
    ]
    assert match_existing_comment(issue, existing) is None


def test_match_existing_comment_path_normalization() -> None:
    issue = _Issue(
        file_path="./a.py", line=3, description="SQL injection risk in the query builder"
    )
    existing = [
        ExistingComment(
            path="a.py",
            line=3,
            body="SQL injection risk in the query builder",
            html_url="https://x/1",
            resolved=False,
        )
    ]
    assert match_existing_comment(issue, existing) is not None


def test_match_existing_comment_both_file_level() -> None:
    issue = _Issue(
        file_path="a.py", line=None, description="widespread duplication across this file"
    )
    existing = [
        ExistingComment(
            path="a.py",
            line=None,
            body="widespread duplication across this file",
            html_url="https://x/1",
            resolved=False,
        )
    ]
    assert match_existing_comment(issue, existing) is not None


def test_match_existing_comment_no_candidates_returns_none() -> None:
    assert match_existing_comment(_Issue(), []) is None


def test_match_existing_comment_skips_candidates_with_no_path() -> None:
    issue = _Issue(file_path="a.py", line=3)
    existing = [
        ExistingComment(
            path=None, line=3, body=issue.description, html_url="https://x/1", resolved=False
        )
    ]
    assert match_existing_comment(issue, existing) is None


def test_match_existing_comment_length_ratio_rejects_without_full_computation() -> None:
    # description is contained verbatim as a prefix of body, but the length
    # ratio (10/40 = 0.25) is well below the 0.4 floor: even a full ratio()
    # computation could reach at most 2*10/50 = 0.4, under the 0.6 threshold,
    # so rejecting via length alone must agree with rejecting via content.
    issue = _Issue(file_path="a.py", line=3, description="x" * 10)
    existing = [
        ExistingComment(
            path="a.py",
            line=3,
            body="x" * 10 + "y" * 30,
            html_url="https://x/1",
            resolved=False,
        )
    ]
    assert match_existing_comment(issue, existing) is None


def test_match_existing_comment_empty_description_never_matches() -> None:
    issue = _Issue(file_path="a.py", line=3, description="")
    existing = [
        ExistingComment(path="a.py", line=3, body="", html_url="https://x/1", resolved=False)
    ]
    assert match_existing_comment(issue, existing) is None


# ---------------------------------------------------------------------------
# partition_issues_by_existing_comments
# ---------------------------------------------------------------------------


def test_partition_drops_resolved_matches() -> None:
    issue = _Issue(file_path="a.py", line=3, description="SQL injection risk in the query builder")
    existing = [
        ExistingComment(
            path="a.py", line=3, body=issue.description, html_url="https://x/1", resolved=True
        )
    ]
    kept, dropped, references = partition_issues_by_existing_comments([issue], existing)
    assert kept == []
    assert dropped == [issue]
    assert references == {}


def test_partition_keeps_and_references_unresolved_matches() -> None:
    issue = _Issue(file_path="a.py", line=3, description="SQL injection risk in the query builder")
    existing = [
        ExistingComment(
            path="a.py", line=3, body=issue.description, html_url="https://x/1", resolved=False
        )
    ]
    kept, dropped, references = partition_issues_by_existing_comments([issue], existing)
    assert kept == [issue]
    assert dropped == []
    assert references[id(issue)].html_url == "https://x/1"


def test_partition_keeps_unmatched_issues_without_reference() -> None:
    issue = _Issue(file_path="a.py", line=3, description="brand-new problem never seen before")
    kept, dropped, references = partition_issues_by_existing_comments([issue], [])
    assert kept == [issue]
    assert dropped == []
    assert references == {}


def test_partition_preserves_order_and_covers_every_issue() -> None:
    resolved_issue = _Issue(file_path="a.py", line=1, description="dup one")
    unresolved_issue = _Issue(file_path="a.py", line=2, description="dup two")
    fresh_issue = _Issue(file_path="a.py", line=3, description="brand new")
    existing = [
        ExistingComment(path="a.py", line=1, body="dup one", html_url="https://x/1", resolved=True),
        ExistingComment(
            path="a.py", line=2, body="dup two", html_url="https://x/2", resolved=False
        ),
    ]
    issues = [resolved_issue, unresolved_issue, fresh_issue]
    kept, dropped, references = partition_issues_by_existing_comments(issues, existing)
    assert kept == [unresolved_issue, fresh_issue]
    assert dropped == [resolved_issue]
    assert len(kept) + len(dropped) == len(issues)
    assert set(references) == {id(unresolved_issue)}
