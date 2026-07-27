"""
Shared word-count helper for the blogging agent suite.

Multiple agents (copy editor length feedback, writer staccato-prose checks)
independently computed ``len(text.split())`` inline. This module gives that
one naive-but-established convention a single, documented home so the
heuristic and its limitations are stated once instead of re-derived at each
call site.
"""

from __future__ import annotations


def count_words(text: str) -> int:
    """
    Count whitespace-separated tokens in ``text``.

    Preconditions:
        - ``text`` is a ``str`` (may be empty).
    Postconditions:
        - Returns ``len(text.split())``: the number of maximal runs of
          non-whitespace characters in ``text``.

    Limitations:
        This is a naive whitespace-token count, not a linguistic word count.
        It undercounts hyphenated compounds and contractions as a single
        token (``"well-known"`` -> 1, ``"don't"`` -> 1) and cannot segment
        text with no whitespace word boundaries (e.g. CJK text) into
        individual words. It is intentionally kept as-is to match this
        suite's established length-band conventions (target/soft-min/
        soft-max word counts); callers needing a linguistically accurate
        count must use a different tool.
    """
    return len(text.split())
