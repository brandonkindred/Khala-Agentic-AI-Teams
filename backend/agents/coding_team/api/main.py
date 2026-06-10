"""
FastAPI app for coding_team: GET /health, POST /run, GET /status/{job_id}, GET /jobs.
"""

from __future__ import annotations

import base64
import logging
import os
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

from coding_team.github_source import (  # noqa: E402
    GitHubAPIError,
    GitHubClient,
    Issue,
    NotAnIssueError,
    build_review_body,
    choose_event,
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
    create_job,
    get_job,
    list_jobs,
    update_job,
)
from coding_team.job_store import submit_answers as store_submit_answers  # noqa: E402
from coding_team.models import CodingTeamPlanInput  # noqa: E402
from coding_team.orchestrator import run_coding_team_orchestrator  # noqa: E402
from coding_team.review_history_store import (  # noqa: E402
    list_reviews,
    record_review_start,
    update_review,
)
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
    error: Optional[str] = None
    github_context: Optional[Dict[str, Any]] = None
    github_pr_url: Optional[str] = None
    review_summary: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Set by the PR-review flow: total_issues, inline_comments, body_findings, event.",
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


class JobListItem(BaseModel):
    job_id: str
    status: str
    repo_path: Optional[str] = None
    phase: Optional[str] = None


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
                update_job(job_id, status="failed", error=str(e))
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
    )


def _validate_answers(data: Dict[str, Any], request: SubmitAnswersRequest) -> List[Dict[str, Any]]:
    """Validate submitted answers against the job's pending questions; return them as plain dicts.

    Preconditions:
        - ``data`` is the job record; it must be ``waiting_for_answers`` with non-empty
          ``pending_questions``.
    Postconditions:
        - Raises HTTP 400 if the job is not waiting, has no pending questions, any required question
          is unanswered, an answer references an unknown question, or an 'other' selection carries
          no text. Otherwise returns the answers as dicts ready for ``store_submit_answers``.
    """
    if not data.get("waiting_for_answers"):
        raise HTTPException(status_code=400, detail="Job is not waiting for answers.")
    pending = data.get("pending_questions", [])
    if not pending:
        raise HTTPException(status_code=400, detail="No pending questions to answer.")
    pending_ids = {q["id"] for q in pending}
    required_ids = {q["id"] for q in pending if q.get("required", True)}
    answered_ids = {a.question_id for a in request.answers}
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
    return [
        {
            "question_id": a.question_id,
            "selected_option_id": a.selected_option_id,
            "other_text": a.other_text,
        }
        for a in request.answers
    ]


