"""Single-file patch parse helpers on ``change_surface``."""

from __future__ import annotations

from software_engineering_team.code_review_agent.change_surface import (
    extract_touched_lines,
    render_patch_hunks,
)
from software_engineering_team.github_source.pr_review_mapping import (
    render_annotated_hunks,
)

# Realistic single-file hunk: context, removed, added.
_SINGLE_FILE_PATCH = (
    "@@ -1,3 +1,3 @@\n"
    " keep\n"
    "-deleted\n"
    "+added\n"
    " trail\n"
)


def test_extract_touched_lines_added_only_excludes_context_and_removed() -> None:
    # New-file coords: line1 keep (ctx), line2 added (+), line3 trail (ctx).
    # Removed '-' does not advance new-file line numbers.
    assert extract_touched_lines(_SINGLE_FILE_PATCH) == frozenset({2})


def test_extract_touched_lines_empty_patch() -> None:
    assert extract_touched_lines("") == frozenset()
    assert extract_touched_lines("   \n") == frozenset()


def test_render_patch_hunks_matches_annotated_helper() -> None:
    assert render_patch_hunks(_SINGLE_FILE_PATCH) == render_annotated_hunks(
        _SINGLE_FILE_PATCH
    )


def test_render_patch_hunks_empty_patch() -> None:
    assert render_patch_hunks("") == ""
    assert render_patch_hunks("   \n") == render_annotated_hunks("   \n")


def test_touched_set_diverges_from_annotated_context_lines() -> None:
    """Lock the added-only vs annotated(+context) split on one patch."""
    touched = extract_touched_lines(_SINGLE_FILE_PATCH)
    annotated = render_patch_hunks(_SINGLE_FILE_PATCH)
    assert touched == frozenset({2})
    # Annotated includes context lines 1 and 3 as ``N: ...`` prefixes.
    assert "1:" in annotated
    assert "2:" in annotated
    assert "3:" in annotated
