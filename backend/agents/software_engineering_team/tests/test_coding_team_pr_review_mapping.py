"""Tests for the pull-request review mapping helpers (pure, no network/LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import pytest

from software_engineering_team.github_source.client import Issue
from software_engineering_team.github_source.issue_proposals import (
    annotate_duplicate_proposals,
    build_issue_from_proposal,
    duplicate_check_max_open_issues,
    find_matching_open_issue,
    group_similar_findings,
    proposal_from_findings,
)
from software_engineering_team.github_source.pr_review_mapping import (
    _fallback_title,
    _location_prefix,
    build_review_body,
    choose_event,
    format_comment_body,
    format_issue_comment,
    format_numbered_source_line,
    format_removed_excerpt,
    inline_comment_to_timeline_body,
    is_within_diff,
    map_issues_to_comments,
    numbered_line_width,
    parse_removed_lines,
    parse_valid_lines,
    render_annotated_hunks,
    render_removed_hunks,
    split_review_comments,
)


@dataclass
class _Issue:
    """Duck-typed stand-in for CodeReviewIssue."""

    severity: str = "high"
    category: str = "logic"
    file_path: str = ""
    line: Optional[int] = None
    title: str = "Something is wrong"
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


def test_parse_removed_lines_deletion_only_hunk() -> None:
    patch = "@@ -10,3 +10,0 @@\n-alpha\n-beta\n-gamma"
    assert parse_removed_lines(patch) == {10, 11, 12}
    assert parse_valid_lines(patch, added_only=True) == set()


def test_format_removed_excerpt_clips_deleted_text() -> None:
    patch = "@@ -2,2 +2,0 @@\n-keep this\n-drop that"
    excerpt = format_removed_excerpt(patch)
    assert "L2: keep this" in excerpt
    assert "L3: drop that" in excerpt


# ---------------------------------------------------------------------------
# render_annotated_hunks
# ---------------------------------------------------------------------------


def _gutter_and_source(line: str) -> tuple[str, str]:
    """Split one numbered review line into ``(gutter, source)``.

    Accepts either the ``N: `` or ``N| `` gutter, with an optional leading
    ``+``/``>`` change-surface marker column, so this helper can describe the
    alignment contract independently of the separator or marker.
    """
    match = re.match(r"^([+>]?[ ]*\d+(?:: |\| ))(.*)$", line)
    assert match is not None, f"expected a numbered gutter, got {line!r}"
    return match.group(1), match.group(2)


def test_render_annotated_hunks_single_hunk() -> None:
    patch = "@@ -1,2 +1,3 @@\n ctx\n+added\n more"
    # Added line 2 carries a ``+`` marker; context lines 1/3 carry a space. The
    # marker sits before the number, so the rendered numbers are unchanged.
    assert render_annotated_hunks(patch) == " 1| ctx\n+2| added\n 3| more"


def test_render_annotated_hunks_aligns_source_columns_across_digit_widths() -> None:
    """A 4-space hanging indent must stay 4 columns when line numbers cross 9→10.

    Unpadded ``9: `` (3 chars) vs ``10: `` (4 chars) shifts every later source
    column by one, so ``        'bar',`` looks like a 5-space hang — the
    false "inconsistent leading whitespace" finding on continuation arguments.
    """
    patch = "@@ -9,2 +9,3 @@\n     foo(\n+        'bar',\n     )"
    lines = render_annotated_hunks(patch).splitlines()
    gutters, sources = zip(*(_gutter_and_source(line) for line in lines))
    assert list(sources) == ["    foo(", "        'bar',", "    )"]
    assert len({len(g) for g in gutters}) == 1
    # The argument's extra indent is exactly one 4-space hang, not 5.
    assert sources[1].index("'") - sources[0].index("f") == 4


def test_render_annotated_hunks_preserves_call_argument_hanging_indent() -> None:
    """Continuation arguments keep their source indent after the gutter.

    Regression: the reviewer flagged ``append_review_transcript_entries(\n        "j1",``
    as extra leading whitespace on the string argument. The hanging indent is
    real, legal Python; only the numbered rendering made it look irregular.
    """
    patch = (
        "@@ -149,3 +149,5 @@ def test_flush() -> None:\n"
        '     record_review_start("j1", "o", "r", 7, "u", "alice")\n'
        "+    append_review_transcript_entries(\n"
        '+        "j1",\n'
        "+    )\n"
    )
    lines = render_annotated_hunks(patch).splitlines()
    gutters, sources = zip(*(_gutter_and_source(line) for line in lines))
    assert sources[1] == "    append_review_transcript_entries("
    assert sources[2] == '        "j1",'
    assert len({len(g) for g in gutters}) == 1
    assert sources[2].index('"') - sources[1].index("a") == 4


def test_render_annotated_hunks_omits_removed_lines() -> None:
    patch = "@@ -1,3 +1,2 @@\n keep\n-deleted\n+replacement"
    # Removed line has no new-file position and is dropped; numbering stays aligned.
    # Context line 1 gets a space marker; the added replacement line 2 gets ``+``.
    assert render_annotated_hunks(patch) == " 1| keep\n+2| replacement"


def test_render_annotated_hunks_separates_multiple_hunks() -> None:
    patch = "@@ -1,1 +1,2 @@\n a\n+b\n@@ -10,1 +11,2 @@\n c\n+d"
    # Added lines 2/12 carry ``+``; context lines 1/11 carry a space; the bare
    # ``...`` inter-hunk gap is never marked.
    assert render_annotated_hunks(patch) == "  1| a\n+ 2| b\n...\n 11| c\n+12| d"


def test_render_annotated_hunks_empty_patch() -> None:
    assert render_annotated_hunks("") == ""


def test_numbered_line_width_empty_and_widest() -> None:
    assert numbered_line_width([]) == 1
    assert numbered_line_width([9]) == 1
    assert numbered_line_width([9, 10]) == 2
    assert numbered_line_width([99, 100]) == 3


def test_format_numbered_source_line_equal_gutter_width() -> None:
    width = numbered_line_width([9, 10])
    nine = format_numbered_source_line(9, "    foo(", width=width)
    ten = format_numbered_source_line(10, "        'bar',", width=width)
    g9, s9 = _gutter_and_source(nine)
    g10, s10 = _gutter_and_source(ten)
    assert s9 == "    foo("
    assert s10 == "        'bar',"
    assert len(g9) == len(g10)
    assert s10.index("'") - s9.index("f") == 4


def test_format_numbered_source_line_marker_preserves_number() -> None:
    # A marker adds a single leading column but must not change the rendered
    # number: the marked and un-marked lines cite the same number, and a marked
    # touched line and a marked context line keep equal gutter widths.
    width = numbered_line_width([9, 10])
    plain = format_numbered_source_line(9, "foo", width=width)
    touched = format_numbered_source_line(9, "foo", width=width, marker="+")
    context = format_numbered_source_line(10, "bar", width=width, marker=" ")
    assert plain == " 9| foo"
    assert touched == "+ 9| foo"
    assert context == " 10| bar"
    # The digit run recovered from each variant is identical (9), regardless of
    # the marker column.
    for rendered, expected in ((plain, 9), (touched, 9), (context, 10)):
        gutter, _ = _gutter_and_source(rendered)
        assert int(re.search(r"\d+", gutter).group()) == expected


def test_render_annotated_hunks_lines_align_with_valid_lines() -> None:
    # Every commentable line appears with its correct number; added lines carry a
    # ``+`` marker and context lines a space, and the numbers align 1:1 with
    # parse_valid_lines so a cited line maps to a real location. Two hunks so the
    # bare ``...`` inter-hunk gap row (which carries no marker/number) is also
    # exercised and confirmed to be skipped, not mismatched as a numbered line.
    patch = "@@ -5,2 +5,3 @@\n keep\n+new1\n+new2\n@@ -20,1 +21,2 @@\n c\n+d"
    rendered = render_annotated_hunks(patch)
    by_number = {}
    for ln in rendered.splitlines():
        if ln == "...":
            continue
        m = re.match(r"^([+> ])[ ]*(\d+)\| ", ln)
        assert m is not None, f"expected a marked numbered gutter, got {ln!r}"
        by_number[int(m.group(2))] = m.group(1)
    added = parse_valid_lines(patch, added_only=True)
    assert added == {6, 7, 22}
    # Every valid (added + context) line is rendered exactly once...
    assert set(by_number) == parse_valid_lines(patch) == {5, 6, 7, 21, 22}
    # ...added lines marked ``+``, context lines marked with a space.
    for number, marker in by_number.items():
        assert marker == ("+" if number in added else " ")


# ---------------------------------------------------------------------------
# render_removed_hunks
# ---------------------------------------------------------------------------


def test_render_removed_hunks_single_hunk() -> None:
    patch = "@@ -1,3 +1,2 @@\n keep\n-deleted\n+replacement"
    # Old-file side: context line, then the removed line; the added
    # replacement has no old-file position and is omitted.
    assert render_removed_hunks(patch) == "keep\ndeleted"


def test_render_removed_hunks_omits_added_lines() -> None:
    patch = "@@ -1,2 +1,3 @@\n ctx\n+added\n more"
    # No removed lines in this hunk; only the two context rows survive.
    assert render_removed_hunks(patch) == "ctx\nmore"


def test_render_removed_hunks_deletion_only_hunk() -> None:
    patch = "@@ -10,3 +10,0 @@\n-alpha\n-beta\n-gamma"
    assert render_removed_hunks(patch) == "alpha\nbeta\ngamma"


def test_render_removed_hunks_separates_multiple_hunks() -> None:
    patch = "@@ -1,2 +1,1 @@\n a\n-b\n@@ -10,2 +11,1 @@\n c\n-d"
    assert render_removed_hunks(patch) == "a\nb\n...\nc\nd"


def test_render_removed_hunks_empty_patch() -> None:
    assert render_removed_hunks("") == ""
    assert render_removed_hunks(None) == ""


def test_render_removed_hunks_no_gutter_unlike_render_annotated_hunks() -> None:
    # replaced_content mirrors full_content's plain-body shape, not the
    # pre_numbered N| -gutter shape used for hunk_files.
    patch = "@@ -5,2 +5,2 @@\n keep\n-old line\n+new line"
    assert render_removed_hunks(patch) == "keep\nold line"
    assert "|" not in render_removed_hunks(patch)


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


def test_map_out_of_diff_line_becomes_file_level_comment() -> None:
    # The file changed but the cited line is not in the diff: the finding attaches
    # to the file as a whole (no fabricated line) rather than becoming a leftover.
    valid = {"app/main.py": {2}}
    issues = [_Issue(file_path="app/main.py", line=99)]
    comments, leftover = map_issues_to_comments(issues, valid)
    assert leftover == []
    assert comments == [
        {"path": "app/main.py", "subject_type": "file", "body": format_comment_body(issues[0])}
    ]


def test_map_missing_line_becomes_file_level_comment() -> None:
    valid = {"app/main.py": {2}}
    issues = [_Issue(file_path="app/main.py", line=None)]
    comments, leftover = map_issues_to_comments(issues, valid)
    assert leftover == []
    assert comments == [
        {"path": "app/main.py", "subject_type": "file", "body": format_comment_body(issues[0])}
    ]


def test_map_non_numeric_line_becomes_file_level_comment() -> None:
    # A finding whose line is a non-numeric string (e.g. from a malformed LLM
    # response) must not raise ValueError -- it falls back to a file-level
    # comment, same as a missing line.
    valid = {"app/main.py": {2}}
    issues = [_Issue(file_path="app/main.py", line="N/A")]  # type: ignore[arg-type]
    comments, leftover = map_issues_to_comments(issues, valid)
    assert leftover == []
    assert comments == [
        {"path": "app/main.py", "subject_type": "file", "body": format_comment_body(issues[0])}
    ]


def test_map_normalizes_leading_dot_slash() -> None:
    valid = {"app/main.py": {3}}
    issues = [_Issue(file_path="./app/main.py", line=3)]
    comments, leftover = map_issues_to_comments(issues, valid)
    assert len(comments) == 1
    assert comments[0]["path"] == "app/main.py"
    assert comments[0]["line"] == 3


def test_map_does_not_over_strip_dotfile_into_unrelated_match() -> None:
    # Before the fix, _normalize_path used file_path.lstrip("./"), which strips any
    # combination of "." and "/" -- turning ".gitignore" into "gitignore" and risking
    # a match against an unrelated file that happens to be named "gitignore" in the
    # diff. The fix only strips a literal leading "./" prefix, so this must NOT match.
    valid = {"gitignore": {1}}
    issues = [_Issue(file_path=".gitignore", line=1)]
    comments, leftover = map_issues_to_comments(issues, valid)
    assert comments == []
    assert leftover == issues


def test_map_basename_fallback_when_unique() -> None:
    valid = {"src/app/main.py": {4}}
    issues = [_Issue(file_path="main.py", line=4)]
    comments, _ = map_issues_to_comments(issues, valid)
    assert comments[0]["path"] == "src/app/main.py"


def test_map_unknown_file_goes_to_leftover() -> None:
    # The file is not in the diff at all, so it can't be a review comment (line- or
    # file-level): it falls back to a standalone conversation comment.
    valid = {"app/main.py": {2}}
    issues = [_Issue(file_path="other.py", line=2)]
    comments, leftover = map_issues_to_comments(issues, valid)
    assert comments == []
    assert leftover == issues


def test_map_no_file_goes_to_leftover() -> None:
    # A finding naming no file can't anchor to the review at all.
    valid = {"app/main.py": {2}}
    issues = [_Issue(file_path="", line=2)]
    comments, leftover = map_issues_to_comments(issues, valid)
    assert comments == []
    assert leftover == issues


def test_map_mixed_findings_split_three_ways() -> None:
    # One in-diff line, one off-diff line on a changed file, one unknown file:
    # inline + file-level review comments, plus a single leftover.
    valid = {"app/main.py": {2, 5}}
    in_diff = _Issue(file_path="app/main.py", line=5, description="anchored")
    off_diff = _Issue(file_path="app/main.py", line=99, description="file-level")
    unknown = _Issue(file_path="gone.py", line=2, description="leftover")
    comments, leftover = map_issues_to_comments([in_diff, off_diff, unknown], valid)
    assert leftover == [unknown]
    assert {
        "path": "app/main.py",
        "line": 5,
        "side": "RIGHT",
        "body": format_comment_body(in_diff),
    } in comments
    assert {
        "path": "app/main.py",
        "subject_type": "file",
        "body": format_comment_body(off_diff),
    } in comments
    assert len(comments) == 2


# ---------------------------------------------------------------------------
# is_within_diff
# ---------------------------------------------------------------------------


def test_is_within_diff_true_for_commentable_line() -> None:
    valid = {"app/main.py": {2, 5}}
    finding = _Issue(file_path="app/main.py", line=5)
    assert is_within_diff(finding, valid) is True


def test_is_within_diff_false_for_off_diff_line() -> None:
    valid = {"app/main.py": {2}}
    finding = _Issue(file_path="app/main.py", line=99)
    assert is_within_diff(finding, valid) is False


def test_is_within_diff_false_for_unknown_file() -> None:
    valid = {"app/main.py": {2}}
    finding = _Issue(file_path="other.py", line=2)
    assert is_within_diff(finding, valid) is False


def test_is_within_diff_false_for_missing_line() -> None:
    valid = {"app/main.py": {2}}
    finding = _Issue(file_path="app/main.py", line=None)
    assert is_within_diff(finding, valid) is False


def test_is_within_diff_false_for_non_numeric_line_does_not_raise() -> None:
    # A malformed finding with a non-numeric line must not raise ValueError -- it is
    # simply treated as not within the diff.
    valid = {"app/main.py": {2}}
    finding = _Issue(file_path="app/main.py", line="N/A")  # type: ignore[arg-type]
    assert is_within_diff(finding, valid) is False


# ---------------------------------------------------------------------------
# split_review_comments
# ---------------------------------------------------------------------------


def test_split_review_comments_partitions_by_shape() -> None:
    line_a = {"path": "a.py", "line": 2, "side": "RIGHT", "body": "x"}
    file_b = {"path": "b.py", "subject_type": "file", "body": "y"}
    line_c = {"path": "c.py", "line": 9, "side": "RIGHT", "body": "z"}
    line_anchored, file_level = split_review_comments([line_a, file_b, line_c])
    assert line_anchored == [line_a, line_c]  # order preserved
    assert file_level == [file_b]
    # No entry is dropped or duplicated.
    assert len(line_anchored) + len(file_level) == 3


def test_split_review_comments_empty() -> None:
    assert split_review_comments([]) == ([], [])


def test_split_review_comments_all_line_anchored() -> None:
    items = [{"path": "a.py", "line": 1, "side": "RIGHT", "body": "x"}]
    assert split_review_comments(items) == (items, [])


def test_split_review_comments_all_file_level() -> None:
    items = [{"path": "a.py", "subject_type": "file", "body": "x"}]
    assert split_review_comments(items) == ([], items)


def test_split_review_comments_unexpected_shape_not_dropped() -> None:
    # A comment that is neither line-anchored nor subject_type=file must still be
    # conserved (routed to file_level) rather than silently lost.
    weird = {"path": "a.py", "body": "no anchor"}
    line_anchored, file_level = split_review_comments([weird])
    assert line_anchored == []
    assert file_level == [weird]


# ---------------------------------------------------------------------------
# format_comment_body / format_issue_comment / inline_comment_to_timeline_body
# ---------------------------------------------------------------------------


def test_format_comment_body_includes_title_severity_description_and_fix() -> None:
    body = format_comment_body(
        _Issue(
            severity="high", category="logic", title="Bug title", description="X", suggestion="Y"
        )
    )
    assert "**Bug title**" in body
    assert "[HIGH] logic" in body
    assert "X" in body
    assert "**Suggested fix:** Y" in body
    # Title heading must come before the description, and description before the fix.
    assert body.index("Bug title") < body.index("X") < body.index("Suggested fix")


def test_format_comment_body_no_suggestion() -> None:
    body = format_comment_body(_Issue(suggestion=""))
    assert "Suggested fix" not in body


def test_format_comment_body_omits_description_when_it_equals_derived_title() -> None:
    # A short description becomes its own derived title verbatim; the body must
    # not then repeat that exact text a second time as the description paragraph.
    body = format_comment_body(_Issue(title="", description="leftover one"))
    assert body.count("leftover one") == 1


def test_fallback_title_never_exceeds_max_len() -> None:
    # A first line with no word boundary must still respect the documented "at
    # most _TITLE_MAX_LEN characters TOTAL" postcondition, ellipsis included.
    title = _fallback_title("a" * 200)
    assert len(title) <= 80
    assert title.endswith("…")


def test_format_comment_body_falls_back_to_derived_title_when_blank() -> None:
    body = format_comment_body(
        _Issue(title="", description="The UserListComponent does not paginate results.")
    )
    assert "**The UserListComponent does not paginate results.**" in body


def test_format_comment_body_no_duplicate_severity_tag_when_title_and_description_blank() -> None:
    # With no title and no description to derive one from, the heading itself
    # falls back to "[SEVERITY] category" -- the separate tag line must not
    # repeat that same text a second time.
    body = format_comment_body(
        _Issue(severity="info", category="general", title="", description="", suggestion="")
    )
    assert body == "**[INFO] general**"
    assert body.count("general") == 1


def test_location_prefix_with_line() -> None:
    assert _location_prefix("a.py", 5) == "`a.py:5` — "


def test_location_prefix_without_line() -> None:
    assert _location_prefix("a.py") == "`a.py` — "


def test_location_prefix_nonpositive_line_omitted() -> None:
    assert _location_prefix("a.py", 0) == "`a.py` — "


def test_location_prefix_empty_path_is_empty() -> None:
    assert _location_prefix("") == ""


def test_format_issue_comment_prefixes_file_location() -> None:
    body = format_issue_comment(_Issue(file_path="a.py", title="Bad import", description="D1"))
    assert body.startswith("`a.py` — ")
    assert "D1" in body
    assert "Bad import" in body


def test_format_issue_comment_without_file_has_no_prefix() -> None:
    body = format_issue_comment(_Issue(file_path="", title="Bad import", description="D2"))
    assert not body.startswith("`")
    assert "D2" in body
    assert "Bad import" in body


def test_format_issue_comment_with_none_file_has_no_prefix() -> None:
    # A finding can carry file_path=None; the `... or ""` guard must treat it like
    # an empty path (no location prefix) rather than rendering `None`.
    body = format_issue_comment(
        _Issue(file_path=None, title="Bad import", description="D3")  # type: ignore[arg-type]
    )
    assert not body.startswith("`")
    assert "None" not in body
    assert "D3" in body
    assert "Bad import" in body


def test_inline_comment_to_timeline_body_prefixes_path_and_line() -> None:
    # inline_comment_to_timeline_body is deliberately content-agnostic -- it
    # only prepends a location to whatever body string it's given (the body's
    # own title/description/fix layout is format_comment_body's concern,
    # covered by the dedicated format_comment_body tests above).
    body_text = format_comment_body(_Issue(severity="high", category="logic", title="Bug"))
    comment = {"path": "a.py", "line": 12, "side": "RIGHT", "body": body_text}
    body = inline_comment_to_timeline_body(comment)
    assert body == f"`a.py:12` — {body_text}"


def test_inline_comment_to_timeline_body_drops_nonpositive_line() -> None:
    # A non-positive line (invalid in GitHub's 1-based diff) must not render as
    # `path:0`; it falls back to just the path.
    comment = {"path": "a.py", "line": 0, "body": "**[HIGH] logic** — boom"}
    assert inline_comment_to_timeline_body(comment) == "`a.py` — **[HIGH] logic** — boom"


def test_inline_comment_to_timeline_body_file_level_has_path_only() -> None:
    # A dropped file-level comment carries no `line`; it re-posts with a bare
    # `path` location prefix.
    comment = {"path": "a.py", "subject_type": "file", "body": "**[LOW] logic** — boom"}
    assert inline_comment_to_timeline_body(comment) == "`a.py` — **[LOW] logic** — boom"


# ---------------------------------------------------------------------------
# build_review_body — summary-only; never lists findings
# ---------------------------------------------------------------------------


def test_build_review_body_is_summary_only() -> None:
    # issue_count=1 keeps this test on the "findings exist" branch, which is
    # what it means to test here — the zero-issue case always short-circuits
    # to the fixed affirmational message regardless of summary/spec notes.
    body = build_review_body("Summary text", "Spec notes", issue_count=1)
    assert "Summary text" in body
    assert "**Spec compliance:** Spec notes" in body
    # Findings are never folded into the body — each gets its own comment.
    assert "General findings" not in body


def test_build_review_body_omits_spec_section_when_notes_empty() -> None:
    # With findings present but no spec gaps, the reviewer returns empty spec
    # notes and the body carries only the high-level summary — no spec section.
    body = build_review_body("Issues cluster in the auth flow.", "", issue_count=2)
    assert body == "Issues cluster in the auth flow."
    assert "Spec compliance" not in body


def test_build_review_body_rejects_negative_issue_count() -> None:
    with pytest.raises(AssertionError):
        build_review_body("", "", issue_count=-1)


def test_build_review_body_zero_issues_is_short_and_affirmational() -> None:
    assert (
        build_review_body("", "", issue_count=0) == "No issues found — the code is of good quality."
    )


def test_build_review_body_zero_issues_ignores_summary() -> None:
    # A clean review always gets the terse "all good" message, even when the
    # LLM returned a real (long) narrative summary and spec-compliance notes.
    body = build_review_body(
        "A long LLM summary", "Meets every acceptance criterion", issue_count=0
    )
    assert body == "No issues found — the code is of good quality."


def test_build_review_body_fallback_reflects_findings_when_summary_empty() -> None:
    # An empty summary must not claim "no blocking issues" when findings were
    # posted as comments — the fallback reports the count instead.
    body = build_review_body("", "", issue_count=2)
    assert "No blocking issues" not in body
    assert "2 findings reported" in body


def test_build_review_body_fallback_singular_finding() -> None:
    # Proper singular/plural: one finding uses "finding", not "finding(s)".
    body = build_review_body("", "", issue_count=1)
    assert "1 finding reported" in body
    assert "findings" not in body


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


def test_choose_event_comment_when_authorship_unknown() -> None:
    issues = [_Issue(severity="critical")]
    # author/reviewer both omitted (default ""): authorship is unknown, so this
    # must not assume the reviewer isn't the author and risk a GitHub 422.
    assert choose_event(issues) == "COMMENT"


# ---------------------------------------------------------------------------
# group_similar_findings
# ---------------------------------------------------------------------------


def test_group_similar_findings_merges_near_duplicates_same_category() -> None:
    findings = [
        _Issue(category="standards", description="bare import `os` should be scoped"),
        _Issue(category="standards", description="bare import `sys` should be scoped"),
        _Issue(category="standards", description="bare import `re` should be scoped"),
    ]
    groups = group_similar_findings(findings)
    assert len(groups) == 1
    assert groups[0] == findings


def test_group_similar_findings_never_merges_across_categories() -> None:
    findings = [
        _Issue(category="standards", description="bare import `os` should be scoped"),
        _Issue(category="logic", description="bare import `os` should be scoped"),
    ]
    groups = group_similar_findings(findings)
    assert len(groups) == 2


def test_group_similar_findings_keeps_dissimilar_descriptions_separate() -> None:
    findings = [
        _Issue(category="logic", description="off-by-one error in loop bound"),
        _Issue(category="logic", description="null pointer dereference on missing config"),
    ]
    groups = group_similar_findings(findings)
    assert len(groups) == 2


def test_group_similar_findings_preserves_order() -> None:
    a = _Issue(category="logic", description="off-by-one error in loop bound")
    b = _Issue(category="logic", description="off-by-one error in the loop bound")
    c = _Issue(category="logic", description="null pointer dereference on missing config")
    groups = group_similar_findings([a, b, c])
    assert groups == [[a, b], [c]]


# ---------------------------------------------------------------------------
# proposal_from_findings / build_issue_from_proposal
# ---------------------------------------------------------------------------


def test_proposal_from_findings_serializes_single_finding_group() -> None:
    issue = _Issue(
        severity="critical",
        category="logic",
        file_path="src/a.py",
        line=12,
        description="latent bug",
        suggestion="do X",
    )
    p = proposal_from_findings([issue], 2)
    assert p == {
        "id": "p2",
        "severity": "critical",
        "category": "logic",
        "file_path": "src/a.py",
        "line": 12,
        "description": "latent bug",
        "suggestion": "do X",
        "locations": [
            {
                "file_path": "src/a.py",
                "line": 12,
                "description": "latent bug",
                "suggestion": "do X",
            }
        ],
        "issue_number": None,
        "issue_url": None,
    }


def test_proposal_from_findings_drops_nonpositive_line_and_defaults() -> None:
    p = proposal_from_findings([_Issue(severity="", category="", file_path="", line=0)], 0)
    assert p["line"] is None
    assert p["severity"] == "info"
    assert p["category"] == "general"
    assert p["file_path"] == ""


def test_proposal_from_findings_combines_a_group() -> None:
    findings = [
        _Issue(
            severity="medium",
            category="standards",
            file_path="src/a.py",
            line=1,
            description="bare import `os`",
            suggestion="scope the import",
        ),
        _Issue(
            severity="high",
            category="standards",
            file_path="src/b.py",
            line=5,
            description="bare import `sys`",
            suggestion="scope the import",
        ),
    ]
    p = proposal_from_findings(findings, 0)
    # Severity is the most urgent across the group; top-level fields mirror
    # the first (representative) finding; locations carries every finding.
    assert p["severity"] == "high"
    assert p["category"] == "standards"
    assert p["file_path"] == "src/a.py"
    assert p["line"] == 1
    assert p["description"] == "bare import `os`"
    assert len(p["locations"]) == 2
    assert p["locations"][0]["file_path"] == "src/a.py"
    assert p["locations"][1]["file_path"] == "src/b.py"


def test_build_issue_from_proposal_full_detail() -> None:
    p = proposal_from_findings(
        [
            _Issue(
                severity="high",
                category="logic",
                file_path="src/a.py",
                line=12,
                description="off-by-one",
                suggestion="use <=",
            )
        ],
        0,
    )
    title, body = build_issue_from_proposal(p, pr_number=7, pr_url="https://x/pull/7")
    assert title == "[high] off-by-one"
    assert "pull request #7 (https://x/pull/7)" in body
    assert "**Severity:** high" in body
    assert "**Location:** `src/a.py:12`" in body
    assert "off-by-one" in body
    assert "### Suggested fix" in body and "use <=" in body


def test_build_issue_from_proposal_no_file_and_no_suggestion() -> None:
    p = proposal_from_findings(
        [_Issue(severity="low", file_path="", line=None, description="x", suggestion="")],
        0,
    )
    title, body = build_issue_from_proposal(p, pr_number=1, pr_url="u")
    assert title == "[low] x"
    assert "**Location:** n/a" in body
    assert "### Suggested fix" not in body


def test_build_issue_from_proposal_blank_description_and_title_truncation() -> None:
    # Blank description -> generic headline and a placeholder description line.
    p_blank = proposal_from_findings([_Issue(description="", suggestion="")], 0)
    title, body = build_issue_from_proposal(p_blank, pr_number=1, pr_url="u")
    assert "code review finding" in title
    assert "_No description provided._" in body
    # A very long description is truncated to a single-line, bounded title.
    p_long = proposal_from_findings([_Issue(description="Z" * 400)], 0)
    long_title, _ = build_issue_from_proposal(p_long, pr_number=1, pr_url="u")
    assert len(long_title) <= 120
    assert long_title.endswith("…")


# ---------------------------------------------------------------------------
# find_matching_open_issue / annotate_duplicate_proposals
# ---------------------------------------------------------------------------


def _open_issue(number: int, title: str, body: str = "") -> Issue:
    return Issue(
        number=number,
        title=title,
        body=body,
        state="open",
        html_url=f"https://x/issues/{number}",
        labels=(),
        id=number,
    )


def test_find_matching_open_issue_matches_own_combined_proposal_title_wrapper() -> None:
    # Regression: an issue Khala itself filed from a combined (multi-location)
    # proposal carries a "[severity] headline (N occurrences)" title -- without
    # un-wrapping that title first, the wrapper text dilutes a short headline's
    # similarity ratio (0.489, computed) just below the with-location threshold
    # (0.5), so a rerun would fail to recognize its own previously-filed issue.
    proposal = proposal_from_findings(
        [_Issue(file_path="src/a.py", severity="high", description="memory leak")], 0
    )
    issue = _open_issue(1, "[high] memory leak (2 occurrences)", body="See `src/a.py` for details.")
    assert find_matching_open_issue(proposal, [issue]) is issue


def test_find_matching_open_issue_matches_own_single_location_title_wrapper() -> None:
    # Same wrapper concern for a single-location Khala-filed issue (no
    # "(N occurrences)" suffix, just the severity prefix) -- text similarity
    # alone (no location signal) must still clear the no-location threshold.
    proposal = proposal_from_findings(
        [_Issue(file_path="", severity="critical", description="off-by-one error")], 0
    )
    issue = _open_issue(1, "[critical] off-by-one error", body="")
    assert find_matching_open_issue(proposal, [issue]) is issue


def test_find_matching_open_issue_location_plus_moderate_text_matches() -> None:
    proposal = proposal_from_findings(
        [_Issue(file_path="src/a.py", description="race condition in worker pool")], 0
    )
    # Same words, reordered: character ratio (~0.509) barely clears the "with
    # location" bar (0.5) but is well below the "no location" bar (0.8) -- and
    # since every word carries over, word-set overlap (0.8) comfortably clears
    # the with-location token-overlap floor (0.7) too. The issue body repeats
    # the exact file_path, so the location signal makes the looser ratio bar
    # sufficient.
    issue = _open_issue(1, "worker pool race condition", body="See `src/a.py:12` for details.")
    assert find_matching_open_issue(proposal, [issue]) is issue


def test_find_matching_open_issue_moderate_text_without_location_does_not_match() -> None:
    proposal = proposal_from_findings(
        [_Issue(file_path="src/a.py", description="null pointer dereference in parser")], 0
    )
    # Moderate-similarity title (ratio ~0.585, below the "no location" bar of
    # 0.8), but nothing in the issue mentions the file_path -- moderate
    # similarity alone is not enough.
    issue = _open_issue(
        1, "possible null pointer issue in the parser module", body="unrelated notes"
    )
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_strong_text_alone_matches_without_location() -> None:
    proposal = proposal_from_findings(
        [_Issue(file_path="", description="off-by-one error in loop bound")], 0
    )
    issue = _open_issue(1, "off-by-one error in loop bound", body="")
    assert find_matching_open_issue(proposal, [issue]) is issue


def test_find_matching_open_issue_dissimilar_and_no_location_returns_none() -> None:
    proposal = proposal_from_findings(
        [_Issue(file_path="src/a.py", description="off-by-one error")], 0
    )
    issue = _open_issue(1, "unrelated feature request", body="nothing to do with this")
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_empty_open_issues_returns_none() -> None:
    proposal = proposal_from_findings(
        [_Issue(file_path="src/a.py", description="off-by-one error")], 0
    )
    assert find_matching_open_issue(proposal, []) is None


def test_find_matching_open_issue_blank_file_path_never_triggers_location_signal() -> None:
    # Regression: an empty file_path must never "match" via the location signal,
    # since "" is a substring of every string in Python.
    proposal = proposal_from_findings(
        [_Issue(file_path="", description="a mildly related headline")], 0
    )
    issue = _open_issue(1, "a totally different headline", body="")
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_picks_highest_ratio_among_multiple_matches() -> None:
    proposal = proposal_from_findings(
        [_Issue(file_path="", description="off-by-one error in loop bound")], 0
    )
    close = _open_issue(1, "off-by-one error in the loop bound", body="")
    exact = _open_issue(2, "off-by-one error in loop bound", body="")
    assert find_matching_open_issue(proposal, [close, exact]) is exact


def test_find_matching_open_issue_ties_break_by_lowest_issue_number() -> None:
    proposal = proposal_from_findings(
        [_Issue(file_path="", description="off-by-one error in loop bound")], 0
    )
    first = _open_issue(5, "off-by-one error in loop bound", body="")
    second = _open_issue(2, "off-by-one error in loop bound", body="")
    assert find_matching_open_issue(proposal, [first, second]) is second


def test_find_matching_open_issue_threshold_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = proposal_from_findings(
        [_Issue(file_path="", description="leaked file handle in worker cleanup path")], 0
    )
    # Same words, scrambled order: character ratio is low (~0.48, below the
    # default 0.8 no-location bar) even though every word matches (token overlap
    # 0.857, above the token-overlap floor) -- so this fails to match by default.
    issue = _open_issue(1, "worker cleanup path leaked file handle", body="")
    assert find_matching_open_issue(proposal, [issue]) is None
    # A very low ratio override turns the otherwise low-character-ratio,
    # high-token-overlap pair into a match.
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_THRESHOLD_NO_LOCATION", "0.0")
    assert find_matching_open_issue(proposal, [issue]) is issue
    # Garbage falls back to the documented default rather than raising.
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_THRESHOLD_NO_LOCATION", "not-a-float")
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_high_char_ratio_but_low_token_overlap_does_not_match() -> None:
    # Regression (Codex-flagged): two headlines sharing a long templated
    # prefix/suffix around one differing keyword score a deceptively high
    # character ratio (~0.83, clears the default 0.8 no-location bar) despite
    # describing unrelated bugs. Word-set overlap (0.6) is well below the
    # token-overlap floor (0.8), so this must not match without a location signal.
    proposal = proposal_from_findings(
        [_Issue(file_path="", description="hardcoded secret in config")], 0
    )
    issue = _open_issue(1, "hardcoded timeout in config", body="")
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_token_overlap_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = proposal_from_findings(
        [_Issue(file_path="", description="hardcoded secret in config")], 0
    )
    issue = _open_issue(1, "hardcoded timeout in config", body="")
    assert find_matching_open_issue(proposal, [issue]) is None
    # A low override accepts the pair's 0.6 token overlap as sufficient.
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_TOKEN_OVERLAP_MIN", "0.5")
    assert find_matching_open_issue(proposal, [issue]) is issue
    # Garbage falls back to the documented default rather than raising.
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_TOKEN_OVERLAP_MIN", "not-a-float")
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_location_signal_alone_insufficient_for_low_token_overlap() -> (
    None
):
    # Regression (Codex-flagged): the location signal alone is weaker
    # corroboration than it looks -- many genuinely distinct bugs share the
    # same file. "hardcoded secret in config" vs "hardcoded timeout in
    # config" both mentioning config.py clears the with-location ratio bar
    # (0.83 >= 0.5) via the location signal, but word-set overlap (0.6) is
    # below the with-location token-overlap floor (0.7), so this must not
    # match even with the file_path present in the issue body.
    proposal = proposal_from_findings(
        [_Issue(file_path="config.py", description="hardcoded secret in config")], 0
    )
    issue = _open_issue(
        1, "hardcoded timeout in config", body="See `config.py` for where it's set."
    )
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_with_location_token_overlap_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = proposal_from_findings(
        [_Issue(file_path="config.py", description="hardcoded secret in config")], 0
    )
    issue = _open_issue(
        1, "hardcoded timeout in config", body="See `config.py` for where it's set."
    )
    assert find_matching_open_issue(proposal, [issue]) is None
    # A low override accepts the pair's 0.6 token overlap as sufficient.
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_TOKEN_OVERLAP_MIN_WITH_LOCATION", "0.5")
    assert find_matching_open_issue(proposal, [issue]) is issue
    # Garbage falls back to the documented default rather than raising.
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_TOKEN_OVERLAP_MIN_WITH_LOCATION", "not-a-float")
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_threshold_with_location_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = proposal_from_findings(
        [_Issue(file_path="src/a.py", description="race condition in worker pool")], 0
    )
    # The file_path appears in the issue body (location signal present), but the
    # headline/title ratio (~0.16) is far below both the default with-location
    # (0.5) and no-location (0.8) thresholds, so there is no match by default.
    issue = _open_issue(
        1, "totally unrelated feature request", body="See `src/a.py:5` for context."
    )
    assert find_matching_open_issue(proposal, [issue]) is None
    # A very low with-location override alone is still not enough -- the headline
    # and title share zero tokens, so the (still-default) token-overlap floor
    # blocks it.
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_THRESHOLD_WITH_LOCATION", "0.0")
    assert find_matching_open_issue(proposal, [issue]) is None
    # Also lowering the with-location token-overlap floor turns the
    # location-corroborated, otherwise too-dissimilar pair into a match.
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_TOKEN_OVERLAP_MIN_WITH_LOCATION", "0.0")
    assert find_matching_open_issue(proposal, [issue]) is issue
    # Garbage falls back to the documented default rather than raising.
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_THRESHOLD_WITH_LOCATION", "not-a-float")
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_location_in_title_matches() -> None:
    # The location signal must check the issue TITLE, not only its body: the
    # file_path here appears only in the title, and the body is blank.
    proposal = proposal_from_findings(
        [
            _Issue(
                file_path="a.py",
                description="race condition detected inside the shared background worker pool",
            )
        ],
        0,
    )
    # Reordered words plus the trailing file_path: ratio (~0.512) clears the
    # with-location bar (0.5) but not the no-location bar (0.8); word-set
    # overlap (~0.727) clears the with-location token-overlap floor (0.7) since
    # the base headline is long enough that the two extra path tokens barely
    # dilute it. This match happens only because the location signal (title
    # contains the file_path) is honored.
    issue = _open_issue(
        1,
        "shared background worker pool race condition detected inside a.py",
        body="",
    )
    assert find_matching_open_issue(proposal, [issue]) is issue


def test_find_matching_open_issue_rejects_substring_within_unrelated_filename() -> None:
    # Regression (Codex-flagged): a short/top-level file_path like "app.py" is a
    # literal substring of an unrelated file's name like "myapp.py" -- a plain
    # substring check would wrongly treat that as the location signal firing.
    proposal = proposal_from_findings(
        [_Issue(file_path="app.py", description="null pointer dereference in parser")], 0
    )
    # Same moderate-similarity title as test_find_matching_open_issue_location_plus_moderate_text_matches
    # (ratio ~0.585, between the with-location bar of 0.5 and the no-location bar
    # of 0.8), so a match here could only happen via the (bogus) location signal.
    issue = _open_issue(
        1, "possible null pointer issue in the parser module", body="See `myapp.py` for details."
    )
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_rejects_extension_variant_of_a_different_file() -> None:
    # Regression: "app.py" is a literal prefix of an unrelated "app.py.bak" -- a
    # trailing-boundary check that only rejects a following word character (not a
    # following ".") would wrongly treat that as the location signal firing.
    proposal = proposal_from_findings(
        [_Issue(file_path="app.py", description="null pointer dereference in parser")], 0
    )
    # Same moderate-similarity title as test_find_matching_open_issue_location_plus_moderate_text_matches
    # (ratio ~0.585, between the with-location bar of 0.5 and the no-location bar
    # of 0.8), so a match here could only happen via the (bogus) location signal.
    issue = _open_issue(
        1, "possible null pointer issue in the parser module", body="See `app.py.bak` for details."
    )
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_location_signal_still_matches_at_path_boundary() -> None:
    # The stricter boundary check must not reject a genuine match: file_path
    # bounded by a path separator and punctuation (not embedded in a longer
    # identifier) still counts as the location signal. Same reordered-words
    # pair as test_find_matching_open_issue_location_plus_moderate_text_matches
    # (ratio ~0.509, token overlap ~0.8) so the with-location token-overlap
    # floor (0.7) is also cleared.
    proposal = proposal_from_findings(
        [_Issue(file_path="app.py", description="race condition in worker pool")], 0
    )
    issue = _open_issue(
        1,
        "worker pool race condition",
        body="See `src/app.py:12` for details.",
    )
    assert find_matching_open_issue(proposal, [issue]) is issue


def test_find_matching_open_issue_uses_description_body_for_truncated_title() -> None:
    # Regression (Codex-flagged): a headline long enough that _proposal_title
    # truncates the filed issue's TITLE to fit _ISSUE_TITLE_MAX scores a low
    # ratio against that truncated title, even though the issue BODY's
    # "### Description" section (rendered by build_issue_from_proposal) always
    # carries the full, untruncated text -- so a rerun must still recognize its
    # own previously-filed issue via the body excerpt.
    long_headline = " ".join(["distinct", "unusual", "descriptive", "word"] * 30)
    proposal = proposal_from_findings(
        [_Issue(file_path="src/a.py", severity="high", description=long_headline)], 0
    )
    truncated_title = long_headline[:105].rstrip() + "…"
    issue = _open_issue(
        1,
        f"[high] {truncated_title}",
        body=f"- **Location:** `src/a.py`\n\n### Description\n{long_headline}\n\n### Suggested fix\nfix it",
    )
    assert find_matching_open_issue(proposal, [issue]) is issue


def test_find_matching_open_issue_reduces_multiline_description_excerpt_to_headline() -> None:
    # Regression (Codex-flagged): when the originally-filed finding's description
    # spanned multiple lines, the title is truncated to just the first line's
    # headline (per _proposal_title), but the issue body's "### Description"
    # section renders the FULL multi-line text verbatim. Comparing the fresh
    # proposal's single-line headline against that whole excerpt -- extra lines
    # included -- tanks the word-set overlap (0.16, computed) even though the
    # first line is an exact match, so the excerpt must be reduced to its own
    # first-line headline before comparing.
    long_headline = " ".join(["distinct", "unusual", "descriptive", "word"] * 30)
    proposal = proposal_from_findings(
        [_Issue(file_path="src/a.py", severity="high", description=long_headline)], 0
    )
    truncated_title = long_headline[:105].rstrip() + "…"
    extra_detail = (
        "This happens under concurrent load when two workers acquire the same "
        "slot at once, corrupting shared state and causing intermittent crashes."
    )
    issue = _open_issue(
        1,
        f"[high] {truncated_title}",
        body=(
            f"- **Location:** `src/a.py`\n\n### Description\n{long_headline}\n"
            f"{extra_detail}\n\n### Suggested fix\nfix it"
        ),
    )
    assert find_matching_open_issue(proposal, [issue]) is issue


def test_find_matching_open_issue_ignores_description_section_when_absent() -> None:
    # A human-filed issue (or one without Khala's "### Description" structure)
    # must fall back to title-only matching -- no false match conjured from an
    # absent section.
    proposal = proposal_from_findings(
        [_Issue(file_path="src/a.py", description="off-by-one error")], 0
    )
    issue = _open_issue(
        1, "unrelated feature request", body="src/a.py mentioned here but nothing else"
    )
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_generic_headline_does_not_override_conflicting_location() -> None:
    # Regression (Codex-flagged): a generic/short headline can be identical
    # between two genuinely distinct findings. When the proposal names its own
    # file_path and the candidate issue explicitly declares a DIFFERENT
    # location, that's counter-evidence the text-alone signal must not
    # override -- without this, "missing null check" in b.py would wrongly
    # pre-link to an existing issue about the same headline in a.py.
    proposal = proposal_from_findings(
        [_Issue(file_path="b.py", severity="high", description="missing null check")], 0
    )
    issue = _open_issue(
        1,
        "[high] missing null check",
        body="- **Location:** `a.py`\n\n### Description\nmissing null check",
    )
    assert find_matching_open_issue(proposal, [issue]) is None


def test_find_matching_open_issue_generic_headline_matches_when_issue_is_silent_on_location() -> (
    None
):
    # Control for the regression above: an issue that names NO location at
    # all (silent, not conflicting) must still match via the text-alone
    # signal as before -- the fix only suppresses an explicit mismatch.
    proposal = proposal_from_findings(
        [_Issue(file_path="b.py", description="off-by-one error in loop bound")], 0
    )
    issue = _open_issue(1, "off-by-one error in loop bound", body="")
    assert find_matching_open_issue(proposal, [issue]) is issue


def test_annotate_duplicate_proposals_marks_matched_and_preserves_order_and_length() -> None:
    proposals = [
        proposal_from_findings(
            [_Issue(file_path="", description="off-by-one error in loop bound")], 0
        ),
        proposal_from_findings([_Issue(file_path="", description="unrelated latent bug")], 1),
        proposal_from_findings([_Issue(file_path="", description="another unrelated bug")], 2),
    ]
    match = _open_issue(9, "off-by-one error in loop bound", body="")
    out = annotate_duplicate_proposals(proposals, [match])
    assert len(out) == 3
    assert [p["id"] for p in out] == ["p0", "p1", "p2"]
    assert out[0]["matched_existing"] is True
    assert out[0]["issue_number"] == 9
    assert out[0]["issue_url"] == "https://x/issues/9"
    assert out[1]["matched_existing"] is False
    assert out[1]["issue_url"] is None
    assert out[2]["matched_existing"] is False
    assert out[2]["issue_url"] is None


def test_annotate_duplicate_proposals_does_not_mutate_input() -> None:
    proposals = [
        proposal_from_findings(
            [_Issue(file_path="", description="off-by-one error in loop bound")], 0
        )
    ]
    match = _open_issue(9, "off-by-one error in loop bound", body="")
    annotate_duplicate_proposals(proposals, [match])
    assert proposals[0]["issue_url"] is None
    assert "matched_existing" not in proposals[0]


def test_annotate_duplicate_proposals_empty_open_issues_all_unmatched() -> None:
    proposals = [proposal_from_findings([_Issue(file_path="", description="off-by-one error")], 0)]
    out = annotate_duplicate_proposals(proposals, [])
    assert out[0]["matched_existing"] is False
    assert out[0]["issue_url"] is None


# ---------------------------------------------------------------------------
# duplicate_check_max_open_issues
# ---------------------------------------------------------------------------


def test_duplicate_check_max_open_issues_default() -> None:
    assert duplicate_check_max_open_issues() == 100


def test_duplicate_check_max_open_issues_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_MAX_OPEN_ISSUES", "5")
    assert duplicate_check_max_open_issues() == 5


def test_duplicate_check_max_open_issues_garbage_or_non_positive_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_MAX_OPEN_ISSUES", "not-an-int")
    assert duplicate_check_max_open_issues() == 100
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_MAX_OPEN_ISSUES", "0")
    assert duplicate_check_max_open_issues() == 100
    monkeypatch.setenv("PR_REVIEW_DUPLICATE_MAX_OPEN_ISSUES", "-5")
    assert duplicate_check_max_open_issues() == 100


def test_build_issue_from_proposal_multi_location_body_and_title() -> None:
    findings = [
        _Issue(
            severity="medium",
            category="standards",
            file_path="src/a.py",
            line=1,
            description="bare import `os`",
            suggestion="scope the import",
        ),
        _Issue(
            severity="medium",
            category="standards",
            file_path="src/b.py",
            line=5,
            description="bare import `sys`",
            suggestion="scope the import",
        ),
        _Issue(
            severity="medium",
            category="standards",
            file_path="src/c.py",
            line=None,
            description="bare import `re`",
            suggestion="scope the import",
        ),
    ]
    p = proposal_from_findings(findings, 0)
    title, body = build_issue_from_proposal(p, pr_number=1, pr_url="u")
    assert title.endswith("(3 occurrences)")
    assert "### Locations" in body
    assert "- `src/a.py:1` — bare import `os`" in body
    assert "- `src/b.py:5` — bare import `sys`" in body
    assert "- `src/c.py` — bare import `re`" in body
    assert "**Location:**" not in body
    # Identical suggestions across every location dedupe to one bullet.
    assert body.count("scope the import") == 1
    assert "### Suggested fixes" in body
