"""Tests for the shared count_words() helper."""

from __future__ import annotations

from agents.blogging.shared import count_words


def test_counts_plain_whitespace_separated_words() -> None:
    assert count_words("the quick brown fox") == 4


def test_empty_string_is_zero_words() -> None:
    assert count_words("") == 0


def test_whitespace_only_string_is_zero_words() -> None:
    assert count_words("   \n\t  ") == 0


def test_hyphenated_compound_counts_as_one_token() -> None:
    """Documents the naive-heuristic limitation: no sub-token splitting on hyphens."""
    assert count_words("a well-known fact") == 3


def test_contraction_counts_as_one_token() -> None:
    """Documents the naive-heuristic limitation: contractions aren't split."""
    assert count_words("don't stop") == 2
