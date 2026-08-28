"""coding_team API — "address & respond to unresolved PR comments" execution.

Given an open pull request, this flow works through every UNRESOLVED review
comment that raises an issue and, for each, drives the software-engineering team
through the required steps:

  1. Read the comment to understand the issue it raises.
  2. Use the codebase to decide whether the comment is a FALSE POSITIVE or a REAL
     issue.
  3. For a real issue:
     1. Identify the requirements the fix must satisfy to be considered resolved.
     2. Identify the top-3 candidate solutions, each scored on how well it meets
        the requirements plus computational performance, memory usage, and
        expected code complexity.
     3. Plan the best-scoring solution.
     4. Implement the plan (dispatch the SE implementation pipeline).
     5. Go through the existing review processes.
     6. Commit and push the changes to the PR branch.
     7. Reply to the comment.
     8. Resolve the comment.
  4. Once every comment is handled, move the PR to "waiting for review".

Runs in a background thread (mirroring ``pr_review._run_pr_review``) so the HTTP
route returns immediately and the UI polls ``GET /status/{job_id}``. Every
monkeypatched collaborator is dereferenced through the ``coding_team_main``
module object at call time so tests can ``monkeypatch.setattr(_main, ...)``.

Contract summary (see per-function docstrings for detail):
  - Preconditions: a job row already exists for ``job_id``; ``token`` authorizes
    the PR's repository; ``request`` carries the target ``owner``/``repo``/
    ``pr_number``.
  - Postconditions: the job ends ``completed`` (even when there was nothing to
    do) or ``failed`` (only on an error that prevented the flow from running);
    per-comment failures degrade to a recorded outcome rather than failing the
    whole job. The background hook NEVER raises.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import AddressCommentsRequest
from software_engineering_team.github_source import (
    ReviewComment,
    ReviewThread,
    scrub_token_from_text,
)
from software_engineering_team.models import CodingTeamPlanInput, JobStatus

logger = logging.getLogger(__name__)

# Label Khala applies to a PR to mark it "waiting for review" once every
# unresolved comment has been addressed. GitHub has no native PR status for this,
# so it is a label (a PR is an issue in the REST API, so update_issue applies it).
WAITING_FOR_REVIEW_LABEL = "waiting for review"

# A review comment must actually raise an issue for this flow to act on it. A
# comment that is only a question, an acknowledgement ("thanks"), or otherwise
# not actionable is skipped. Comments Khala itself posted (carrying the marker)
# are also skipped so the flow never chases its own replies.
_KHALA_COMMENT_MARKER = "<!-- khala-generated -->"


# ---------------------------------------------------------------------------
# LLM schemas — triage a comment, then score candidate solutions
# ---------------------------------------------------------------------------


class CommentTriage(BaseModel):
    """LLM verdict on whether a review comment raises a real, actionable issue."""

    raises_issue: bool = Field(
        description="True when the comment identifies a concrete problem to fix (a bug, a missing "
        "requirement, a correctness/quality/security concern), as opposed to a question, a "
        "compliment, or a non-actionable remark."
    )
    is_false_positive: bool = Field(
        description="True when, after checking the cited code, the comment's concern does NOT hold "
        "for the actual codebase (the issue it describes is not real)."
    )
    issue_summary: str = Field(
        description="One or two sentences restating the concrete issue the comment raises, or why "
        "it is a false positive / not an issue."
    )


class SolutionCandidate(BaseModel):
    """One candidate solution, scored on the required dimensions (1-10 each)."""

    summary: str = Field(description="Short description of the approach.")
    requirement_fit: int = Field(
        description="How well this fulfils the resolution requirements (1-10, higher is better).",
        ge=1,
        le=10,
    )
    computational_performance: int = Field(
        description="Runtime performance of this approach (1-10, higher is better/faster).",
        ge=1,
        le=10,
    )
    memory_usage: int = Field(
        description="Memory efficiency of this approach (1-10, higher is leaner).", ge=1, le=10
    )
    code_complexity: int = Field(
        description="Expected code complexity (1-10, higher means SIMPLER/less complex).",
        ge=1,
        le=10,
    )

    @property
    def score(self) -> int:
        """Aggregate score used to rank candidates (sum of the four dimensions)."""
        return (
            self.requirement_fit
            + self.computational_performance
            + self.memory_usage
            + self.code_complexity
        )


class IssueResolutionPlan(BaseModel):
    """The SE team's resolution plan for one real issue raised by a comment."""

    requirements: List[str] = Field(
        default_factory=list,
        description="The requirements that must all hold for the issue to be considered resolved.",
    )
    candidate_solutions: List[SolutionCandidate] = Field(
        default_factory=list,
        description="The top candidate solutions (aim for 3), each scored on the four dimensions.",
    )
    chosen_plan: str = Field(
        description="The concrete implementation plan for the best-scoring solution."
    )


