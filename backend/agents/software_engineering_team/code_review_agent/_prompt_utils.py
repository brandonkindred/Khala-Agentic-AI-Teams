"""Shared prompt-rendering helpers for the code-review verification passes.

These small, pure helpers render finding blocks and cap inlined context fields
for the LLM prompts built by the verification / classification passes
(``false_positive_filter``, ``scope_filter``, ``scope_classifier``). They live
here — a common location — rather than in any single pass so that reusing them
across passes does not couple one pass to another pass's private internals.

Invariants:
    - Every helper is pure and never raises.
"""

from __future__ import annotations

import re
from typing import List

from .models import CodeReviewIssue

# Char cap for an inlined task/acceptance-criterion field in a verifier prompt.
_CONTEXT_FIELD_CHARS = 4_000
_CONTEXT_FIELD_TRUNCATION_MARKER = "\n... (truncated)"


def _sanitize_finding_field(text: str) -> str:
    """Collapse whitespace and neutralize prompt-structure metacharacters.

    Finding ``description`` / ``suggestion`` text is untrusted reviewer output.
    Runs of three or more backticks can mimic a CommonMark fence; runs of three
    or more hyphens can mimic the ``--- Finding index i ---`` separators this
    module emits. Breaking those runs with U+200B keeps the text readable while
    preventing structural corruption of the verifier prompt.

    Preconditions:
        - ``text`` is a string (may be empty).

    Postconditions:
        - Returns a single line (all whitespace collapsed to spaces).
        - Contains no run of three or more consecutive backticks or hyphens.
        - Never raises.
    """
    collapsed = " ".join(text.split())

    def _break_runs(match: re.Match[str]) -> str:
        # Explicit U+200B (zero-width space) escape, not an invisible literal, so
        # the intentional run-breaking char is obvious and edit-safe.
        return "\u200b".join(match.group())

    collapsed = re.sub(r"`{3,}", _break_runs, collapsed)
    collapsed = re.sub(r"-{3,}", _break_runs, collapsed)
    return collapsed


def _render_finding_block(i: int, issue: CodeReviewIssue) -> List[str]:
    """Render one indexed finding block (anchor line + metadata) for the prompt.

    Preconditions:
        - ``i`` is an integer finding index.
        - ``issue`` is a ``CodeReviewIssue`` exposing ``file_path``, ``line``,
          ``severity``, ``category``, ``description``, and optional
          ``suggestion``.

    Postconditions:
        - Returns the lines for finding ``i``: an ``--- Finding index i ---``
          anchor the verdict contract refers back to, a severity/category/
          location line, the description, and the suggestion when present.
        - ``description`` and ``suggestion`` are whitespace-normalized and
          sanitized via ``_sanitize_finding_field`` so multi-line or oddly
          spaced text collapses to a single prompt line and backtick / ``---``
          runs cannot corrupt the surrounding prompt structure. The structural
          finding-index anchor is built here and is not passed through the
          sanitizer.
    """
    location = issue.file_path or "(file unknown)"
    if issue.line is not None:
        location = f"{location}:{issue.line}"
    block = [
        f"--- Finding index {i} ---",
        f"severity: {issue.severity} | category: {issue.category} | location: {location}",
        f"description: {_sanitize_finding_field(issue.description)}",
    ]
    if issue.suggestion:
        block.append(f"suggestion: {_sanitize_finding_field(issue.suggestion)}")
    return block


def _truncate_with_marker(text: str, limit: int, marker: str) -> str:
    """Return ``text`` unchanged within ``limit``, else a prefix plus ``marker``.

    The single prefix-truncation primitive shared by the prompt-capping call
    sites (``_cap_context_field`` here, ``scope_classifier._file_excerpt``), so
    the "return the whole string, or a bounded prefix with a truncation marker"
    rule lives in one place with its own contract.

    Preconditions: ``text`` and ``marker`` are strings; ``limit >= 0``.
    Postconditions: returns ``text`` when ``len(text) <= limit``; otherwise the
        first ``limit`` characters followed by ``marker``. Never raises.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + marker


def _cap_context_field(text: str) -> str:
    """Truncate an inlined task/AC field to ``_CONTEXT_FIELD_CHARS``.

    Preconditions: ``text`` is a non-None string (may be empty).
    Postconditions: returns ``text`` unchanged when within the cap; otherwise
        a prefix of length ``_CONTEXT_FIELD_CHARS`` plus
        ``_CONTEXT_FIELD_TRUNCATION_MARKER``. Never raises.
    """
    return _truncate_with_marker(text, _CONTEXT_FIELD_CHARS, _CONTEXT_FIELD_TRUNCATION_MARKER)
