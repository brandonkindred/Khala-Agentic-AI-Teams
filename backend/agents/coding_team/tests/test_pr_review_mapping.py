"""Tests for the pull-request review mapping helpers (pure, no network/LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from coding_team.github_source.pr_review_mapping import (
    build_review_body,
    choose_event,
    format_comment_body,
    format_issue_comment,
    inline_comment_to_timeline_body,
    map_issues_to_comments,
    parse_valid_lines,
    render_annotated_hunks,
)


@dataclass
class _Issue:
    """Duck-typed stand-in for CodeReviewIssue."""

    severity: str = "high"
    category: str = "logic"
    file_path: str = ""
    line: Optional[int] = None
    description: str = "something is wrong"
    suggestion: str = "fix it"


# ---------------------------------------------------------------------------
# parse_valid_lines
# ---------------------------------------------------------------------------


def test_parse_valid_lines_added_only_excludes_context() -> None:
    patch = "@@ -1,2 +1,3 @@\n context\n+added line\n more context"
    # added_only: only line2 (the +added line) is commentable.
    assert parse_valid_lines(patch, added_only=True) == {2}


def test_parse_valid_lines_default_includes_context() -> None:
    # Default now matches what render_annotated_hunks shows (added + context lines),
    # both of which GitHub allows inline comments on within a hunk.
    patch = "@@ -1,2 +1,3 @@\n context\n+added\n ctx2"
    assert parse_valid_lines(patch) == {1, 2, 3}
    assert parse_valid_lines(patch, added_only=False) == {1, 2, 3}


def test_parse_valid_lines_removed_lines_excluded() -> None:
    patch = "@@ -1,3 +1,2 @@\n keep\n-deleted\n+replacement"
    # new file: line1 keep (context), line2 replacement (added); deleted is left-only.
    assert parse_valid_lines(patch) == {1, 2}
    assert parse_valid_lines(patch, added_only=True) == {2}


def test_parse_valid_lines_multiple_hunks() -> None:
    patch = "@@ -1,1 +1,2 @@\n a\n+b\n@@ -10,1 +11,2 @@\n c\n+d"
    # hunk1: +1 -> line1 ctx, line2 added. hunk2: +11 -> line11 ctx, line12 added.
    assert parse_valid_lines(patch) == {1, 2, 11, 12}
    assert parse_valid_lines(patch, added_only=True) == {2, 12}


def test_parse_valid_lines_no_newline_marker_skipped() -> None:
    patch = "@@ -1 +1 @@\n+only line\n\\ No newline at end of file"
    assert parse_valid_lines(patch) == {1}


def test_parse_valid_lines_empty_patch() -> None:
    assert parse_valid_lines("") == set()
    assert parse_valid_lines(None) == set()  # type: ignore[arg-type]


def test_parse_valid_lines_ignores_lines_before_first_hunk() -> None:
    patch = "garbage header\n+not in a hunk\n@@ -1 +1 @@\n+real"
    assert parse_valid_lines(patch) == {1}


# ---------------------------------------------------------------------------
# render_annotated_hunks
# ---------------------------------------------------------------------------


def test_render_annotated_hunks_single_hunk() -> None:
    patch = "@@ -1,2 +1,3 @@\n ctx\n+added\n more"
    assert render_annotated_hunks(patch) == "1: ctx\n2: added\n3: more"


def test_render_annotated_hunks_omits_removed_lines() -> None:
    patch = "@@ -1,3 +1,2 @@\n keep\n-deleted\n+replacement"
    # Removed line has no new-file position and is dropped; numbering stays aligned.
    assert render_annotated_hunks(patch) == "1: keep\n2: replacement"


def test_render_annotated_hunks_separates_multiple_hunks() -> None:
    patch = "@@ -1,1 +1,2 @@\n a\n+b\n@@ -10,1 +11,2 @@\n c\n+d"
    assert render_annotated_hunks(patch) == "1: a\n2: b\n...\n11: c\n12: d"


def test_render_annotated_hunks_empty_patch() -> None:
    assert render_annotated_hunks("") == ""


def test_render_annotated_hunks_lines_align_with_valid_lines() -> None:
    # Every commentable (added) line must appear in the rendered output with its number.
    patch = "@@ -5,2 +5,3 @@\n keep\n+new1\n+new2"
    rendered = render_annotated_hunks(patch)
    for line in parse_valid_lines(patch):
        assert f"{line}: " in rendered


# ---------------------------------------------------------------------------
# map_issues_to_comments
# ---------------------------------------------------------------------------


def test_map_in_diff_line_becomes_inline_comment() -> None:
    valid = {"app/main.py": {2, 5}}
    issues = [_Issue(file_path="app/main.py", line=5, description="bug here")]
    inline, leftover = map_issues_to_comments(issues, valid)
    assert leftover == []
    assert inline == [
        {"path": "app/main.py", "line": 5, "side": "RIGHT", "body": format_comment_body(issues[0])}
    ]


def test_map_out_of_diff_line_goes_to_body() -> None:
    valid = {"app/main.py": {2}}
    issues = [_Issue(file_path="app/main.py", line=99)]
    inline, leftover = map_issues_to_comments(issues, valid)
    assert inline == []
    assert leftover == issues


def test_map_missing_line_goes_to_body() -> None:
    valid = {"app/main.py": {2}}
    issues = [_Issue(file_path="app/main.py", line=None)]
    inline, leftover = map_issues_to_comments(issues, valid)
    assert inline == []
    assert leftover == issues


def test_map_normalizes_leading_dot_slash() -> None:
    valid = {"app/main.py": {3}}
    issues = [_Issue(file_path="./app/main.py", line=3)]
    inline, leftover = map_issues_to_comments(issues, valid)
    assert len(inline) == 1
    assert inline[0]["path"] == "app/main.py"


def test_map_basename_fallback_when_unique() -> None:
    valid = {"src/app/main.py": {4}}
    issues = [_Issue(file_path="main.py", line=4)]
    inline, _ = map_issues_to_comments(issues, valid)
    assert inline[0]["path"] == "src/app/main.py"


def test_map_unknown_file_goes_to_body() -> None:
    valid = {"app/main.py": {2}}
    issues = [_Issue(file_path="other.py", line=2)]
    inline, leftover = map_issues_to_comments(issues, valid)
    assert inline == []
    assert leftover == issues


# ---------------------------------------------------------------------------
# format_comment_body / format_issue_comment / inline_comment_to_timeline_body
# ---------------------------------------------------------------------------


def test_format_comment_body_includes_severity_and_fix() -> None:
    body = format_comment_body(_Issue(severity="high", category="logic", description="X", suggestion="Y"))
    assert "**[HIGH] logic** — X" in body
    assert "**Suggested fix:** Y" in body


def test_format_comment_body_no_suggestion() -> None:
    body = format_comment_body(_Issue(suggestion=""))
    assert "Suggested fix" not in body


def test_format_issue_comment_prefixes_file_location() -> None:
    body = format_issue_comment(_Issue(file_path="a.py", description="D1"))
    assert body.startswith("`a.py` — ")
    assert "D1" in body


def test_format_issue_comment_without_file_has_no_prefix() -> None:
    body = format_issue_comment(_Issue(file_path="", description="D2"))
    assert not body.startswith("`")
    assert "D2" in body


def test_inline_comment_to_timeline_body_prefixes_path_and_line() -> None:
    comment = {"path": "a.py", "line": 12, "side": "RIGHT", "body": "**[HIGH] logic** — boom"}
    body = inline_comment_to_timeline_body(comment)
    assert body == "`a.py:12` — **[HIGH] logic** — boom"


# ---------------------------------------------------------------------------
# build_review_body — summary-only; never lists findings
# ---------------------------------------------------------------------------


def test_build_review_body_is_summary_only() -> None:
    body = build_review_body("Summary text", "Spec notes")
    assert "Summary text" in body
    assert "**Spec compliance:** Spec notes" in body
    # Findings are never folded into the body — each gets its own comment.
    assert "General findings" not in body


def test_build_review_body_fallback_when_empty() -> None:
    assert "No blocking issues" in build_review_body("", "")


def test_build_review_body_fallback_reflects_findings_when_summary_empty() -> None:
    # An empty summary must not claim "no blocking issues" when findings were
    # posted as comments — the fallback reports the count instead.
    body = build_review_body("", "", issue_count=2)
    assert "No blocking issues" not in body
    assert "2 finding(s) posted as comment(s)" in body


# ---------------------------------------------------------------------------
# choose_event
# ---------------------------------------------------------------------------


def test_choose_event_request_changes_on_high() -> None:
    issues = [_Issue(severity="high")]
    assert choose_event(issues, author="alice", reviewer="bot") == "REQUEST_CHANGES"


def test_choose_event_comment_when_no_blocking() -> None:
    issues = [_Issue(severity="low")]
    assert choose_event(issues, author="alice", reviewer="bot") == "COMMENT"


def test_choose_event_comment_on_self_pr() -> None:
    issues = [_Issue(severity="critical")]
    # Same author == reviewer: GitHub would 422 on REQUEST_CHANGES, so use COMMENT.
    assert choose_event(issues, author="bot", reviewer="bot") == "COMMENT"


def test_choose_event_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PR_REVIEW_EVENT", "approve")
    assert choose_event([_Issue(severity="critical")], author="a", reviewer="b") == "APPROVE"
