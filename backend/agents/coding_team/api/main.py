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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure backend/agents is on path for coding_team and job_service_client
_agents_root = Path(__file__).resolve().parent.parent.parent
if str(_agents_root) not in sys.path:
    sys.path.insert(0, str(_agents_root))

from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from coding_team.github_source import (  # noqa: E402
    GitHubAPIError,
    GitHubClient,
    Issue,
    NotAnIssueError,
    is_ready,
    issue_to_plan_input,
    pick_ready_issue,
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
from coding_team.models import CodingTeamPlanInput  # noqa: E402
from coding_team.orchestrator import run_coding_team_orchestrator  # noqa: E402
from shared_observability import init_otel, instrument_fastapi_app  # noqa: E402
from software_engineering_team.shared.git_utils import DEVELOPMENT_BRANCH  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

init_otel(service_name="coding-team", team_key="coding_team")

app = FastAPI(
    title="Coding Team API",
    description="Tech Lead and Senior SWEs with Task Graph. POST /run to start a job; poll GET /status/{job_id}.",
)
instrument_fastapi_app(app, team_key="coding_team")


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


class StatusResponse(BaseModel):
    job_id: str
    status: str
    phase: Optional[str] = None
    status_text: Optional[str] = None
    repo_path: Optional[str] = None
    task_graph_snapshot: List[Dict[str, Any]] = Field(default_factory=list)
    agent_task_map: Dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None
    github_context: Optional[Dict[str, Any]] = None
    github_pr_url: Optional[str] = None


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
        repo_path=data.get("repo_path"),
        task_graph_snapshot=data.get("task_graph_snapshot", []),
        agent_task_map=data.get("agent_task_map", {}),
        error=data.get("error"),
        github_context=data.get("github_context"),
        github_pr_url=data.get("github_pr_url"),
    )


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


def _record_failure(
    client: GitHubClient, owner: str, repo: str, num: int, job_id: str, error: str
) -> None:
    """Mark the job failed, capture the error, and post a (scrubbed) comment.

    Used for every post-orchestrator failure so callers polling /status see a
    consistent ``status="failed"`` instead of stale ``status="completed"``.
    """
    safe = scrub_token_from_text(error)
    update_job(job_id, status="failed", error=safe)
    _safe_comment(client, owner, repo, num, f"Coding team job `{job_id}` failed: {safe}")


def _has_merged_tasks(job: Dict[str, Any]) -> bool:
    return any((t or {}).get("status") == "merged" for t in (job.get("task_graph_snapshot") or []))


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


def _git(
    repo_path: str,
    *args: str,
    timeout: float = 120.0,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str]:
    """Run a git subcommand in ``repo_path``.

    Postconditions:
        - Returns ``(returncode, scrubbed_message)``; the message has any token
          redacted via ``scrub_token_from_text``.
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
        msg = (r.stderr or r.stdout).strip()[:500]
        return r.returncode, scrub_token_from_text(msg)
    except subprocess.TimeoutExpired:
        return 124, f"git {' '.join(args)} timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return 1, scrub_token_from_text(str(e))


def _working_tree_dirty(repo_path: str) -> Tuple[bool, Optional[str]]:
    """Return (True, listing) if the working tree has uncommitted changes.

    The listing is bounded (up to 500 chars of porcelain output) so we can
    surface the conflicting paths in the failure comment without dumping
    arbitrary file contents.
    """
    rc, msg = _git(repo_path, "status", "--porcelain")
    if rc != 0:
        return True, msg or "git status failed"
    return (bool(msg.strip()), msg if msg.strip() else None)


def _prepare_issue_branch(
    repo_path: str,
    remote: str,
    default_branch: str,
    integration_branch: str,
    token: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    # Refuse to overwrite the operator's in-flight work. Without this check,
    # `git checkout -B` happily carries unrelated dirty files across the
    # branch switch and they leak into the issue's PR.
    dirty, listing = _working_tree_dirty(repo_path)
    if dirty:
        return False, f"working tree has uncommitted changes; clean it before retrying:\n{listing}"

    # Defense-in-depth: reject ref names that could be parsed as git options.
    if not _is_safe_ref(default_branch):
        return False, f"unsafe default_branch ref: {default_branch!r}"
    if not _is_safe_ref(integration_branch):
        return False, f"unsafe integration_branch ref: {integration_branch!r}"

    # `fetch` is the only network op here (the checkouts below are local), so it
    # needs the credential. The clone was authenticated transiently by the
    # unified API; that auth is not persisted, so we re-supply it per fetch.
    auth_env = _git_auth_env(token) if token else None
    rc, msg = _git(repo_path, "fetch", "--", remote, default_branch, env=auth_env)
    if rc != 0:
        return False, msg
    rc, msg = _git(
        repo_path,
        "checkout",
        "-B",
        DEVELOPMENT_BRANCH,
        f"{remote}/{default_branch}",
        "--",
    )
    if rc != 0:
        return False, msg
    rc, msg = _git(repo_path, "checkout", "-B", integration_branch, "--")
    if rc != 0:
        return False, msg
    return True, None


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

        _safe_comment(client, owner, repo, num, f"Coding team started job `{job_id}`.")

        prep_ok, prep_err = _prepare_issue_branch(
            request.repo_path, request.remote, base, integration_branch, token
        )
        if not prep_ok:
            _record_failure(client, owner, repo, num, job_id, f"branch prep failed: {prep_err}")
            return

        try:
            run_coding_team_orchestrator(
                job_id,
                request.repo_path,
                plan,
                update_job_fn=lambda **kw: update_job(job_id, **kw),
                get_job_fn=lambda jid: get_job(jid),
                cache_dir=DEFAULT_CACHE_DIR,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Coding team orchestrator failed: %s", e)
            _record_failure(client, owner, repo, num, job_id, str(e))
            return

        if not _has_merged_tasks(get_job(job_id) or {}):
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

        if existing is not None:
            pr_url, created = existing.html_url, False
        else:
            try:
                pr = client.create_pull_request(
                    owner=owner,
                    repo=repo,
                    title=_truncate_title(issue.title, num),
                    head=integration_branch,
                    base=base,
                    body=(f"Closes #{num}\n\nGenerated by Khala coding team job `{job_id}`."),
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
