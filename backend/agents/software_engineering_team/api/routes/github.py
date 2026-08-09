"""coding_team API — github-issue-driven run route."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import (
    RunFromGitHubRequest,
    RunFromGitHubResponse,
)
from software_engineering_team.api.routes._common import resolve_github_token
from software_engineering_team.github_source import (
    GitHubAPIError,
    NotAnIssueError,
    is_ready,
    issue_to_plan_input,
    pick_ready_issue,
)
from software_engineering_team.models import JobStatus
from software_engineering_team.temporal.coding_team_start_workflow import (
    start_coding_team_workflow,
)
from software_engineering_team.token_crypto import encrypt_token

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/run-from-github", response_model=RunFromGitHubResponse)
def post_run_from_github(request: RunFromGitHubRequest) -> RunFromGitHubResponse:
    """Discover (or verify) a ready GitHub issue and start a coding job for it.

    Preconditions:
        - ``request`` names an existing local checkout and provides a GitHub token
          either directly or through the configured environment.
    Postconditions:
        - A job record is created and tagged with GitHub context for the selected
          ready issue.
        - The CodingTeamWorkflow is started with a GitHub payload that contains
          branch metadata but never the plaintext token.
    """
    token = resolve_github_token(request)
    if not Path(request.repo_path).is_dir():
        raise HTTPException(status_code=400, detail=f"repo_path not found: {request.repo_path}")

    with _main.GitHubClient(token=token) as client:
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
            default_branch = (
                None
                if request.base_branch
                else client.get_repo(request.owner, request.repo).default_branch
            )
        except NotAnIssueError as e:
            # Operator passed a PR number — that's a 400, not an upstream error.
            raise HTTPException(status_code=400, detail=str(e)) from e
        except GitHubAPIError as e:
            raise HTTPException(status_code=502, detail=f"github api error: {e}") from e

    running = _main._running_job_for_issue(request.owner, request.repo, issue.number)
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
    _main.create_job(job_id=job_id, repo_path=request.repo_path, plan_input=plan.model_dump())
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
    _main.update_job(job_id, **job_fields)

    base = request.base_branch or default_branch
    if not base:
        raise HTTPException(
            status_code=500,
            detail="unable to resolve base branch for GitHub-issue run",
        )
    integration_branch = f"khala/issue-{issue.number}"
    try:
        start_coding_team_workflow(
            job_id,
            request.repo_path,
            plan.model_dump(),
            github={
                "owner": request.owner,
                "repo": request.repo,
                "issue_number": issue.number,
                "issue_title": issue.title,
                "remote": request.remote,
                "base": base,
                "integration_branch": integration_branch,
                "cleanup_checkout_on_success": request.cleanup_checkout_on_success,
            },
        )
    except Exception as e:
        # Dispatch failed (worker not ready, start timeout, bad config). Mark
        # the freshly-created row failed so it is not orphaned in 'pending',
        # and surface a retryable error instead of an opaque 500.
        logger.exception("Coding team Temporal dispatch failed: %s", e)
        _main.update_job(
            job_id,
            status=JobStatus.FAILED.value,
            error=f"Temporal dispatch failed: {e}",
            current_activity=None,
        )
        raise HTTPException(
            status_code=503,
            detail="Temporal dispatch failed (worker unavailable); job marked failed. Retry.",
        ) from e
    return RunFromGitHubResponse(job_id=job_id, issue_number=issue.number, issue_url=issue.html_url)