@app.post("/run/{job_id}/answers", response_model=StatusResponse)
def submit_pending_answers(job_id: str, request: SubmitAnswersRequest) -> StatusResponse:
    """Submit answers to a paused coding-team job's pending questions and resume it.

    The orchestrator's blocked wait loop clears on the stored answers (thread alive). If the thread
    died (e.g. a server restart), the answers are stored and the caller should POST /run/{job_id}/resume.
    """
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    answers = _validate_answers(data, request)
    store_submit_answers(job_id, answers)
    if not _is_run_thread_alive(job_id):
        logger.info(
            "Orchestrator thread for job %s is not running; answers stored. "
            "Call POST /run/%s/resume to restart it.",
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

    No-op-safe: if a thread is still running, it will resume on its own and this just reports status.
    Only the standalone (plan_input) path is resumable here; the GitHub-issue publish flow is not
    re-driven (its hook thread is gone), so a restarted GitHub job continues the code work but you
    must re-trigger publication.
    """
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    if _is_run_thread_alive(job_id):
        return RunResponse(
            job_id=job_id, status=data.get("status", "running"), message="Job already running."
        )
    plan_raw = data.get("plan_input") or {}
    repo_path = data.get("repo_path") or plan_raw.get("repo_path")
    if not repo_path:
        raise HTTPException(status_code=400, detail="Job has no plan_input/repo_path to resume.")
    plan = CodingTeamPlanInput.model_validate({**plan_raw, "repo_path": repo_path})

    # Atomically claim the right to start the thread so two concurrent /resume calls (or one racing
    # an as-yet-unregistered original thread) cannot both spawn an orchestrator for this job.
    if not _claim_run_thread(job_id):
        return RunResponse(
            job_id=job_id, status=data.get("status", "running"), message="Job already running."
        )

    def run() -> None:
        _register_run_thread(job_id)
        try:
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
            update_job(job_id, status="failed", error=str(e))
        finally:
            _clear_run_thread(job_id)

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        # The thread never started, so run()'s finally will never release the claim — release it
        # here so the job stays resumable instead of being wedged in _starting_run_jobs.
        _clear_run_thread(job_id)
        raise
    return RunResponse(job_id=job_id, status="running", message="Job resumed.")


@app.get("/jobs", response_model=List[JobListItem])
def get_jobs() -> List[JobListItem]:
    """List coding_team jobs."""
    jobs = list_jobs()
    return [
        JobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", "pending"),
            repo_path=j.get("repo_path"),
            phase=j.get("phase"),
        )
        for j in jobs
    ]


# ---------------------------------------------------------------------------
# GitHub-issue-driven runs
# ---------------------------------------------------------------------------


def _running_job_for_issue(owner: str, repo: str, issue_number: int) -> Optional[str]:
    """Return the job_id of any non-terminal job already working this issue."""
    for j in list_jobs(running_only=True):
        ctx = (j or {}).get("github_context") or {}
        if (
            ctx.get("owner") == owner
            and ctx.get("repo") == repo
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
    for j in list_jobs(running_only=True):
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
    update_job(
        job_id,
        github_context={
            "owner": request.owner,
            "repo": request.repo,
            "issue_number": issue.number,
            "issue_url": issue.html_url,
            "base_branch": request.base_branch,
            "remote": request.remote,
        },
    )

    _start_hook_thread(job_id, request, plan, issue, token)
    return RunFromGitHubResponse(job_id=job_id, issue_number=issue.number, issue_url=issue.html_url)


# ---------------------------------------------------------------------------
# Pull-request review flow (code reviewer agents review an open PR)
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    """Read a positive int env var, falling back to ``default`` on absent/garbage/non-positive."""
    try:
        val = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return val if val > 0 else default


# Cap how many changed files are sent to the reviewer, bounding the prompt size
# on a very large PR. Reviewable files past the cap are reported as skipped (see
# _build_review_code) so a partial review is never presented as a full one.
PR_REVIEW_MAX_FILES = _env_int("PR_REVIEW_MAX_FILES", 50)


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
    files_skipped: int


def _build_review_code(files: List[Any]) -> ReviewCode:
    """Assemble the line-annotated ``code`` input for the reviewer from the diff.

    Renders each changed file's diff hunks (added + context lines, new-file line
    numbers) — not whole files — so the reviewer is scoped to what the PR changed
    and cited line numbers align with the commentable-line map. Each file is wrapped
    in a ``### path ###`` block so the reviewer's coordinator can chunk large PRs.
    Built entirely from the already-fetched ``files`` payload (no extra requests).

    Postconditions:
        - Returns ``ReviewCode(code, files_reviewed, files_skipped)``. ``files_skipped``
          counts only files with reviewable rendered content that were left out beyond
          ``PR_REVIEW_MAX_FILES`` — so the caller can honestly disclose a partial review.
          Binary/removed files and files whose diff renders empty are not reviewable and
          are never counted as skipped.
    """
    blocks: List[str] = []
    reviewed = 0
    skipped = 0
    for f in files:
        if not f.patch or f.status == "removed":
            continue
        rendered = render_annotated_hunks(f.patch)
        if not rendered:
            continue
        # Only files that actually have content to review count toward the cap and
        # the skipped disclosure — checked after rendering so an empty render is not
        # miscounted as a skipped file.
        if reviewed >= PR_REVIEW_MAX_FILES:
            skipped += 1
            continue
        blocks.append(f"### {f.filename} ###\n{rendered}")
        reviewed += 1
    return ReviewCode("\n\n".join(blocks), reviewed, skipped)


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
    """Background hook: review the PR and post one review with inline comments.

    Postconditions:
        - On success the job is ``completed`` with ``github_pr_url`` set and exactly
          one PR review submitted (REQUEST_CHANGES on critical/high findings from a
          PR the bot did not author, else COMMENT). Findings tied to a diff line
          become inline comments; the rest are folded into the review body, so no
          finding is dropped. Any failure marks the job ``failed`` and posts a
          (token-scrubbed) PR comment — never raises.
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
            code, files_reviewed, files_skipped = _build_review_code(files)
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
                task_description=f"Review pull request #{pr_number}: {pr.title}",
                task_requirements=pr.body or "",
                language=_infer_review_language(files),
            )

            def _pr_progress(step: str, detail: str, fraction: float) -> None:
                """Surface review sub-steps on the job record (best-effort).

                Preconditions: invoked by the review agent per its progress contract.
                Postconditions: a failed store write is logged and swallowed —
                    observability must never fail the review itself.
                """
                try:
                    update_job(
                        job_id,
                        status_text=f"Reviewing PR #{pr_number} ({int(fraction * 100)}%): "
                        f"{detail or step}",
                        current_activity={
                            "agent": "code_review",
                            "step": step,
                            "detail": detail,
                            "fraction": fraction,
                        },
                    )
                except Exception:  # noqa: BLE001 - observability must not break the review
                    logger.warning("PR review progress update failed (ignored)", exc_info=True)

            try:
                output = CodeReviewAgent().run(review_input, progress_callback=_pr_progress)
            except Exception as e:  # noqa: BLE001 - any reviewer failure fails the job cleanly
                logger.exception("PR review agent failed: %s", e)
                _record_failure(client, owner, repo, pr_number, job_id, f"code review failed: {e}")
                return
            finally:
                # Clear so a stale sub-progress entry never outlives the review itself.
                try:
                    update_job(job_id, current_activity=None)
                except Exception:  # noqa: BLE001 - observability must not break the review
                    logger.warning("PR review activity clear failed (ignored)", exc_info=True)

            comments, leftovers = map_issues_to_comments(output.issues, valid_by_path)
            body = build_review_body(output.summary, output.spec_compliance_notes, leftovers)
            if files_skipped:
                # Disclose a partial review so a capped run is never presented as complete.
                body += (
                    f"\n\n_Note: this review covered the first {files_reviewed} changed file(s); "
                    f"{files_skipped} further changed file(s) exceeded the per-review cap "
                    f"(`PR_REVIEW_MAX_FILES`) and were not inspected._"
                )
            event = choose_event(output.issues, author=pr.author, reviewer=reviewer_login)

            _submit_review(client, owner, repo, pr_number, pr.head_sha, body, event, comments)

            review_summary = {
                "total_issues": len(output.issues),
                "inline_comments": len(comments),
                "body_findings": len(leftovers),
                "event": event,
                "files_reviewed": files_reviewed,
                "files_skipped": files_skipped,
            }
            status_text = (
                f"Review posted: {len(output.issues)} finding(s), "
                f"{len(comments)} inline, event={event}"
                + (f"; {files_skipped} file(s) skipped (cap)" if files_skipped else "")
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
                _record_failure(client, owner, repo, pr_number, job_id, f"code review failed: {review_exc}")
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
) -> None:
    """Submit the PR review, degrading gracefully on GitHub rejections.

    GitHub rejects the whole review (422) if it requests changes on the bot's own
    PR, or if any single inline comment lands off the diff. So: try the chosen
    event with inline comments; on failure retry as COMMENT keeping the comments
    (handles the self-PR case without losing inline feedback); on a further failure
    retry as COMMENT with no inline comments (handles a stray bad line — all
    findings already live in the body).

    Postconditions:
        - Exactly one review is submitted on success; raises ``GitHubAPIError`` only
          if every attempt fails.
    """
    attempts = [(event, comments), ("COMMENT", comments), ("COMMENT", [])]
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
            return
        except GitHubAPIError as e:
            logger.warning("PR review submit failed (event=%s, comments=%d): %s", ev, len(cs), e)
            last_exc = e
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Pre/post hooks for the GitHub flow (no orchestrator changes)
# ---------------------------------------------------------------------------


