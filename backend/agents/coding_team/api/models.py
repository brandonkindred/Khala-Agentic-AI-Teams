"""Pydantic request/response models for the coding_team API.

Pure data schemas shared by the route modules; no runtime logic, no I/O.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from coding_team.models import AgentStatusEntry


class RunRequest(BaseModel):
    """Request body for POST /run."""

    repo_path: str = Field(..., description="Path to the repository")
    plan_input: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional plan from Planning team (CodingTeamPlanInput); if omitted, job is created but orchestrator expects to be run in-process with plan_input.",
    )


class RunResponse(BaseModel):
    job_id: str
    status: str = "pending"
    message: str = "Job started. Poll GET /status/{job_id} for progress."


class QuestionOption(BaseModel):
    """A selectable option for a pending question."""

    id: str = Field(..., description="Unique identifier for this option.")
    label: str = Field(..., description="Display text for this option.")
    is_default: bool = Field(
        default=False, description="Whether this option is the suggested default."
    )


class PendingQuestion(BaseModel):
    """A product/design decision the coding team escalated to the user before it could proceed."""

    id: str = Field(..., description="Unique identifier for this question.")
    question_text: str = Field(..., description="The question to display to the user.")
    context: Optional[str] = Field(None, description="Why this decision matters.")
    options: List[QuestionOption] = Field(
        default_factory=list,
        description="Selectable answer options. The UI always offers an 'other' free-text option.",
    )
    required: bool = Field(default=True, description="Whether this question must be answered.")
    source: str = Field(
        default="coding_team",
        description="Origin of the question: plan_input, tech_lead, engineer:<agent>, etc.",
    )


class AnswerSubmission(BaseModel):
    """A user's answer to a pending question."""

    question_id: str = Field(..., description="ID of the question being answered.")
    selected_option_id: Optional[str] = Field(
        None, description="ID of the selected option, or 'other' if custom text is provided."
    )
    other_text: Optional[str] = Field(None, description="Custom text when 'other' is selected.")


class SubmitAnswersRequest(BaseModel):
    """Request body for submitting answers to a coding-team job's pending questions."""

    answers: List[AnswerSubmission] = Field(..., description="List of answers to submit.")


class StatusResponse(BaseModel):
    job_id: str
    status: str
    phase: Optional[str] = None
    status_text: Optional[str] = None
    thinking: Optional[str] = Field(
        default=None,
        description="Most recent agent reasoning ('thinking') tokens, for live display.",
    )
    repo_path: Optional[str] = None
    task_graph_snapshot: List[Dict[str, Any]] = Field(default_factory=list)
    agent_task_map: Dict[str, str] = Field(default_factory=dict)
    agents: List[AgentStatusEntry] = Field(
        default_factory=list,
        description="Per-agent status roster (Tech Lead + implementation workers): who is "
        "working, each agent's status, and the task each is on. Derived from stack_specs, "
        "agent_task_map, the task graph, and current_activity.",
    )
    error: Optional[str] = None
    github_context: Optional[Dict[str, Any]] = None
    github_pr_url: Optional[str] = None
    review_summary: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Set by the PR-review flow: total_issues, inline_comments, "
            "comment_findings, comments_failed, files_reviewed, event."
        ),
    )
    pending_questions: List[PendingQuestion] = Field(
        default_factory=list,
        description="Decisions awaiting a user answer before the job can proceed.",
    )
    waiting_for_answers: bool = Field(
        default=False,
        description="True when the job is paused waiting for the user to answer pending questions.",
    )
    current_activity: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Fine-grained activity of the currently running sub-agent "
        "(agent, step, detail, fraction, task_id, task_title).",
    )
    last_activity_at: Optional[str] = Field(
        default=None,
        description="ISO timestamp of the last real orchestrator update (heartbeats excluded).",
    )
    updated_at: Optional[str] = Field(
        default=None, description="ISO timestamp of the last job update."
    )
    last_heartbeat_at: Optional[str] = Field(
        default=None,
        description="ISO timestamp of the last heartbeat (liveness of the worker process).",
    )
    progress: Optional[int] = Field(
        default=None,
        description="Overall job progress (0-100) derived from terminal tasks in the graph.",
    )
    server_time: Optional[str] = Field(
        default=None,
        description="Server UTC time when this response was built; clients should compute "
        "activity staleness against this, not their own clock (skew immunity).",
    )


class JobListItem(BaseModel):
    job_id: str
    status: str
    repo_path: Optional[str] = None
    phase: Optional[str] = None
    status_text: Optional[str] = None
    updated_at: Optional[str] = None
    waiting_for_answers: bool = Field(
        default=False,
        description="True when the job is paused waiting for the user to answer pending questions.",
    )
    github_context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="GitHub issue/PR metadata (owner, repo, issue_number, issue_url) when the run was started from an issue.",
    )


class RunFromGitHubRequest(BaseModel):
    """Request body for POST /run-from-github."""

    owner: str = Field(..., description="GitHub repository owner (user or org)")
    repo: str = Field(..., description="GitHub repository name")
    repo_path: str = Field(..., description="Local checkout the implementation teams work in")
    label: Optional[str] = Field(default=None, description="Optional label filter")
    issue_number: Optional[int] = Field(
        default=None,
        description="If set, verify this specific issue is ready instead of discovering one.",
    )
    github_token: Optional[str] = Field(
        default=None,
        description="Overrides GITHUB_TOKEN env var for this request.",
    )
    base_branch: Optional[str] = Field(
        default=None,
        description="PR base; defaults to the repo's default branch.",
    )
    remote: str = Field(default="origin", description="Git remote name in repo_path")
    cleanup_checkout_on_success: bool = Field(
        default=False,
        description=(
            "When true, the per-issue checkout at repo_path is platform-owned and ephemeral: "
            "delete it after the job completes cleanly and the work is published to a PR. "
            "Defaults to false so an operator-managed checkout is never removed."
        ),
    )


class RunFromGitHubResponse(BaseModel):
    job_id: str
    issue_number: int
    issue_url: str
    status: str = "pending"
    message: str = "Job started. Poll GET /status/{job_id} for progress."


class ReviewPrRequest(BaseModel):
    """Request body for POST /review-pr."""

    owner: str = Field(..., description="GitHub repository owner (user or org)")
    repo: str = Field(..., description="GitHub repository name")
    repo_path: str = Field(
        default="",
        description="Local checkout path, accepted for parity with /run-from-github. "
        "The PR review reads the diff via the GitHub API and never touches the checkout.",
    )
    pr_number: int = Field(..., description="Pull request number to review")
    github_token: Optional[str] = Field(
        default=None, description="Overrides GITHUB_TOKEN env var for this request."
    )
    base_branch: Optional[str] = Field(
        default=None, description="Informational; the PR already records its base."
    )


class ReviewPrResponse(BaseModel):
    job_id: str
    pr_number: int
    pr_url: str
    status: str = "pending"
    message: str = "Review started. Poll GET /status/{job_id} for progress."


class ReviewRunItem(BaseModel):
    """One persisted code-review run for a pull request (GET /reviews)."""

    job_id: str
    owner: str
    repo: str
    pr_number: int
    pr_url: Optional[str] = None
    status: str
    status_text: Optional[str] = None
    review_summary: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    author: str
    created_at: datetime
    completed_at: Optional[datetime] = None
