"""Pydantic request/response models for the coding_team API.

Pure data schemas shared by the route modules; no runtime logic, no I/O.

Invariants:
    - Import-side-effect free beyond class definition.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# The HITL "pending question / answer" schemas live in the shared.hitl package so every
# team shares one reconciled definition; re-exported here so existing importers (routes,
# StatusResponse) keep using `coding_team.api.models`.
from shared.hitl.models import (  # noqa: F401
    AnswerSubmission,
    PendingQuestion,
    QuestionOption,
    SubmitAnswersRequest,
)
from software_engineering_team.models import AgentStatusEntry


class RunRequest(BaseModel):
    """Request body for POST /run."""

    repo_path: str = Field(..., description="Path to the repository")
    plan_input: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional plan from Planning team (CodingTeamPlanInput); if omitted, job is created but orchestrator expects to be run in-process with plan_input.",
    )
    acknowledged_resume_token: Optional[str] = Field(
        default=None,
        description=(
            "Set by CodingTeamWorkflow (system_design/hitl_pause_resume_contract.md "
            "§2/§3) on the invocation that resolves a pause, naming the resume_token "
            "of the persisted pause it resolves. run_pipeline_activity forwards this "
            "to run_orchestrator_wired, whose orchestrator re-entry check "
            "(pause_cycle._check_pending_pause_reentry) consumes the matching "
            "persisted pause envelope instead of re-running planning work; a "
            "missing/stale value re-emits that pause unchanged (a pre-work activity "
            "retry)."
        ),
    )


class RunResponse(BaseModel):
    job_id: str
    status: str = "pending"
    message: str = "Job started. Poll GET /status/{job_id} for progress."


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
    grooming: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Set by the GitHub issue grooming flow (IssueGroomingRunner) via the same "
            "generic update_job(job_id, **fields) convention every coding-team job uses "
            "for phase/status_text/progress -- there is no separate grooming-specific "
            "status path. Holds {'score': {...}} once Phase A (complexity scoring) "
            "completes, and adds 'sub_issues': [{'number', 'title'}, ...] once Phase B "
            "(sub-issue split) runs. There is no thread-mode grooming implementation to "
            "diverge from, so this field is the sole surface for grooming progress/stats "
            "on either execution engine -- full parity by construction."
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
    resume_token: Optional[str] = Field(
        default=None,
        description='Set only for a Temporal-native (pause_strategy="return") pause; the client '
        "must echo this back on SubmitAnswersRequest.resume_token. A client discovering the "
        "pause via polling status (rather than the original pause notification) has no other "
        "way to obtain it.",
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


class GroomGithubIssuesRequest(BaseModel):
    """Request body for POST /groom-github-issues."""

    owner: str = Field(..., description="GitHub repository owner (user or org)")
    repo: str = Field(..., description="GitHub repository name")
    issue_number: int = Field(..., description="Issue to groom")
    github_token: Optional[str] = Field(
        default=None,
        description="Overrides GITHUB_TOKEN env var for this request.",
    )


class GroomGithubIssuesResponse(BaseModel):
    job_id: str
    issue_number: int
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
    """Response for ``POST /review-pr``: the started review job's id, PR number/url,
    and initial status, plus the server-clock start time (``created_at``) used to
    compute a live review duration on one clock."""

    job_id: str
    pr_number: int
    pr_url: str
    status: str = "pending"
    message: str = "Review started. Poll GET /status/{job_id} for progress."
    # Server-clock start time of the review, so the UI can compute a live duration
    # from server timestamps at both ends (start here, completion from job status)
    # rather than mixing the browser clock in. Optional: absent when the start
    # timestamp is unavailable, in which case the UI falls back to its own clock.
    created_at: Optional[datetime] = None


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


class TranscriptEntry(BaseModel):
    """One LLM call the review pipeline made (GET /reviews/{job_id}/transcript)."""

    stage: str
    target: str
    model: str
    prompt: str
    response: str
    started_at: str
    duration_ms: int


class TranscriptResponse(BaseModel):
    """A review's full durable transcript, in call order."""

    job_id: str
    entries: List[TranscriptEntry]


class CreateReviewIssuesRequest(BaseModel):
    """Request body for POST /reviews/{job_id}/issues."""

    proposal_ids: List[str] = Field(
        default_factory=list,
        description="Ids of the review's pending issue proposals to file as GitHub issues.",
    )
    owner: Optional[str] = Field(
        default=None,
        description="Expected repository owner; validated against the stored review so issues are "
        "only filed into the repository that was actually reviewed.",
    )
    repo: Optional[str] = Field(
        default=None,
        description="Expected repository name; validated against the stored review (see owner).",
    )
    github_token: Optional[str] = Field(
        default=None, description="Overrides GITHUB_TOKEN env var for this request."
    )


class CreatedIssueItem(BaseModel):
    """One GitHub issue opened from a pending issue proposal."""

    proposal_id: str
    issue_number: int
    issue_url: str
    title: str


class CreateReviewIssuesResponse(BaseModel):
    """Result of POST /reviews/{job_id}/issues.

    ``proposals`` is the review's full, updated pending-proposal list (created
    ones now carry ``issue_number``/``issue_url``) so the UI can reconcile in one
    round-trip; ``created`` names only the issues opened by this request.
    """

    job_id: str
    created: List[CreatedIssueItem] = Field(default_factory=list)
    proposals: List[Dict[str, Any]] = Field(default_factory=list)
