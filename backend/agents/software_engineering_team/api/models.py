"""Pydantic request/response models for the SE team API.

Pure data schemas shared by the route modules; no runtime logic, no I/O.

Invariants:
    - Import-side-effect free beyond class definition.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# The HITL "pending question / answer" schemas live in the shared.hitl package so every
# team shares one reconciled definition; re-exported here so existing importers keep using
# `software_engineering_team.api.models`. SE's superset fields (recommendation/allow_multiple
# on PendingQuestion, rationale/confidence on QuestionOption) are the ones the shared models
# adopt, so these are field-identical to the previous local definitions.
from shared.hitl.models import (  # noqa: F401
    AnswerSubmission,
    PendingQuestion,
    QuestionOption,
    SubmitAnswersRequest,
)


class RunTeamRequest(BaseModel):
    """Request body for the run-team endpoint."""

    repo_path: str = Field(
        ...,
        max_length=4096,
        description="Local filesystem path to the folder where work will be saved. Must contain a spec: at root (initial_spec.md or spec.md) or under plan/ or plan/product_analysis/ (e.g. validated_spec.md, updated_spec_vN.md). Does not need to be a git repository.",
    )
    sprint_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description=(
            "When set (#370), pull planned scope from the product_delivery "
            "sprint's stories instead of parsing a spec from the repo. "
            "Discovery's LLM spec-parse and the PRA agent are skipped."
        ),
    )

    @field_validator("sprint_id")
    @classmethod
    def _normalise_sprint_id(cls, value: Optional[str]) -> Optional[str]:
        # Reject blank / whitespace-only ids at the API boundary so a
        # caller can't accidentally enable "sprint mode" with a value
        # that leads to a runtime "unknown sprint" 500 — the right
        # response is a clear 422 (Codex review on PR #396).
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("sprint_id must not be blank or whitespace-only")
        return stripped


class RunTeamResponse(BaseModel):
    """Response from POST /run-team."""

    job_id: str = Field(..., description="Job ID for polling status.")
    status: str = Field(default="running", description="Initial status.")
    message: str = Field(default="Orchestrator started. Poll GET /run-team/{job_id} for status.")


class RunningJobSummary(BaseModel):
    """Summary of a single job for the running jobs list."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(..., description="pending or running.")
    repo_path: Optional[str] = Field(None, description="Path to the repo.")
    job_type: str = Field(
        default="run_team",
        description="run_team or backend_code_v2.",
    )
    created_at: Optional[str] = Field(None, description="ISO timestamp when job was created.")


class RunningJobsResponse(BaseModel):
    """Response from GET /run-team/jobs (list of running/pending jobs)."""

    jobs: List[RunningJobSummary] = Field(
        default_factory=list, description="Running or pending jobs."
    )


class FailedTaskDetail(BaseModel):
    """Detail about a single failed task."""

    task_id: str = Field(..., description="ID of the failed task.")
    title: str = Field(default="", description="Task title.")
    reason: str = Field(default="", description="Why the task failed.")


class TaskStateEntry(BaseModel):
    """Per-task execution state for tracking panel / graph."""

    status: str = Field(..., description="pending, in_progress, done, failed")
    assignee: str = Field(
        ..., description="Team: backend-code-v2, backend, frontend, git_setup, devops"
    )
    title: Optional[str] = Field(None, description="Task title.")
    dependencies: List[str] = Field(
        default_factory=list, description="Task IDs this task depends on."
    )
    started_at: Optional[str] = Field(None, description="ISO timestamp when task started.")
    finished_at: Optional[str] = Field(None, description="ISO timestamp when task finished.")
    error: Optional[str] = Field(None, description="Error message if failed.")
    initiative_id: Optional[str] = Field(
        None, description="Parent initiative ID from planning hierarchy."
    )
    epic_id: Optional[str] = Field(None, description="Parent epic ID from planning hierarchy.")
    story_id: Optional[str] = Field(None, description="Parent story ID from planning hierarchy.")


