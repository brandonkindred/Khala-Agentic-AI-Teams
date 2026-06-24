"""
FastAPI app for coding_team: GET /health, POST /run, GET /status/{job_id}, GET /jobs.
"""

from __future__ import annotations

import base64
import fcntl
import logging
import os
import shutil
import subprocess
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

# Ensure backend/agents is on path for coding_team and job_service_client
_agents_root = Path(__file__).resolve().parent.parent.parent
if str(_agents_root) not in sys.path:
    sys.path.insert(0, str(_agents_root))

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from coding_team import hitl  # noqa: E402
from coding_team.activity import ActivityBridge  # noqa: E402
from coding_team.agent_status import build_agent_statuses  # noqa: E402
from coding_team.clone_workspace import (  # noqa: E402
    clone_lock_path,
    is_per_issue_dir,
    is_within_ephemeral_workspace,
)
from coding_team.github_source import (  # noqa: E402
    GitHubAPIError,
    GitHubClient,
    Issue,
    NotAnIssueError,
    build_review_body,
    choose_event,
    format_issue_comment,
    inline_comment_to_timeline_body,
    is_ready,
    issue_to_plan_input,
    map_issues_to_comments,
    parse_valid_lines,
    pick_ready_issue,
    render_annotated_hunks,
    scrub_token_from_text,
)
from coding_team.github_source.client import _is_safe_ref  # noqa: E402
from coding_team.job_store import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    RESUME_CLAIM_TTL_S,
    claim_resume,
    create_job,
    get_job,
    list_jobs,
    release_resume_claim,
    update_job,
)
from coding_team.job_store import submit_answers as store_submit_answers  # noqa: E402
from coding_team.models import AgentStatusEntry, CodingTeamPlanInput  # noqa: E402
from coding_team.orchestrator import run_coding_team_orchestrator  # noqa: E402
from coding_team.review_history_store import (  # noqa: E402
    list_reviews,
    record_review_start,
    update_review,
)
from coding_team.token_crypto import decrypt_token, encrypt_token  # noqa: E402
from shared_observability import init_otel, instrument_fastapi_app  # noqa: E402
from software_engineering_team.shared.git_utils import (  # noqa: E402
    DEVELOPMENT_BRANCH,
    commit_working_tree,
    git_identity_env,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

init_otel(service_name="coding-team", team_key="coding_team")


@asynccontextmanager
async def _coding_team_lifespan(
    app: FastAPI,
):  # pragma: no cover - exercised only with a live Postgres pool
    # Register the code-review-history schema (no-op when POSTGRES_HOST is unset).
    try:
        from coding_team.postgres import SCHEMA as CODE_REVIEW_SCHEMA
        from shared_postgres import register_team_schemas

        register_team_schemas(CODE_REVIEW_SCHEMA)
    except Exception:
        logger.exception("coding_team postgres schema registration failed")
    yield
    try:
        from shared_postgres import close_pool

        close_pool()
    except Exception:
        logger.warning("coding_team shared_postgres close_pool failed", exc_info=True)


app = FastAPI(
    title="Coding Team API",
    description="Tech Lead and Senior SWEs with Task Graph. POST /run to start a job; poll GET /status/{job_id}.",
    lifespan=_coding_team_lifespan,
)
instrument_fastapi_app(app, team_key="coding_team")

# Tracks the orchestrator thread per job so the answers endpoint can tell whether a blocked wait
# loop will pick up answers automatically (thread alive) or the job needs an explicit /resume (the
# thread died, e.g. on a server restart). Mirrors the SE team's _active_orchestrator_threads.
_active_run_threads: Dict[str, threading.Thread] = {}
# Jobs whose orchestrator thread has been claimed but not yet started/registered. The claim closes
# the check-then-spawn race in resume_job: a not-yet-started Thread reports is_alive()==False, so
# without this marker two concurrent /resume calls could both spawn an orchestrator for one job.
_starting_run_jobs: set[str] = set()
_run_thread_lock = threading.Lock()


def _register_run_thread(job_id: str) -> None:
    with _run_thread_lock:
        _active_run_threads[job_id] = threading.current_thread()
        _starting_run_jobs.discard(job_id)


def _clear_run_thread(job_id: str) -> None:
    with _run_thread_lock:
        _active_run_threads.pop(job_id, None)
        _starting_run_jobs.discard(job_id)


def _is_run_thread_alive(job_id: str) -> bool:
    """True if an orchestrator thread for this job is still running (so a blocked wait will resume)."""
    t = _active_run_threads.get(job_id)
    return t is not None and t.is_alive()


def _claim_run_thread(job_id: str) -> bool:
    """Atomically claim the right to start an orchestrator thread for *job_id*.

    Postconditions:
        - Returns True (and marks the job 'starting') iff no thread is running or already being
          started for it; False otherwise. The claim is released by _register_run_thread (once the
          new thread registers) or _clear_run_thread.
    """
    with _run_thread_lock:
        if (
            _active_run_threads.get(job_id) is not None and _active_run_threads[job_id].is_alive()
        ) or (job_id in _starting_run_jobs):
            return False
        _starting_run_jobs.add(job_id)
        return True


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
        description="Per-agent status roster (Tech Lead + one Senior SWE per stack): who is "
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
    repo_path: str = Field(..., description="Local checkout the SWEs work in")
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "coding-team"}


@app.post("/run", response_model=RunResponse)
def post_run(request: RunRequest) -> RunResponse:
    """Start a coding_team job. If plan_input is provided, runs orchestrator in background."""
    job_id = str(uuid.uuid4())
    create_job(job_id=job_id, repo_path=request.repo_path, plan_input=request.plan_input)
    if request.plan_input:
        plan = CodingTeamPlanInput.model_validate(
            {**request.plan_input, "repo_path": request.repo_path}
        )

        def run() -> None:
            _register_run_thread(job_id)
            try:
                run_coding_team_orchestrator(
                    job_id,
                    request.repo_path,
                    plan,
                    update_job_fn=lambda **kw: update_job(job_id, **kw),
                    get_job_fn=lambda jid: get_job(jid),
                    cache_dir=DEFAULT_CACHE_DIR,
                )
            except Exception as e:
                logger.exception("Coding team orchestrator failed: %s", e)
                # current_activity=None: a crash skips the in-flow clears, and a
                # failed job must not keep serving a frozen mid-review sub-bar.
                update_job(job_id, status="failed", error=str(e), current_activity=None)
            finally:
                _clear_run_thread(job_id)

        t = threading.Thread(target=run, daemon=True)
        t.start()
    return RunResponse(job_id=job_id, status="pending")


