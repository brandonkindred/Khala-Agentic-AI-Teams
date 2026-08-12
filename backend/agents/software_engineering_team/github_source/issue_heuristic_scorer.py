"""Heuristic Fibonacci scorer for GitHub issue grooming.

Pure, deterministic, no LLM/network/I-O: derives a :class:`ScoreBreakdown`
from an issue's title/body/labels using text-length and keyword signals only.
This is the always-available scoring path — it is what ``heuristic_only``
mode uses exclusively, and what ``auto`` mode falls back to when the LLM path
is unavailable or fails (see ``issue_scorer.score_issue``).
"""

from __future__ import annotations

from .issue_scoring import FIBONACCI_COMPLEXITY_VALUES, ScoreBreakdown

# Body-length buckets (stripped char count) mapped directly to a Fibonacci
# value. Deliberately simple thresholds -- this heuristic is a fallback, not
# a tuned estimator.
_LOC_LENGTH_BUCKETS: tuple[tuple[int, int], ...] = (
    (200, 1),
    (500, 2),
    (1000, 3),
    (2500, 5),
    (6000, 8),
)
_LOC_LENGTH_MAX_BUCKET = 13

_CONCEPTUAL_KEYWORDS: tuple[str, ...] = (
    "architecture",
    "migration",
    "redesign",
    "cross-team",
    "breaking change",
    "distributed",
    "epic",
    "unknowns",
)

_CODE_COMPLEXITY_KEYWORDS: tuple[str, ...] = (
    "algorithm",
    "race condition",
    "concurrency",
    "concurrent",
    "state machine",
    "protocol",
    "parser",
    "retry",
    "async",
)


def _nearest_fibonacci(weight: float) -> int:
    """Bucket ``weight`` to the closest member of ``FIBONACCI_COMPLEXITY_VALUES``.

    Preconditions: ``weight`` is a finite non-negative number.
    Postconditions: returns a value in ``FIBONACCI_COMPLEXITY_VALUES``; ties
        resolve to the smaller candidate (stable, deterministic).
    """
    return min(FIBONACCI_COMPLEXITY_VALUES, key=lambda v: (abs(v - weight), v))


def _bucket_body_length(length: int) -> int:
    """Map a stripped body character count to a Fibonacci LOC bucket.

    Preconditions: ``length >= 0``.
    Postconditions: returns a value in ``FIBONACCI_COMPLEXITY_VALUES``,
        non-decreasing in ``length``.
    """
    for threshold, bucket in _LOC_LENGTH_BUCKETS:
        if length < threshold:
            return bucket
    return _LOC_LENGTH_MAX_BUCKET


def _count_keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if keyword in text)


def score_issue_heuristically(title: str, body: str, labels: list[str]) -> ScoreBreakdown:
    """Score a GitHub issue's Fibonacci complexity without calling an LLM.

    Preconditions: ``title``/``body`` are strings (``body`` may be empty);
        ``labels`` is a list of strings (may be empty).
    Postconditions: returns a ``ScoreBreakdown`` whose four ``*_score``
        fields are each a member of ``FIBONACCI_COMPLEXITY_VALUES`` and whose
        ``*_rationale`` fields are non-blank, deterministic strings describing
        the signal that produced that score. Calling this twice with the same
        arguments returns the same scores. Never performs I/O.
    """
    body_text = body.strip() if body else ""
    haystack = f"{title}\n{body_text}".lower()

    body_length = len(body_text)
    loc_score = _bucket_body_length(body_length)

    conceptual_hits = _count_keyword_hits(haystack, _CONCEPTUAL_KEYWORDS)
    conceptual_score = _nearest_fibonacci(1 + conceptual_hits * 2)

    code_hits = _count_keyword_hits(haystack, _CODE_COMPLEXITY_KEYWORDS)
    label_bump = 1 if len(labels) > 3 else 0
    code_complexity_score = _nearest_fibonacci(1 + code_hits * 2 + label_bump)

    aggregate_score = _nearest_fibonacci((conceptual_score + loc_score + code_complexity_score) / 3)

    return ScoreBreakdown(
        conceptual_score=conceptual_score,
        conceptual_rationale=(
            f"Heuristic: {conceptual_hits} conceptual-complexity keyword(s) found in title/body."
        ),
        loc_score=loc_score,
        loc_rationale=f"Heuristic: body length {body_length} chars.",
        code_complexity_score=code_complexity_score,
        code_complexity_rationale=(
            f"Heuristic: {code_hits} code-complexity keyword(s) found; {len(labels)} label(s) applied."
        ),
        aggregate_score=aggregate_score,
        aggregate_rationale=(
            "Heuristic: nearest-Fibonacci mean of conceptual, loc, and code-complexity scores."
        ),
        suggested_labels=[],
    )


__all__ = ["score_issue_heuristically"]
