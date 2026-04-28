"""Pydantic models for the Product Delivery team.

Shape mirrors :mod:`software_engineering_team.shared.models` (Initiative
→ Epic → StoryPlan → TaskPlan) so the SE Tech Lead can later persist its
``PlanningHierarchy`` directly into these tables without translation.
The principal differences are:

* every entity has a stable ``id`` allocated by the store (UUID4 hex);
* every entity carries an ``author`` handle and audit timestamps;
* status is a free-form ``str`` (no Postgres ENUM) so adding a state
  doesn't require a migration — matches the convention in
  ``agent_console_runs.status`` and ``branding`` jobs.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field


def _reject_bool(value: Any) -> Any:
    """``BeforeValidator``: reject JSON booleans before float coercion.

    Pydantic's default ``float`` coercion accepts JSON booleans
    (``true → 1.0``, ``false → 0.0``). For score / estimate fields that
    silently mutates ranking data with non-numeric semantics, so we
    refuse booleans up front. ``isinstance(True, int)`` is True, hence
    the explicit ``bool`` check.
    """
    if isinstance(value, bool):
        raise ValueError("must be a number, not a boolean")
    return value


def _finite_or_none(value: float | None) -> float | None:
    """``AfterValidator``: reject NaN / ±Infinity. Booleans are blocked
    earlier by ``_reject_bool``. Non-finite scores break Starlette's
    JSON encoder downstream and corrupt persisted ranking data.
    """
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError("value must be a finite number (NaN / Infinity not allowed)")
    return value


FiniteScore = Annotated[
    float | None,
    BeforeValidator(_reject_bool),
    AfterValidator(_finite_or_none),
]


def _positive_finite_or_none(value: float | None) -> float | None:
    """``AfterValidator``: like ``_finite_or_none`` but also rejects ≤ 0.

    Used by ``estimate_points``: zero / negative values silently inflate
    WSJF/RICE priority (denominators clamp to 1), and ``Infinity``
    passes ``gt=0`` but is non-finite. ``None`` still means
    "unestimated"; booleans are blocked by ``_reject_bool`` upstream.
    """
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError("estimate_points must be a finite positive number")
    if value <= 0:
        raise ValueError("estimate_points must be > 0")
    return value


PositiveFiniteEstimate = Annotated[
    float | None,
    BeforeValidator(_reject_bool),
    AfterValidator(_positive_finite_or_none),
]


def _strip_and_bound(*, max_len: int) -> Any:
    """Build an ``AfterValidator`` that strips, rejects blank, then
    enforces the max length on the *trimmed* value.

    Codex flagged that putting ``Field(max_length=N)`` *before* the
    ``AfterValidator`` strips makes Pydantic compare the raw length —
    so ``"x" * N + " "`` (N+1 chars, but trims to N) gets rejected at
    the API while the store's stripped value is accepted, drifting
    the two contracts. Doing both checks inside one validator keeps
    them aligned.
    """

    def _validate(value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank or whitespace-only")
        if len(stripped) > max_len:
            raise ValueError(f"must be at most {max_len} chars after trimming; got {len(stripped)}")
        return stripped

    return AfterValidator(_validate)


def _reject_blank_str(value: str) -> str:
    """``AfterValidator``: reject whitespace-only strings AND
    normalise leading/trailing whitespace.

    Used by fields that don't have a max-length cap (e.g. acceptance
    criterion ``text``). Bounded fields use ``_strip_and_bound`` instead
    so Pydantic can't reject a value at the API that the store would
    accept after trimming.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank or whitespace-only")
    return stripped


# Status string bound shared by every create payload + StatusUpdate so
# the create and patch contracts can't drift apart (Codex caught a case
# where Create accepted unbounded strings while StatusUpdate enforced
# 1..40 chars). The ``min_length=1`` on Field rejects empty strings
# fast (no surprise traceback for the AfterValidator); the actual
# whitespace + max-length checks run inside ``_strip_and_bound`` AFTER
# stripping so accidental trailing spaces don't trip the boundary
# while leaving the equivalent trimmed value valid in the store.
StatusStr = Annotated[
    str,
    Field(min_length=1),
    _strip_and_bound(max_len=40),
]