@app.get("/status/{job_id}", response_model=StatusResponse)
def get_status(job_id: str) -> StatusResponse:
    """Get job status and task graph summary."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    return StatusResponse(
        job_id=data.get("job_id", job_id),
        status=data.get("status", "pending"),
        phase=data.get("phase"),
        status_text=data.get("status_text"),
        thinking=data.get("thinking"),
        repo_path=data.get("repo_path"),
        task_graph_snapshot=data.get("task_graph_snapshot", []),
        agent_task_map=data.get("agent_task_map", {}),
        agents=build_agent_statuses(
            data.get("stack_specs", []),
            data.get("agent_task_map", {}),
            data.get("task_graph_snapshot", []),
            data.get("current_activity"),
            data.get("phase"),
        ),
        error=data.get("error"),
        github_context=data.get("github_context"),
        github_pr_url=data.get("github_pr_url"),
        review_summary=data.get("review_summary"),
        pending_questions=[PendingQuestion(**q) for q in data.get("pending_questions", [])],
        waiting_for_answers=bool(data.get("waiting_for_answers", False)),
        current_activity=data.get("current_activity")
        if isinstance(data.get("current_activity"), dict)
        else None,
        last_activity_at=data.get("last_activity_at"),
        updated_at=data.get("updated_at"),
        last_heartbeat_at=data.get("last_heartbeat_at"),
        progress=_coerce_progress(data.get("progress")),
        server_time=datetime.now(timezone.utc).isoformat(),
    )


def _coerce_progress(value: Any) -> Optional[int]:
    """Coerce a stored progress value to an int in [0, 100], or None.

    Postconditions: garbage (non-numeric) yields None; numeric values are
    clamped so a corrupt record can never render an out-of-range bar.
    """
    try:
        return min(max(int(value), 0), 100)
    except (TypeError, ValueError):
        return None


def _validate_answers(data: Dict[str, Any], request: SubmitAnswersRequest) -> List[Dict[str, Any]]:
    """Validate submitted answers against the job's pending questions; return them as plain dicts.

    Preconditions:
        - ``data`` is the job record; it must be ``waiting_for_answers`` with non-empty
          ``pending_questions``.
    Postconditions:
        - Raises HTTP 400 if the job is not waiting, has no pending questions, any required question
          is unanswered, two answers target the same question, an answer references an unknown
          question, or an 'other' selection carries no text. Otherwise returns the answers as dicts
          ready for ``store_submit_answers``, each
          carrying the ``question_text`` of the pending question it answers (so a later resume can
          match answers to re-asked questions by text).
    """
    if not data.get("waiting_for_answers"):
        raise HTTPException(status_code=400, detail="Job is not waiting for answers.")
    pending = data.get("pending_questions", [])
    if not pending:
        raise HTTPException(status_code=400, detail="No pending questions to answer.")
    # A pending question without an "id" is a corrupted job record (the orchestrator always stamps
    # one), not bad client input — surface it as a controlled 500 instead of a bare KeyError so the
    # failure is attributed to the server and carries a clear message.
    if any("id" not in q for q in pending):
        raise HTTPException(
            status_code=500, detail="Corrupted job record: pending question missing 'id'."
        )
    pending_ids = {q["id"] for q in pending}
    required_ids = {q["id"] for q in pending if q.get("required", True)}
    # Reject duplicate answers for the same question up front: the set below collapses them, so the
    # batch would pass validation while every conflicting entry is still persisted — letting the
    # orchestrator proceed with contradictory decisions for one required question.
    answered_id_list = [a.question_id for a in request.answers]
    seen: set[str] = set()
    dupes: set[str] = set()
    for qid in answered_id_list:
        (dupes if qid in seen else seen).add(qid)
    duplicate_ids = sorted(dupes)
    if duplicate_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Duplicate answers for questions: {', '.join(duplicate_ids)}",
        )
    answered_ids = set(answered_id_list)
    missing = required_ids - answered_ids
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing answers for required questions: {', '.join(sorted(missing))}",
        )
    unknown = answered_ids - pending_ids
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"Unknown question IDs: {', '.join(sorted(unknown))}"
        )
    options_by_qid = {q["id"]: {o.get("id") for o in (q.get("options") or [])} for q in pending}
    for a in request.answers:
        # Whitespace-only free text is not a decision: strip before the emptiness checks so a blank
        # or all-whitespace answer can never be recorded as a (vacuous) decision that 'covers' the
        # open question.
        other_text = (a.other_text or "").strip()
        if a.selected_option_id == "other":
            if not other_text:
                raise HTTPException(
                    status_code=400,
                    detail=f"Question {a.question_id}: 'other' selected but no text provided.",
                )
        elif a.selected_option_id:
            # A non-'other' option id must be one this question actually offered; a bogus id would
            # otherwise be threaded through as the literal user 'decision'.
            if a.selected_option_id not in options_by_qid.get(a.question_id, set()):
                raise HTTPException(
                    status_code=400,
                    detail=f"Question {a.question_id}: unknown option '{a.selected_option_id}'.",
                )
        elif not other_text:
            # Neither an option nor (non-blank) free text: not a decision. Reject it.
            raise HTTPException(
                status_code=400,
                detail=f"Question {a.question_id}: no option selected and no text provided.",
            )
    # Persist the question text alongside each answer: the orchestrator's resume hydration
    # (_hydrate_resolved_from_record) and the HITL coverage check match strictly by question
    # text, so answers stored without it would be discarded — and the question re-asked — on
    # any resume after the original thread died.
    text_by_qid = {q["id"]: q.get("question_text", "") for q in pending}
    return [
        {
            "question_id": a.question_id,
            "question_text": text_by_qid.get(a.question_id, ""),
            "selected_option_id": a.selected_option_id,
            "other_text": a.other_text,
        }
        for a in request.answers
    ]


# A paused orchestrator's wait loop heartbeats every poll (~5s); anything older than this many
# seconds means no live wait loop exists anywhere — including other worker processes, which the
# process-local thread registry cannot see.
_ANSWER_WAIT_HEARTBEAT_STALE_S = 30.0

# Tolerated clock skew between worker hosts: a heartbeat stamped up to this many seconds in the
# future (relative to the checking worker) is still treated as fresh. This covers NTP drift in
# multi-host deployments without blocking resume indefinitely on a far-future/corrupt stamp.
_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S = 10.0


def _answer_wait_heartbeat_fresh(data: Dict[str, Any]) -> bool:
    """True when a live answer-wait loop (possibly in another worker process) heartbeated recently.

    Preconditions:
        - ``data`` is a job record dict (possibly empty).
    Postconditions:
        - Returns True iff ``answer_wait_heartbeat_at`` parses as an ISO timestamp whose age is in
          ``(-_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S, _ANSWER_WAIT_HEARTBEAT_STALE_S)``. Stamps more
          than ``_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S`` seconds in the future (implausible skew or
          corruption) are NOT fresh — they must never block resume indefinitely. Missing/garbage
          values → False, never raises.
    """
    raw = (data or {}).get("answer_wait_heartbeat_at")
    if not raw:
        return False
    try:
        beat = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - beat).total_seconds()
    return age > -_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S and age < _ANSWER_WAIT_HEARTBEAT_STALE_S


def _start_orchestrator_thread(job_id: str, repo_path: str, plan: CodingTeamPlanInput) -> None:
    """Spawn the daemon orchestrator thread for a job whose run-thread claim is held.

    Preconditions:
        - The caller holds the run-thread claim for ``job_id`` (via ``_claim_run_thread``).
    Postconditions:
        - A daemon thread is running the orchestrator; the claim is released by the thread's
          ``finally`` (or here, if the thread never started — in which case the exception
          propagates so the job stays resumable).
    """

    def run() -> None:
        try:
            # Registration is inside the try so the finally always releases the claim — even if
            # _register_run_thread itself fails — instead of leaving it wedged in _starting_run_jobs.
            _register_run_thread(job_id)
            run_coding_team_orchestrator(
                job_id,
                repo_path,
                plan,
                update_job_fn=lambda **kw: update_job(job_id, **kw),
                get_job_fn=lambda jid: get_job(jid),
                cache_dir=DEFAULT_CACHE_DIR,
            )
        except Exception as e:
            logger.exception("Coding team orchestrator resume failed: %s", e)
            update_job(job_id, status="failed", error=str(e), current_activity=None)
        finally:
            _clear_run_thread(job_id)

    try:
        # The dead attempt may have left a mid-review current_activity behind (its
        # finally clears never ran); wipe it so the UI does not render a frozen
        # sub-bar through the resumed run's early phases. This sits INSIDE the
        # claim-releasing try: it is the first job-service write after the claim,
        # and a raise here (store outage) that escaped without releasing would
        # wedge the job — every later /resume would see the claim and no-op.
        update_job(job_id, current_activity=None)
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        # The thread never started, so run()'s finally will never release the claim — release it
        # here so the job stays resumable instead of being wedged in _starting_run_jobs.
        _clear_run_thread(job_id)
        raise


def _start_github_resume_thread(
    job_id: str, ctx: Dict[str, Any], repo_path: str, plan: CodingTeamPlanInput, token: str
) -> None:
    """Spawn a resume of a GitHub-issue job through the full hook path (comments, branch prep, PR).

    A plain orchestrator restart would silently drop publication (no PR, no issue comments), so
    GitHub-issue jobs must resume through ``_run_with_github_hooks``. The spawned thread registers
    itself in the run-thread registry immediately — before any GitHub I/O — so liveness checks see
    it and the claim is released.

    Preconditions:
        - The caller holds the run-thread claim for ``job_id``; ``ctx`` carries ``owner``, ``repo``
          and ``issue_number``; ``token`` is a non-empty GitHub token.
    Postconditions:
        - A daemon thread is running the hook-path resume; on thread-start failure the claim is
          released and the exception propagates. A failed issue re-fetch inside the thread marks
          the job failed rather than silently degrading to the hook-less path.
        - The resumed run reproduces the fresh run's checkout-cleanup decision, read from
          ``ctx['cleanup_checkout_on_success']`` (absent for jobs persisted before this field
          existed → ``False``, the safe no-cleanup default).
    """
    request = RunFromGitHubRequest(
        owner=str(ctx["owner"]),
        repo=str(ctx["repo"]),
        repo_path=repo_path,
        issue_number=int(ctx["issue_number"]),
        base_branch=ctx.get("base_branch"),
        remote=str(ctx.get("remote") or "origin"),
        # `is True` (not bool()) so any non-bool persisted value — e.g. a string
        # "False" from a future serialization change, which bool() would read as
        # truthy — fails safe to no-cleanup rather than deleting the checkout.
        cleanup_checkout_on_success=ctx.get("cleanup_checkout_on_success") is True,
    )

    def run() -> None:
        try:
            # Registration is inside the try so the finally always releases the claim — even if
            # _register_run_thread itself fails — instead of leaving it wedged in _starting_run_jobs.
            _register_run_thread(job_id)
            # Advance the job out of waiting_for_user BEFORE the GitHub network I/O. The
            # cross-worker resume claim (claim_resume) has a TTL of RESUME_CLAIM_TTL_S; if the
            # issue fetch or branch prep takes longer than that, another worker could treat the
            # expired claim as abandoned and spawn a second hook path. Moving the status to
            # "running" here makes _try_auto_resume and resume_job decline (they only proceed for
            # waiting_for_user), so the re-claiming window closes before the slow I/O begins.
            update_job(job_id, status="running", status_text="Resuming via GitHub hook…")
            with GitHubClient(token=token) as client:
                issue = client.get_issue(request.owner, request.repo, int(ctx["issue_number"]))
            _run_with_github_hooks(job_id, request, plan, issue, token)
        except Exception as e:
            logger.exception("GitHub-path resume failed for job %s: %s", job_id, e)
            update_job(job_id, status="failed", error=f"resume failed: {e}")
        finally:
            _clear_run_thread(job_id)

    try:
        # Mirror _start_orchestrator_thread: a dead prior attempt may have left a mid-review
        # current_activity behind (its finally never ran), which would render a frozen sub-bar
        # through the resumed run's early phases. Wipe it first. This is the first job-service
        # write after the claim, inside the claim-releasing try, so a store-outage raise here is
        # handled by the except below rather than wedging the job.
        update_job(job_id, current_activity=None)
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        _clear_run_thread(job_id)
        raise


# How long after deferring to a fresh heartbeat we re-check that the deferred-to wait loop really
# consumed the answers. Slightly past the staleness window so a loop that died right after its
# last heartbeat is unambiguously dead by the time the recheck runs.
_RESUME_RECHECK_DELAY_S = _ANSWER_WAIT_HEARTBEAT_STALE_S + 5.0


def _schedule_resume_recheck(job_id: str, delay: float = _RESUME_RECHECK_DELAY_S) -> None:
    """Schedule a one-shot recheck for a resume that was deferred to another live owner.

    Two deferral cases share this safety net: deferring to a fresh answer-wait heartbeat (a wait
    loop elsewhere should consume the answers) and deferring to another worker's resume claim. In
    both, the owner could die right after we deferred, leaving the job paused with no resume control
    (the UI shows "resuming" forever). The recheck runs after ``delay``: if the job is still paused
    with no live thread and no fresh heartbeat, it resumes it for real. Callers pass a ``delay`` past
    whichever liveness window applies (the heartbeat staleness window, or the resume-claim TTL).

    Postconditions:
        - A daemon timer is scheduled; its callback is a no-op when the job moved on (status no
          longer waiting), a thread is alive here, or the heartbeat is fresh again (the loop
          really is alive elsewhere). Scheduling failures are logged, never raised.
    """

    def _recheck() -> None:
        try:
            data = get_job(job_id) or {}
            if data.get("status") != hitl.WAITING_STATUS:
                return
            if _is_run_thread_alive(job_id) or _answer_wait_heartbeat_fresh(data):
                return
            if _try_auto_resume(job_id, data):
                logger.info(
                    "Deferred resume recheck restarted the orchestrator for job %s.", job_id
                )
            else:
                update_job(
                    job_id,
                    status_text="Answers received. Resume the job to continue processing.",
                )
        except Exception:
            logger.exception("Deferred resume recheck failed for job %s.", job_id)

    try:
        t = threading.Timer(delay, _recheck)
        t.daemon = True
        t.start()
    except Exception:
        logger.exception("Could not schedule resume recheck for job %s.", job_id)


def _try_auto_resume(job_id: str, data: Dict[str, Any]) -> bool:
    """Best-effort restart of a dead orchestrator after answers arrived.

    The thread registry is process-local, so "not alive here" does not mean "not alive anywhere":
    a paused wait loop in another worker process heartbeats the job record every poll, and a fresh
    heartbeat means that loop will consume the just-stored answers itself — spawning a second
    orchestrator would double-drive the job and its checkout. GitHub-issue jobs resume through the
    full hook path so publication (PR, issue comments) is preserved.

    Preconditions:
        - ``data`` is the job record for ``job_id`` and the caller observed the run thread
          as not alive in this process.
    Postconditions:
        - Returns True when the run is resuming (a live wait loop heartbeated recently — with a
          deferred recheck scheduled in case that loop died right after its last beat — a thread
          was spawned here, or another caller holds the start claim); False when the job is
          terminal, the record lacks a usable ``repo_path``/``plan_input``, a GitHub-issue job
          has no token to resume its publish flow, or the thread could not be started.
          Never raises.
    """
    if hitl.is_terminal(data):
        logger.warning("Auto-resume for job %s skipped: job is terminal.", job_id)
        return False
    # Only a paused job is safely resumable: a non-paused (e.g. running) job has no heartbeat to
    # prove it dead, so it may be alive in another worker. Every current caller already passes a
    # waiting_for_user record; this is a defensive invariant so the function stays safe if reused.
    if data.get("status") != hitl.WAITING_STATUS:
        logger.warning(
            "Auto-resume for job %s skipped: not paused (status=%s).",
            job_id,
            data.get("status"),
        )
        return False
    if _answer_wait_heartbeat_fresh(data):
        _schedule_resume_recheck(job_id)
        return True
    plan_raw = data.get("plan_input") or {}
    if not isinstance(plan_raw, dict):
        # A corrupted record could carry a non-dict plan_input; .get() on it would raise
        # AttributeError and break the "Never raises" contract. Treat it as no usable plan.
        plan_raw = {}
    repo_path = data.get("repo_path") or plan_raw.get("repo_path")
    if not repo_path:
        return False
    try:
        plan = CodingTeamPlanInput.model_validate({**plan_raw, "repo_path": repo_path})
    except Exception:
        logger.exception("Auto-resume for job %s skipped: invalid plan_input.", job_id)
        return False
    ctx = data.get("github_context") or {}
    is_github_job = bool(
        ctx.get("owner") and ctx.get("repo") and ctx.get("issue_number") is not None
    )
    # Prefer the token persisted (encrypted) at job creation; fall back to GITHUB_TOKEN env.
    token = (
        (decrypt_token(data.get("github_token_encrypted")) or os.environ.get("GITHUB_TOKEN"))
        if is_github_job
        else None
    )
    if is_github_job and not token:
        # Without a token the publish flow (PR, issue comments) cannot be resumed; fall back to
        # the explicit-resume hint rather than silently completing without a PR.
        logger.warning("Auto-resume for GitHub job %s skipped: no GitHub token available.", job_id)
        return False
    # Cross-worker claim FIRST: the process-local _claim_run_thread cannot stop a different worker
    # process from also spawning. The shared-store claim is the authoritative gate; only the worker
    # that wins it proceeds to the local claim and spawn. claim_resume() is the one job-store
    # read-modify-write here and may raise on a transport error; this function promises "Never
    # raises", so degrade a store failure to a False (manual-resume hint) rather than letting it
    # escape into submit_pending_answers after the answers were already stored.
    try:
        claimed = claim_resume(job_id)
    except Exception:
        logger.exception("Auto-resume for job %s skipped: resume-claim store error.", job_id)
        return False
    if not claimed:
        logger.info(
            "Auto-resume for job %s skipped: another worker holds the resume claim.", job_id
        )
        # The winner could die after claiming but before advancing the job out of waiting_for_user;
        # its lease then expires (RESUME_CLAIM_TTL_S) with nobody retrying, leaving the job paused
        # until the next user request. Schedule a recheck past the lease TTL: if the job is still
        # waiting with no live thread, that recheck reclaims and resumes it.
        _schedule_resume_recheck(job_id, delay=RESUME_CLAIM_TTL_S + 5.0)
        return True
    # Post-claim freshness check: the job could have transitioned out of waiting_for_user between
    # the caller's snapshot and the claim. claim_resume checks only the claim stamp, not the
    # job status, so re-read here. If the job is no longer waiting (terminal OR a wait loop in
    # another worker consumed the answers and moved the job to 'running'), release the claim and
    # abort — spawning here would double-drive a running job or clobber a terminal one. If the
    # read itself fails (store temporarily unavailable), the unknown state is treated conservatively:
    # release the claim and return False so the caller gets the manual-resume hint.
    try:
        post_claim_data = get_job(job_id)
    except Exception:
        logger.exception(
            "Auto-resume for job %s aborted: could not verify state after acquiring claim.", job_id
        )
        release_resume_claim(job_id)
        return False
    if post_claim_data and post_claim_data.get("status") != hitl.WAITING_STATUS:
        release_resume_claim(job_id)
        logger.warning(
            "Auto-resume for job %s aborted: status is '%s' after claim (no longer waiting).",
            job_id,
            post_claim_data.get("status"),
        )
        return False
    if not _claim_run_thread(job_id):
        # The cross-worker claim is ours but this process is already spawning (a racing thread):
        # release the shared claim so the in-flight spawn (or a later retry) isn't blocked.
        release_resume_claim(job_id)
        return True
    try:
        if is_github_job:
            _start_github_resume_thread(job_id, ctx, repo_path, plan, token or "")
        else:
            _start_orchestrator_thread(job_id, repo_path, plan)
    except Exception:
        logger.exception("Auto-resume for job %s failed to start the orchestrator thread.", job_id)
        release_resume_claim(job_id)
        return False
    return True


@app.post("/run/{job_id}/answers", response_model=StatusResponse)
def submit_pending_answers(job_id: str, request: SubmitAnswersRequest) -> StatusResponse:
    """Submit answers to a paused coding-team job's pending questions and resume it.

    The orchestrator's blocked wait loop clears on the stored answers (thread alive). If the
    thread died (e.g. a server restart), the orchestrator is restarted automatically; only when
    that is impossible (no usable plan/repo_path) are the answers merely stored with a
    status_text directing the caller to POST /run/{job_id}/resume.

    Authentication/authorization is enforced by the unified API security gateway in front of all
    team mounts; like every other coding-team route, this endpoint assumes that perimeter.
    """
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    answers = _validate_answers(data, request)
    store_submit_answers(job_id, answers)
    if not _is_run_thread_alive(job_id):
        # Re-read the record after storing answers: the job may have been cancelled between the
        # initial get_job and now. _try_auto_resume's terminal check must see that current state, or
        # it could spawn a fresh orchestrator for an already-terminal job and overwrite its status.
        current = get_job(job_id) or data
        # Write the optimistic status BEFORE spawning so the endpoint never clobbers a newer
        # status_text the freshly started orchestrator may have already written.
        update_job(job_id, status_text="Answers received; resuming the run.")
        if _try_auto_resume(job_id, current):
            logger.info(
                "Orchestrator thread for job %s was not running; restarted it after answers.",
                job_id,
            )
        else:
            logger.info(
                "Orchestrator thread for job %s is not running and could not be auto-resumed; "
                "answers stored. Call POST /run/%s/resume to restart it.",
                job_id,
                job_id,
            )
            update_job(
                job_id,
                status_text="Answers received. Resume the job to continue processing.",
            )
    return get_status(job_id)


@app.post("/run/{job_id}/resume", response_model=RunResponse)
def resume_job(job_id: str) -> RunResponse:
    """Restart a paused coding-team job's orchestrator after answers were stored but its thread died.

    No-op-safe: if a thread is still running (or a wait loop heartbeats from another worker), it
    will resume on its own and this just reports status. GitHub-issue jobs are restarted through
    the full hook path so publication (PR, issue comments) is preserved; that path needs a GitHub
    token, sourced by decrypting the one persisted (as opaque ciphertext) on the job record at
    creation (falling back to the ``GITHUB_TOKEN`` env).

    Authentication/authorization is enforced by the unified API security gateway in front of all
    team mounts; like every other coding-team route, this endpoint assumes that perimeter.

    Preconditions:
        - The job exists, is not terminal, and (once liveness can't be proven) is paused in the
          ``waiting_for_user`` state — the only state a resume is both needed and provably safe.
    Postconditions:
        - Raises 404 (unknown job), 400 (terminal job, a non-paused job that can't be proven
          alive, missing repo_path/plan, or a GitHub-issue job with no usable token); returns
          "already running" without spawning when a live thread, fresh heartbeat, or concurrent
          claim exists; otherwise spawns the orchestrator (hook path for GitHub-issue jobs) and
          reports "Job resumed."
    """
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    if hitl.is_terminal(data):
        raise HTTPException(
            status_code=400,
            detail=f"Job is {data.get('status', 'terminal')} and cannot be resumed.",
        )
    if _is_run_thread_alive(job_id) or _answer_wait_heartbeat_fresh(data):
        # The thread registry is process-local; a fresh answer-wait heartbeat means the job's
        # wait loop is alive in another worker — resuming here would double-drive the job.
        return RunResponse(
            job_id=job_id, status=data.get("status", "running"), message="Job already running."
        )
    # Past the liveness no-op, we could not PROVE the job is alive — but proof is only possible for
    # a paused job (its wait loop heartbeats). A job in any other non-terminal state (most
    # dangerously ``running``, actively doing code work with no heartbeat) might still be alive in
    # another worker, and a heartbeat goes stale 30s after a pause ends. Only a paused
    # (waiting_for_user) job is safely resumable; restarting anything else risks a second
    # orchestrator mutating the same checkout concurrently.
    if data.get("status") != hitl.WAITING_STATUS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Job is {data.get('status', 'in an unknown state')}, not paused waiting for "
                "answers; only a paused (waiting_for_user) job can be resumed."
            ),
        )
    plan_raw = data.get("plan_input") or {}
    if not isinstance(plan_raw, dict):
        raise HTTPException(
            status_code=400, detail="Job has a corrupted plan_input and cannot be resumed."
        )
    repo_path = data.get("repo_path") or plan_raw.get("repo_path")
    if not repo_path:
        raise HTTPException(status_code=400, detail="Job has no plan_input/repo_path to resume.")
    plan = CodingTeamPlanInput.model_validate({**plan_raw, "repo_path": repo_path})

    ctx = data.get("github_context") or {}
    is_github_job = bool(
        ctx.get("owner") and ctx.get("repo") and ctx.get("issue_number") is not None
    )
    # Prefer the token persisted (encrypted) at job creation; fall back to GITHUB_TOKEN env.
    token = (
        (decrypt_token(data.get("github_token_encrypted")) or os.environ.get("GITHUB_TOKEN"))
        if is_github_job
        else None
    )
    if is_github_job and not token:
        raise HTTPException(
            status_code=400,
            detail=(
                "GitHub-issue job cannot resume: no GitHub token is available (none persisted on "
                "the job record and GITHUB_TOKEN unset), and the publish flow (PR, issue comments) "
                "would be lost without one."
            ),
        )

    # Cross-worker claim FIRST (shared store), then the process-local claim: together they stop two
    # concurrent resume requests — in the same OR different worker processes — from both spawning an
    # orchestrator for this job. A store transport error here must surface as a controlled 500: a
    # bare propagation 500s opaquely, and swallowing it to False would falsely report "already
    # running" when no claim was actually taken.
    try:
        claimed = claim_resume(job_id)
    except Exception as e:
        logger.exception("Resume for job %s: resume-claim store error.", job_id)
        raise HTTPException(
            status_code=500, detail="Failed to acquire the resume claim due to a job-store error."
        ) from e
    if not claimed:
        return RunResponse(
            job_id=job_id, status=data.get("status", "running"), message="Job already running."
        )
    # Post-claim re-read: a wait loop in another worker may have consumed answers and advanced the
    # job out of waiting_for_user between the initial GET and the claim. claim_resume checks only
    # the stamp, not the status, so verify freshness here before spawning.
    try:
        post_claim = get_job(job_id)
    except Exception as exc:
        release_resume_claim(job_id)
        raise HTTPException(
            status_code=500, detail="Failed to verify job state after acquiring resume claim."
        ) from exc
    if not post_claim or post_claim.get("status") != hitl.WAITING_STATUS:
        release_resume_claim(job_id)
        return RunResponse(
            job_id=job_id,
            status=(post_claim or data).get("status", "running"),
            message="Job already running.",
        )
    if not _claim_run_thread(job_id):
        release_resume_claim(job_id)
        return RunResponse(
            job_id=job_id, status=data.get("status", "running"), message="Job already running."
        )

    try:
        if is_github_job:
            _start_github_resume_thread(job_id, ctx, repo_path, plan, token or "")
        else:
            _start_orchestrator_thread(job_id, repo_path, plan)
    except Exception:
        # A failed spawn must release the shared claim so a later /resume can win.
        release_resume_claim(job_id)
        raise
    return RunResponse(job_id=job_id, status="running", message="Job resumed.")


@app.get("/jobs", response_model=List[JobListItem])
def get_jobs(active: bool = False) -> List[JobListItem]:
    """List coding_team jobs.

    Postconditions:
        - With ``active=true``, only non-terminal jobs (pending/running/waiting_for_user) are
          returned, filtered at the job service so terminal jobs' full records (task graphs,
          thinking text) never cross the wire just to be discarded.
        - Every item carries the job's ``github_context`` (when present) and its
          ``waiting_for_answers`` flag, so list consumers can identify paused
          GitHub-issue runs without a per-job status call.
        - Missing fields fall back to ``None``/``False``; ``status`` defaults to
          ``"pending"`` for records that predate the field.
    """
    jobs = list_jobs(active_only=active)
    return [
        JobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", "pending"),
            repo_path=j.get("repo_path"),
            phase=j.get("phase"),
            status_text=j.get("status_text"),
            updated_at=j.get("updated_at"),
            waiting_for_answers=bool(j.get("waiting_for_answers", False)),
            github_context=j.get("github_context"),
        )
        for j in jobs
    ]


# ---------------------------------------------------------------------------
# GitHub-issue-driven runs
# ---------------------------------------------------------------------------


def _running_job_for_issue(owner: str, repo: str, issue_number: int) -> Optional[str]:
    """Return the job_id of any non-terminal job already working this issue.

    Owner/repo compare case-insensitively — GitHub treats them as case-insensitive, so two
    casings of the same repository are the same repository here too.

    Performance: this is an O(active-jobs) linear scan over the non-terminal set on each
    run-from-issue request. That set is small in practice (a handful of concurrent runs), so the
    scan is acceptable; if active-job volume ever grows materially, add an owner/repo/issue filter
    to ``list_jobs`` (or an in-memory index) rather than scanning here.
    """
    for j in list_jobs(active_only=True):
        ctx = (j or {}).get("github_context") or {}
        if (
            str(ctx.get("owner") or "").casefold() == owner.casefold()
            and str(ctx.get("repo") or "").casefold() == repo.casefold()
            and ctx.get("issue_number") == issue_number
        ):
            return j.get("job_id")
    return None


def _running_sibling_on_checkout(repo_path: str, own_job_id: str) -> Optional[Dict[str, Any]]:
    """Return another non-terminal job using this checkout, if any.

    Branch prep mutates the working tree (dirty-tree recovery commits files,
    `checkout -B` switches branches). Doing that under a job that is actively
    working would corrupt its run — the pre-recovery code's fail-fast dirty
    guard prevented this by accident, and recovery must not regress it. The
    job store can answer liveness (a deleted job is not running) even though
    it cannot answer leftover attribution — that remains the marker's job.

    Postconditions:
        - Returns the sibling job dict when one exists with a non-terminal
          status and the same checkout; None otherwise. Paths are compared
          canonically (symlinks, ``.``/``..``, trailing slashes resolved), so
          a sibling registered under a different spelling of the same
          checkout still matches. The caller's own job (``own_job_id``) is
          never reported.
    """
    target = os.path.realpath(repo_path)
    for j in list_jobs(active_only=True):
        if not j or j.get("job_id") == own_job_id:
            continue
        sibling_path = j.get("repo_path")
        if sibling_path and os.path.realpath(sibling_path) == target:
            return j
    return None


def _start_hook_thread(
    job_id: str,
    request: RunFromGitHubRequest,
    plan: CodingTeamPlanInput,
    issue: Issue,
    token: str,
) -> None:
    """Spawn the post-creation hook in a background thread.

    Indirection so tests can monkey-patch this to invoke the hook synchronously.
    """
    t = threading.Thread(
        target=_run_with_github_hooks,
        args=(job_id, request, plan, issue, token),
        daemon=True,
    )
    t.start()


@app.post("/run-from-github", response_model=RunFromGitHubResponse)
def post_run_from_github(request: RunFromGitHubRequest) -> RunFromGitHubResponse:
    """Discover (or verify) a ready GitHub issue and start a coding job for it."""
    token = request.github_token or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise HTTPException(status_code=400, detail="GITHUB_TOKEN not configured")
    if not Path(request.repo_path).is_dir():
        raise HTTPException(status_code=400, detail=f"repo_path not found: {request.repo_path}")

    with GitHubClient(token=token) as client:
        try:
            if request.issue_number is not None:
                issue = client.get_issue(request.owner, request.repo, request.issue_number)
                ready = is_ready(client, request.owner, request.repo, issue)
                if not ready.ready:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"issue #{issue.number} blocked by sub-issues {list(ready.blocking)}"
                        ),
                    )
            else:
                picked = pick_ready_issue(client, request.owner, request.repo, label=request.label)
                if picked is None:
                    raise HTTPException(status_code=404, detail="no ready issues")
                issue, ready = picked
        except NotAnIssueError as e:
            # Operator passed a PR number — that's a 400, not an upstream error.
            raise HTTPException(status_code=400, detail=str(e)) from e
        except GitHubAPIError as e:
            raise HTTPException(status_code=502, detail=f"github api error: {e}") from e

    running = _running_job_for_issue(request.owner, request.repo, issue.number)
    if running:
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {running} already running for {request.owner}/{request.repo}#{issue.number}"
            ),
        )

    plan = issue_to_plan_input(
        issue,
        request.repo_path,
        list(ready.sub_issues),
        request.owner,
        request.repo,
    )

    job_id = str(uuid.uuid4())
    create_job(job_id=job_id, repo_path=request.repo_path, plan_input=plan.model_dump())
    job_fields: Dict[str, Any] = {
        "github_context": {
            "owner": request.owner,
            "repo": request.repo,
            "issue_number": issue.number,
            "issue_url": issue.html_url,
            "base_branch": request.base_branch,
            "remote": request.remote,
            # Persisted so a resume reconstructs the SAME cleanup decision the
            # fresh run made; without it a resumed job would default to False and
            # leak its ephemeral per-issue checkout on clean completion.
            "cleanup_checkout_on_success": request.cleanup_checkout_on_success,
        },
    }
    # Persist the token (encrypted) so a resume after the orchestrator thread dies (server restart,
    # different worker process) can re-drive the GitHub publish flow. In the standard deployment the
    # token is a per-request PAT from the credential store and the coding-team container has no
    # GITHUB_TOKEN env, so without this the job could never resume. Only OPAQUE CIPHERTEXT is stored
    # — never a usable PAT — because the raw job record is echoed verbatim by the generic
    # GET /api/jobs/{team} route. When no encryption key is configured the token is not persisted
    # and resume falls back to GITHUB_TOKEN env (or refuses); we never store plaintext.
    encrypted = encrypt_token(token)
    if encrypted:
        job_fields["github_token_encrypted"] = encrypted
    update_job(job_id, **job_fields)

    _start_hook_thread(job_id, request, plan, issue, token)
    return RunFromGitHubResponse(job_id=job_id, issue_number=issue.number, issue_url=issue.html_url)


# ---------------------------------------------------------------------------
# Pull-request review flow (code reviewer agents review an open PR)
# ---------------------------------------------------------------------------


def _infer_review_language(files: List[Any]) -> str:
    """Pick the dominant language label for the reviewer from the changed filenames.

    Postconditions:
        - Returns "typescript" when TS/JS-family files outnumber Python files,
          else "python" (the agent's two supported language buckets).
    """
    ts = sum(1 for f in files if f.filename.endswith((".ts", ".tsx", ".js", ".jsx")))
    py = sum(1 for f in files if f.filename.endswith(".py"))
    return "typescript" if ts > py else "python"


class ReviewCode(NamedTuple):
    """Result of assembling the reviewer's ``code`` input from a PR's diff."""

    code: str
    files_reviewed: int


def _build_review_code(files: List[Any]) -> ReviewCode:
    """Assemble the line-annotated ``code`` input for the reviewer from the diff.

    Renders each changed file's diff hunks (added + context lines, new-file line
    numbers) — not whole files — so the reviewer is scoped to what the PR changed
    and cited line numbers align with the commentable-line map. Each file is wrapped
    in a ``### path ###`` block so the reviewer's coordinator can chunk large PRs.
    Built entirely from the already-fetched ``files`` payload (no extra requests).

    Every reviewable changed file is included — there is no cap on file count.
    The reviewer's coordinator bounds its own per-call prompts, so a large PR is
    chunked rather than truncated.

    Postconditions:
        - Returns ``ReviewCode(code, files_reviewed)`` covering every changed
          file with reviewable rendered content. Binary/removed files and files
          whose diff renders empty are not reviewable and are simply absent.
    """
    blocks: List[str] = []
    reviewed = 0
    for f in files:
        if not f.patch or f.status == "removed":
            continue
        rendered = render_annotated_hunks(f.patch)
        if not rendered:
            continue
        blocks.append(f"### {f.filename} ###\n{rendered}")
        reviewed += 1
    return ReviewCode("\n\n".join(blocks), reviewed)


# Optional dependency: author tagging for persisted review history. Imported once
# at module load behind a try/except so a missing/broken ``agent_console`` (or its
# transitive deps) can never break importing this API; ``_review_author`` falls
# back to "anonymous" when it is unavailable.
try:
    from agent_console.author import resolve_author as _resolve_author  # noqa: E402
except Exception:  # noqa: BLE001 - author tagging is optional, never fatal at import
    _resolve_author = None


def _review_author() -> str:
    """Resolve the author handle for a review row (best-effort, never raises).

    Postconditions:
        - Returns the resolved author handle, or ``"anonymous"`` when the optional
          ``agent_console`` author helper is unavailable or raises.
    """
    if _resolve_author is None:
        return "anonymous"
    try:
        return _resolve_author()
    except Exception:  # noqa: BLE001 - author tagging must never block a review
        return "anonymous"


def _start_pr_review_thread(job_id: str, request: ReviewPrRequest, token: str) -> None:
    """Spawn the PR-review hook in a background thread.

    Indirection so tests can monkey-patch this to invoke the hook synchronously
    (mirrors ``_start_hook_thread``).
    """
    t = threading.Thread(
        target=_run_pr_review,
        args=(job_id, request, token),
        daemon=True,
    )
    t.start()


@app.post("/review-pr", response_model=ReviewPrResponse)
def post_review_pr(request: ReviewPrRequest) -> ReviewPrResponse:
    """Start a code-reviewer-agent review of an open GitHub pull request.

    Reads the PR diff via the GitHub API (no checkout), runs the SE code-review
    agent over the changed files, and posts one PR review with inline comments.

    Preconditions:
        - A GitHub token is configured (request body or ``GITHUB_TOKEN``).
        - ``pr_number`` names an existing pull request in ``owner/repo``.
    Postconditions:
        - Creates a job, starts the review hook in the background, and returns the
          job id plus the PR URL. Poll ``GET /status/{job_id}`` for progress.
    """
    token = request.github_token or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise HTTPException(status_code=400, detail="GITHUB_TOKEN not configured")

    with GitHubClient(token=token) as client:
        try:
            pr = client.get_pull_request(request.owner, request.repo, request.pr_number)
        except GitHubAPIError as e:
            raise HTTPException(status_code=502, detail=f"github api error: {e}") from e

    job_id = str(uuid.uuid4())
    create_job(job_id=job_id, repo_path=request.repo_path)
    update_job(
        job_id,
        github_context={
            "owner": request.owner,
            "repo": request.repo,
            "pr_number": request.pr_number,
            "pr_url": pr.html_url,
        },
    )
    # Persist a row so the Code Review page can show this review's history (best-effort).
    record_review_start(
        job_id, request.owner, request.repo, request.pr_number, pr.html_url, _review_author()
    )
    _start_pr_review_thread(job_id, request, token)
    return ReviewPrResponse(job_id=job_id, pr_number=request.pr_number, pr_url=pr.html_url)


@app.get("/reviews", response_model=List[ReviewRunItem])
def get_reviews(
    owner: str,
    repo: str,
    pr_number: Optional[int] = None,
    limit: int = Query(default=500, ge=1, le=2000),
) -> List[ReviewRunItem]:
    """List persisted code-review runs for a repository (optionally one PR).

    Preconditions:
        - ``owner``/``repo`` name a repository; ``pr_number`` filters to one PR.
    Postconditions:
        - Returns up to ``limit`` runs ordered newest-first. Returns an empty
          list when Postgres is unavailable (the history feature degrades to
          "no history" rather than erroring).
    """
    rows = list_reviews(owner, repo, pr_number, limit=limit)
    return [ReviewRunItem.model_validate(row) for row in rows]


def _run_pr_review(job_id: str, request: ReviewPrRequest, token: str) -> None:
    """Background hook: review the PR, posting exactly one comment per finding.

    Postconditions:
        - On success the job is ``completed`` with ``github_pr_url`` set and one PR
          review submitted (REQUEST_CHANGES on critical/high findings from a PR the
          bot did not author, else COMMENT) whose body carries only the summary.
          Every finding produces exactly one comment and no comment lists more than
          one finding: a finding tied to a changed line becomes an individual
          line-anchored inline comment; a finding whose file changed but whose
          cited line is off-diff becomes an individual file-level review comment;
          only a finding naming a file absent from the diff is posted as a
          standalone conversation comment, so no finding is dropped. Any failure
          marks the job ``failed`` and posts a (token-scrubbed) PR comment — never
          raises.
    """
    owner, repo, pr_number = request.owner, request.repo, request.pr_number
    update_job(job_id, status="running", phase="reviewing", status_text="Reviewing pull request")
    update_review(job_id, status="running", status_text="Reviewing pull request")
    try:
        with GitHubClient(token=token) as client:
            pr = client.get_pull_request(owner, repo, pr_number)
            files = client.get_pull_request_files(owner, repo, pr_number)
            if not files:
                _safe_comment(
                    client, owner, repo, pr_number, "Code review: no changed files to review."
                )
                update_job(
                    job_id,
                    status="completed",
                    phase="completed",
                    status_text="No changed files to review",
                    github_pr_url=pr.html_url,
                )
                update_review(
                    job_id,
                    status="completed",
                    status_text="No changed files to review",
                    completed=True,
                )
                return

            valid_by_path = {f.filename: parse_valid_lines(f.patch) for f in files}
            code, files_reviewed = _build_review_code(files)
            if not code:
                _safe_comment(
                    client, owner, repo, pr_number, "Code review: no reviewable file content."
                )
                update_job(
                    job_id,
                    status="completed",
                    phase="completed",
                    status_text="No reviewable file content",
                    github_pr_url=pr.html_url,
                )
                update_review(
                    job_id,
                    status="completed",
                    status_text="No reviewable file content",
                    completed=True,
                )
                return

            try:
                reviewer_login = client.get_authenticated_login()
            except GitHubAPIError:
                reviewer_login = ""

            # Import the reviewer lazily: it pulls in strands/llm_service, and
            # keeping it out of module import lets tests stub it cheaply.
            from software_engineering_team.code_review_agent import CodeReviewAgent, CodeReviewInput

            review_input = CodeReviewInput(
                code=code,
                # _build_review_code renders every line with its original
                # line-number prefix; declaring it here (instead of letting the
                # reviewer sniff the format) keeps issue lines verbatim.
                pre_numbered=True,
                task_description=f"Review pull request #{pr_number}: {pr.title}",
                task_requirements=pr.body or "",
                language=_infer_review_language(files),
            )

            # Same bridge as the orchestrator's review sites: shared schema,
            # coalescing, swallow-on-failure, and clear-on-exit in one place.
            # last_activity_at is stamped centrally by the job service on every
            # real update, so these writes count as activity for stall detection.
            pr_bridge = ActivityBridge(
                lambda **kw: update_job(job_id, **kw),
                agent="code_review",
                label=f"Reviewing PR #{pr_number}",
            )

            try:
                output = CodeReviewAgent().run(review_input, progress_callback=pr_bridge)
            except Exception as e:  # noqa: BLE001 - any reviewer failure fails the job cleanly
                logger.exception("PR review agent failed: %s", e)
                _record_failure(client, owner, repo, pr_number, job_id, f"code review failed: {e}")
                return
            finally:
                # Clear so a stale sub-progress entry never outlives the review itself.
                pr_bridge.clear()

            comments, leftovers = map_issues_to_comments(output.issues, valid_by_path)
            body = build_review_body(
                output.summary, output.spec_compliance_notes, issue_count=len(output.issues)
            )
            event = choose_event(output.issues, author=pr.author, reviewer=reviewer_login)

            dropped = _submit_review(
                client, owner, repo, pr_number, pr.head_sha, body, event, comments
            )

            # One comment per finding: post each leftover finding (its file is not
            # in the diff, so it can't be a review comment) as its own conversation
            # comment, plus any review comments the submission had to drop (rare 422
            # body-only fallback) so no finding is lost and none is batched. These
            # findings no longer live in the review body, so a failed post would
            # drop the finding silently — count failures and fail the job instead
            # of falsely reporting every finding as posted.
            standalone_bodies = [format_issue_comment(issue) for issue in leftovers]
            standalone_bodies += [inline_comment_to_timeline_body(c) for c in dropped]
            comments_failed = sum(
                0 if _safe_comment(client, owner, repo, pr_number, body) else 1
                for body in standalone_bodies
            )

            # `comments` carries both line-anchored and file-level review
            # comments; count them by shape (file-level entries carry
            # "subject_type"). `dropped` is non-empty only on the rare body-only
            # fallback, where every review comment was dropped and re-posted.
            posted = comments if not dropped else []
            inline_count = sum(1 for c in posted if "line" in c)
            file_comment_count = sum(1 for c in posted if "subject_type" in c)
            comment_findings = len(leftovers) + len(dropped)
            review_summary = {
                "total_issues": len(output.issues),
                "inline_comments": inline_count,
                "file_comments": file_comment_count,
                "comment_findings": comment_findings,
                "comments_failed": comments_failed,
                "event": event,
                "files_reviewed": files_reviewed,
            }
            if comments_failed:
                # Some findings could not be posted as their own comment; the
                # review (inline comments + body) is already submitted, but the
                # contract "one comment per finding" is broken — surface it as a
                # failure rather than reporting completion.
                err = (
                    f"{comments_failed} of {comment_findings} finding comment(s) "
                    "could not be posted"
                )
                # Notify on the PR itself: the dropped findings no longer live in
                # the review body, so without this the author has no signal on
                # GitHub that part of the review is missing.
                _safe_comment(
                    client,
                    owner,
                    repo,
                    pr_number,
                    f"Code review incomplete: {err}. See the coding team job for details.",
                )
                update_job(
                    job_id,
                    status="failed",
                    status_text=err,
                    github_pr_url=pr.html_url,
                    review_summary=review_summary,
                    error=err,
                )
                update_review(
                    job_id,
                    status="failed",
                    status_text=err,
                    review_summary=review_summary,
                    error=err,
                    completed=True,
                )
                return
            status_text = (
                f"Review posted: {len(output.issues)} finding(s), "
                f"{inline_count} inline, {file_comment_count} file-level, "
                f"{comment_findings} comment(s), event={event}"
            )
            update_job(
                job_id,
                status="completed",
                phase="completed",
                status_text=status_text,
                github_pr_url=pr.html_url,
                review_summary=review_summary,
            )
            update_review(
                job_id,
                status="completed",
                status_text=status_text,
                review_summary=review_summary,
                completed=True,
            )
    except Exception as review_exc:  # noqa: BLE001 - any failure must mark the job, never wedge it
        # The hook runs in a daemon thread; if we let an exception escape, the thread
        # dies and the job is stuck in "running" forever. Mark it failed (mirroring
        # post_run) and post a best-effort, token-scrubbed PR comment.
        logger.exception("PR review hook failed: %s", review_exc)
        try:
            with GitHubClient(token=token) as client:
                _record_failure(
                    client, owner, repo, pr_number, job_id, f"code review failed: {review_exc}"
                )
        except Exception:  # noqa: BLE001 - the status update below is the last resort
            # Safety net: ``_record_failure`` above may already have marked the
            # job/review failed, but if it raised (e.g. the GitHub client itself
            # failed) these direct updates ensure the job never wedges in
            # "running". Both writes are idempotent, so a duplicate update here
            # (when _record_failure had partly succeeded) is harmless.
            # ``review_exc`` is the original review failure (the inner except has
            # no exception of its own); surface it on both the job and review row.
            safe_err = scrub_token_from_text(str(review_exc))
            update_job(job_id, status="failed", error=safe_err)
            update_review(job_id, status="failed", error=safe_err, completed=True)


def _submit_review(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    body: str,
    event: str,
    comments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Submit the PR review, degrading gracefully on GitHub rejections.

    GitHub rejects the whole review (422) if it requests changes on the bot's own
    PR, or if any single inline comment lands off the diff. So: try the chosen
    event with inline comments; on failure retry as COMMENT keeping the comments
    (handles the self-PR case without losing inline feedback); on a further failure
    retry as COMMENT with no inline comments (handles a stray bad line — the caller
    re-posts the dropped findings as standalone comments).

    Postconditions:
        - Exactly one review is submitted on success; raises ``GitHubAPIError`` only
          if every attempt fails. The review body and every inline-comment body are
          token-scrubbed before submission (LLM output may echo a secret from the
          reviewed code). Returns the inline comments that were *not* posted: ``[]``
          when the successful attempt carried the inline comments, or the original
          ``comments`` when it succeeded only by dropping them.
    """
    # Scrub before anything leaves for GitHub: the body (LLM summary) and each
    # inline-comment body (LLM description/suggestion) can echo a token from the
    # reviewed code, just like the standalone comments _safe_comment scrubs. Build
    # scrubbed copies so the caller's ``comments`` (used for the dropped-set return
    # and standalone re-posting) keep their original identity.
    body = scrub_token_from_text(body)
    scrubbed = [{**c, "body": scrub_token_from_text(c.get("body", ""))} for c in comments]
    attempts = [(event, scrubbed), ("COMMENT", scrubbed), ("COMMENT", [])]
    last_exc: Optional[GitHubAPIError] = None
    seen: set[tuple[str, int]] = set()
    for ev, cs in attempts:
        key = (ev, len(cs))
        if key in seen:
            continue  # skip a redundant attempt (e.g. event already COMMENT)
        seen.add(key)
        try:
            client.create_pull_request_review(
                owner=owner,
                repo=repo,
                number=pr_number,
                commit_id=head_sha,
                body=body,
                event=ev,
                comments=cs,
            )
            # When the successful attempt carried no inline comments (the final
            # body-only fallback), every finding was dropped and the caller re-posts
            # them as standalone comments; otherwise none were dropped.
            return [] if cs else list(comments)
        except GitHubAPIError as e:
            logger.warning("PR review submit failed (event=%s, comments=%d): %s", ev, len(cs), e)
            last_exc = e
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Pre/post hooks for the GitHub flow (no orchestrator changes)
# ---------------------------------------------------------------------------


