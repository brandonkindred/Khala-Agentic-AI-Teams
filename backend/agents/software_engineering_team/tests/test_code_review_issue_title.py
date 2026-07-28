"""Tests for ``code_review_agent.models.derive_issue_title``."""

from __future__ import annotations

import pytest
from code_review_agent.models import derive_issue_title


def test_derive_issue_title_blank_description_returns_empty() -> None:
    assert derive_issue_title("") == ""
    assert derive_issue_title("   \n  ") == ""


def test_derive_issue_title_short_description_returned_verbatim() -> None:
    assert derive_issue_title("Missing pagination in UserListComponent") == (
        "Missing pagination in UserListComponent"
    )


def test_derive_issue_title_uses_first_line_only() -> None:
    assert derive_issue_title("Short title\nRest of the description body.") == "Short title"


def test_derive_issue_title_never_exceeds_max_len() -> None:
    # A first line with no word boundary in the first max_len chars must still
    # respect the documented "at most max_len characters TOTAL" postcondition,
    # including the trailing ellipsis.
    description = "a" * 200
    title = derive_issue_title(description, max_len=10)
    assert len(title) <= 10
    assert title.endswith("…")


def test_derive_issue_title_truncates_at_word_boundary_within_budget() -> None:
    description = "word " * 30  # well over the default 80-char budget
    title = derive_issue_title(description.strip())
    assert len(title) <= 80
    assert title.endswith("…")
    assert not title[:-1].endswith(" ")


def test_derive_issue_title_rejects_non_positive_max_len() -> None:
    with pytest.raises(AssertionError):
        derive_issue_title("some description", max_len=0)
    with pytest.raises(AssertionError):
        derive_issue_title("some description", max_len=-5)