# Title/name string bound — matches the store's ``_validate_title``
# helper so blank/whitespace and oversized titles fail validation at
# the API boundary (422), not at the store (500). Used by every
# ``*Create.title`` field plus ``ProductCreate.name``. Same length-
# after-strip ordering as ``StatusStr`` so trailing-whitespace inputs
# don't drift between the API and the store.
TitleStr = Annotated[
    str,
    Field(min_length=1),
    _strip_and_bound(max_len=200),
]

# ---------------------------------------------------------------------------
# Backlog entities
# ---------------------------------------------------------------------------


class _AuditedRow(BaseModel):
    """Common fields written by the store for every backlog row."""

    id: str
    author: str
    created_at: datetime
    updated_at: datetime


class Product(_AuditedRow):
    name: str
    description: str = ""
    vision: str = ""


class _ScoredRow(_AuditedRow):
    title: str
    summary: str = ""
    status: StatusStr = "proposed"
    wsjf_score: float | None = None
    rice_score: float | None = None


class Initiative(_ScoredRow):
    product_id: str


class Epic(_ScoredRow):
    initiative_id: str


class Story(_AuditedRow):
    epic_id: str
    title: str
    user_story: str = ""
    status: StatusStr = "proposed"
    wsjf_score: float | None = None
    rice_score: float | None = None
    estimate_points: float | None = None


class Task(_AuditedRow):
    story_id: str
    title: str
    description: str = ""
    status: StatusStr = "todo"
    owner: str | None = None


class AcceptanceCriterion(_AuditedRow):
    story_id: str
    text: str
    satisfied: bool = False


class FeedbackItem(_AuditedRow):
    product_id: str
    source: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    severity: str = "normal"
    status: StatusStr = "open"
    linked_story_id: str | None = None


# ---------------------------------------------------------------------------
# Create / update payloads
# ---------------------------------------------------------------------------


class ProductCreate(BaseModel):
    name: TitleStr
    description: str = ""
    vision: str = ""


class InitiativeCreate(BaseModel):
    product_id: str
    title: TitleStr
    summary: str = ""
    status: StatusStr = "proposed"


class EpicCreate(BaseModel):
    initiative_id: str
    title: TitleStr
    summary: str = ""
    status: StatusStr = "proposed"


class StoryCreate(BaseModel):
    epic_id: str
    title: TitleStr
    user_story: str = ""
    status: StatusStr = "proposed"
    # Strictly positive AND finite: zero / negative effort silently
    # inflates WSJF (job_size <= 0 clamps to 1) and RICE (effort <= 0
    # clamps to 1), and Infinity would pass `gt=0` but propagate as
    # non-finite into scoring fallbacks. Reject at the API boundary.
    estimate_points: PositiveFiniteEstimate = None


class TaskCreate(BaseModel):
    story_id: str
    title: TitleStr
    description: str = ""
    status: StatusStr = "todo"
    owner: str | None = None


class AcceptanceCriterionCreate(BaseModel):
    story_id: str
    # Wrapped in `_reject_blank_str` so whitespace-only text (`'   '`,
    # `'\t'`) doesn't slip past `min_length=1` and persist a
    # semantically empty acceptance criterion that breaks the
    # satisfied/total ratio operators read from the backlog tree.
    text: Annotated[str, Field(min_length=1), AfterValidator(_reject_blank_str)]
    satisfied: bool = False


class FeedbackItemCreate(BaseModel):
    product_id: str
    # `source` is used for triage and reporting (e.g. "support",
    # "sales-call", "bug-tracker"). A blank value would break source
    # filtering and degrade the provenance trail, so reject up front.
    source: Annotated[str, Field(min_length=1), _strip_and_bound(max_len=120)]
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    severity: str = "normal"
    linked_story_id: str | None = None


class StatusUpdate(BaseModel):
    """PATCH body for status transitions on any backlog entity."""

    status: StatusStr


class ScoreUpdate(BaseModel):
    """PATCH body for setting scores on initiatives/epics/stories."""

    wsjf_score: FiniteScore = None
    rice_score: FiniteScore = None


# ---------------------------------------------------------------------------
# Read projections
# ---------------------------------------------------------------------------