def _safe_comment(client: GitHubClient, owner: str, repo: str, number: int, body: str) -> bool:
    """Best-effort issue comment; never blocks the job on a failed comment.

    Body is scrubbed to redact tokens that might have leaked from git stderr.

    Postconditions:
        - Returns True when the comment was posted, False when GitHub rejected it.
          Never raises — callers that must not drop a finding inspect the result.
    """
    try:
        client.add_issue_comment(owner, repo, number, scrub_token_from_text(body))
        return True
    except GitHubAPIError as e:
        logger.warning("Failed to comment on issue #%s: %s", number, e)
        return False


def _format_questions_comment(questions: List[Dict[str, Any]], job_id: str) -> str:
    """Render escalated open questions as a single GitHub issue comment.

    Postconditions:
        - Returns markdown listing each question (with context and selectable option ids when
          present) and how to answer it, so a human can unblock the paused job.
    """
    lines = [
        f"⏸️ Coding team job `{job_id}` is **paused for a decision** and will not proceed until "
        f"these are answered. Submit answers to `POST /run/{job_id}/answers`:",
        "",
    ]
    for i, q in enumerate(questions or [], 1):
        lines.append(f"{i}. **{q.get('question_text', '')}**  _(id: `{q.get('id', '')}`)_")
        if q.get("context"):
            lines.append(f"   - _Why:_ {q['context']}")
        opts = q.get("options") or []
        if opts:
            opt_str = ", ".join(f"`{o.get('id')}` ({o.get('label')})" for o in opts)
            lines.append(f"   - Options: {opt_str} (or `other` with free text)")
    return "\n".join(lines)


