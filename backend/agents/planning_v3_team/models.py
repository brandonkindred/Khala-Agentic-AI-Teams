"""
Models for the Planning V3 Team.

Request/response for API, phase enum, context, handoff package, and open questions.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Phase(str, Enum):
    """Phases of the Planning V3 workflow."""

    INTAKE = "intake"
    DISCOVERY = "discovery"
    REQUIREMENTS = "requirements"
    SYNTHESIS = "synthesis"
    DOCUMENT_PRODUCTION = "document_production"
    SUB_AGENT_PROVISIONING = "sub_agent_provisioning"


# ---------------------------------------------------------------------------
# Run request / response
# ---------------------------------------------------------------------------


class PlanningV3RunRequest(BaseModel):
    """Request body for POST /planning-v3/run."""

    repo_path: str = Field(
        ...,
        max_length=4096,
        description="Local path where artifacts (context doc, PRD, handoff) will be written.",
    )
    client_name: Optional[str] = Field(
        None,
        description="Client or organization name.",
    )
    initial_brief: Optional[str] = Field(
        None,
        max_length=100_000,
        description="Initial brief, problem statement, or spec from the client.",
    )
    spec_content: Optional[str] = Field(
        None,
        max_length=500_000,
        description="Optional full spec content; if provided, used as starting point.",
    )
    use_product_analysis: bool = Field(
        default=True,
        description="Whether to call Product Requirements Analysis for validated spec and PRD.",
    )
    use_planning_v2: bool = Field(
        default=False,
        description="Whether to call Planning V2 for full project plan after PRA.",
    )
    use_market_research: bool = Field(
        default=False,
        description="Whether to call Market Research for user/customer discovery when needed.",
    )


class PlanningV3RunResponse(BaseModel):
    """Response from POST /planning-v3/run."""

    job_id: str = Field(..., description="Job ID for polling status.")
    status: str = Field(default="running")
    message: str = Field(
        default="Planning V3 started. Poll GET /planning-v3/status/{job_id} for progress."
    )


# ---------------------------------------------------------------------------
# Job status and result (API responses)
# ---------------------------------------------------------------------------


class PlanningV3StatusResponse(BaseModel):
    """Response from GET /planning-v3/status/{job_id}."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(..., description="pending, running, completed, failed.")
    repo_path: Optional[str] = Field(None)
    current_phase: Optional[str] = Field(None)
    status_text: Optional[str] = Field(None)
    progress: int = Field(default=0, ge=0, le=100)
    pending_questions: List[Dict[str, Any]] = Field(default_factory=list)
    waiting_for_answers: bool = Field(default=False)
    error: Optional[str] = Field(None)
    summary: Optional[str] = Field(None)


class PlanningV3ResultResponse(BaseModel):
    """Response from GET /planning-v3/result/{job_id}. Final handoff and artifacts."""

    job_id: str = Field(..., description="Job ID.")
    success: bool = Field(default=False)
    handoff_package: Optional[Dict[str, Any]] = Field(
        None,
        description="Client context, validated spec, PRD, optional Planning V2 plan.",
    )
    client_context_document_path: Optional[str] = Field(None)
    validated_spec_path: Optional[str] = Field(None)
    prd_path: Optional[str] = Field(None)
    summary: Optional[str] = Field(None)
    failure_reason: Optional[str] = Field(None)


# ---------------------------------------------------------------------------
# Context and handoff (internal + API result)
# ---------------------------------------------------------------------------


class ClientContext(BaseModel):
    """Client and problem context gathered during intake and discovery."""

    client_name: Optional[str] = Field(None)
    client_domain: Optional[str] = Field(None)
    problem_summary: Optional[str] = Field(None)
    opportunity_statement: Optional[str] = Field(None)
    target_users: List[str] = Field(default_factory=list)
    success_criteria: List[str] = Field(default_factory=list)
    constraints: Dict[str, Any] = Field(default_factory=dict)
    rpo_rto: Optional[str] = Field(None, description="RPO/RTO or disaster-recovery notes.")
    slas: Optional[str] = Field(None)
    compliance_notes: Optional[str] = Field(None)
    tech_constraints: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    existing_artifacts: List[str] = Field(default_factory=list)
    raw_brief: Optional[str] = Field(None)
    raw_spec: Optional[str] = Field(None)


class HandoffPackage(BaseModel):
    """Bundled artifacts for dev, UI, and UX teams."""

    client_context: Optional[ClientContext] = Field(None)
    client_context_document_path: Optional[str] = Field(None)
    validated_spec_path: Optional[str] = Field(None)
    validated_spec_content: Optional[str] = Field(None)
    prd_path: Optional[str] = Field(None)
    prd_content: Optional[str] = Field(None)
    planning_v2_artifact_paths: Dict[str, str] = Field(
        default_factory=dict,
        description="e.g. architecture.md, task_breakdown.md from Planning V2.",
    )
    architecture_overview: Optional[str] = Field(
        None,
        description="Software architecture overview (from Planning V2 or merged architecture step).",
    )
    sub_agent_blueprint: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional blueprint or runnable from AI Systems Team.",
    )
    summary: Optional[str] = Field(None)


# ---------------------------------------------------------------------------
# Open questions and answers (for interactive clarification)
# ---------------------------------------------------------------------------


class OpenQuestionOption(BaseModel):
    """A selectable option for an open question."""

    id: str = Field(..., description="Option identifier.")
    label: str = Field(..., description="Display text.")
    is_default: bool = Field(default=False)


class OpenQuestion(BaseModel):
    """An open question requiring user or stakeholder input."""

    id: str = Field(..., description="Unique question identifier.")
    question_text: str = Field(..., description="The question text.")
    context: Optional[str] = Field(None, description="Why this matters.")
    category: str = Field(default="general")
    priority: str = Field(default="medium")
    options: List[OpenQuestionOption] = Field(default_factory=list)
    allow_multiple: bool = Field(default=False)
    source: str = Field(default="planning_v3")


class AnsweredQuestion(BaseModel):
    """A question that has been answered."""

    question_id: str = Field(...)
    selected_option_id: str = Field(default="")
    selected_option_ids: List[str] = Field(default_factory=list)
    selected_answer: str = Field(default="")
    other_text: str = Field(default="")