class TeamProgressEntry(BaseModel):
    """Per-team progress when multiple teams run in parallel."""

    current_phase: Optional[str] = Field(
        None, description="e.g. planning, execution, review (backend-code-v2)."
    )
    progress: Optional[int] = Field(None, description="0-100 completion for this team.")
    current_task_id: Optional[str] = Field(
        None, description="Task ID currently being executed by this team."
    )
    current_microtask: Optional[str] = Field(
        None, description="Title of the currently executing microtask."
    )
    current_microtask_phase: Optional[str] = Field(
        None,
        description=(
            "Current phase of the microtask: coding, code_review, qa_testing, security_testing, "
            "qa_security_testing, documentation, or completed. qa_security_testing means QA and "
            "Security are running concurrently -- neither has a confirmed outcome yet, so it must "
            "not be treated as qa_testing having passed."
        ),
    )
    phase_detail: Optional[str] = Field(
        None,
        description="Human-readable detail about what's happening within the current phase.",
    )
    current_microtask_index: Optional[int] = Field(
        None, description="1-based index of the currently executing microtask."
    )
    microtasks_completed: Optional[int] = Field(None, description="Number of microtasks completed.")
    microtasks_total: Optional[int] = Field(None, description="Total number of microtasks.")


class CurrentActivityEntry(BaseModel):
    """Fine-grained activity of the currently running sub-agent (e.g. code review sub-steps)."""

    agent: Optional[str] = Field(
        None, description="Sub-agent reporting the activity: code_review or tech_lead_review."
    )
    step: Optional[str] = Field(
        None,
        description="Current step: preparing, reviewing, waiting_retry, parsing, finalizing, or done.",
    )
    detail: Optional[str] = Field(
        None, description="Human-readable detail (e.g. 'chunk 2/5: src/app.py' or 'attempt 2/3')."
    )
    fraction: Optional[float] = Field(
        None, description="0.0-1.0 progress through the sub-agent's own process."
    )
    task_id: Optional[str] = Field(None, description="Task the sub-agent is working on.")
    task_title: Optional[str] = Field(None, description="Title of that task.")


class JobStatusResponse(BaseModel):
    """Response from GET /run-team/{job_id}."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(
        ...,
        description="pending, running, completed, failed, paused_llm_limit (Ollama weekly limit; call retry-failed after reset), or paused_llm_connectivity (LLM unreachable; call resume-after-llm-check when connectivity is restored).",
    )
    repo_path: Optional[str] = Field(None, description="Path to the repo.")
    requirements_title: Optional[str] = Field(None, description="Parsed project title.")
    architecture_overview: Optional[str] = Field(None, description="Architecture overview.")
    current_task: Optional[str] = Field(None, description="Current task being executed.")
    status_text: Optional[str] = Field(
        None, description="Human-readable status message describing current activity."
    )
    task_results: list = Field(default_factory=list, description="Completed task results.")
    task_ids: list = Field(default_factory=list, description="Task IDs in execution order.")
    progress: Optional[int] = Field(None, description="Progress percentage.")
    error: Optional[str] = Field(None, description="Error message if failed.")
    failed_tasks: List[FailedTaskDetail] = Field(
        default_factory=list,
        description="Details about tasks that failed, including the reason for failure.",
    )
    phase: Optional[str] = Field(
        None,
        description="Job-level phase: planning, execution, or completed.",
    )
    task_states: Optional[Dict[str, TaskStateEntry]] = Field(
        None,
        description="Per-task state (status, assignee, etc.) for execution tracking graph.",
    )
    team_progress: Optional[Dict[str, TeamProgressEntry]] = Field(
        None,
        description="Per-team progress when multiple teams run in parallel.",
    )
    pending_questions: List[PendingQuestion] = Field(
        default_factory=list,
        description="Questions awaiting user response before job can proceed.",
    )
    waiting_for_answers: bool = Field(
        default=False,
        description="True when job is blocked waiting for user to answer pending questions.",
    )
    planning_subprocess: Optional[str] = Field(
        None,
        description=(
            "Current subprocess within planning phase (intake, discovery, requirements, "
            "synthesis, document_production, sub_agent_provisioning)."
        ),
    )
    planning_completed_phases: List[str] = Field(
        default_factory=list,
        description="Completed subprocesses within the planning phase.",
    )
    analysis_subprocess: Optional[str] = Field(
        None,
        description="Current subprocess within product_analysis phase (spec_review, communicate, spec_update, spec_cleanup).",
    )
    analysis_completed_phases: List[str] = Field(
        default_factory=list,
        description="Completed subprocesses within the product_analysis phase.",
    )
    planning_hierarchy: Optional[Dict[str, Any]] = Field(
        None,
        description="Planning hierarchy with initiatives, epics, stories for work breakdown tree display.",
    )
    current_activity: Optional[CurrentActivityEntry] = Field(
        None,
        description="Fine-grained activity of the currently running sub-agent (e.g. code review sub-steps).",
    )
    last_activity_at: Optional[str] = Field(
        None,
        description="ISO timestamp of the last real orchestrator update (heartbeats excluded); "
        "the UI's stall warning reads this.",
    )
    updated_at: Optional[str] = Field(None, description="ISO timestamp of the last job update.")
    last_heartbeat_at: Optional[str] = Field(
        None, description="ISO timestamp of the last heartbeat (liveness of the worker process)."
    )
    server_time: Optional[str] = Field(
        None,
        description="Server UTC time when this response was built; clients should compute "
        "activity staleness against this, not their own clock (skew immunity).",
    )


class RetryResponse(BaseModel):
    """Response from POST /run-team/{job_id}/retry-failed."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(default="running", description="Status after retry start.")
    retrying_tasks: List[str] = Field(default_factory=list, description="Task IDs being retried.")
    message: str = Field(default="")