def _record_failure(
    client: GitHubClient, owner: str, repo: str, num: int, job_id: str, error: str
) -> None:
    """Mark the job failed, capture the error, and post a (scrubbed) comment.

    Used for every post-orchestrator failure so callers polling /status see a
    consistent ``status="failed"`` instead of stale ``status="completed"``.
    """
    safe = scrub_token_from_text(error)
    # status_text/current_activity are reset so a failed job cannot keep claiming
    # mid-review progress (e.g. a frozen "Reviewing PR #7 (85%)" line) forever.
    update_job(job_id, status="failed", error=safe, status_text=None, current_activity=None)
    # No-op for non-review jobs (no matching code_review_runs row); persists the
    # failure for review jobs so the Code Review page shows the failed outcome.
    update_review(job_id, status="failed", error=safe, completed=True)
    _safe_comment(client, owner, repo, num, f"Coding team job `{job_id}` failed: {safe}")


def _has_merged_tasks(job: Dict[str, Any]) -> bool:
    """True iff the job landed at least one REAL merge — a task that is MERGED and actually changed
    code. Tasks the Tech Lead adjudicated as already-done (``resolved_without_changes``) are MERGED
    but landed no diff on ``development``, so they do not count: a job whose only merged tasks are
    such no-op resolutions has nothing to publish, and treating them as publishable would push an
    empty branch / open a no-op PR instead of reporting that no real work landed."""
    return any(
        (t or {}).get("status") == "merged" and not (t or {}).get("resolved_without_changes")
        for t in (job.get("task_graph_snapshot") or [])
    )


