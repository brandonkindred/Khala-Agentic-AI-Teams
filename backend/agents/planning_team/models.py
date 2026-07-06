"""
Models for the Planning Team.

Request/response for API, phase enum, context, handoff package, and open questions.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class Phase(str, Enum):
    """Phases of the Planning workflow."""

    INTAKE = "intake"
    DISCOVERY = "discovery"
    REQUIREMENTS = "requirements"
    SYNTHESIS = "synthesis"
    DOCUMENT_PRODUCTION = "document_production"
    SUB_AGENT_PROVISIONING = "sub_agent_provisioning"


# ---------------------------------------------------------------------------
# Run request / response
# ---------------------------------------------------------------------------


class PlanningRunRequest(BaseModel):
    """Request body for ``POST /planning/run``.

    Fields:
        - ``repo_path``: optional output-folder *label* (see the field
          description); reduced to a sanitized workspace name, never read as
          source.
        - ``client_name``: optional client/organization name.
        - ``initial_brief`` / ``spec_content``: the work to plan; **at least one**
          must be non-blank (enforced by ``_require_brief_or_spec``).
        - ``use_product_analysis`` / ``use_market_research``:
          optional pipeline toggles.

    Invariant:
        - A validated instance always has a non-blank ``initial_brief`` or
          ``spec_content``.
    """

    repo_path: Optional[str] = Field(
        None,
        max_length=4096,
        description=(
            "Optional label for the output workspace, not a literal path. Any value "
            "(filesystem path, git URL, or empty) is reduced to a single sanitized "
            "directory name; the workspace is always created server-side under "
            "AGENT_CACHE/planning/<sanitized-label>/<job_id>, never at the supplied "
            "path. Planning writes artifacts (context doc, PRD, handoff) here and "
            "never reads source code from it."
        ),
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
    use_market_research: bool = Field(
        default=False,
        description="Whether to call Market Research for user/customer discovery when needed.",
    )

    @model_validator(mode="after")
    def _require_brief_or_spec(self) -> "PlanningRunRequest":
        """Require meaningful input to start the workflow.

        Preconditions:
            - Field-level validation has already run.
        Postconditions:
            - Returns ``self`` unchanged when at least one of ``initial_brief``
              or ``spec_content`` is a non-blank string.
            - Raises ``ValueError`` (surfaced by FastAPI as HTTP 422) when both
              are missing/blank, since the workflow would otherwise run on an
              empty placeholder spec. ``repo_path`` is intentionally not part of
              this requirement — it is only an output folder.
        """
        if not (self.initial_brief and self.initial_brief.strip()) and not (
            self.spec_content and self.spec_content.strip()
        ):
            raise ValueError("Provide at least one of initial_brief or spec_content.")
        return self


class PlanningRunResponse(BaseModel):
    """Response from POST /planning/run."""

    job_id: str = Field(..., description="Job ID for polling status.")
    status: str = Field(default="running")
    message: str = Field(
        default="Planning started. Poll GET /planning/status/{job_id} for progress."
    )


# ---------------------------------------------------------------------------
# Job status and result (API responses)
# ---------------------------------------------------------------------------


class PlanningStatusResponse(BaseModel):
    """Response from GET /planning/status/{job_id}."""

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


class PlanningResultResponse(BaseModel):
    """Response from GET /planning/result/{job_id}. Final handoff and artifacts."""

    job_id: str = Field(..., description="Job ID.")
    success: bool = Field(default=False)
    handoff_package: Optional[Dict[str, Any]] = Field(
        None,
        description="Client context, validated spec, PRD, and handoff.",
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
    architecture_overview: Optional[str] = Field(
        None,
        description="Software architecture overview (from the merged architecture step).",
    )
    sub_agent_blueprint: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional blueprint or runnable from AI Systems Team.",
    )
    open_questions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Unanswered product/design questions surfaced during planning. These must be escalated "
            "to the user downstream, never auto-decided. Each entry mirrors OpenQuestion "
            "(id, question_text, options, ...)."
        ),
    )
    resolved_questions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Questions answered by the user during planning, carried across the handoff so the "
            "decisions reach the implementing team. Each entry mirrors AnsweredQuestion."
        ),
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
    source: str = Field(default="planning")


class AnsweredQuestion(BaseModel):
    """A question that has been answered."""

    question_id: str = Field(...)
    selected_option_id: str = Field(default="")
    selected_option_ids: List[str] = Field(default_factory=list)
    selected_answer: str = Field(default="")
    other_text: str = Field(default="")