_TRIAGE_SYSTEM_PROMPT = (
    "You are a senior software engineer triaging a pull-request review comment. "
    "Decide whether the comment raises a real, actionable issue, and whether — after "
    "checking the actual code it refers to — that issue is a false positive. Be strict: "
    "only treat a comment as raising an issue when there is a concrete problem to fix."
)

_PLAN_SYSTEM_PROMPT = (
    "You are the software-engineering team resolving a real issue raised by a pull-request "
    "review comment. First state the requirements the fix must satisfy to be considered "
    "resolved. Then propose the top three candidate solutions and score each from 1-10 on: "
    "requirement fit, computational performance, memory usage, and (inverted) code complexity "
    "where a higher score means simpler code. Finally, write a concrete implementation plan for "
    "the highest-scoring solution."
)


# ---------------------------------------------------------------------------
# Per-comment outcome record (serialized into the job's review_summary)
# ---------------------------------------------------------------------------


class CommentOutcome(BaseModel):
    """What the flow did with one unresolved comment (recorded for the UI)."""

    comment_id: int
    path: str
    line: Optional[int] = None
    html_url: str = ""
    outcome: str = Field(
        description="One of: 'resolved' (real issue fixed & thread resolved), 'false_positive', "
        "'not_an_issue', 'failed'."
    )
    detail: str = ""


# ---------------------------------------------------------------------------
# Gather unresolved comments
# ---------------------------------------------------------------------------


def _unresolved_comments(
    client: Any, owner: str, repo: str, pr_number: int
) -> tuple[List[ReviewComment], Dict[int, ReviewThread]]:
    """Return the PR's unresolved review comments plus a comment-id → thread map.

    Preconditions:
        - ``client`` is a live :class:`GitHubClient`; ``pr_number`` names an open PR.
    Postconditions:
        - Returns ``(comments, thread_by_comment_id)`` where ``comments`` are the
          review comments whose thread GitHub reports as UNRESOLVED and which Khala
          did not itself author (marker check), in GitHub's response order, and
          ``thread_by_comment_id`` maps every such comment's id to its owning
          :class:`ReviewThread` (so the caller can resolve the thread later). A
          comment with no discoverable thread is treated as unresolved and included
          with no thread entry (it can still be replied to; only resolution needs
          the thread id).
    """
    all_comments = client.list_review_comments(owner, repo, pr_number)
    threads = client.list_review_threads(owner, repo, pr_number)

    thread_by_comment: Dict[int, ReviewThread] = {}
    resolved_ids: set[int] = set()
    for thread in threads:
        for cid in thread.comment_ids:
            thread_by_comment[cid] = thread
            if thread.is_resolved:
                resolved_ids.add(cid)

    unresolved: List[ReviewComment] = []
    for comment in all_comments:
        if comment.id in resolved_ids:
            continue
        if _KHALA_COMMENT_MARKER in (comment.body or ""):
            continue
        unresolved.append(comment)
    return unresolved, thread_by_comment


