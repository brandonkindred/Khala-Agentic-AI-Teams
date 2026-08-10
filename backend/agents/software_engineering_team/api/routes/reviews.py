"""coding_team API — PR-review routes: review-pr and reviews list."""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import (
    CreateReviewIssuesRequest,
    CreateReviewIssuesResponse,
    ReviewPrRequest,
    ReviewPrResponse,
    ReviewRunItem,
    TranscriptEntry,
    TranscriptResponse,
)
from software_engineering_team.api.routes._common import resolve_github_token
from software_engineering_team.github_source import (
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
    token = resolve_github_token(request)

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
    # The returned server-clock start time is surfaced on the response so the UI computes
    # a live duration on one clock (this start + the completion from job status).
    created_at = _main.record_review_start(
        job_id, request.owner, request.repo, request.pr_number, pr.html_url, _main._review_author()
    )
    _main._start_pr_review_thread(job_id, request, token)
    return ReviewPrResponse(
        job_id=job_id, pr_number=request.pr_number, pr_url=pr.html_url, created_at=created_at
    )


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


@router.get("/reviews/{job_id}/transcript", response_model=TranscriptResponse)
def get_review_transcript(
    job_id: str,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
) -> TranscriptResponse:
    """Return one review's full durable transcript (every LLM call it made).

    Preconditions:
        - ``job_id`` names a review that was started via ``POST /review-pr``.
        - ``owner``/``repo``, when both supplied, are checked (case-insensitively)
          against the stored review's repository — the same repo-mismatch guard
          ``POST /reviews/{job_id}/issues`` applies — so a job id belonging to a
          different (PAT-accessible) repository is refused rather than leaking
          its transcript.
    Postconditions:
        - Returns the transcript's entries in call order. Returns 404 when
          ``job_id`` names no known review, or when the review is known but has
          not yet recorded a transcript (never started, or predates this
          feature); 409 on an ``owner``/``repo`` mismatch.
    """
    review = _main.get_review(job_id)
    if review is None:
        raise HTTPException(status_code=404, detail=f"no review found for job {job_id}")
    if owner is not None and repo is not None:
        if review["owner"].lower() != owner.lower() or review["repo"].lower() != repo.lower():
            raise HTTPException(
                status_code=409,
                detail="The requested repository does not match the reviewed repository.",
            )
    entries = _main.get_review_transcript(job_id)
    if entries is None:
        raise HTTPException(status_code=404, detail=f"no transcript recorded for job {job_id}")
    return TranscriptResponse(
        job_id=job_id, entries=[TranscriptEntry.model_validate(e) for e in entries]
    )


@router.post("/reviews/{job_id}/issues", response_model=CreateReviewIssuesResponse)
def post_create_review_issues(
    job_id: str, request: CreateReviewIssuesRequest
) -> CreateReviewIssuesResponse:
    """Open GitHub issues for the selected pre-existing findings of a review.

    A PR review does not comment on bugs it finds in pre-existing, unchanged code;
    it collects them as ``pending_issue_proposals`` on the review summary. This
    endpoint lets a human turn the ones they choose into real GitHub issues.

    Preconditions:
        - A GitHub token is configured (request body or ``GITHUB_TOKEN``).
        - ``job_id`` names a completed review; ``proposal_ids`` are ids from its
          pending proposals.
    Postconditions:
        - Files one GitHub issue per selected, not-yet-filed proposal (with the
          finding's full detail) in the reviewed repository, records the issue
          number/url on the proposal, and returns the created issues plus the
          updated proposal list. Idempotent per proposal — a proposal already
          filed is skipped, and an unknown id is ignored. Returns 404 when the
          review is unknown, 400 when no token is configured, 409 when the
          requested owner/repo do not match the reviewed repository, and 502 on a
          GitHub API failure.
    """
    token = resolve_github_token(request)
    try:
        data = _main.create_review_issues(
            job_id,
            request.proposal_ids,
            token,
            expected_owner=request.owner,
            expected_repo=request.repo,
        )
    except _main.ReviewNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"no review found for job {job_id}") from e
    except _main.RepoMismatchError as e:
        # Log the detailed mismatch server-side (owner/repo), but keep the actual
        # repository name off the client-facing response — the caller already knows
        # what repo they asked for, and echoing back which repo a job_id belongs to
        # would let a caller enumerate job_ids to learn repository names.
        logger.warning("repo mismatch for job %s: %s", job_id, e)
        raise HTTPException(
            status_code=409,
            detail="The requested repository does not match the reviewed repository.",
        ) from e
    except GitHubAPIError as e:
        raise HTTPException(status_code=502, detail=f"github api error: {e}") from e
    return CreateReviewIssuesResponse.model_validate(data)
