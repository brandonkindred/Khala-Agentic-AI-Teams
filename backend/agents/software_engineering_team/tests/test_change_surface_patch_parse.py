"""Single-file patch parse helpers on ``change_surface``."""

from __future__ import annotations

from software_engineering_team.code_review_agent.change_surface import (
    extract_touched_lines,
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
