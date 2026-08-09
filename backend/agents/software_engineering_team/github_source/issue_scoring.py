"""LLM Fibonacci-scoring prompt and response-parse contract for GitHub issue grooming.

Prompt/parse contract only: this module never calls an LLM (no ``get_client``,
no provider list, no ``Agent``/``Model``) and never wires into Phase A
orchestration. It gives callers two pure functions:

- ``build_scoring_prompt`` — render a scoring prompt from an issue's
  title/body/labels.
- ``parse_score_response`` — parse a model's raw text response into a
  validated :class:`ScoreBreakdown`, or an explicit, typed
  :class:`ScoreParseFailure` — never a silently coerced best-effort guess.

:data:`FIBONACCI_COMPLEXITY_VALUES` is the canonical Fibonacci scale for
issue-grooming complexity scores, exported so a future heuristic scorer and
Phase A wiring reuse this exact set rather than redefining it. This module
deliberately does not clamp an out-of-set score to the nearest legal value —
a non-Fibonacci score is a parse failure here, not a rounding problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Union

from pydantic import BaseModel, Field, ValidationError, field_validator

from llm_service import LLMJsonParseError, extract_json_from_response

# Canonical Fibonacci complexity scale for issue-grooming scoring. Matches the
# upper bound already used by TaskPlan.complexity_points
# (shared/dev_models/models.py, ge=1/le=13) so a Phase A score and a
# downstream TaskPlan point value stay comparable. Hardcoded rather than
# env-overridable: a future heuristic scorer is expected to import this exact
# set rather than have it drift per-deployment.
FIBONACCI_COMPLEXITY_VALUES: tuple[int, ...] = (1, 2, 3, 5, 8, 13)

_FIBONACCI_COMPLEXITY_SET = frozenset(FIBONACCI_COMPLEXITY_VALUES)

_SCORE_FIELDS = ("conceptual_score", "loc_score", "code_complexity_score", "aggregate_score")
_RATIONALE_FIELDS = (
    "conceptual_rationale",
    "loc_rationale",
    "code_complexity_rationale",
    "aggregate_rationale",
)

# Passed to extract_json_from_response so its last-resort key-matching fallback
# is scoped to this contract's fields, not that function's own unrelated
# default expected-key set.
_EXPECTED_KEYS = frozenset(_SCORE_FIELDS)


class ScoreBreakdown(BaseModel):
    """Validated LLM Fibonacci-scoring output for one GitHub issue.

    Preconditions: constructed only via ``ScoreBreakdown(**data)`` /
        ``model_validate(data)`` from :func:`parse_score_response` — never
        hand-built from unvalidated input elsewhere.
    Postconditions: ``conceptual_score``, ``loc_score``,
        ``code_complexity_score``, and ``aggregate_score`` are each
        guaranteed to be a member of :data:`FIBONACCI_COMPLEXITY_VALUES`
        (pydantic raises ``ValidationError`` otherwise — no clamping to the
        nearest legal value happens here). Each ``*_rationale`` is a
        non-blank, stripped string. ``suggested_labels`` defaults to ``[]``
        when omitted.
    """

    conceptual_score: int
    conceptual_rationale: str
    loc_score: int
    loc_rationale: str
    code_complexity_score: int
    code_complexity_rationale: str
    aggregate_score: int
    aggregate_rationale: str
    suggested_labels: list[str] = Field(default_factory=list)

    @field_validator(*_SCORE_FIELDS)
    @classmethod
    def _validate_fibonacci(cls, v: int) -> int:
        if v not in _FIBONACCI_COMPLEXITY_SET:
            raise ValueError(
                f"score {v} is not in the Fibonacci complexity set {FIBONACCI_COMPLEXITY_VALUES}"
            )
        return v

    @field_validator(*_RATIONALE_FIELDS)
    @classmethod
    def _validate_rationale_nonblank(cls, v: str) -> str:
        text = v.strip()
        if not text:
            raise ValueError("rationale must not be blank")
        return text


class ScoreParseFailureReason(str, Enum):
    """Classifies why :func:`parse_score_response` could not produce a ScoreBreakdown."""

    MALFORMED_JSON = "malformed_json"
    INVALID_SHAPE = "invalid_shape"
    VALIDATION_ERROR = "validation_error"


@dataclass(frozen=True)
class ScoreParseFailure:
    """Explicit parse-failure result for :func:`parse_score_response`.

    Postconditions: ``reason`` classifies the failure; ``detail`` is a
        human-readable explanation (pydantic ``ValidationError`` text,
        ``LLMJsonParseError`` message, or a fixed string for
        ``INVALID_SHAPE``).
    """

    reason: ScoreParseFailureReason
    detail: str


def build_scoring_prompt(title: str, body: str, labels: list[str]) -> str:
    """Build the Fibonacci-scoring prompt for one GitHub issue.

    Preconditions: ``title``, ``body`` are strings (``body`` may be empty —
        GitHub issues may have no body). ``labels`` is a list of strings (may
        be empty).
    Postconditions: returns a non-empty prompt embedding ``title``/``body``/
        ``labels`` verbatim (blank body/labels rendered as a literal
        "(none)" placeholder, never an ambiguous blank section), the exact
        legal values from :data:`FIBONACCI_COMPLEXITY_VALUES`, and a JSON
        response-shape example whose keys exactly match
        :class:`ScoreBreakdown`'s field names, so a well-formed reply
        round-trips through :func:`parse_score_response`. Pure string
        formatting — no I/O, no LLM call.
    """
    allowed = ", ".join(str(v) for v in FIBONACCI_COMPLEXITY_VALUES)
    body_text = body.strip() if body and body.strip() else "(none)"
    label_text = ", ".join(labels) if labels else "(none)"

    return f"""You are scoring a GitHub issue's implementation complexity for engineering-backlog grooming.