def _failed_tasks(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Tasks that reached the terminal FAILED state (rejected past the revision cap, blocked by a
    failed dependency, or an unrecoverable implementation/review error)."""
    return [
        t for t in (job.get("task_graph_snapshot") or []) if (t or {}).get("status") == "failed"
    ]


def _format_failed_tasks(failed: List[Dict[str, Any]]) -> str:
    """Render a markdown bullet list of failed tasks for a PR body / issue comment."""
    return "\n".join(
        f"- `{(t.get('id') or '?')}`: {((t.get('title') or '').strip() or 'untitled')}"
        for t in failed
    )


def _truncate_title(title: str, issue_num: int, limit: int = 256) -> str:
    suffix = f" (closes #{issue_num})"
    head = title[: max(0, limit - len(suffix))].rstrip()
    return f"{head}{suffix}" if head else f"Issue #{issue_num}{suffix}"


def _git_auth_env(token: str) -> Dict[str, str]:
    """Build an env dict that injects Basic credentials via ``GIT_CONFIG_*`` vars.

    Mirrors the unified API's clone-time auth (``_git_auth_env`` in
    ``unified_api/routes/integrations.py``): the credential is passed
    transiently through the environment and never written to ``.git/config``.
    That matters because the checkout lives on the shared ``agents_data``
    volume — a persisted token would outlive the job and leak across runs.

    The scheme must be ``Basic`` with the ``x-access-token`` username:
    GitHub's git smart-HTTP endpoint rejects a ``Bearer`` header (401
    ``invalid credentials``) even for a valid token — Bearer is only accepted
    by the REST API — after which git tries to prompt for a username and
    fails headless ("terminal prompts disabled").

    Preconditions:
        - ``token`` is a non-empty GitHub credential authorizing the operation.
    Postconditions:
        - Returns a copy of ``os.environ`` augmented with a single transient
          ``http.extraHeader`` git-config entry (Authorization: Basic) and
          ``GIT_TERMINAL_PROMPT=0`` so a missing/invalid credential fails fast
          instead of blocking on an interactive prompt until the git timeout.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _scrub_auth_header_values(msg: str, env: Optional[Dict[str, str]]) -> str:
    """Redact the transient auth header from git output.

    ``scrub_token_from_text`` only covers URL-embedded credentials; the
    header value built by ``_git_auth_env`` is a second representation of
    the token (Basic + base64) that verbose/trace git output can echo. Job
    errors and issue comments are built from these messages, so every
    representation must be redacted.

    Postconditions:
        - Neither the full header value, the ``Basic <b64>`` credential, nor
          the bare base64 form appears in the returned text.
    """
    if not env:
        return msg
    header = env.get("GIT_CONFIG_VALUE_0") or ""
    if not header.startswith("Authorization: "):
        return msg
    credential = header[len("Authorization: ") :]  # "Basic <b64>"
    encoded = credential.rsplit(" ", 1)[-1]
    for needle in (header, credential, encoded):
        if needle:
            msg = msg.replace(needle, "***")
    return msg


