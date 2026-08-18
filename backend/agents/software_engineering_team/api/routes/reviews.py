"""coding_team API — PR-review routes: review-pr and reviews list."""

from __future__ import annotations

import asyncio
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
from software_engineering_team.code_review_agent import transcript
from software_engineering_team.github_source import (
    GitHubAPIError,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _fetch_pr_sync(token: str, request: ReviewPrRequest):
    """Synchronous helper: Fetch PR from GitHub."""
    with _main.GitHubClient(token=token) as client:
        try:
            return client.get_pull_request(request.owner, request.repo, request.pr_number)
        except GitHubAPIError as e:
            raise HTTPException(status_code=502, detail=f"github api error: {e}") from e


def _create_review_job_sync(request: ReviewPrRequest, pr_url: str) -> str:
    """Synchronous helper: Take admission lock and create the job."""
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
                "pr_url": pr_url,
            },
        )
        return job_id


@router.post("/review-pr", response_model=ReviewPrResponse)
async def post_review_pr(request: ReviewPrRequest) -> ReviewPrResponse:
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

    # 1. Offload the synchronous GitHub fetch to a background thread
    pr = await asyncio.to_thread(_fetch_pr_sync, token, request)

    # 2. Offload the synchronous admission lock and DB writes to a background thread
    job_id = await asyncio.to_thread(_create_review_job_sync, request, pr.html_url)

    # 3. Offload the start time recording (DB write) to a background thread
    created_at = await asyncio.to_thread(
        _main.record_review_start,
        job_id, request.owner, request.repo, request.pr_number, pr.html_url, _main._review_author()
    )
    
    # 4. Asynchronous Temporal Dispatch
    try:
        await _main._start_pr_review_temporal(job_id, request, token)
    except Exception as exc:
        # Mark the job as failed so UI polling doesn't hang forever
        # (Using to_thread since update_job is a synchronous DB call)
        await asyncio.to_thread(
            _main.update_job,
            job_id, 
            status="failed", 
            error=f"temporal dispatch failed: {exc}"
        )
        # Return a clean 503 instead of crashing the server
        raise HTTPException(
            status_code=503, 
            detail=f"review dispatch failed: {exc}"
        ) from exc

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
def fetch_review_transcript(
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
        - Returns the transcript's entries in call order. Returns 404 only
          when ``job_id`` names no known review; 409 on an ``owner``/``repo``
          mismatch. A known review with no ``code_review_transcripts`` row
          (never started, predates this feature, or — the review-level cache
          short-circuit, which excludes ``job_id`` from its key so an
          identical resubmission can hit it — made no LLM call at all) still
          returns 200 with an empty ``entries`` list rather than 404, so the
          UI's "View Transcript" action (shown for any terminal review) never
          errors on a review that legitimately has nothing to show. Entries
          still sitting in this process's in-memory buffer (a final ``drain()``
          that requeued after a Postgres blip) are included, so the one-shot
          dialog is not empty while the heartbeat retries. The durable read
          runs while holding the drain lock so an in-flight flush cannot
          clear the buffer after this query but before the snapshot, or
          commit after this query has already returned a stale list.
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
    entries = transcript.merge_unflushed(job_id, lambda: _main.get_review_transcript(job_id) or [])
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