Score the issue on four dimensions. Each score MUST be exactly one of these Fibonacci values: {allowed}.
Do not use any other number and do not interpolate/average to a non-Fibonacci value.

Dimensions:
- conceptual: how hard is this to reason about / design (novel domain, unknowns, cross-team coordination)?
- loc: how large is the anticipated code change, bucketed to the nearest Fibonacci value?
- code: how complex is the resulting code itself (algorithmic complexity, edge cases, error handling)?
- aggregate: your overall complexity score for the issue, considering all of the above together.

Issue title:
{title}

Issue body:
{body_text}

Issue labels:
{label_text}

Respond with ONLY a single JSON object (no markdown fences, no commentary) with exactly this shape:
{{
  "conceptual_score": <one of {allowed}>,
  "conceptual_rationale": "<one sentence>",
  "loc_score": <one of {allowed}>,
  "loc_rationale": "<one sentence>",
  "code_complexity_score": <one of {allowed}>,
  "code_complexity_rationale": "<one sentence>",
  "aggregate_score": <one of {allowed}>,
  "aggregate_rationale": "<one sentence>",
  "suggested_labels": ["<optional label>", ...]
}}

"suggested_labels" is optional -- return [] if you have no suggestions.
All four *_score fields are required and each must be one of: {allowed}.
"""


def parse_score_response(raw: str) -> Union[ScoreBreakdown, ScoreParseFailure]:
    """Parse a raw LLM response into a validated ScoreBreakdown, or an explicit failure.

    Preconditions: ``raw`` is a string (the LLM's raw text response; may
        include markdown code fences / stray prose per
        ``extract_json_from_response``'s recovery ladder).
    Postconditions:
        - Unrecoverable JSON -> ``ScoreParseFailure(MALFORMED_JSON, ...)``.
        - Recovered JSON whose top-level value isn't a dict ->
          ``ScoreParseFailure(INVALID_SHAPE, ...)``.
        - A dict missing a required field, with a wrong-typed field, a
          ``*_score`` outside :data:`FIBONACCI_COMPLEXITY_VALUES`, or a blank
          ``*_rationale`` -> ``ScoreParseFailure(VALIDATION_ERROR, ...)``
          carrying pydantic's ``ValidationError`` text verbatim in
          ``.detail``.
        - Otherwise -> a validated :class:`ScoreBreakdown`.
        - Never raises. Never silently coerces an invalid response into a
          "best effort" ScoreBreakdown -- in particular, a non-Fibonacci
          score is REJECTED, not clamped to the nearest legal value.
    """
    try:
        data = extract_json_from_response(raw, expected_keys=_EXPECTED_KEYS)
    except LLMJsonParseError as e:
        return ScoreParseFailure(ScoreParseFailureReason.MALFORMED_JSON, str(e))

    if not isinstance(data, dict):
        return ScoreParseFailure(
            ScoreParseFailureReason.INVALID_SHAPE,
            f"expected a JSON object, got {type(data).__name__}",
        )

    try:
        return ScoreBreakdown.model_validate(data)
    except ValidationError as e:
        return ScoreParseFailure(ScoreParseFailureReason.VALIDATION_ERROR, str(e))
