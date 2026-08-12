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

import types
from typing import Any, Dict, List, Optional, Union, get_args, get_origin

from pydantic import BaseModel, Field, create_model
from pydantic.fields import FieldInfo

from branding_team.models import (
    BrandCheckRequest,
    BrandingMission,
    BrandingMissionFields,
    TeamOutput,
)
from branding_team.shared.job_store import JOB_STATUS_PENDING
from shared.job_contracts import JobListItemBase, JobStatusResponseBase

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


def _unwrap_noneable(annotation: Any) -> Any:
    """Return the non-None arm of ``Optional[T]`` / ``T | None``, else ``annotation``.

    Preconditions:
        - ``annotation`` is a typing annotation object.
    Postconditions:
        - If ``annotation`` is a union of exactly one non-None type and ``None``,
          return that non-None type; otherwise return ``annotation`` unchanged.
    """
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if len(args) == len(non_none) + 1 and len(non_none) == 1:
            return non_none[0]
    return annotation


def _constraint_kwargs(info: FieldInfo) -> dict[str, Any]:
    """Copy validation metadata that must survive optionalization.

    Preconditions:
        - ``info`` is a Pydantic v2 ``FieldInfo``.
    Postconditions:
        - Returned dict contains only constraint keys present on ``info`` with
          non-``None`` values from the supported set below (including
          ``annotated_types`` metadata such as ``MinLen`` / ``MaxLen``).
    """
    out: dict[str, Any] = {}
    for key in (
        "min_length",
        "max_length",
        "ge",
        "le",
        "gt",
        "lt",
        "pattern",
        "description",
        "title",
    ):
        value = getattr(info, key, None)
        if value is not None:
            out[key] = value
    # Pydantic v2 stores many Field(...) constraints on ``metadata`` (e.g. MinLen)
    # rather than as direct FieldInfo attributes.
    for item in info.metadata:
        min_length = getattr(item, "min_length", None)
        if min_length is not None and "min_length" not in out:
            out["min_length"] = min_length
        max_length = getattr(item, "max_length", None)
        if max_length is not None and "max_length" not in out:
            out["max_length"] = max_length
        ge = getattr(item, "ge", None)
        if ge is not None and "ge" not in out:
            out["ge"] = ge
        le = getattr(item, "le", None)
        if le is not None and "le" not in out:
            out["le"] = le
        gt = getattr(item, "gt", None)
        if gt is not None and "gt" not in out:
            out["gt"] = gt
        lt = getattr(item, "lt", None)
        if lt is not None and "lt" not in out:
            out["lt"] = lt
    if info.description is not None and "description" not in out:
        out["description"] = info.description
    if info.title is not None and "title" not in out:
        out["title"] = info.title
    return out


def _optionalize_model(base: type[BaseModel], *, name: str) -> type[BaseModel]:
    """Build an all-Optional twin of ``base`` with defaults forced to ``None``.

    Preconditions:
        - ``base`` is a Pydantic ``BaseModel`` subclass with a non-empty
          ``model_fields`` mapping.
        - ``name`` is a non-empty Python identifier string.
    Postconditions:
        - Returned model has the same field names as ``base``.
        - Every field is annotated ``Optional[...]`` with default ``None``.
        - Create-path defaults from ``base`` are not copied.
        - Supported Field constraints (e.g. ``min_length``) are preserved.
    """
    assert issubclass(base, BaseModel)
    assert name.isidentifier()
    assert base.model_fields, "base model must declare fields"

    field_definitions: dict[str, Any] = {}
    for field_name, field_info in base.model_fields.items():
        inner = _unwrap_noneable(field_info.annotation)
        field_definitions[field_name] = (
            Optional[inner],
            Field(default=None, **_constraint_kwargs(field_info)),
        )
    return create_model(name, __base__=BaseModel, **field_definitions)


_BrandingMissionFieldsPartial = _optionalize_model(
    BrandingMissionFields, name="_BrandingMissionFieldsPartial"
)


class UpdateBrandRequest(_BrandingMissionFieldsPartial):
    """Partial brand update: optionalized mission fields plus name/status extras.

    Preconditions:
        - Supplied mission string fields must satisfy the same ``min_length``
          constraints as ``BrandingMissionFields`` when not ``None``.
    Postconditions:
        - Omitted fields remain ``None`` so callers can
          ``model_dump(exclude_none=True)`` for selective overwrite.
        - ``name`` and ``status`` are API-only extras (not mission fields).
    """

    name: Optional[str] = Field(None, min_length=1)
    status: Optional[str] = None


class RunBrandRequest(BaseModel):
    human_approved: bool = True
    human_feedback: str = ""
    include_market_research: bool = False
    include_design_assets: bool = False
    brand_checks: List[BrandCheckRequest] = Field(default_factory=list)
    target_phase: Optional[str] = None


class RunBrandingTeamRequest(BrandingMissionFields):
    """Run/session body: shared mission fields plus run-routing extras.

    Preconditions:
        - Same as ``BrandingMissionFields`` for the eight shared mission fields.
    Postconditions:
        - Instance exposes shared mission fields plus the six run extras below;
          omitted extras use the defaults declared here.
    """

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
    degraded: bool = False


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


class RunBrandJobResponse(JobStatusResponseBase):
    status: str = JOB_STATUS_PENDING


class BrandJobStatusResponse(JobStatusResponseBase):
    client_id: Optional[str] = None
    brand_id: Optional[str] = None
    current_phase: Optional[str] = None
    result: Optional[Dict[str, Any]] = None


class BrandJobListItem(JobListItemBase):
    client_id: Optional[str] = None
    brand_id: Optional[str] = None


class BrandJobListResponse(BaseModel):
    jobs: List[BrandJobListItem]
