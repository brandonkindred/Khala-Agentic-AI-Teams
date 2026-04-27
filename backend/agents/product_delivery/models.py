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

from pydantic import AfterValidator, BaseModel, Field


def _finite_or_none(value: float | None) -> float | None:
    """Reject NaN / ±Infinity. Pydantic happily coerces ``"NaN"`` / ``"Infinity"``
    on plain ``float`` fields; non-finite scores break Starlette's JSON
    encoder downstream and corrupt persisted ranking data, so we refuse
    them at the boundary.
    """
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError("value must be a finite number (NaN / Infinity not allowed)")
    return value


FiniteScore = Annotated[float | None, AfterValidator(_finite_or_none)]


def _positive_finite_or_none(value: float | None) -> float | None:
    """Like ``_finite_or_none`` but also rejects values ≤ 0.

    Used by ``estimate_points``: zero / negative values silently inflate
    WSJF/RICE priority (denominators clamp to 1), and ``Infinity``
    passes ``gt=0`` but is non-finite. ``None`` still means "unestimated".
    """
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError("estimate_points must be a finite positive number")
    if value <= 0:
        raise ValueError("estimate_points must be > 0")
    return value


PositiveFiniteEstimate = Annotated[float | None, AfterValidator(_positive_finite_or_none)]


# Status string bounds shared by every create payload + StatusUpdate so
# the create and patch contracts can't drift apart (Codex caught a case
# where Create accepted unbounded strings while StatusUpdate enforced
# 1..40 chars).
StatusStr = Annotated[str, Field(min_length=1, max_length=40)]

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
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    vision: str = ""


class InitiativeCreate(BaseModel):
    product_id: str
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = ""
    status: StatusStr = "proposed"


class EpicCreate(BaseModel):
    initiative_id: str
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = ""
    status: StatusStr = "proposed"


class StoryCreate(BaseModel):
    epic_id: str
    title: str = Field(..., min_length=1, max_length=200)
    user_story: str = ""
    status: StatusStr = "proposed"
    # Strictly positive AND finite: zero / negative effort silently
    # inflates WSJF (job_size <= 0 clamps to 1) and RICE (effort <= 0
    # clamps to 1), and Infinity would pass `gt=0` but propagate as
    # non-finite into scoring fallbacks. Reject at the API boundary.
    estimate_points: PositiveFiniteEstimate = None


class TaskCreate(BaseModel):
    story_id: str
    title: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    status: StatusStr = "todo"
    owner: str | None = None


class AcceptanceCriterionCreate(BaseModel):
    story_id: str
    text: str = Field(..., min_length=1)
    satisfied: bool = False


class FeedbackItemCreate(BaseModel):
    product_id: str
    source: str = Field(..., min_length=1, max_length=120)
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