# ---------------------------------------------------------------------------
# LLM steps (routed through llm_service.generate_structured)
# ---------------------------------------------------------------------------


def _read_cited_code(client: Any, owner: str, repo: str, comment: ReviewComment, ref: str) -> str:
    """Read the file the comment points at (best-effort), for grounding the LLM.

    Postconditions:
        - Returns the cited file's text at ``ref`` (the PR head SHA) or "" when it
          cannot be read. Never raises — a missing file just means the triage runs
          on the comment alone.
    """
    if not comment.path:
        return ""
    try:
        content = client.get_file_contents(owner, repo, comment.path, ref)
    except Exception as e:  # noqa: BLE001 - grounding is best-effort
        logger.warning(
            "address-comments: could not read %s@%s: %s",
            comment.path,
            ref,
            scrub_token_from_text(str(e)),
        )
        return ""
    return content or ""


def _triage_comment(comment: ReviewComment, cited_code: str) -> CommentTriage:
    """Ask the LLM whether the comment raises a real issue or is a false positive.

    Postconditions:
        - Returns a :class:`CommentTriage`. Any LLM error degrades to a
          conservative verdict (``raises_issue=False``) so the flow skips a
          comment it could not analyze rather than acting on a guess.
    """
    prompt = (
        "## Review comment\n"
        f"File: {comment.path or '(none)'}\n"
        f"Line: {comment.line if comment.line is not None else '(file-level)'}\n\n"
        f"{comment.body}\n\n"
        "## Cited file content (may be empty if unavailable)\n"
        f"{cited_code[:20000] if cited_code else '(unavailable)'}\n\n"
        "Decide whether this comment raises a real, actionable issue, and whether that "
        "issue is a false positive given the actual code. Respond as JSON."
    )
    try:
        return _main.generate_structured(
            prompt,
            schema=CommentTriage,
            objective="Triage a PR review comment",
            system_prompt=_TRIAGE_SYSTEM_PROMPT,
            agent_key="code_review",
        )
    except Exception as e:  # noqa: BLE001 - degrade to "skip" on any LLM failure
        logger.warning(
            "address-comments: triage LLM call failed for comment %s: %s",
            comment.id,
            scrub_token_from_text(str(e)),
        )
        return CommentTriage(
            raises_issue=False,
            is_false_positive=False,
            issue_summary="Could not analyze this comment (triage unavailable).",
        )


def _plan_resolution(comment: ReviewComment, cited_code: str) -> Optional[IssueResolutionPlan]:
    """Produce requirements, top-3 scored solutions, and a plan for the best one.

    Postconditions:
        - Returns an :class:`IssueResolutionPlan` whose ``candidate_solutions`` are
          sorted best-first by :attr:`SolutionCandidate.score`, or ``None`` when the
          LLM planning step fails (the caller then records the comment as failed
          rather than implementing a plan it does not have).
    """
    prompt = (
        "## Real issue raised by a review comment\n"
        f"File: {comment.path or '(none)'}\n"
        f"Line: {comment.line if comment.line is not None else '(file-level)'}\n\n"
        f"{comment.body}\n\n"
        "## Cited file content (may be empty if unavailable)\n"
        f"{cited_code[:20000] if cited_code else '(unavailable)'}\n\n"
        "Identify the resolution requirements, the top THREE candidate solutions (each scored "
        "1-10 on requirement fit, computational performance, memory usage, and inverted code "
        "complexity), and a concrete implementation plan for the best-scoring one. Respond as JSON."
    )
    try:
        plan = _main.generate_structured(
            prompt,
            schema=IssueResolutionPlan,
            objective="Plan the resolution of a PR review comment",
            system_prompt=_PLAN_SYSTEM_PROMPT,
            agent_key="code_review",
        )
    except Exception as e:  # noqa: BLE001 - a planning failure fails only this comment
        logger.warning(
            "address-comments: planning LLM call failed for comment %s: %s",
            comment.id,
            scrub_token_from_text(str(e)),
        )
        return None
    # Rank candidates best-first so the chosen plan corresponds to the top score.
    plan.candidate_solutions.sort(key=lambda c: c.score, reverse=True)
    return plan


