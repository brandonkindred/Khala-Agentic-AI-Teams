"""coding_team API — PR-review routes: review-pr and reviews list."""

from __future__ import annotations

import asyncio
import itertools
import logging
import uuid
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import (
    CreateEnhancedIssuesRequest,
    CreateEnhancedIssuesResponse,
    CreateReviewIssuesRequest,
    CreateReviewIssuesResponse,
    EnhancedCreatedIssueItem,
    OutOfScopeProposalItem,
    OutOfScopeProposalsResponse,
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
    Issue,
    build_enhanced_issue_from_proposal,
    compute_complexity_score,
    duplicate_check_max_open_issues,
    scrub_token_from_text,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM-based duplicate detection for filing out-of-scope issues
# ---------------------------------------------------------------------------


class _SimilarityVerdict(BaseModel):
    """LLM response schema for issue-similarity determination."""

    is_duplicate: bool = Field(
        description="True if the proposed issue is substantially the same problem as one of the "
        "existing issues — i.e., filing it would create a duplicate."
    )
    matched_issue_number: Optional[int] = Field(
        default=None,
        description="The issue number of the existing issue that matches, or null if no match.",
    )
    reasoning: str = Field(
        description="Brief explanation of why this is or is not a duplicate.",
    )


_SIMILARITY_SYSTEM_PROMPT = """\
You are a GitHub issue triage agent. Your job is to determine whether a proposed \
new issue is a duplicate of any existing open issue in the repository.

Two issues are duplicates if they describe substantially the same underlying \
problem, bug, or improvement — even if they use different wording, different \
levels of detail, or are discovered in different files. Focus on the semantic \
meaning, not surface-level text similarity.

Consider issues as duplicates when:
- They describe the same bug or defect (even if found in different locations)
- They request the same enhancement or fix
- One is a more specific instance of a broader issue already filed
- They would be resolved by the same code change

Do NOT consider issues as duplicates when:
- They happen to be in the same file but describe different problems
- They share a category (e.g., both are "security") but address different concerns
- They have superficially similar titles but describe distinct issues
"""

_SIMILARITY_PROMPT_TEMPLATE = """\
## Proposed Issue

**Description:** {description}
**File:** {file_path}
**Category:** {category}
**Severity:** {severity}
**Suggestion:** {suggestion}

## Existing Open Issues

{existing_issues_text}

## Task

Is the proposed issue a duplicate of any of the existing open issues listed above? \
If yes, which issue number is it a duplicate of? Respond with JSON.
"""


def _format_existing_issues(issues: list[Issue], max_issues: int = 30) -> str:
    """Format existing issues into a text block for the LLM prompt."""
    if not issues:
        return "(no existing open issues)"
    lines: list[str] = []
    for issue in issues[:max_issues]:
        title = (issue.title or "").strip()
        # Truncate body to avoid blowing up the context window
        body = (issue.body or "").strip()
        if len(body) > 500:
            body = body[:500] + "..."
        labels_str = ", ".join(issue.labels) if issue.labels else "none"
        lines.append(
            f"### Issue #{issue.number}: {title}\n"
            f"**Labels:** {labels_str}\n"
            f"**Body:** {body}\n"
        )
    return "\n".join(lines)


def _find_similar_issue_via_llm(
    proposal: dict[str, Any], open_issues: list[Issue]
) -> Issue | None:
    """Use an LLM to determine if a proposal duplicates an existing open issue.

    Makes a single structured LLM call with the proposal details and a summary
    of existing open issues. The LLM decides whether the proposal is a duplicate
    and, if so, which existing issue it matches.

    Returns the matched Issue object, or None if no duplicate is found.
    Falls back to None (create new issue) on any LLM error.
    """
    if not open_issues:
        return None

    description = str(proposal.get("description") or "")
    file_path = str(proposal.get("file_path") or "")
    category = str(proposal.get("category") or "general")
    severity = str(proposal.get("severity") or "info")
    suggestion = str(proposal.get("suggestion") or "")

    existing_issues_text = _format_existing_issues(open_issues)

    prompt = _SIMILARITY_PROMPT_TEMPLATE.format(
        description=description,
        file_path=file_path,
        category=category,
        severity=severity,
        suggestion=suggestion,
        existing_issues_text=existing_issues_text,
    )

    try:
        from llm_service import generate_structured

        verdict = generate_structured(
            prompt,
            schema=_SimilarityVerdict,
            objective="determine if out-of-scope issue duplicates an existing GitHub issue",
            system_prompt=_SIMILARITY_SYSTEM_PROMPT,
            agent_key="code_review",
            temperature=0.0,
            correction_attempts=1,
        )
    except Exception:  # noqa: BLE001
        # Any LLM failure (not configured, parse error, etc.) degrades to
        # "no match found" — the issue will be created as new, which is the
        # safe default (a duplicate is better than a lost finding).
        logger.warning(
            "LLM similarity check failed; treating as no duplicate", exc_info=True
        )
        return None

    if not verdict.is_duplicate or verdict.matched_issue_number is None:
        return None

    # Find the matched issue object by number
    for issue in open_issues:
        if issue.number == verdict.matched_issue_number:
            return issue

    # LLM returned a number that doesn't match any issue we gave it — treat as no match
    logger.warning(
        "LLM returned issue #%d but it was not in the candidate list",
        verdict.matched_issue_number,
    )
    return None


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


# ---------------------------------------------------------------------------
# Out-of-scope issue proposals — aggregated across reviews for a repo
# ---------------------------------------------------------------------------


@router.get("/reviews/out-of-scope-issues", response_model=OutOfScopeProposalsResponse)
def get_out_of_scope_issues(
    owner: str,
    repo: str,
    limit: int = Query(default=500, ge=1, le=2000),
) -> OutOfScopeProposalsResponse:
    """Return all out-of-scope issue proposals across reviews for a repository.

    Aggregates ``pending_issue_proposals`` from every completed review in the
    repo, returning only unfiled proposals (those without ``issue_url``). Used
    by the Coding Team Issues tab.

    Preconditions:
        - ``owner``/``repo`` name a repository with at least one completed review.
    Postconditions:
        - Returns unfiled proposals newest-review-first. Each proposal carries
          the originating review's ``job_id`` and ``pr_number`` for provenance.
          Returns an empty list (not an error) when no reviews or proposals exist.
    """
    rows = _main.list_reviews(owner, repo, limit=limit)
    proposals: list[OutOfScopeProposalItem] = []
    total = 0

    for row in rows:
        summary = row.get("review_summary")
        if not isinstance(summary, dict):
            continue
        pending = summary.get("pending_issue_proposals")
        if not isinstance(pending, list):
            continue
        job_id = str(row.get("job_id") or "")
        pr_number = row.get("pr_number") or 0
        pr_url = row.get("pr_url") or ""

        for p in pending:
            if not isinstance(p, dict):
                continue
            total += 1
            # Skip already-filed proposals
            if p.get("issue_url"):
                continue
            proposals.append(
                OutOfScopeProposalItem(
                    id=str(p.get("id") or ""),
                    job_id=job_id,
                    pr_number=int(pr_number),
                    pr_url=pr_url or None,
                    severity=str(p.get("severity") or "info"),
                    category=str(p.get("category") or "general"),
                    file_path=str(p.get("file_path") or ""),
                    line=p.get("line") if isinstance(p.get("line"), int) else None,
                    description=str(p.get("description") or ""),
                    suggestion=str(p.get("suggestion") or ""),
                    locations=p.get("locations") or [],
                    issue_number=p.get("issue_number"),
                    issue_url=p.get("issue_url"),
                )
            )

    unfiled = len(proposals)
    return OutOfScopeProposalsResponse(
        owner=owner,
        repo=repo,
        proposals=proposals,
        total=total,
        unfiled=unfiled,
    )


@router.post("/reviews/out-of-scope-issues/file", response_model=CreateEnhancedIssuesResponse)
def post_file_out_of_scope_issues(
    request: CreateEnhancedIssuesRequest,
) -> CreateEnhancedIssuesResponse:
    """File selected out-of-scope proposals as enhanced GitHub issues.

    For each selected proposal, checks if a similar issue already exists in the
    repository. If found, merges the proposal into the existing issue (appends a
    comment). If not, creates a new enhanced GitHub issue with Fibonacci
    complexity scoring, acceptance criteria, dependencies, and desired outcome.

    Preconditions:
        - A GitHub token is configured (request body or ``GITHUB_TOKEN``).
        - ``proposal_ids`` are composite ids of the form ``"job_id:proposal_id"``.
    Postconditions:
        - For each valid proposal: creates or merges into a GitHub issue. Updates
          the proposal's ``issue_url``/``issue_number`` in the review store.
          Returns the created/merged issues and any per-proposal errors.
    """
    token = resolve_github_token(request)
    created: list[EnhancedCreatedIssueItem] = []
    errors: list[str] = []

    # Group proposal_ids by job_id for efficient loading
    proposals_by_job: dict[str, list[str]] = {}
    for composite_id in request.proposal_ids:
        parts = composite_id.split(":", 1)
        if len(parts) != 2:
            errors.append(f"Invalid proposal id format: {composite_id}")
            continue
        job_id, prop_id = parts
        proposals_by_job.setdefault(job_id, []).append(prop_id)

    try:
        with _main.GitHubClient(token=token) as client:
            # Fetch open issues once for duplicate detection
            try:
                cap = duplicate_check_max_open_issues()
                open_issues = list(
                    itertools.islice(client.list_open_issues(request.owner, request.repo), cap)
                )
            except GitHubAPIError:
                open_issues = []

            for job_id, prop_ids in proposals_by_job.items():
                # Load the review context
                review = _main.get_review(job_id)
                if review is None:
                    errors.append(f"Review not found: {job_id}")
                    continue

                # Validate repo match
                review_owner = str(review.get("owner") or "")
                review_repo = str(review.get("repo") or "")
                if (
                    review_owner.casefold() != request.owner.casefold()
                    or review_repo.casefold() != request.repo.casefold()
                ):
                    errors.append(f"Repo mismatch for review {job_id}")
                    continue

                summary = review.get("review_summary")
                if not isinstance(summary, dict):
                    errors.append(f"No review summary for {job_id}")
                    continue

                pending = summary.get("pending_issue_proposals")
                if not isinstance(pending, list):
                    errors.append(f"No proposals for {job_id}")
                    continue

                by_id = {str(p.get("id")): p for p in pending if isinstance(p, dict)}
                pr_number = review.get("pr_number") or 0
                pr_url = str(review.get("pr_url") or "")

                for prop_id in prop_ids:
                    composite_id = f"{job_id}:{prop_id}"
                    proposal = by_id.get(prop_id)
                    if proposal is None:
                        errors.append(f"Proposal not found: {composite_id}")
                        continue
                    if proposal.get("issue_url"):
                        # Already filed — skip
                        continue

                    try:
                        # Check for similar existing issue
                        match = _find_similar_issue_via_llm(proposal, open_issues)

                        if match is not None:
                            # Similar issue exists — update it with the new proposal's details.
                            # Build the updated body by appending the new occurrence to the
                            # existing issue body.
                            existing_body = match.body or ""
                            update_section = (
                                f"\n\n---\n\n"
                                f"## Additional Occurrence\n\n"
                                f"Found during code review of PR #{pr_number} ({pr_url}):\n\n"
                                f"- **Severity:** {proposal.get('severity', 'info')}\n"
                                f"- **Category:** {proposal.get('category', 'general')}\n"
                                f"- **Location:** `{proposal.get('file_path', 'unknown')}`\n"
                            )
                            locations = proposal.get("locations") or []
                            if len(locations) > 1:
                                update_section += f"- **Occurrences:** {len(locations)}\n"
                                for loc in locations:
                                    fp = str(loc.get("file_path") or "unknown")
                                    ln = loc.get("line")
                                    loc_text = f"`{fp}:{ln}`" if isinstance(ln, int) and ln > 0 else f"`{fp}`"
                                    loc_desc = str(loc.get("description") or "").strip()
                                    update_section += f"  - {loc_text} — {loc_desc or '_No description._'}\n"
                            update_section += (
                                f"\n**Description:** {proposal.get('description', '')}\n\n"
                                f"**Suggested fix:** {proposal.get('suggestion', 'N/A')}"
                            )
                            updated_body = existing_body + update_section

                            client.update_issue(
                                request.owner, request.repo, match.number,
                                body=scrub_token_from_text(updated_body),
                            )
                            # Mark the proposal as filed (merged into existing)
                            proposal["issue_number"] = match.number
                            proposal["issue_url"] = match.html_url

                            complexity = compute_complexity_score(proposal)
                            created.append(
                                EnhancedCreatedIssueItem(
                                    proposal_id=composite_id,
                                    issue_number=match.number,
                                    issue_url=match.html_url,
                                    title=match.title or "",
                                    label="",
                                    complexity_score=complexity["aggregate"],
                                    merged_into_existing=True,
                                )
                            )
                        else:
                            # Create new enhanced issue
                            title, body, label = build_enhanced_issue_from_proposal(
                                proposal, pr_number=int(pr_number), pr_url=pr_url
                            )
                            scrubbed_title = scrub_token_from_text(title)
                            scrubbed_body = scrub_token_from_text(body)

                            # Create the issue with the label
                            issue = client.create_issue(
                                request.owner,
                                request.repo,
                                title=scrubbed_title,
                                body=scrubbed_body,
                                labels=[label],
                            )
                            # Mark the proposal as filed
                            proposal["issue_number"] = issue.number
                            proposal["issue_url"] = issue.html_url

                            # Add to the open issues list so subsequent proposals
                            # can be matched against it
                            open_issues.append(issue)

                            complexity = compute_complexity_score(proposal)
                            created.append(
                                EnhancedCreatedIssueItem(
                                    proposal_id=composite_id,
                                    issue_number=issue.number,
                                    issue_url=issue.html_url,
                                    title=scrubbed_title,
                                    label=label,
                                    complexity_score=complexity["aggregate"],
                                    merged_into_existing=False,
                                )
                            )
                    except GitHubAPIError as e:
                        errors.append(f"GitHub error for {composite_id}: {e}")
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "Failed to file proposal %s: %s", composite_id, e, exc_info=True
                        )
                        errors.append(f"Error filing {composite_id}: {e}")

                # Persist updated proposals back to both stores (best-effort):
                # the in-memory job store (survives for the session) and the durable
                # review row (survives restarts).
                review_status = str(review.get("status") or "completed")
                try:
                    _main.update_job(job_id, review_summary=summary)
                except Exception:  # noqa: BLE001
                    pass  # job may have aged out; the review row is the durable copy
                try:
                    _main.update_review(
                        job_id,
                        status=review_status,
                        review_summary=summary,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Could not persist updated proposals for %s", job_id, exc_info=True
                    )

    except GitHubAPIError as e:
        raise HTTPException(status_code=502, detail=f"github api error: {e}") from e

    return CreateEnhancedIssuesResponse(created=created, errors=errors)
