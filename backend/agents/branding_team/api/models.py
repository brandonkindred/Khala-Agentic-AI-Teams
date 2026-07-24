"""Pydantic request/response schemas for the branding team API.

These are the API-layer DTOs (request bodies + response envelopes) — distinct
from the domain models in ``branding_team.models``, which describe the branding
pipeline's inputs and outputs. ``BrandingQuestion`` and ``BrandingSession``
straddle both roles: they are returned to API clients *and* persisted verbatim by
the interactive-review session store (``api.state.BrandingSessionStore``).

This module imports nothing from ``api.routes``/``api.background``/``api.main``,
so it never participates in an import cycle.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from branding_team.models import (
    BrandCheckRequest,
    BrandingMission,
    BrandingMissionFields,
    TeamOutput,
)
from branding_team.shared.job_store import JOB_STATUS_PENDING

# ---------------------------------------------------------------------------
# Client / brand request models
# ---------------------------------------------------------------------------


class CreateClientRequest(BaseModel):
    name: str = Field(..., min_length=1)
    contact_info: Optional[str] = None
    notes: Optional[str] = None


class CreateBrandRequest(BrandingMissionFields):
    """Create-brand API body: shared mission fields plus brand/conversation extras.

    Preconditions:
        - Same as ``BrandingMissionFields`` for the eight shared mission fields.
    Postconditions:
        - Instance exposes shared mission fields plus optional ``name`` and
          ``conversation_id``; omitted extras default to ``None``.
    """

    name: Optional[str] = None
    conversation_id: Optional[str] = None


class UpdateBrandRequest(BaseModel):
    company_name: Optional[str] = Field(None, min_length=2)
    company_description: Optional[str] = Field(None, min_length=10)
    target_audience: Optional[str] = Field(None, min_length=3)
    name: Optional[str] = Field(None, min_length=1)
    values: Optional[List[str]] = None
    differentiators: Optional[List[str]] = None
    desired_voice: Optional[str] = None
    existing_brand_material: Optional[List[str]] = None
    wiki_path: Optional[str] = None
    status: Optional[str] = None


class RunBrandRequest(BaseModel):
    human_approved: bool = True
    human_feedback: str = ""
    include_market_research: bool = False
    include_design_assets: bool = False
    brand_checks: List[BrandCheckRequest] = Field(default_factory=list)
    target_phase: Optional[str] = None


class RunBrandingTeamRequest(BaseModel):
    company_name: str = Field(..., min_length=2)
    company_description: str = Field(..., min_length=10)
    target_audience: str = Field(..., min_length=3)
    values: List[str] = Field(default_factory=list)
    differentiators: List[str] = Field(default_factory=list)
    desired_voice: str = Field(default="clear, confident, human")
    existing_brand_material: List[str] = Field(default_factory=list)
    wiki_path: Optional[str] = None
    brand_checks: List[BrandCheckRequest] = Field(default_factory=list)
    human_approved: bool = False
    human_feedback: str = ""
    client_id: Optional[str] = None
    brand_id: Optional[str] = None
    target_phase: Optional[str] = None


# ---------------------------------------------------------------------------
# Session (interactive review) models
# ---------------------------------------------------------------------------


class BrandingQuestion(BaseModel):
    id: str
    question: str
    context: str
    target_field: str
    status: str = "open"
    answer: Optional[str] = None


class BrandingSessionResponse(BaseModel):
    session_id: str
    status: str
    current_phase: str = "strategic_core"
    mission: BrandingMission
    latest_output: TeamOutput
    open_questions: List[BrandingQuestion] = Field(default_factory=list)
    answered_questions: List[BrandingQuestion] = Field(default_factory=list)


class AnswerBrandingQuestionRequest(BaseModel):
    answer: str = Field(..., min_length=1)


class BrandingSession(BaseModel):
    """Interactive-review session state.

    A Pydantic model so persistence is just ``model_dump(mode="json")`` /
    ``model_validate`` — no hand-rolled field-by-field (de)serialisation.
    """

    mission: BrandingMission
    questions: List[BrandingQuestion]
    latest_output: TeamOutput


# ---------------------------------------------------------------------------
# Conversation (chat) models
# ---------------------------------------------------------------------------


class CreateConversationRequest(BaseModel):
    initial_message: Optional[str] = None
    brand_id: Optional[str] = None
    skip_save: bool = False


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1)
    skip_save: bool = False


class ConversationMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str
    timestamp: str = ""


class ConversationStateResponse(BaseModel):
    conversation_id: str
    brand_id: Optional[str] = None
    messages: List[ConversationMessage] = Field(default_factory=list)
    mission: BrandingMission
    latest_output: Optional[TeamOutput] = None
    suggested_questions: List[str] = Field(default_factory=list)


class ConversationSummaryResponse(BaseModel):
    conversation_id: str
    brand_id: Optional[str] = None
    brand_name: Optional[str] = None
    created_at: str
    updated_at: str
    message_count: int


class AttachConversationBrandRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Brand run / job models
# ---------------------------------------------------------------------------


class RunBrandJobResponse(BaseModel):
    job_id: str
    status: str = JOB_STATUS_PENDING


class BrandJobStatusResponse(BaseModel):
    job_id: str
    status: str
    client_id: Optional[str] = None
    brand_id: Optional[str] = None
    current_phase: Optional[str] = None
    progress: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BrandJobListItem(BaseModel):
    job_id: str
    status: str
    client_id: Optional[str] = None
    brand_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BrandJobListResponse(BaseModel):
    jobs: List[BrandJobListItem]