# ---------------------------------------------------------------------------
# Implement (dispatch the SE pipeline) + push
# ---------------------------------------------------------------------------


def _dispatch_implementation(
    job_id: str,
    request: AddressCommentsRequest,
    comment: ReviewComment,
    plan: IssueResolutionPlan,
    pr_head: str,
) -> None:
    """Hand the chosen plan to the SE implementation pipeline for the PR branch.

    Builds a :class:`CodingTeamPlanInput` describing the fix and starts the coding
    team's implementation workflow against the PR's head branch, so steps 4-6 of
    the flow (implement → existing review processes → commit & push to the PR) run
    inside the established pipeline rather than being re-implemented here.

    Postconditions:
        - Dispatches ``_main.start_coding_team_workflow`` with a plan input whose
          ``requirements_description`` captures the comment, the resolution
          requirements, and the chosen plan, and whose ``project_overview`` carries
          the PR/comment context (owner, repo, pr_number, head branch, comment id).
          Raises whatever the dispatch raises (the caller treats a dispatch failure
          as a per-comment failure, not a job failure).
    """
    requirements = "\n".join(f"- {r}" for r in plan.requirements) or "- (none stated)"
    description = (
        f"Address the following pull-request review comment on {request.owner}/{request.repo}"
        f"#{request.pr_number} (file {comment.path}, "
        f"line {comment.line if comment.line is not None else 'file-level'}):\n\n"
        f"{comment.body}\n\n"
        f"Resolution requirements:\n{requirements}\n\n"
        f"Implementation plan:\n{plan.chosen_plan}"
    )
    plan_input = CodingTeamPlanInput(
        requirements_title=f"Address review comment on PR #{request.pr_number}",
        requirements_description=description,
        repo_path=request.repo_path,
        project_overview={
            "pr_comment_resolution": {
                "owner": request.owner,
                "repo": request.repo,
                "pr_number": request.pr_number,
                "head_branch": pr_head,
                "comment_id": comment.id,
                "comment_url": comment.html_url,
            }
        },
    )
    _main.start_coding_team_workflow(
        job_id,
        request.repo_path,
        plan_input.model_dump(),
        github={
            "owner": request.owner,
            "repo": request.repo,
            "pr_number": request.pr_number,
            # Work on the PR's own branch so the fix commits & pushes back to the PR
            # (steps 4.6) rather than opening a new branch.
            "base": pr_head,
            "integration_branch": pr_head,
        },
    )


# ---------------------------------------------------------------------------
# Reply + resolve one comment
# ---------------------------------------------------------------------------


def _reply_and_resolve(
    client: Any,
    request: AddressCommentsRequest,
    comment: ReviewComment,
    thread: Optional[ReviewThread],
    reply_body: str,
) -> bool:
    """Reply to a review comment and resolve its thread (best-effort each).

    Postconditions:
        - Posts a threaded reply under ``comment`` and, when ``thread`` is known,
          resolves it. Returns True when BOTH the reply and (if attempted) the
          resolve succeeded; False otherwise. Never raises — a failure here is
          recorded as a per-comment outcome, not a job failure.
    """
    replied = False
    try:
        client.reply_to_review_comment(
            owner=request.owner,
            repo=request.repo,
            number=request.pr_number,
            comment_id=comment.id,
            body=scrub_token_from_text(reply_body),
        )
        replied = True
    except Exception as e:  # noqa: BLE001 - reply is best-effort
        logger.warning(
            "address-comments: failed to reply to comment %s: %s",
            comment.id,
            scrub_token_from_text(str(e)),
        )

    resolved = True
    if thread is not None:
        resolved = client.resolve_review_thread(thread.id)

    return replied and resolved


