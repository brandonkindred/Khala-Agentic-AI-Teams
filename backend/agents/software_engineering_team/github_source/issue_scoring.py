"""LLM Fibonacci-scoring prompt and schema contract for GitHub issue grooming.

Prompt/schema contract only: this module never calls an LLM (no ``get_client``,
no provider list, no ``Agent``/``Model``) and never wires into Phase A
orchestration. It gives callers:

- ``build_scoring_prompt`` — a pure function rendering a scoring prompt from
  an issue's title/body/labels.
- ``ScoreBreakdown`` — the Pydantic schema a model response must satisfy.

A future call site that actually invokes the LLM should pass both straight
into the shared client's canonical structured-output entrypoint, e.g.
``generate_structured(build_scoring_prompt(title, body, labels),
schema=ScoreBreakdown, ...)`` — that entrypoint already owns JSON-mode
enforcement, schema validation, and a self-correction retry, so this module
does not duplicate a hand-rolled parse/validate path on top of it.

:data:`FIBONACCI_COMPLEXITY_VALUES` is the canonical Fibonacci scale for
issue-grooming complexity scores, exported so a future heuristic scorer and
Phase A wiring reuse this exact set rather than redefining it. ``ScoreBreakdown``
deliberately does not clamp an out-of-set score to the nearest legal value —
a non-Fibonacci score fails validation, it is not rounded.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

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


class ScoreBreakdown(BaseModel):
    """Validated LLM Fibonacci-scoring output for one GitHub issue.

    Preconditions: constructed only via ``ScoreBreakdown(**data)`` /
        ``model_validate(data)`` from a schema-validated LLM call (e.g.
        ``generate_structured(..., schema=ScoreBreakdown)``) — never
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
        validates against that schema. Pure string formatting — no I/O, no
        LLM call.
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
