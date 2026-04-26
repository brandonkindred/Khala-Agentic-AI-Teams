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

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

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
    status: str = "proposed"
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
    status: str = "proposed"
    wsjf_score: float | None = None
    rice_score: float | None = None
    estimate_points: float | None = None


class Task(_AuditedRow):
    story_id: str
    title: str
    description: str = ""
    status: str = "todo"
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
    status: str = "open"
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
    status: str = "proposed"


class EpicCreate(BaseModel):
    initiative_id: str
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = ""
    status: str = "proposed"


class StoryCreate(BaseModel):
    epic_id: str
    title: str = Field(..., min_length=1, max_length=200)
    user_story: str = ""
    status: str = "proposed"
    # Strictly positive: zero / negative effort silently inflates WSJF
    # (job_size <= 0 clamps to 1) and RICE (effort <= 0 clamps to 1),
    # which would let bad input bubble misleading priorities into the
    # ranking. Reject at the API boundary instead.
    estimate_points: float | None = Field(default=None, gt=0)


class TaskCreate(BaseModel):
    story_id: str
    title: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    status: str = "todo"
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

    status: str = Field(..., min_length=1, max_length=40)


class ScoreUpdate(BaseModel):
    """PATCH body for setting scores on initiatives/epics/stories."""

    wsjf_score: float | None = None
    rice_score: float | None = None


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
            "story / epic / initiative row. Set to False for what-if scoring."
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