# ---------------------------------------------------------------------------
# Mark the PR "waiting for review"
# ---------------------------------------------------------------------------


def _mark_waiting_for_review(client: Any, owner: str, repo: str, pr_number: int) -> None:
    """Add the "waiting for review" label to the PR (best-effort).

    GitHub has no native "waiting for review" PR state, so this is a label. A PR is
    an issue in GitHub's REST API, so ``update_issue`` applies it; existing labels
    are preserved by merging (``update_issue`` replaces the full label set).

    Postconditions:
        - The PR carries ``WAITING_FOR_REVIEW_LABEL`` in addition to its existing
          labels. Never raises — the label is a convenience signal, so a failure to
          apply it does not fail the job (the comments are already addressed).
    """
    try:
        pr = client.get_pull_request(owner, repo, pr_number)
        merged = list(dict.fromkeys([*pr.labels, WAITING_FOR_REVIEW_LABEL]))
        client.update_issue(owner, repo, pr_number, labels=merged)
    except Exception as e:  # noqa: BLE001 - status label is best-effort
        logger.warning(
            "address-comments: could not mark PR %s/%s#%s waiting-for-review: %s",
            owner,
            repo,
            pr_number,
            scrub_token_from_text(str(e)),
        )


# ---------------------------------------------------------------------------
# Per-comment driver
# ---------------------------------------------------------------------------


def _handle_comment(
    client: Any,
    job_id: str,
    request: AddressCommentsRequest,
    comment: ReviewComment,
    thread: Optional[ReviewThread],
    pr_head: str,
) -> CommentOutcome:
    """Run the full triage → plan → implement → reply → resolve flow for one comment.

    Postconditions:
        - Returns the :class:`CommentOutcome` recording what happened. Never raises:
          any error becomes an ``outcome="failed"`` record so one bad comment cannot
          sink the job.
    """
    base = CommentOutcome(
        comment_id=comment.id,
        path=comment.path,
        line=comment.line,
        html_url=comment.html_url,
        outcome="failed",
    )
    try:
        cited_code = _read_cited_code(client, request.owner, request.repo, comment, pr_head)
        triage = _triage_comment(comment, cited_code)

        if not triage.raises_issue:
            base.outcome = "not_an_issue"
            base.detail = triage.issue_summary
            return base

        if triage.is_false_positive:
            # Real-looking comment, but the codebase shows the concern does not hold.
            # Reply explaining why and resolve the thread — no code change needed.
            reply = (
                "After reviewing the referenced code, this appears to be a false positive: "
                f"{triage.issue_summary}"
            )
            _reply_and_resolve(client, request, comment, thread, reply)
            base.outcome = "false_positive"
            base.detail = triage.issue_summary
            return base

        # Real issue: requirements → top-3 scored solutions → plan the best one.
        plan = _plan_resolution(comment, cited_code)
        if plan is None:
            base.detail = "Could not produce a resolution plan."
            return base

        # Implement the plan (dispatch the SE pipeline: implement → existing review
        # processes → commit & push to the PR branch).
        _dispatch_implementation(job_id, request, comment, plan, pr_head)

        # Reply to and resolve the comment.
        reply = f"Addressed by the software-engineering team. {plan.chosen_plan}"
        ok = _reply_and_resolve(client, request, comment, thread, reply)
        base.outcome = "resolved" if ok else "failed"
        base.detail = plan.chosen_plan if ok else "Reply/resolve step failed."
        return base
    except Exception as e:  # noqa: BLE001 - one comment's failure must not sink the job
        logger.warning(
            "address-comments: failed to handle comment %s: %s",
            comment.id,
            scrub_token_from_text(str(e)),
        )
        base.detail = scrub_token_from_text(str(e))
        return base


# ---------------------------------------------------------------------------
# Background hook + thread launcher
# ---------------------------------------------------------------------------