def _git(
    repo_path: str,
    *args: str,
    timeout: float = 120.0,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str]:
    """Run a git subcommand in ``repo_path``.

    Postconditions:
        - Returns ``(returncode, scrubbed_message)``; the message has any
          URL-embedded token redacted via ``scrub_token_from_text`` and, when
          an auth env was supplied, the transient Authorization header value
          (including its base64 form) redacted as well.
        - ``env=None`` (default) inherits the parent environment, preserving the
          prior behaviour for local-only operations. Pass an auth env (see
          ``_git_auth_env``) for network operations against a private remote.
    """
    try:
        r = subprocess.run(
            ["git", "-C", repo_path, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        msg = _scrub_auth_header_values((r.stderr or r.stdout).strip(), env)
        return r.returncode, scrub_token_from_text(msg)
    except subprocess.TimeoutExpired:
        return 124, f"git {' '.join(args)} timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return 1, scrub_token_from_text(_scrub_auth_header_values(str(e), env))


RESCUE_BRANCH_PREFIX = "khala/rescue/"
ACTIVE_ISSUE_CONFIG_KEY = "khala.active-issue"


def _utc_timestamp() -> str:
    """Wall-clock UTC stamp used in rescue branch names (patchable in tests)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _read_active_issue(repo_path: str) -> Optional[int]:
    """Read the repo-local active-issue marker.

    The marker means: a job for that issue was mid-flight on this checkout
    and terminated abnormally (restart, kill, delete). It is the only state
    that survives job deletion, so leftover work is attributed through it.

    Postconditions:
        - Returns the issue number, or None when the marker is absent or
          unparseable (treated as unattributed).
    """
    rc, msg = _git(repo_path, "config", "--local", "--get", ACTIVE_ISSUE_CONFIG_KEY)
    if rc != 0:
        return None
    try:
        return int(msg.strip())
    except ValueError:
        return None


def _write_active_issue(repo_path: str, issue_number: int) -> None:
    """Record that a job for issue_number is mid-flight on this checkout."""
    _git(repo_path, "config", "--local", ACTIVE_ISSUE_CONFIG_KEY, str(issue_number))


def _clear_active_issue(repo_path: str) -> None:
    """Remove the marker; idempotent (unsetting a missing key is a no-op)."""
    _git(repo_path, "config", "--local", "--unset", ACTIVE_ISSUE_CONFIG_KEY)


def _clear_active_issue_if_matches(repo_path: str, issue_number: int) -> None:
    """Remove the marker only when it belongs to this job's issue.

    Two different issues may legitimately run against the same checkout (the
    duplicate guard is per-issue); an older job publishing after a newer job
    prepped must not unset the newer job's marker, or a crash of the newer
    job would lose its development-work attribution.

    Postconditions:
        - The marker is unset iff it equaled ``issue_number``; any other
          value (or no marker) is left untouched.
    """
    if _read_active_issue(repo_path) == issue_number:
        _clear_active_issue(repo_path)


def _ephemeral_checkout_target(repo_path: str) -> Optional[Path]:
    """Resolve ``repo_path`` and return it iff it is a platform-owned per-issue
    git checkout safe to delete; otherwise ``None``.

    Resolving here (and handing the resolved ``Path`` back) means the path that is
    *validated* is the exact symlink-collapsed path the caller then deletes,
    closing the check-resolved / delete-raw-string gap a directory→symlink swap
    could otherwise exploit. Four conditions must all hold:

    1. the checkout root itself is NOT a symlink — a legitimate platform-owned
       per-issue checkout is a real directory created by ``git clone``. Resolving
       a symlinked root would follow it to its target, so a job that replaced its
       own ``issue-7`` directory with a symlink to a concurrently-running
       ``issue-8`` checkout would otherwise make cleanup delete the *sibling*;
    2. the path lives strictly under one of this deployment's ephemeral
       workspace roots (``is_within_ephemeral_workspace``) — so an
       operator-pinned or arbitrary path is never eligible (and a filesystem
       root or shallow system dir like ``/`` or ``/data`` is excluded because it
       is not under a workspace root), even if a caller sets the cleanup flag and
       points ``repo_path`` at someone else's repo;
    3. its final component is the auto-derived ``issue-{N}`` per-issue shape
       (``is_per_issue_dir``) — so a repo-level checkout that merely sits under an
       ephemeral root (e.g. the PR-review path ``.../github_workspaces/owner/repo``)
       is never deleted, matching the contract that only per-issue clones are
       reclaimed;
    4. it is actually a git checkout (carries a ``.git`` entry).

    Preconditions:
        - None on caller state; ``repo_path`` may be any string (it is validated
          here precisely because it originates from an untrusted request).
    Postconditions:
        - Returns the resolved ``Path`` when all four conditions hold; ``None`` on
          any resolution error (null byte / unresolvable) or when any condition
          fails. Pure apart from filesystem reads.
    """
    try:
        raw = Path(repo_path)
        resolved = raw.resolve()
        root_is_symlink = raw.is_symlink()
    except (OSError, ValueError):
        return None
    # Refuse a symlinked checkout root: resolving it would follow the link to its
    # target and delete *that* (e.g. a sibling issue-N checkout), not the job's own
    # directory. A real per-issue checkout is never a symlink.
    if root_is_symlink:
        return None
    # ``resolve()`` defaults to ``strict=False`` (Python 3.6+), so a not-yet-created
    # path resolves without raising; passing the already-resolved path to is_within
    # keeps its internal resolve idempotent.
    if not is_within_ephemeral_workspace(resolved):
        return None
    if not is_per_issue_dir(resolved.name):
        return None
    if not (resolved / ".git").exists():
        return None
    return resolved


def _is_ephemeral_checkout_path(repo_path: str) -> bool:
    """True only for a platform-owned per-issue git checkout that is safe to delete.

    Thin boolean view over ``_ephemeral_checkout_target`` (see it for the four
    conditions and the threat model). Kept as a predicate for call sites that only
    need the yes/no answer.

    Preconditions:
        - None on caller state; ``repo_path`` may be any string.
    Postconditions:
        - Returns True iff ``_ephemeral_checkout_target`` resolves a deletable
          checkout for ``repo_path``; False otherwise. Pure apart from filesystem
          reads.
    """
    return _ephemeral_checkout_target(repo_path) is not None


def _cleanup_issue_checkout(repo_path: str) -> None:
    """Remove a platform-owned, ephemeral per-issue checkout after clean success.

    Only called once the job has completed with every task merged and the work
    published to a PR, so the local clone holds nothing the remote does not. The
    folder is recreated by the caller's clone-or-fetch on a later run.

    Concurrency:
        The ``rmtree`` runs while holding the SAME sibling ``flock`` that
        unified_api's ``_ensure_repo_clone`` takes around clone/fetch. Without it,
        a quick ``/api/integrations/github/run-issue`` retry — whose clone happens
        in unified_api *before* the coding-team active-job guard runs — could
        clone/fetch into the directory mid-rmtree. The lock lives in the
        checkout's parent, so it survives the rmtree. The lock file is
        deliberately NOT unlinked: unlinking a flock'd file lets a waiter keep the
        old (now-orphaned) inode while a later run creates a fresh lock file and
        locks the new inode, so two runs would each think they hold "the" lock.
        Leaving it makes a stable per-issue lock both clone and cleanup share; the
        files are tiny and bounded by the number of distinct issues per repo.

    Postconditions:
        - Best-effort: the checkout is removed only when
          ``_ephemeral_checkout_target`` resolves ``repo_path`` to a
          platform-owned, non-shallow per-issue git checkout under an ephemeral
          root, and the resolved (symlink-collapsed) path it returns is the one
          deleted; an unsafe path is refused (logged, left in place). Never
          raises — a cleanup failure (permissions, lock unavailable, race with a
          concurrent reader) must not turn a successful job into a failure; it is
          caught and logged. The success line is logged only after ``rmtree``
          returns.

    Note:
        ``rmtree`` is not atomic. A failure partway through can leave a
        partially-deleted directory at ``repo_path`` (possibly missing
        ``.git``); the retained lock keeps serialising access, but a later retry
        whose ``_ensure_repo_clone`` finds a non-empty, non-git directory will
        fail its ``git clone`` and the leftover must be cleared manually. This is
        rare (``rmtree`` usually fails atomically on a permission error) and the
        published work is already safe on the remote PR.
    """
    target = _ephemeral_checkout_target(repo_path)
    if target is None:
        logger.warning("Refusing to remove unsafe or non-checkout path: %s", repo_path)
        return

    # Hold the clone lock around the delete so a concurrent _ensure_repo_clone
    # can't interleave a clone/fetch into the directory being removed. Key the lock
    # on the RESOLVED checkout path (not the raw request string): _ensure_repo_clone
    # receives the already-resolved checkout path from _resolve_repo_path, so keying
    # on the raw string would, for a symlinked path, lock a different name and leave
    # the real checkout unguarded. The lock lives in the checkout's parent, so it
    # outlives the rmtree. clone_lock_path would only raise ValueError on an
    # empty-name path, which a validated per-issue target never is — but guard it
    # anyway so a future change can't break the "never raises" contract.
    try:
        lock_path = clone_lock_path(target)
    except ValueError as e:
        logger.warning("Skipping checkout cleanup; invalid lock path for %s: %s", target, e)
        return
    try:
        lock_file = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115 - closed in finally
    except OSError as e:
        # Can't take the lock (e.g. parent vanished) — skip rather than delete
        # unsynchronised and risk racing a concurrent clone. Best-effort.
        logger.warning("Skipping checkout cleanup; could not open clone lock %s: %s", lock_path, e)
        return
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        except OSError as e:
            # flock can fail (e.g. ENOLCK on some network filesystems). Cleanup must
            # never turn a successful job into a failure, so skip rather than let it
            # propagate — honouring the "never raises" contract.
            logger.warning(
                "Skipping checkout cleanup; could not acquire clone lock %s: %s", lock_path, e
            )
            return
        # Re-validate under the lock, but on the SAME resolved ``target`` captured
        # before locking — NOT by re-resolving the raw ``repo_path``. Re-resolving
        # would let a symlink swapped between the first resolve and lock
        # acquisition redirect the delete to a different checkout than the one this
        # lock protects (the lock is keyed on the original ``target``). Operating
        # on the fixed resolved path closes that window: it is the real directory
        # (never a symlink), so rmtree hits the intended checkout, and rmtree does
        # not follow symlinks *inside* the tree (it unlinks the link, never its
        # target), so a symlink planted in the checkout can't redirect the delete.
        if not (
            is_within_ephemeral_workspace(target)
            and is_per_issue_dir(target.name)
            and (target / ".git").exists()
        ):
            logger.warning("Checkout no longer a deletable per-issue path under lock: %s", target)
            return
        try:
            shutil.rmtree(target)
            logger.info("Removed ephemeral per-issue checkout at %s", target)
        except Exception as e:  # noqa: BLE001 - cleanup must never fail a successful job
            # exc_info so a partial-rmtree failure (the non-atomic case noted
            # above) is diagnosable from the traceback, not just the message.
            logger.warning(
                "Failed to remove ephemeral checkout at %s: %s", repo_path, e, exc_info=True
            )
    finally:
        # Release and close, but do NOT unlink the lock file (see Concurrency).
        # Both are wrapped so a degenerate flock/close can't break "never raises".
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            lock_file.close()
        except OSError:
            pass


def _is_ahead(repo_path: str, ref: str, base_ref: str) -> bool:
    """True if ref resolves to a commit and has commits not reachable from base_ref."""
    rc, _ = _git(repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if rc != 0:
        return False
    rc, out = _git(repo_path, "rev-list", "--count", f"{base_ref}..{ref}")
    if rc != 0:
        return False
    try:
        return int(out.strip()) > 0
    except ValueError:
        return False


def _reachable_from(repo_path: str, tip: str, container: str) -> bool:
    """True if tip is an ancestor of container (resetting container keeps tip reachable)."""
    rc, _ = _git(repo_path, "merge-base", "--is-ancestor", tip, container)
    return rc == 0


def _rescue_branch_name(repo_path: str, issue: Optional[int]) -> Optional[str]:
    """Allocate an unused rescue branch name.

    Postconditions:
        - Returns `khala/rescue/issue-<issue>-<ts>` (issue known) or
          `khala/rescue/<ts>`, suffixed `-1`..`-9` on collision; None when
          all ten candidates exist.
    """
    tag = f"issue-{issue}-" if issue is not None else ""
    base = f"{RESCUE_BRANCH_PREFIX}{tag}{_utc_timestamp()}"
    for cand in [base] + [f"{base}-{i}" for i in range(1, 10)]:
        rc, _ = _git(repo_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{cand}")
        if rc != 0:
            return cand
    return None


def _latest_issue_rescue_ref(repo_path: str, issue_number: int) -> Optional[str]:
    """Newest rescue ref for the issue (timestamps sort lexicographically)."""
    rc, out = _git(
        repo_path,
        "for-each-ref",
        "--sort=-refname",
        "--count=1",
        "--format=%(refname:short)",
        f"refs/heads/{RESCUE_BRANCH_PREFIX}issue-{issue_number}-*",
    )
    if rc != 0 or not out.strip():
        return None
    return out.strip().splitlines()[0]


def _working_tree_dirty(repo_path: str) -> Tuple[bool, bool, Optional[str]]:
    """Inspect the working tree.

    Postconditions:
        - Returns (status_ok, dirty, listing). status_ok=False means
          `git status` itself failed (state unknowable — callers must fail
          closed, never attempt recovery); listing then carries the error.
        - When status_ok, listing is bounded porcelain output (or None when
          clean) so conflicting paths can be surfaced without dumping file
          contents.
    """
    rc, msg = _git(repo_path, "status", "--porcelain")
    if rc != 0:
        return False, True, msg or "git status failed"
    return True, bool(msg.strip()), msg if msg.strip() else None


def _recover_dirty_tree(
    repo_path: str, marker: Optional[int], issue_number: Optional[int], listing: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Commit or preserve a dirty working tree before branch prep.

    Same-issue work (marker == issue_number, HEAD on a real branch) is
    committed in place so it can seed continuation; anything else — foreign
    issue, unknown attribution, detached HEAD — is moved onto a rescue
    branch. Work is never deleted.

    Preconditions:
        - The working tree is dirty and `git status` succeeded (callers
          gate on _working_tree_dirty's status_ok).
    Postconditions:
        - On success (error is None) the working tree is clean and the prior
          dirty state is committed on the returned-or-noted branch; wip_tip
          names the continuation seed candidate when the work belongs to
          issue_number, else None; note is operator-facing.
        - On failure (error set) nothing has been deleted.
    """
    same_issue = marker is not None and issue_number is not None and marker == issue_number
    rc, head = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    head_branch = head.strip() if rc == 0 else "HEAD"
    on_branch = head_branch not in ("", "HEAD")

    if same_issue and on_branch:
        ok, msg = commit_working_tree(
            repo_path,
            f"wip: recover uncommitted changes from interrupted run (issue {issue_number})",
        )
        if not ok:
            return None, None, msg
        note = f"♻️ Recovered uncommitted changes from an interrupted run (committed on `{head_branch}`)."
        return head_branch, note, None

    rescue = _rescue_branch_name(repo_path, marker)
    if rescue is None:
        return None, None, "could not allocate a rescue branch name"
    rc, msg = _git(repo_path, "checkout", "-b", rescue, "--")
    if rc != 0:
        return None, None, f"rescue branch creation failed: {msg}"
    was = f" (was on `{head_branch}`)" if on_branch else ""
    ok, msg = commit_working_tree(
        repo_path,
        f"wip: rescue uncommitted changes from interrupted run{was}\n\n{listing}".rstrip(),
    )
    if not ok:
        return None, None, f"rescue commit failed: {msg}"
    wip_tip = rescue if same_issue else None
    note = f"♻️ Recovered uncommitted changes from an interrupted run; preserved on local branch `{rescue}`."
    return wip_tip, note, None


def _preserve_if_would_orphan(
    repo_path: str, branch: str, base_ref: str, seed: str, marker: Optional[int]
) -> Optional[str]:
    """Create a rescue ref for `branch` (any committish, including a
    remote-tracking ref) when adopting `seed` would strand its commits.

    Invariant served: no commits visible to branch prep may become
    unreachable — neither through prep's own `checkout -B` resets of local
    branches, nor through the job's eventual `--force-with-lease` push
    replacing a remote issue tip the chosen seed does not contain.

    Postconditions:
        - Returns None when nothing needed preserving or a rescue ref now
          holds the tip; returns an error string when preservation was
          needed but failed (callers must fail closed).
    """
    if branch == seed:
        return None
    if not _is_ahead(repo_path, branch, base_ref):
        return None
    if _reachable_from(repo_path, branch, seed):
        return None
    name = _rescue_branch_name(repo_path, marker)
    if name is None:
        return f"could not allocate a rescue branch to preserve `{branch}`"
    rc, msg = _git(repo_path, "branch", name, branch)
    if rc != 0:
        return f"failed to preserve `{branch}` before reset: {msg}"
    logger.warning("Preserved %s on %s before reset (ahead of %s)", branch, name, base_ref)
    return None


def _prepare_issue_branch(
    repo_path: str,
    remote: str,
    default_branch: str,
    integration_branch: str,
    token: Optional[str] = None,
    issue_number: Optional[int] = None,
) -> Tuple[bool, Optional[str], List[str]]:
    """Prepare development + integration branches, recovering interrupted state.

    Dirty trees are recovered (same-issue work committed in place, foreign
    work preserved on khala/rescue/* branches), the integration branch is
    seeded from the best prior-progress tip so a new job picks up where the
    previous one left off, and no reset may orphan commits.

    Preconditions:
        - repo_path is a git checkout; ref arguments may be untrusted.
    Postconditions (success):
        - integration_branch is checked out with a clean working tree;
          khala.active-issue records issue_number when provided; every commit
          reachable from a local branch on entry is still reachable from some
          local or remote ref; the returned notes describe recovery and
          continuation actions for operator-facing reporting.
    Postconditions (failure):
        - No uncommitted work has been deleted and no commit that was
          reachable on entry has become unreachable.
    """
    notes: List[str] = []

    # Defense-in-depth: reject ref names that could be parsed as git options.
    # This must precede dirty-tree recovery — a request that can never
    # proceed must not commit WIP, create rescue branches, or switch the
    # checkout on its way to being rejected.
    if not _is_safe_ref(default_branch):
        return False, f"unsafe default_branch ref: {default_branch!r}", notes
    if not _is_safe_ref(integration_branch):
        return False, f"unsafe integration_branch ref: {integration_branch!r}", notes

    marker = _read_active_issue(repo_path)

    status_ok, dirty, listing = _working_tree_dirty(repo_path)
    if not status_ok:
        return False, f"cannot inspect working tree: {listing}", notes
    wip_tip: Optional[str] = None
    if dirty:
        wip_tip, note, recover_err = _recover_dirty_tree(
            repo_path, marker, issue_number, listing or ""
        )
        if recover_err:
            return (
                False,
                "working tree has uncommitted changes; clean it before retrying:\n"
                f"{listing}\n(automatic recovery failed: {recover_err})",
                notes,
            )
        if note:
            notes.append(note)

    # The marker is NOT cleared here even after recovery: it also drives
    # same-issue continuation (development as a seed candidate), and the
    # development-ahead commits it attributes remain on the checkout until
    # the re-seed below succeeds. The only safe transition is the success
    # path's _write_active_issue overwrite; every failure exit retains it
    # so a retry can still attribute and continue the prior work.

    # `fetch` is the only network op here (the checkouts below are local), so it
    # needs the credential. The clone was authenticated transiently by the
    # unified API; that auth is not persisted, so we re-supply it per fetch.
    auth_env = _git_auth_env(token) if token else None
    rc, msg = _git(repo_path, "fetch", "--", remote, default_branch, env=auth_env)
    if rc != 0:
        return False, msg, notes
    # The issue branch may exist remotely from a previous job that pushed
    # before dying; fetch it as a continuation candidate (absence is fine).
    base_ref = f"{remote}/{default_branch}"
    remote_issue_ref = f"{remote}/{integration_branch}"
    rc_issue_fetch, issue_fetch_msg = _git(
        repo_path, "fetch", "--", remote, integration_branch, env=auth_env
    )
    if rc_issue_fetch != 0:
        # `fetch` exit codes do not distinguish "no such remote ref" from a
        # transient transport failure, and only confirmed absence may take
        # the deletion path below — dropping the tracking ref on a network
        # blip would hide live remote progress from candidate selection and
        # let the final force-with-lease push race against it. Probe absence
        # explicitly: `ls-remote --exit-code` exits 2 when the remote has no
        # matching head, 0 when it does, anything else on transport failure.
        rc_probe, probe_out = _git(
            repo_path,
            "ls-remote",
            "--exit-code",
            "--heads",
            "--",
            remote,
            integration_branch,
            env=auth_env,
        )
        if rc_probe == 0:
            return (
                False,
                f"could not fetch remote issue branch {integration_branch!r} "
                f"(it exists on the remote — transient failure?): {issue_fetch_msg}",
                notes,
            )
        if rc_probe != 2:
            return (
                False,
                f"cannot verify whether remote issue branch {integration_branch!r} still "
                f"exists (fetch failed: {issue_fetch_msg}; probe failed: {probe_out})",
                notes,
            )
        # The remote branch is absent (deleted/pruned — the probe confirmed
        # it, and the base-branch fetch succeeded so the remote itself is
        # reachable). A stale remote-tracking ref from an earlier fetch would
        # otherwise pose as live remote state: candidate selection could seed
        # from it and the final force push would republish commits the remote
        # deliberately no longer has. Pin its tip first (never-lose-work
        # invariant), then
        # drop the tracking ref. The rescue is deliberately UNTAGGED: a
        # remote deletion is an explicit signal not to continue this state,
        # so it is preserved for manual recovery without becoming an
        # automatic continuation candidate (unlike preserved local
        # divergence, which the system itself was still carrying).
        if _is_ahead(repo_path, remote_issue_ref, base_ref):
            preserve_err = _preserve_if_would_orphan(
                repo_path, remote_issue_ref, base_ref, base_ref, None
            )
            if preserve_err:
                return False, preserve_err, notes
        _git(repo_path, "update-ref", "-d", f"refs/remotes/{remote_issue_ref}")
        # Postcondition, not return-code, check: deletion legitimately fails
        # when the ref never existed (fresh issue), but a ref that SURVIVES
        # (lock, permissions, concurrent git op) would re-enter candidate
        # selection and re-anchor the remote floor to a state the remote
        # deliberately deleted — fail closed instead.
        rc_gone, _ = _git(
            repo_path, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_issue_ref}"
        )
        if rc_gone == 0:
            return (
                False,
                f"could not drop stale remote-tracking ref {remote_issue_ref!r}; "
                f"refusing to continue while it can pose as live remote state",
                notes,
            )
    candidates: List[str] = []
    if marker is not None and issue_number is not None and marker == issue_number:
        # Same-issue continuation: the interrupted run's progress may live on
        # BOTH the wip tip (wherever HEAD was at crash time) and development
        # (merged task work). Order graph-aware — a tip containing the other
        # goes first; diverged tips put development first (the canonical
        # integration line; the wip branch is never reset, and a diverged
        # integration-branch wip is pinned by the orphan-prevention pass).
        if wip_tip and wip_tip != DEVELOPMENT_BRANCH:
            if _reachable_from(repo_path, DEVELOPMENT_BRANCH, wip_tip):
                candidates.extend((wip_tip, DEVELOPMENT_BRANCH))
            else:
                candidates.extend((DEVELOPMENT_BRANCH, wip_tip))
        else:
            candidates.append(DEVELOPMENT_BRANCH)
    # Local-vs-remote issue tip: prefer local only when it already contains
    # the remote tip. The eventual publish is `push --force-with-lease` and
    # this function's own fetch refreshes the lease, so seeding from a tip
    # that lacks remote-only commits would let the push silently drop them.
    # A diverged local tip is pinned by the orphan-prevention pass below.
    if _reachable_from(repo_path, remote_issue_ref, integration_branch):
        candidates.extend((integration_branch, remote_issue_ref))
    else:
        candidates.extend((remote_issue_ref, integration_branch))
    if issue_number is not None:
        rescue_ref = _latest_issue_rescue_ref(repo_path, issue_number)
        if rescue_ref:
            candidates.append(rescue_ref)
    # Remote floor: when the remote issue branch is live and ahead, no
    # candidate that lacks its commits may seed — the force-with-lease push
    # (lease refreshed by this function's own fetch) would silently drop the
    # remote-only commits from the published PR. Locally-pinned rescue refs
    # are no substitute for commits the remote is expected to keep.
    remote_floor = _is_ahead(repo_path, remote_issue_ref, base_ref)

    def _eligible(candidate: str) -> bool:
        if not _is_ahead(repo_path, candidate, base_ref):
            return False
        if not remote_floor or candidate == remote_issue_ref:
            return True
        return _reachable_from(repo_path, remote_issue_ref, candidate)

    seed = next((c for c in candidates if _eligible(c)), base_ref)

    if seed != base_ref:
        rc, count = _git(repo_path, "rev-list", "--count", f"{base_ref}..{seed}")
        ahead = count.strip() if rc == 0 else "?"
        notes.append(
            f"▶️ Continuing issue from previous progress: `{seed}` ({ahead} commits ahead of `{default_branch}`)."
        )

    # Invariant: no commits visible to prep — on local branches about to be
    # reset, or on the just-fetched remote issue tip that the final
    # --force-with-lease push would replace — may become unreachable.
    # Rescue-tag attribution is per ref: work on the issue branch (local or
    # remote tip) belongs to the issue being prepared by construction, so its
    # rescue ref is issue-tagged for _latest_issue_rescue_ref continuation;
    # development work is only attributable through the marker.
    for ref, owner_issue in (
        (DEVELOPMENT_BRANCH, marker),
        (integration_branch, issue_number),
        (remote_issue_ref, issue_number),
    ):
        preserve_err = _preserve_if_would_orphan(repo_path, ref, base_ref, seed, owner_issue)
        if preserve_err:
            return False, preserve_err, notes

    rc, msg = _git(repo_path, "checkout", "-B", DEVELOPMENT_BRANCH, seed, "--")
    if rc != 0:
        return False, msg, notes

    if (
        wip_tip
        and _is_safe_ref(wip_tip)
        and not _reachable_from(repo_path, wip_tip, DEVELOPMENT_BRANCH)
    ):
        # Same-issue WIP was recovered onto a branch that diverged from the
        # chosen seed (e.g. a feature branch cut before other work merged
        # into development). Recovery reported that WIP as continuation
        # state, so it must reach the resumed line — left only on a side
        # branch the orchestrator never reads, "recovered" would be a lie.
        # Merge it in; on conflict, abort and tell the operator rather than
        # guessing a resolution (the WIP branch itself is never reset, so
        # nothing is lost either way).
        rc, msg = _git(repo_path, "merge", "--no-edit", wip_tip, env=git_identity_env())
        if rc == 0:
            notes.append(
                f"🔀 Merged recovered work-in-progress from `{wip_tip}` into the continuation line."
            )
        else:
            _git(repo_path, "merge", "--abort")
            status_ok, still_dirty, _ = _working_tree_dirty(repo_path)
            if not status_ok or still_dirty:
                return (
                    False,
                    f"merge of recovered work-in-progress `{wip_tip}` failed and could not "
                    f"be cleanly aborted: {msg}",
                    notes,
                )
            notes.append(
                f"⚠️ Recovered work-in-progress on `{wip_tip}` conflicts with the continuation "
                f"line; left unmerged on that branch for manual integration."
            )

    rc, msg = _git(repo_path, "checkout", "-B", integration_branch, "--")
    if rc != 0:
        return False, msg, notes
    if issue_number is not None:
        _write_active_issue(repo_path, issue_number)
    return True, None, notes


def _fast_forward(repo_path: str, branch: str, source_ref: str) -> Tuple[bool, Optional[str]]:
    if not _is_safe_ref(branch) or not _is_safe_ref(source_ref):
        return False, f"unsafe ref: {branch!r} <- {source_ref!r}"
    rc, msg = _git(repo_path, "branch", "-f", "--", branch, source_ref)
    return (rc == 0), (None if rc == 0 else msg)


def _push_branch(
    repo_path: str, remote: str, branch: str, token: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    if not _is_safe_ref(branch):
        return False, f"unsafe branch name: {branch!r}"
    # Push is a network op against the (HTTPS) origin; supply the transient
    # credential so the PR branch actually lands instead of hanging on an auth
    # prompt until the timeout (GIT_TERMINAL_PROMPT=0 turns that into a fast
    # failure for public repos too).
    rc, msg = _git(
        repo_path,
        "push",
        "--force-with-lease",
        "-u",
        remote,
        branch,
        timeout=180,
        env=_git_auth_env(token) if token else None,
    )
    return (rc == 0), (None if rc == 0 else msg)


def _defer_terminal_success(job_id: str):
    """Build an ``update_job_fn`` that holds the job non-terminal until publish.

    The orchestrator marks its job ``completed`` when the code work finishes,
    but the GitHub hook keeps mutating the shared checkout afterwards
    (fast-forward, push, PR creation, marker clear) and the busy-checkout
    guard keys liveness off the job store's non-terminal statuses. Mapping the orchestrator's
    terminal success to ``(running, publishing)`` keeps the job visible to
    the guard for that whole window; ``_run_with_github_hooks`` sets the real
    terminal status only once it is fully done with the checkout. Failure
    statuses pass through unchanged — every post-orchestrator failure path
    stops touching the checkout.

    Postconditions:
        - The returned callable forwards every update to ``update_job`` for
          ``job_id``, rewriting only ``status="completed"`` updates.
    """

    def _update(**kw: Any) -> None:
        if kw.get("status") in hitl.TERMINAL_SUCCESS_STATUSES:
            kw = {**kw, "status": "running", "phase": "publishing"}
        update_job(job_id, **kw)

    return _update


def _run_with_github_hooks(
    job_id: str,
    request: RunFromGitHubRequest,
    plan: CodingTeamPlanInput,
    issue: Issue,
    token: str,
) -> None:
    """Wrap the orchestrator with GitHub-side actions: comments, branch prep, push, PR."""
    owner, repo, num = request.owner, request.repo, issue.number
    integration_branch = f"khala/issue-{num}"

    with GitHubClient(token=token) as client:
        # Validate the token via get_repo *before* posting the start-comment
        # so a bad token surfaces a single failure event on the issue rather
        # than a silently-dropped comment + a separate failure later.
        try:
            default_branch = client.get_repo(owner, repo).default_branch
        except GitHubAPIError as e:
            _record_failure(client, owner, repo, num, job_id, f"github get_repo: {e}")
            return
        base = request.base_branch or default_branch

        # Branch prep mutates the shared checkout; never do that under a
        # sibling job that is actively working it. Leftovers from DEAD jobs
        # are recovered below — live work is not a leftover.
        sibling = _running_sibling_on_checkout(request.repo_path, job_id)
        if sibling is not None:
            sib_ctx = sibling.get("github_context") or {}
            _record_failure(
                client,
                owner,
                repo,
                num,
                job_id,
                f"checkout busy: job `{sibling.get('job_id')}` "
                f"(issue #{sib_ctx.get('issue_number', '?')}) is still running on this "
                f"checkout; retry after it finishes",
            )
            return

        _safe_comment(client, owner, repo, num, f"Coding team started job `{job_id}`.")

        prep_ok, prep_err, prep_notes = _prepare_issue_branch(
            request.repo_path, request.remote, base, integration_branch, token, issue_number=num
        )
        if not prep_ok:
            _record_failure(client, owner, repo, num, job_id, f"branch prep failed: {prep_err}")
            return
        for note in prep_notes:
            _safe_comment(client, owner, repo, num, note)

        # When the coding team pauses for a user decision, surface the questions on the issue so a
        # human can answer them (via POST /run/{job_id}/answers); the hook thread stays blocked in
        # the orchestrator's wait until they do.
        def _on_pause(questions: List[Dict[str, Any]]) -> None:
            _safe_comment(client, owner, repo, num, _format_questions_comment(questions, job_id))

        _register_run_thread(job_id)
        try:
            run_coding_team_orchestrator(
                job_id,
                request.repo_path,
                plan,
                update_job_fn=_defer_terminal_success(job_id),
                get_job_fn=lambda jid: get_job(jid),
                cache_dir=DEFAULT_CACHE_DIR,
                on_pause=_on_pause,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Coding team orchestrator failed: %s", e)
            _record_failure(client, owner, repo, num, job_id, str(e))
            return

        job_after = get_job(job_id) or {}
        # The orchestrator may have already set a terminal/paused status — e.g. a decision pause
        # timed out (status=failed) or is still waiting for the user. Surface that diagnostic rather
        # than overwriting it with the generic "no merged tasks" message, which would hide the real
        # cause (an unanswered question) from the operator.
        if job_after.get("status") in ("failed", "cancelled", "waiting_for_user"):
            reason = (
                job_after.get("error") or job_after.get("status_text") or job_after.get("status")
            )
            _safe_comment(
                client, owner, repo, num, f"Coding team job `{job_id}` did not complete: {reason}"
            )
            return

        if job_after.get("already_complete"):
            # The team determined the issue's work was already done (planning recognized it, or every
            # task resolved as already-satisfied with no real diff). Recommend closing the issue and
            # do NOT open a no-op PR — there is nothing to merge.
            evidence = str(job_after.get("completion_evidence") or "").strip()
            body = f"Coding team job `{job_id}`: this work appears to be already complete"
            if evidence:
                body += f" — {evidence}"
            body += f"\n\nNo changes were needed. Recommend closing #{num}."
            _safe_comment(client, owner, repo, num, body)
            # An already-complete run is a clean no-op success, so it must run the SAME checkout
            # cleanup as the normal success path below — otherwise it leaves the active-issue marker
            # set (a later same-issue retry would treat stale local state as interrupted progress)
            # and leaks the per-issue clone when cleanup_checkout_on_success is set. Cleanup runs
            # BEFORE the terminal status write so the job stays in list_jobs(active_only=True) while
            # the checkout is removed (same ordering rationale as the merged-work path).
            _clear_active_issue_if_matches(request.repo_path, num)
            if request.cleanup_checkout_on_success:
                _cleanup_issue_checkout(request.repo_path)
            update_job(
                job_id,
                status="already_complete",
                phase="completed",
                status_text="Work already complete; no changes needed",
            )
            return

        if not _has_merged_tasks(job_after):
            update_job(
                job_id,
                status="failed",
                error="orchestrator produced no merged tasks",
            )
            _safe_comment(
                client,
                owner,
                repo,
                num,
                f"Coding team job `{job_id}` finished but produced no merged tasks.",
            )
            return

        ff_ok, ff_err = _fast_forward(request.repo_path, integration_branch, DEVELOPMENT_BRANCH)
        if not ff_ok:
            _record_failure(client, owner, repo, num, job_id, f"fast-forward failed: {ff_err}")
            return

        push_ok, push_err = _push_branch(
            request.repo_path, request.remote, integration_branch, token
        )
        if not push_ok:
            _record_failure(client, owner, repo, num, job_id, f"git push failed: {push_err}")
            return

        try:
            existing = client.find_existing_pr(owner, repo, integration_branch)
        except GitHubAPIError as e:
            _record_failure(client, owner, repo, num, job_id, f"github find_existing_pr: {e}")
            return

        # Some tasks may have merged while others reached a terminal FAILED state. We still
        # publish the merged work, but the PR and the job status must surface the gap rather than
        # present incomplete work as a clean success.
        failed = _failed_tasks(get_job(job_id) or {})
        # Only auto-close the issue when every task landed. A partial result still leaves
        # requested work undone, so use a non-closing reference ("Refs") to avoid closing the
        # issue when the PR merges into the default branch.
        ref_keyword = "Refs" if failed else "Closes"
        pr_body = f"{ref_keyword} #{num}\n\nGenerated by Khala coding team job `{job_id}`."
        if failed:
            pr_body += (
                f"\n\n> ⚠️ {len(failed)} task(s) did not complete and are **not** included in "
                f"this PR:\n{_format_failed_tasks(failed)}"
            )

        if existing is not None:
            pr_url, created = existing.html_url, False
            # Always refresh the reused PR's body so it reflects the latest run: add a
            # partial-failure warning when this run left tasks unfinished, and clear a stale
            # warning (and old job id) from an earlier partial run that a later retry completed.
            try:
                updated = client.update_pull_request(
                    owner=owner, repo=repo, number=existing.number, body=pr_body
                )
                pr_url = updated.html_url
            except GitHubAPIError as e:
                # Non-fatal: the warning (if any) is still posted as a comment below.
                logger.warning("Failed to update reused PR #%s body: %s", existing.number, e)
        else:
            try:
                pr = client.create_pull_request(
                    owner=owner,
                    repo=repo,
                    title=_truncate_title(issue.title, num),
                    head=integration_branch,
                    base=base,
                    body=pr_body,
                    draft=True,
                )
            except GitHubAPIError as e:
                _record_failure(
                    client, owner, repo, num, job_id, f"github create_pull_request: {e}"
                )
                return
            pr_url, created = pr.html_url, True

        update_job(job_id, github_pr_url=pr_url, integration_branch=integration_branch)
        if created:
            _safe_comment(client, owner, repo, num, f"Draft PR opened: {pr_url}")
        else:
            _safe_comment(client, owner, repo, num, f"Reusing existing draft PR: {pr_url}")
        if failed:
            _safe_comment(
                client,
                owner,
                repo,
                num,
                f"⚠️ {len(failed)} task(s) did not complete and were not merged:\n"
                f"{_format_failed_tasks(failed)}",
            )
        # Publication is the marker's end of life: the work now lives on the
        # remote PR branch, so the checkout no longer holds unpublished work
        # for this issue. Every earlier return (orchestrator failure, no
        # merged tasks, fast-forward/push/PR failure) retains the marker so a
        # retry continues from development instead of starting over. Scoped
        # to this job's issue: a sibling job for another issue may have
        # re-marked the checkout since this job prepped.
        _clear_active_issue_if_matches(request.repo_path, num)

        # Drop the per-issue clone only on a clean completion: every task merged
        # and the work published to the PR, so nothing local is unrecoverable. A
        # partial result (some tasks FAILED) keeps the checkout so a retry can
        # seed from its local progress, as does every earlier failure return.
        # Operator-managed checkouts never set the flag, so they are never removed.
        #
        # Cleanup runs BEFORE the terminal status update so the job stays in
        # list_jobs(active_only=True) while the checkout is being removed: a
        # quick same-issue retry is then rejected by the duplicate guard in
        # /run-from-github instead of cloning into a directory mid-rmtree. That
        # guard (_running_job_for_issue) scans the active-jobs list by
        # github_context, NOT the active-issue git-config marker cleared just
        # above — the marker only attributes leftover work to an issue after a
        # job dies — so clearing the marker early does not open the race.
        if not failed and request.cleanup_checkout_on_success:
            _cleanup_issue_checkout(request.repo_path)

        # Terminal status comes last: the busy-checkout guard treats a
        # terminal job as done with the checkout, so this must be the hook's
        # final action after every checkout-touching step above (including the
        # cleanup rmtree). A job that merged some work but also has failed tasks
        # is reported as a partial success so it is not presented as a clean
        # completion.
        update_job(
            job_id,
            status="completed_with_failures" if failed else "completed",
            phase="completed",
        )
