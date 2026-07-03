"""coding_team API — PR-review routes: review-pr and reviews list.

Handlers register on a module-local ``APIRouter`` that ``main`` mounts with
``app.include_router`` (absolute paths unchanged). Collaborators are dereferenced
through the ``main`` hub so patches applied to ``main`` still take effect.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import List, Optional

# Ensure backend/agents is on path for coding_team and job_service_client
from fastapi import APIRouter, HTTPException, Query

from coding_team.api import main as _main
from coding_team.api.models import (
    ReviewPrRequest,
    ReviewPrResponse,
    ReviewRunItem,
)
from coding_team.github_source import (
    GitHubAPIError,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/review-pr", response_model=ReviewPrResponse)
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

    # Validate the PR exists BEFORE taking the admission lock: the GitHub round-trip is
    # the slowest step, and keeping it outside the critical section keeps admission
    # serialization to two fast job-service writes.
    with _main.GitHubClient(token=token) as client:
        try:
            pr = client.get_pull_request(request.owner, request.repo, request.pr_number)
        except GitHubAPIError as e:
            raise HTTPException(status_code=502, detail=f"github api error: {e}") from e

    # Cross-worker idempotency: refuse a second review while one is already running for
    # this PR (the webhook's per-process delivery-id dedup can't see other workers; this
    # also covers the manual UI trigger). The admission lock makes scan + job creation
    # atomic — without it, two concurrent requests both pass the scan before either has
    # written the github_context that makes its job visible to the other.
    with _main._pr_review_admission(request.owner, request.repo, request.pr_number):
        running = _main._running_review_for_pr(request.owner, request.repo, request.pr_number)
        if running:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"review job {running} already running for {request.owner}/{request.repo}#{request.pr_number}"
                ),
            )

        job_id = str(uuid.uuid4())
        _main.create_job(job_id=job_id, repo_path=request.repo_path)
        _main.update_job(
            job_id,
            github_context={
                "owner": request.owner,
                "repo": request.repo,
                "pr_number": request.pr_number,
                "pr_url": pr.html_url,
            },
        )

    # Persist a row so the Code Review page can show this review's history (best-effort).
    _main.record_review_start(
        job_id, request.owner, request.repo, request.pr_number, pr.html_url, _main._review_author()
    )
    _main._start_pr_review_thread(job_id, request, token)
    return ReviewPrResponse(job_id=job_id, pr_number=request.pr_number, pr_url=pr.html_url)


@router.get("/reviews", response_model=List[ReviewRunItem])
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
    rows = _main.list_reviews(owner, repo, pr_number, limit=limit)
    return [ReviewRunItem.model_validate(row) for row in rows]