def _start_address_comments_thread(
    job_id: str, request: AddressCommentsRequest, token: str
) -> None:
    """Spawn the address-comments hook in a background thread.

    Indirection so tests can monkey-patch this to invoke the hook synchronously.
    """
    t = threading.Thread(
        target=_run_address_comments,
        args=(job_id, request, token),
        daemon=True,
    )
    t.start()


def _run_address_comments(job_id: str, request: AddressCommentsRequest, token: str) -> None:
    """Background hook: address every unresolved review comment on the PR.

    Postconditions:
        - On success the job ends ``completed`` with a ``review_summary`` listing
          each comment's outcome, and the PR is labelled "waiting for review".
        - On an error that prevents the flow from running (e.g. the initial GitHub
          reads fail), the job ends ``failed`` with a scrubbed error. Per-comment
          failures are recorded as outcomes and do NOT fail the job.
        - NEVER raises — the daemon thread cannot leave a job wedged.
    """
    owner, repo, pr_number = request.owner, request.repo, request.pr_number
    try:
        _main.update_job(
            job_id,
            status=JobStatus.RUNNING.value,
            phase="addressing_comments",
            status_text="Addressing unresolved review comments",
        )
        _main.update_review(
            job_id,
            status=JobStatus.RUNNING.value,
            status_text="Addressing unresolved review comments",
        )

        with _main.GitHubClient(token=token) as client:
            pr = client.get_pull_request(owner, repo, pr_number)
            unresolved, thread_by_comment = _unresolved_comments(client, owner, repo, pr_number)

            outcomes: List[CommentOutcome] = []
            for comment in unresolved:
                outcome = _handle_comment(
                    client,
                    job_id,
                    request,
                    comment,
                    thread_by_comment.get(comment.id),
                    pr.head,
                )
                outcomes.append(outcome)

            # Every unresolved comment handled → the PR is ready for another look.
            _mark_waiting_for_review(client, owner, repo, pr_number)

        summary = _build_summary(outcomes)
        _main.update_job(
            job_id,
            status=JobStatus.COMPLETED.value,
            phase="completed",
            status_text=summary["status_text"],
            github_pr_url=pr.html_url,
            review_summary=summary,
        )
        _main.update_review(
            job_id,
            status=JobStatus.COMPLETED.value,
            status_text=summary["status_text"],
            review_summary=summary,
            completed=True,
        )
    except Exception as e:  # noqa: BLE001 - the hook must never raise
        error = f"Failed to address comments: {scrub_token_from_text(str(e))}"
        logger.exception("address-comments job %s failed: %s", job_id, error)
        try:
            _main.update_job(job_id, status=JobStatus.FAILED.value, phase="completed", error=error)
            _main.update_review(
                job_id,
                status=JobStatus.FAILED.value,
                status_text="Failed to address comments",
                error=error,
                completed=True,
            )
        except Exception as inner:  # noqa: BLE001 - finalize is best-effort
            logger.warning(
                "address-comments job %s: could not record failure: %s",
                job_id,
                scrub_token_from_text(str(inner)),
            )


def _build_summary(outcomes: List[CommentOutcome]) -> Dict[str, Any]:
    """Fold per-comment outcomes into the job's ``review_summary`` shape.

    Postconditions:
        - Returns a dict carrying per-outcome counts, a human ``status_text``, and
          the serialized per-comment outcome list, suitable for the UI's status/
          review-summary rendering.
    """
    counts: Dict[str, int] = {}
    for o in outcomes:
        counts[o.outcome] = counts.get(o.outcome, 0) + 1
    resolved = counts.get("resolved", 0)
    total = len(outcomes)
    status_text = (
        f"Addressed {resolved}/{total} unresolved comment(s)"
        if total
        else "No unresolved comments to address"
    )
    return {
        "kind": "address_comments",
        "status_text": status_text,
        "total_comments": total,
        "counts": counts,
        "outcomes": [o.model_dump() for o in outcomes],
    }