class StoryNode(Story):
    """Story with nested tasks + acceptance criteria for the tree view."""

    tasks: list[Task] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)


class EpicNode(Epic):
    stories: list[StoryNode] = Field(default_factory=list)


class InitiativeNode(Initiative):
    epics: list[EpicNode] = Field(default_factory=list)


class BacklogTree(BaseModel):
    """Full nested backlog projection used by ``GET /products/{id}/backlog``."""

    product: Product
    initiatives: list[InitiativeNode] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Grooming
# ---------------------------------------------------------------------------


GroomMethod = Literal["wsjf", "rice"]


class GroomRequest(BaseModel):
    product_id: str
    method: GroomMethod = "wsjf"
    persist: bool = Field(
        default=True,
        description=(
            "When True (default), persist the computed scores back onto each "
            "scored **story** row. Epic and initiative rows are not updated by "
            "grooming today — those scores are set explicitly via "
            "PATCH /{kind}/{id}/scores. Set to False for what-if scoring."
        ),
    )


class RankedBacklogItem(BaseModel):
    kind: Literal["initiative", "epic", "story"]
    id: str
    title: str
    score: float
    wsjf_score: float | None = None
    rice_score: float | None = None
    rationale: str = ""


class GroomResult(BaseModel):
    product_id: str
    method: GroomMethod
    ranked: list[RankedBacklogItem] = Field(default_factory=list)
    rationale: str = ""


# ---------------------------------------------------------------------------
# Sprints + releases (Phase 2 of #243)
# ---------------------------------------------------------------------------


def _non_negative_finite(value: float) -> float:
    """``AfterValidator``: reject NaN / ±Infinity / negatives.

    ``capacity_points`` is a float so half-points are expressible
    (Fibonacci-3 ≈ 3.0, but a half-point spike can land at 0.5), but
    NaN / ±Infinity break ``select_sprint_scope`` (every comparison
    against NaN is False, so the greedy fit accepts everything; +Inf
    accepts everything; −Inf accepts nothing) and corrupt the on-wire
    JSON. Negatives don't make physical sense and would make the
    "remaining_capacity" arithmetic in ``SprintPlanResult`` go below
    zero on the first allocation. Reject all three at the boundary.
    """
    if not math.isfinite(value):
        raise ValueError("capacity_points must be a finite number (NaN / Infinity not allowed)")
    if value < 0:
        raise ValueError("capacity_points must be >= 0")
    return value


CapacityPoints = Annotated[
    float,
    BeforeValidator(_reject_bool),
    AfterValidator(_non_negative_finite),
]


class Sprint(_AuditedRow):
    product_id: str
    name: str
    capacity_points: float = 0.0
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    status: StatusStr = "planned"


class CreateSprintRequest(BaseModel):
    product_id: str
    name: TitleStr
    capacity_points: CapacityPoints = 0.0
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    status: StatusStr = "planned"


class SprintWithStories(BaseModel):
    """Sprint header + the planned stories, ordered by WSJF then created_at."""

    sprint: Sprint
    stories: list[Story] = Field(default_factory=list)


class Release(_AuditedRow):
    sprint_id: str
    version: str
    notes_path: str | None = None
    shipped_at: datetime | None = None


class CreateReleaseRequest(BaseModel):
    """Phase 2 ships only the model + table; routes land with #371."""

    sprint_id: str
    version: Annotated[str, Field(min_length=1), _strip_and_bound(max_len=80)]
    notes_path: str | None = None
    shipped_at: datetime | None = None


class SprintPlanRequest(BaseModel):
    """Body for ``POST /sprints/{id}/plan``.

    ``capacity_points`` overrides the sprint row's stored capacity for
    this run when supplied (useful for ad-hoc what-if planning). When
    ``None`` the agent reads ``capacity_points`` from the sprint row.
    """

    capacity_points: CapacityPoints | None = None


class SprintPlanResult(BaseModel):
    """Output of ``SprintPlannerAgent.plan`` and ``select_sprint_scope``."""

    sprint_id: str
    selected_story_ids: list[str] = Field(default_factory=list)
    skipped_story_ids: list[str] = Field(default_factory=list)
    used_capacity: float = 0.0
    remaining_capacity: float = 0.0
    rationale: str = ""