def _safe_comment(client: GitHubClient, owner: str, repo: str, number: int, body: str) -> None:
    """Best-effort issue comment; never blocks the job on a failed comment.

    Body is scrubbed to redact tokens that might have leaked from git stderr.
    """
    try:
        client.add_issue_comment(owner, repo, number, scrub_token_from_text(body))
    except GitHubAPIError as e:
        logger.warning("Failed to comment on issue #%s: %s", number, e)


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
    update_job(job_id, status="failed", error=safe)
    # No-op for non-review jobs (no matching code_review_runs row); persists the
    # failure for review jobs so the Code Review page shows the failed outcome.
    update_review(job_id, status="failed", error=safe, completed=True)
    _safe_comment(client, owner, repo, num, f"Coding team job `{job_id}` failed: {safe}")


def _has_merged_tasks(job: Dict[str, Any]) -> bool:
    return any((t or {}).get("status") == "merged" for t in (job.get("task_graph_snapshot") or []))


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
    guard keys liveness off pending/running. Mapping the orchestrator's
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
        if kw.get("status") in ("completed", "completed_with_failures"):
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
        # Terminal status comes last: the busy-checkout guard treats a
        # terminal job as done with the checkout, so this must be the hook's
        # final action after every checkout-touching step above. A job that
        # merged some work but also has failed tasks is reported as a partial
        # success so it is not presented as a clean completion.
        update_job(
            job_id,
            status="completed_with_failures" if failed else "completed",
            phase="completed",
        )
