"""coding_team API — GitHub issue grooming dispatch route."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException

from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import (
    GroomGithubIssuesRequest,
    GroomGithubIssuesResponse,
)
from software_engineering_team.api.routes._common import resolve_github_token
from software_engineering_team.models import JobStatus
from software_engineering_team.temporal.issue_grooming_start_workflow import (
    start_issue_grooming_workflow,
)
from software_engineering_team.token_crypto import encrypt_token

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/groom-github-issues", response_model=GroomGithubIssuesResponse)
def post_groom_github_issues(request: GroomGithubIssuesRequest) -> GroomGithubIssuesResponse:
    """Create a job and start ``IssueGroomingWorkflow`` for a GitHub issue.

    Preconditions:
        - ``request`` names an issue via owner/repo/issue_number and provides a
          GitHub token either directly or through the configured environment.
    Postconditions:
        - A job record is created and tagged with the GitHub issue context. The
          resolved token is persisted encrypted (``github_token_encrypted``, same
          as ``post_run_from_github``) so ``run_issue_grooming_activity`` can
          resolve it from the job record rather than the environment -- never
          the plaintext token itself.
        - ``IssueGroomingWorkflow`` is started for the job. When dispatch fails
          (worker unreachable, start timeout), the freshly-created job row is
          marked failed instead of being left orphaned in 'pending', and the
          route raises a retryable 503.
    """
    token = resolve_github_token(request)

    job_id = str(uuid.uuid4())
    _main.create_job(job_id=job_id, repo_path=f"{request.owner}/{request.repo}")
    job_fields = {
        "job_type": "issue_grooming",
        "github_context": {
            "owner": request.owner,
            "repo": request.repo,
            "issue_number": request.issue_number,
        },
    }
    encrypted = encrypt_token(token)
    if encrypted:
        job_fields["github_token_encrypted"] = encrypted
    _main.update_job(job_id, **job_fields)

    try:
        start_issue_grooming_workflow(job_id, request.owner, request.repo, request.issue_number)
    except Exception as e:
        # Dispatch failed (worker not ready, start timeout, bad config). Mark the
        # freshly-created row failed so it is not orphaned in 'pending', and surface
        # a retryable error instead of an opaque 500 -- same shape as github.py's
        # post_run_from_github.
        logger.exception("Issue grooming Temporal dispatch failed: %s", e)
        _main.update_job(
            job_id, status=JobStatus.FAILED.value, error=f"Temporal dispatch failed: {e}"
        )
        raise HTTPException(
            status_code=503,
            detail="Temporal dispatch failed (worker unavailable); job marked failed. Retry.",
        ) from e

    return GroomGithubIssuesResponse(job_id=job_id, issue_number=request.issue_number)