class ArchitectDesignRequest(BaseModel):
    """Request body for the architect/design endpoint."""

    spec: str = Field(..., description="Product/engineering specification text")
    use_llm: bool = Field(
        default=False,
        description="Use LLM for spec parsing (slower but higher quality); default uses heuristic",
    )


class ArchitectDesignResponse(BaseModel):
    """Response from POST /architect/design."""

    overview: str = Field(..., description="High-level architecture overview")
    architecture_document: str = Field(
        default="", description="Full markdown architecture document"
    )
    components: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Architecture components (name, type, description, technology, etc.)",
    )
    diagrams: Dict[str, str] = Field(
        default_factory=dict,
        description="Mermaid diagram code keyed by diagram name",
    )
    decisions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Architecture decision records",
    )
    tenancy_model: str = Field(default="", description="Tenancy model")
    reliability_model: str = Field(default="", description="Reliability model")
    summary: str = Field(default="", description="Architecture summary")


class CancelJobResponse(BaseModel):
    """Response from POST /run-team/{job_id}/cancel."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(default="cancelled", description="New status after cancellation.")
    message: str = Field(default="Job cancellation requested.")


class DeleteJobResponse(BaseModel):
    """Response from DELETE /run-team/{job_id}."""

    job_id: str = Field(..., description="Job ID that was deleted.")
    message: str = Field(default="Job deleted", description="Human-readable result.")


class BackendCodeV2TaskInput(BaseModel):
    """Task input for backend-code-v2."""

    id: str = Field(default="", description="Task ID (auto-generated if empty)")
    title: str = Field(default="", description="Short task title")
    description: str = Field(default="", description="Detailed description")
    requirements: str = Field(default="", description="Technical requirements")
    acceptance_criteria: List[str] = Field(
        default_factory=list, description="Acceptance criteria list"
    )


class BackendCodeV2RunRequest(BaseModel):
    """Request body for POST /backend-code-v2/run."""

    task: BackendCodeV2TaskInput = Field(..., description="Task to implement")
    repo_path: str = Field(..., description="Local path to the repository")
    architecture: Optional[str] = Field(None, description="Optional architecture overview")


class BackendCodeV2RunResponse(BaseModel):
    """Response from POST /backend-code-v2/run."""

    job_id: str = Field(..., description="Job ID for polling status")
    status: str = Field(default="running")
    message: str = Field(default="")


class BackendCodeV2MicrotaskStatus(BaseModel):
    """Status of a single microtask."""

    id: str = Field(default="")
    title: str = Field(default="")
    status: str = Field(default="pending")


class BackendCodeV2StatusResponse(BaseModel):
    """Response from GET /backend-code-v2/status/{job_id}."""

    job_id: str = Field(...)
    status: str = Field(default="pending", description="pending, running, completed, failed")
    repo_path: Optional[str] = None
    current_phase: Optional[str] = None
    current_microtask: Optional[str] = None
    progress: int = Field(default=0, description="0-100 completion percentage")
    microtasks_completed: int = Field(default=0)
    microtasks_total: int = Field(default=0)
    completed_phases: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    summary: Optional[str] = None
    status_text: Optional[str] = Field(
        None,
        description="Short human-readable status (e.g. what is being worked on right now).",
    )


# ---------------------------------------------------------------------------
# Frontend-Code-V2 endpoints
# ---------------------------------------------------------------------------


class FrontendCodeV2TaskInput(BaseModel):
    """Task input for frontend-code-v2."""

    id: str = Field(default="", description="Task ID (auto-generated if empty)")
    title: str = Field(default="", description="Short task title")
    description: str = Field(default="", description="Detailed description")
    requirements: str = Field(default="", description="Technical requirements")
    acceptance_criteria: List[str] = Field(
        default_factory=list, description="Acceptance criteria list"
    )


class FrontendCodeV2RunRequest(BaseModel):
    """Request body for POST /frontend-code-v2/run."""

    task: FrontendCodeV2TaskInput = Field(..., description="Task to implement")
    repo_path: str = Field(..., description="Local path to the repository")
    architecture: Optional[str] = Field(None, description="Optional architecture overview")


class FrontendCodeV2RunResponse(BaseModel):
    """Response from POST /frontend-code-v2/run."""

    job_id: str = Field(..., description="Job ID for polling status")
    status: str = Field(default="running")
    message: str = Field(default="")


class FrontendCodeV2StatusResponse(BaseModel):
    """Response from GET /frontend-code-v2/status/{job_id}."""

    job_id: str = Field(...)
    status: str = Field(default="pending", description="pending, running, completed, failed")
    repo_path: Optional[str] = None
    current_phase: Optional[str] = None
    current_microtask: Optional[str] = None
    progress: int = Field(default=0, description="0-100 completion percentage")
    microtasks_completed: int = Field(default=0)
    microtasks_total: int = Field(default=0)
    completed_phases: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    summary: Optional[str] = None
    status_text: Optional[str] = Field(
        None,
        description="Short human-readable status (e.g. what is being worked on right now).",
    )


# ---------------------------------------------------------------------------
# Codegen (unified backend/frontend) endpoints
#
# A stepping-stone alongside the existing split /backend-code-v2 and
# /frontend-code-v2 endpoints above (which stay unchanged for existing
# callers): one endpoint, `stack` selects which codegen team stack runs.
# Field-identical to the per-stack request/response models except for the
# added `stack` field.
# ---------------------------------------------------------------------------


class CodegenTaskInput(BaseModel):
    """Task input for /code-v2/run."""

    id: str = Field(default="", description="Task ID (auto-generated if empty)")
    title: str = Field(default="", description="Short task title")
    description: str = Field(default="", description="Detailed description")
    requirements: str = Field(default="", description="Technical requirements")
    acceptance_criteria: List[str] = Field(
        default_factory=list, description="Acceptance criteria list"
    )


class CodegenRunRequest(BaseModel):
    """Request body for POST /code-v2/run."""

    task: CodegenTaskInput = Field(..., description="Task to implement")
    repo_path: str = Field(..., description="Local path to the repository")
    architecture: Optional[str] = Field(None, description="Optional architecture overview")
    stack: Literal["backend", "frontend"] = Field(
        ..., description="Which codegen team stack should run this task"
    )


class CodegenRunResponse(BaseModel):
    """Response from POST /code-v2/run."""

    job_id: str = Field(..., description="Job ID for polling status")
    status: str = Field(default="running")
    message: str = Field(default="")


class CodegenStatusResponse(BaseModel):
    """Response from GET /code-v2/status/{job_id}."""

    job_id: str = Field(...)
    status: str = Field(default="pending", description="pending, running, completed, failed")
    stack: Optional[Literal["backend", "frontend"]] = None
    repo_path: Optional[str] = None
    current_phase: Optional[str] = None
    current_microtask: Optional[str] = None
    progress: int = Field(default=0, description="0-100 completion percentage")
    microtasks_completed: int = Field(default=0)
    microtasks_total: int = Field(default=0)
    completed_phases: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    summary: Optional[str] = None
    status_text: Optional[str] = Field(
        None,
        description="Short human-readable status (e.g. what is being worked on right now).",
    )


class AutoAnswerRequest(BaseModel):
    """Request body for auto-answering a question."""

    spec_context: Optional[str] = Field(
        None,
        description="Additional context to help the LLM make a better choice.",
    )


class AutoAnswerResponse(BaseModel):
    """Response from auto-answering a question."""

    question_id: str = Field(..., description="ID of the question that was answered.")
    selected_option_id: str = Field(..., description="ID of the selected option.")
    selected_answer: str = Field(..., description="Text of the selected answer.")
    rationale: str = Field(..., description="Detailed explanation of why this choice was made.")
    confidence: float = Field(..., description="Confidence score (0.0-1.0) in this answer.")
    risks: List[str] = Field(default_factory=list, description="Potential risks of this choice.")
    applied: bool = Field(
        default=False,
        description="Whether the answer was auto-applied to the job.",
    )


class ProductAnalysisRunRequest(BaseModel):
    """Request body for starting Product Requirements Analysis."""

    repo_path: str = Field(
        ...,
        max_length=4096,
        description="Local filesystem path to the folder. A spec can be at root (initial_spec.md or spec.md) or under plan/ or plan/product_analysis/ (e.g. validated_spec.md, updated_spec_vN.md).",
    )
    spec_content: Optional[str] = Field(
        None,
        max_length=500_000,
        description="Optional spec content. If not provided, the system loads the newest spec file whose name contains '_spec' "
        "(by modification time) from plan/product_analysis/, plan/, or root. Leave empty to use that file. "
        "If the agent needs more detail and the input was validated_spec.md, it is renamed to updated_spec_vN; "
        "subsequent updates use later versions.",
    )


class StartFromSpecRequest(BaseModel):
    """Request body for creating a project from an uploaded spec and starting PRA."""

    project_name: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Project name (no spaces; only letters, numbers, hyphens, underscores).",
    )
    spec_content: str = Field(
        ...,
        min_length=1,
        max_length=500_000,
        description="Full content of the spec file (text or markdown).",
    )


class ProductAnalysisRunResponse(BaseModel):
    """Response from POST /product-analysis/run."""

    job_id: str = Field(..., description="Job ID for polling status.")
    status: str = Field(default="running", description="Initial status.")
    message: str = Field(
        default="Product analysis started. Poll GET /product-analysis/status/{job_id} for progress."
    )


class ProductAnalysisStatusResponse(BaseModel):
    """Response from GET /product-analysis/status/{job_id}."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(..., description="pending, running, completed, or failed.")
    repo_path: Optional[str] = Field(None, description="Path to the repo.")
    current_phase: Optional[str] = Field(
        None, description="spec_review, communicate, spec_update, or spec_cleanup."
    )
    status_text: Optional[str] = Field(
        None, description="Human-readable status message describing current activity."
    )
    progress: int = Field(default=0, description="Progress percentage 0-100.")
    iterations: int = Field(default=0, description="Number of spec review iterations completed.")
    pending_questions: List[PendingQuestion] = Field(
        default_factory=list,
        description="Questions awaiting user response.",
    )
    waiting_for_answers: bool = Field(
        default=False,
        description="True when blocked waiting for user answers.",
    )
    error: Optional[str] = Field(None, description="Error message if failed.")
    summary: Optional[str] = Field(None, description="Summary of analysis results.")
    validated_spec_path: Optional[str] = Field(
        None, description="Path to validated spec file when complete."
    )
