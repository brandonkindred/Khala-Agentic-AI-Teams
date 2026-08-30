"""coding_team API — "address & respond to unresolved PR comments" execution.

Given an open pull request, this flow works through every UNRESOLVED review
comment that raises an issue and, for each, drives the software-engineering team
through the required steps:

  1. Read the comment to understand the issue it raises.
  2. Use the codebase to decide whether the comment is a FALSE POSITIVE or a REAL
     issue.
  3. For a real issue, plan the best of three scored solutions, run a distinct
     durable implementation workflow, wait for it to finish successfully, push
     the result to the existing PR branch, then reply and resolve the thread.
  4. For a false positive, reply with the evidence and resolve the thread.

Runs in a background thread (mirroring ``pr_review._run_pr_review``) so the HTTP
route returns immediately and the UI polls ``GET /status/{job_id}``. Every
monkeypatched collaborator is dereferenced through the ``coding_team_main``
module object at call time so tests can ``monkeypatch.setattr(_main, ...)``.

Contract summary (see per-function docstrings for detail):
  - Preconditions: a job row already exists for ``job_id``; ``token`` authorizes
    the PR's repository; ``request`` carries the target ``owner``/``repo``/
    ``pr_number``.
  - Postconditions: the job ends ``completed`` (even when there was nothing to
    do) or ``failed`` (on an error that prevented the flow from running, including
    review-thread state being unavailable — the flow fails closed rather than
    treating unknown state as unresolved). Per-comment failures degrade to a
    recorded outcome rather than failing the whole job. The background hook NEVER
    raises.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import AddressCommentsRequest
from software_engineering_team.github_source import (
    ReviewComment,
    ReviewThread,
    ReviewThreadsUnavailableError,
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


def _is_khala_authored(comment: ReviewComment, authenticated_login: str) -> bool:
    """True iff ``comment`` is trustworthy as Khala's own generated reply.

    Preconditions:
        - ``authenticated_login`` is the GitHub login the request's token
          authenticates as (:meth:`GitHubClient.get_authenticated_login`), or
          ``""`` when it could not be resolved.
    Postconditions:
        - Returns True only when BOTH ``comment.body`` carries
          ``_KHALA_COMMENT_MARKER`` AND ``comment.author`` case-insensitively
          matches ``authenticated_login`` (GitHub logins are
          case-insensitive). ``_KHALA_COMMENT_MARKER`` is a public literal
          string in ``body`` that any commenter could include, accidentally
          or deliberately, so it is never trusted as provenance on its own.
        - Returns False whenever ``authenticated_login`` is empty (identity
          could not be resolved) — fails closed to "not Khala's" rather than
          matching an unauthenticated marker against nothing.
    """
    if not authenticated_login:
        return False
    if _KHALA_COMMENT_MARKER not in (comment.body or ""):
        return False
    return (comment.author or "").casefold() == authenticated_login.casefold()


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
    outcome: Literal["resolved", "false_positive", "not_an_issue", "failed"] = Field(
        description="'resolved' (real issue fixed, pushed, replied to, and thread "
        "resolved), 'false_positive', 'not_an_issue', or 'failed'."
    )
    detail: str = ""


# ---------------------------------------------------------------------------
# Gather unresolved comments
# ---------------------------------------------------------------------------


def _unresolved_comments(
    client: Any, owner: str, repo: str, pr_number: int
) -> tuple[List[ReviewComment], Dict[int, ReviewThread], List[Tuple[str, int]], Dict[int, List[ReviewComment]]]:
    """Return the PR's unresolved review comments plus a comment-id → thread map.

    Preconditions:
        - ``client`` is a live :class:`GitHubClient`; ``pr_number`` names an open PR.
    Postconditions:
        - Returns ``(comments, thread_by_comment_id, retry_resolve_threads,
          thread_history_by_comment_id)``.
        - ``comments`` has AT MOST ONE entry per unresolved thread — its LATEST
          message in GitHub's response order — even when the thread carries
          multiple messages; a thread's replies share the same underlying issue,
          and the reply/resolve/publish flow operates on the thread as a whole,
          so handling every message separately would triage the same issue
          repeatedly and could dispatch a duplicate implementation workflow per
          extra message. Comments Khala did not itself author whose thread
          GitHub reports as UNRESOLVED are eligible — EXCEPT a thread whose
          LATEST message is Khala's own generated reply (see
          ``retry_resolve_threads`` below). A comment counts as Khala's own
          reply only when BOTH its ``body`` carries the marker AND its
          ``author`` matches the token's own authenticated login
          (:meth:`GitHubClient.get_authenticated_login`, resolved once per
          call) — the marker alone is a public literal string any commenter
          could include, accidentally or deliberately, so body content is
          never trusted as provenance on its own. Using the LATEST message
          (rather than always the thread's root) is deliberate: when a
          reviewer posts NEW feedback after Khala's reply (e.g. "this fix is
          incomplete"), that feedback is the current concern and must be
          re-triaged, never silently discarded in favor of auto-resolving on
          a stale root just because the thread once carried a Khala reply.
        - ``retry_resolve_threads`` lists ``(thread_id, khala_reply_comment_id)``
          for every UNRESOLVED thread whose LATEST message is a Khala-generated
          reply — i.e. the reply landed, no reviewer has said anything since
          (as of THIS snapshot), but the resolve mutation that should have
          followed it failed (or hasn't run yet). The caller retries ONLY the
          resolve step for these, never re-triage/re-implementation — but must
          still re-check the thread's LIVE state immediately before resolving
          (:func:`_thread_has_new_reviewer_feedback` with ``khala_reply_comment_id``
          as ``since_comment_id``), since a reviewer can post a follow-up in the
          window between this snapshot and the retry loop actually running.
        - ``thread_by_comment_id`` maps EVERY comment id appearing in ANY thread
          GitHub returned (resolved threads included) to its owning
          :class:`ReviewThread`. Callers only look up ids drawn from ``comments``,
          so the extra resolved entries are harmless; the map is deliberately not
          narrowed to the unresolved subset. If a REST review comment has no
          discoverable thread, thread state is incomplete and the function fails
          closed rather than guessing that the comment is unresolved.
        - ``thread_history_by_comment_id`` maps each ``comments`` entry's id to
          its thread's FULL message list in chronological order (root through
          the returned latest message, Khala's own prior replies included).
          A thread's latest message alone can be a context-dependent follow-up
          ("this is still broken", "use the other approach") that is
          unintelligible without the root concern and any earlier response —
          triage/planning ground their prompt on this full history, not the
          latest message in isolation.
        - Fails closed: :meth:`GitHubClient.list_review_threads` raises
          :class:`ReviewThreadsUnavailableError` when thread state is unknown or
          incomplete, and this function lets that propagate rather than treating
          unknown state as "all unresolved" (which would re-triage resolved
          discussions and post duplicate replies).
    """
    all_comments = client.list_review_comments(owner, repo, pr_number)
    # Raises ReviewThreadsUnavailableError on unknown/incomplete state — propagated
    # so the caller aborts instead of misclassifying resolved comments as unresolved.
    threads = client.list_review_threads(owner, repo, pr_number)

    # The marker is a public literal string in `body` — anyone who can comment
    # on the PR could include it, accidentally or deliberately — so it is only
    # trusted as proof of Khala's own authorship when it ALSO carries Khala's
    # authenticated identity. Resolved once per call; best-effort (matching
    # pr_review._fetch_pr_metadata's _get_login): a failure degrades to "",
    # which _is_khala_authored never matches, failing closed to "not Khala's"
    # (worst case: a redundant re-triage of an already-fixed comment) rather
    # than trusting an unauthenticated marker.
    try:
        authenticated_login = client.get_authenticated_login()
    except Exception as e:  # noqa: BLE001 - best-effort; never blocks the run
        logger.warning(
            "address-comments: could not resolve authenticated login for %s/%s#%s: %s",
            owner,
            repo,
            pr_number,
            scrub_token_from_text(str(e)),
        )
        authenticated_login = ""

    thread_by_comment_id: Dict[int, ReviewThread] = {}
    for thread in threads:
        for cid in thread.comment_ids:
            thread_by_comment_id[cid] = thread

    # Group every comment by its owning thread, preserving GitHub's
    # chronological response order, so we can tell whether a thread's LATEST
    # message is Khala's own reply (safe to only retry the resolve mutation)
    # or a reviewer's follow-up feedback posted after it (must be re-triaged,
    # never silently discarded just because the thread once carried a reply).
    thread_messages: Dict[str, List[ReviewComment]] = {}
    for comment in all_comments:
        thread = thread_by_comment_id.get(comment.id)
        if thread is None:
            if _is_khala_authored(comment, authenticated_login):
                # Khala's own reply is a best-effort lookup only; an orphaned
                # reply here (a gap in the thread listing) never blocks the
                # run — it is never itself a candidate for triage.
                continue
            raise ReviewThreadsUnavailableError(
                owner,
                repo,
                pr_number,
                f"review comment {comment.id} has no discoverable thread",
            )
        thread_messages.setdefault(thread.id, []).append(comment)

    unresolved: List[ReviewComment] = []
    retry_resolve_threads: List[Tuple[str, int]] = []
    thread_history_by_comment_id: Dict[int, List[ReviewComment]] = {}
    seen_thread_ids: set[str] = set()
    for thread in threads:
        if thread.is_resolved or thread.id in seen_thread_ids:
            continue
        seen_thread_ids.add(thread.id)
        messages = thread_messages.get(thread.id) or []
        if not messages:
            continue
        latest = messages[-1]
        if _is_khala_authored(latest, authenticated_login):
            # Nothing has been said since Khala's reply — the fix already
            # landed but GitHub still reports the thread unresolved, so the
            # resolve mutation failed previously. Retry resolving it only;
            # never re-triage a fix that already landed. The reply's own id
            # travels with the thread id so the caller can re-verify
            # freshness immediately before resolving.
            retry_resolve_threads.append((thread.id, latest.id))
            continue
        # `latest` is either the thread's only message so far, or newer
        # feedback a reviewer posted after an earlier Khala reply in the same
        # thread. Either way it is the current concern to triage.
        unresolved.append(latest)
        thread_history_by_comment_id[latest.id] = messages
    return unresolved, thread_by_comment_id, retry_resolve_threads, thread_history_by_comment_id


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


def _format_thread_history(thread_history: List[ReviewComment]) -> str:
    """Render a thread's messages as a chronological conversation transcript.

    Postconditions:
        - Returns one "Reviewer:" or "Khala:" labelled paragraph per message,
          oldest first (a message counts as Khala's own when its body carries
          ``_KHALA_COMMENT_MARKER`` — a display heuristic only, not the
          authenticated-author check :func:`_is_khala_authored` performs for
          security-relevant decisions). A single-message ``thread_history``
          still renders correctly (just that one message).
    """
    lines = []
    for msg in thread_history:
        speaker = "Khala" if _KHALA_COMMENT_MARKER in (msg.body or "") else "Reviewer"
        lines.append(f"{speaker}: {msg.body}")
    return "\n\n".join(lines)


class TriageUnavailableError(RuntimeError):
    """Raised when the triage LLM call itself fails (e.g. an LLM outage).

    Distinct from a genuine ``raises_issue=False`` verdict: the comment was
    never actually analyzed, so it must not be conflated with "not an issue"
    (which _handle_comment/_run_address_comments treat as a clean, countable
    success that can move the PR to "waiting for review" and reclaim the
    checkout).
    """


def _triage_comment(
    comment: ReviewComment, cited_code: str, thread_history: List[ReviewComment]
) -> CommentTriage:
    """Ask the LLM whether the comment raises a real issue or is a false positive.

    Preconditions:
        - ``thread_history`` is ``comment``'s owning thread's full chronological
          message list (see ``_handle_comment``'s precondition) — never empty.
    Postconditions:
        - Returns a :class:`CommentTriage` reflecting the LLM's actual verdict.
        - Raises :class:`TriageUnavailableError` on any LLM failure — it never
          fabricates a ``raises_issue=False`` verdict to paper over an outage.
          The comment was never analyzed, so it must surface as a FAILED
          outcome (via ``_handle_comment``'s outer exception handler), not a
          false "not an issue" success that could leave the underlying
          problem unaddressed while the PR is reported ready for review.
    """
    prompt = (
        "## Review comment\n"
        f"File: {comment.path or '(none)'}\n"
        f"Line: {comment.line if comment.line is not None else '(file-level)'}\n\n"
        "## Discussion thread (oldest to newest; the LAST message is the current concern "
        "to triage — earlier messages are context, e.g. what a short follow-up like "
        "\"still broken\" or \"use the other approach\" is referring to)\n"
        f"{_format_thread_history(thread_history)}\n\n"
        "## Cited file content (may be empty if unavailable)\n"
        f"{cited_code[:20000] if cited_code else '(unavailable)'}\n\n"
        "Decide whether the thread's LAST message raises a real, actionable issue, and "
        "whether that issue is a false positive given the actual code. Respond as JSON."
    )
    try:
        return _main.generate_structured(
            prompt,
            schema=CommentTriage,
            objective="Triage a PR review comment",
            system_prompt=_TRIAGE_SYSTEM_PROMPT,
            agent_key="code_review",
        )
    except Exception as e:  # noqa: BLE001 - convert to a distinguishable failure
        raise TriageUnavailableError(
            f"triage LLM call failed for comment {comment.id}: {scrub_token_from_text(str(e))}"
        ) from e


def _plan_resolution(
    comment: ReviewComment, cited_code: str, thread_history: List[ReviewComment]
) -> Optional[IssueResolutionPlan]:
    """Produce requirements, top-3 scored solutions, and a plan for the best one.

    Preconditions:
        - ``thread_history`` is ``comment``'s owning thread's full chronological
          message list (see ``_handle_comment``'s precondition) — never empty.
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
        "## Discussion thread (oldest to newest; the LAST message is the issue to resolve — "
        "earlier messages give it context)\n"
        f"{_format_thread_history(thread_history)}\n\n"
        "## Cited file content (may be empty if unavailable)\n"
        f"{cited_code[:20000] if cited_code else '(unavailable)'}\n\n"
        "Identify the resolution requirements for the thread's LAST message, the top THREE "
        "candidate solutions (each scored 1-10 on requirement fit, computational performance, "
        "memory usage, and inverted code complexity), and a concrete implementation plan for "
        "the best-scoring one. Respond as JSON."
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
# Implement, publish to the existing PR, and wait for success
# ---------------------------------------------------------------------------


def _pr_head_remote(owner: str, repo: str, pr: Any) -> Optional[str]:
    """Resolve the git remote to fetch/push the PR's head branch through.

    ``PullRequestDetail.head`` is only the branch's short ref, which is
    ambiguous for a fork-opened PR: the branch lives in the fork's repository,
    not ``owner/repo``, so fetching/pushing it against the literal string
    ``"origin"`` (the base repo's remote) either fails outright or silently
    touches an unrelated same-named branch in the base repo.

    Preconditions:
        - ``owner``/``repo`` are the PR's own (base) repository.
        - ``pr`` carries a ``head_repo_full_name`` attribute (see
          :class:`PullRequestDetail`).
    Postconditions:
        - Returns ``"origin"`` when the head branch lives in ``owner/repo``
          itself (the ordinary, same-repo case) — the checkout's existing
          origin remote is already correct.
        - Returns an HTTPS clone URL for the head repository when it differs
          from ``owner/repo`` (a fork-opened PR). A URL is valid anywhere git
          accepts a remote name, so the existing token-based auth (injected
          via env, not the URL) applies to it exactly as it does to "origin";
          whether the token can actually push there is GitHub's decision at
          push time (a fork typically must opt in via "Allow edits from
          maintainers"), so a permission failure surfaces as an ordinary push
          error rather than being pre-empted here.
        - Returns ``None`` when the head repository is unknown (GitHub reports
          no ``head.repo`` at all — the fork was deleted after the PR was
          opened). There is no remote to resolve in that case; the caller must
          fail closed rather than guess one.
    """
    head_repo_full_name = (pr.head_repo_full_name or "").strip()
    if not head_repo_full_name:
        return None
    if head_repo_full_name.casefold() == f"{owner}/{repo}".casefold():
        return "origin"
    return f"https://github.com/{head_repo_full_name}.git"


def _dispatch_implementation(
    parent_job_id: str,
    request: AddressCommentsRequest,
    comment: ReviewComment,
    plan: IssueResolutionPlan,
    pr_head: str,
    pr_base: str,
    pr_url: str,
    pr_remote: Optional[str],
    token: str,
) -> str:
    """Run one unique child workflow and return its child job id on success.

    The child workflow uses PR-specific publication: it prepares from the PR's
    base/head, runs the normal implementation and review pipeline, pushes the
    resulting commit to the existing head branch, and returns only after that
    publication is terminal. Any non-success result raises so the caller leaves
    the review thread open.

    Preconditions:
        - ``pr_remote`` is the remote to fetch/push the head branch through
          (see :func:`_pr_head_remote`) — ``"origin"`` for a same-repo PR, a
          fork clone URL for a fork PR, or ``None`` when it could not be
          resolved (the fork was deleted). ``None`` raises immediately rather
          than defaulting to "origin", which would silently target the wrong
          repository.
    """
    if pr_remote is None:
        raise RuntimeError(
            f"cannot determine the head repository for {request.owner}/{request.repo}"
            f"#{request.pr_number}'s branch {pr_head!r} (its fork appears to have been "
            "deleted); the fix cannot be published"
        )
    requirements = "\n".join(f"- {r}" for r in plan.requirements) or "- (none stated)"
    description = (
        f"Address the following pull-request review comment on {request.owner}/{request.repo}"
        f"#{request.pr_number} (file {comment.path}, "
        f"line {comment.line if comment.line is not None else 'file-level'}):\n\n"
        f"{comment.body}\n\nResolution requirements:\n{requirements}\n\n"
        f"Implementation plan:\n{plan.chosen_plan}"
    )
    plan_input = CodingTeamPlanInput(
        requirements_title=f"Address review comment {comment.id} on PR #{request.pr_number}",
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
    child_job_id = f"{parent_job_id}:comment:{comment.id}"
    _main.create_job(
        job_id=child_job_id,
        repo_path=request.repo_path,
        plan_input=plan_input.model_dump(),
    )
    child_fields: Dict[str, Any] = {
        "parent_job_id": parent_job_id,
        "github_context": {
            "owner": request.owner,
            "repo": request.repo,
            "pr_number": request.pr_number,
            "pr_url": pr_url,
            "review_comment_id": comment.id,
        },
    }
    encrypted = _main.encrypt_token(token)
    if encrypted:
        child_fields["github_token_encrypted"] = encrypted
    _main.update_job(child_job_id, **child_fields)

    result = _main.execute_coding_team_workflow(
        child_job_id,
        request.repo_path,
        plan_input.model_dump(),
        github={
            "owner": request.owner,
            "repo": request.repo,
            # Failure notices use the generic issue/PR-number field.
            "issue_number": request.pr_number,
            "pr_number": request.pr_number,
            "pr_url": pr_url,
            "publish_mode": "existing_pr",
            "base": pr_base,
            "integration_branch": pr_head,
            "remote": pr_remote,
        },
    )
    # Only an EXACT "completed" counts as success here — github_pr_publish_activity
    # reports "completed_with_failures" when some tasks landed but others didn't,
    # and that partial result must still leave the review thread open for retry
    # rather than being replied to and resolved over unfinished work.
    if result.get("status") != JobStatus.COMPLETED.value:
        status = result.get("status") or result.get("outcome") or "unknown"
        raise RuntimeError(f"implementation workflow did not complete successfully: {status}")
    return child_job_id


# ---------------------------------------------------------------------------
# Reply + resolve one comment
# ---------------------------------------------------------------------------


def _thread_has_new_reviewer_feedback(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    thread_id: str,
    since_comment_id: int,
    since_history: Optional[Sequence[ReviewComment]] = None,
) -> bool:
    """True iff ``thread_id`` now carries reviewer feedback newer than ``since_comment_id``.

    A long-running implementation workflow can take minutes to hours; a reviewer
    may post follow-up feedback (e.g. "this fix is incomplete") on the same
    thread while it runs — after the representative comment was snapshotted but
    before the thread is resolved. Re-checking live state here, right before
    resolving, catches that window; ``_unresolved_comments``'s own latest-message
    check only catches it on the NEXT run.

    Preconditions:
        - ``since_comment_id`` is the id of the comment this run triaged/replied
          to (the thread's representative comment at snapshot time).
        - ``since_history`` is EVERY message (chronological) that was actually
          shown to the LLM for triage/planning — normally the thread's full
          ``thread_history`` — at its snapshotted body (optional — omitting it
          disables the in-place-edit check below; ``[]``/``None`` behave the
          same). Triage/planning consume the WHOLE history, not just the
          representative comment, so an edit to an EARLIER message in it
          (e.g. a reviewer changing the requested approach in the root while
          leaving a later "still broken" reply untouched) is just as
          invalidating as an edit to the representative comment itself.
    Postconditions:
        - Returns True when the thread's CURRENT comment set (re-fetched live)
          contains EITHER (a) an id greater than ``since_comment_id`` that is
          not Khala's own authenticated reply — genuine new reviewer feedback
          the triage that ran never saw — OR (b) a live comment matching ANY
          id in ``since_history`` whose body no longer matches that entry's
          snapshotted body — GitHub retains a comment's id across an edit, so
          a reviewer who edits an ALREADY-triaged message in place (rather
          than posting a reply) would otherwise be invisible to the id-only
          check and get silently resolved over, whether that message was the
          representative comment or an earlier one in the same history — OR
          (c) the LIVE thread is already resolved: a reviewer can resolve a
          thread by hand (via the GitHub UI) while triage/planning/
          implementation for it is still running, which supersedes the
          in-flight work just as decisively as new feedback would —
          dispatching (or pushing) a fix for a concern the reviewer already
          closed is wasted work at best — OR (d) the thread can no longer be
          found at all in the live listing: a reviewer (or the PR author) can
          delete the representative comment or its whole thread while
          triage/planning/implementation is still running, same as resolving
          it by hand — a later reply attempt would fail anyway, but not
          before the implementation workflow has already been dispatched and
          pushed a fix for withdrawn feedback, so this must be caught BEFORE
          dispatch too, not just at the reply step. Returns False only when
          the thread is found, still open, and none of (a)/(b) hold. Fails
          OPEN (returns True) on any error: resolving a thread whose
          freshness could not be verified risks silently hiding real
          feedback, which is worse than a redundant re-check on the next run.
    """
    try:
        authenticated_login = client.get_authenticated_login()
    except Exception:  # noqa: BLE001 - best-effort; missing identity fails open below
        authenticated_login = ""
    try:
        threads = client.list_review_threads(owner, repo, pr_number)
        fresh_thread = next((t for t in threads if t.id == thread_id), None)
        if fresh_thread is None:
            # Deleted (or otherwise no longer discoverable) — treat exactly
            # like an already-resolved thread: superseded, nothing left to
            # protect by proceeding.
            return True
        if fresh_thread.is_resolved:
            return True
        all_comments = client.list_review_comments(owner, repo, pr_number)
        comments_by_id = {c.id: c for c in all_comments}
        for snapshotted in since_history or ():
            live = comments_by_id.get(snapshotted.id)
            if live is not None and (live.body or "") != (snapshotted.body or ""):
                return True
        for cid in fresh_thread.comment_ids:
            if cid <= since_comment_id:
                continue
            newer = comments_by_id.get(cid)
            if newer is not None and not _is_khala_authored(newer, authenticated_login):
                return True
        return False
    except Exception as e:  # noqa: BLE001 - fail open: never resolve on unverifiable state
        logger.warning(
            "address-comments: could not recheck thread %s for new feedback before resolving: %s",
            thread_id,
            scrub_token_from_text(str(e)),
        )
        return True


def _reply_and_resolve(
    client: Any,
    request: AddressCommentsRequest,
    comment: ReviewComment,
    thread: Optional[ReviewThread],
    reply_body: str,
    thread_history: Optional[Sequence[ReviewComment]] = None,
) -> bool:
    """Reply to a review comment's thread and resolve it.

    Preconditions:
        - ``thread_history``, when given, is every message actually shown to
          the LLM for this comment's triage/planning (see ``_handle_comment``'s
          precondition) — passed through to the freshness re-check so an edit
          to ANY of those messages (not just ``comment`` itself) is caught,
          not only an edit to the representative comment.
    Postconditions:
        - Posts a threaded reply and, when ``thread`` is known, resolves it.
          The reply targets the thread's ROOT comment
          (``thread.comment_ids[0]``, not ``comment.id``) when a thread is
          known: GitHub's create-reply endpoint requires the top-level
          comment id, and ``comment`` here may be a reviewer's later
          follow-up (``_unresolved_comments`` surfaces a thread's LATEST
          message, which is only sometimes its root) — replying against a
          non-root id would either be rejected by GitHub or land outside the
          expected thread. Falls back to ``comment.id`` only when no thread
          is known (should not normally happen for a real/false-positive
          outcome, since every triaged comment came from a known thread).
        - Returns True when BOTH the reply and (if attempted) the resolution
          succeeded; False otherwise. Never raises. The resolve is attempted
          ONLY when the reply itself succeeded — resolving after a failed
          reply would close the thread with no explanatory comment ever
          posted, and since a resolved thread is never re-triaged, the
          reviewer's concern would be silently dropped.
        - Checks the thread's LIVE state (:func:`_thread_has_new_reviewer_feedback`)
          BEFORE posting anything: a reviewer may have posted follow-up feedback
          on this thread while this comment's implementation workflow was
          running (between when ``comment`` was snapshotted and now). When
          newer, non-Khala feedback is found, this call posts NEITHER the reply
          NOR the resolution and reports failure — the check must run before the
          reply, not just before the resolve: a reply posted first would itself
          become the thread's new latest message, and since it carries Khala's
          own marker, the NEXT run's ``_unresolved_comments`` would then route
          the thread down the resolve-only retry path — never re-triaging the
          human feedback that prompted skipping the resolve in the first place.
          Leaving the thread exactly as found lets the next run's latest-message
          check correctly see the human's feedback as the thread's live latest
          message.
    """
    if thread is not None and _thread_has_new_reviewer_feedback(
        client,
        request.owner,
        request.repo,
        request.pr_number,
        thread.id,
        comment.id,
        thread_history if thread_history is not None else [comment],
    ):
        logger.info(
            "address-comments: skipping reply/resolve on thread %s — newer "
            "reviewer feedback appeared since comment %s was triaged",
            thread.id,
            comment.id,
        )
        return False

    reply_target_id = thread.comment_ids[0] if thread is not None and thread.comment_ids else comment.id
    replied = False
    try:
        client.reply_to_review_comment(
            owner=request.owner,
            repo=request.repo,
            number=request.pr_number,
            comment_id=reply_target_id,
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
    if replied and thread is not None:
        try:
            resolved = client.resolve_review_thread(thread.id)
        except Exception as e:  # noqa: BLE001 - resolve is best-effort; honor "never raises"
            logger.warning(
                "address-comments: failed to resolve thread %s: %s",
                thread.id,
                scrub_token_from_text(str(e)),
            )
            resolved = False
    elif thread is not None:
        # The reply failed: resolving now would close the thread with no
        # explanatory comment ever posted, and the next run would never
        # re-triage an already-resolved thread. Report failure without
        # attempting the resolve.
        resolved = False

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
    thread_history: List[ReviewComment],
    pr_head: str,
    pr_head_sha: str,
    pr_base: str,
    pr_url: str,
    pr_remote: Optional[str],
    token: str,
) -> CommentOutcome:
    """Run the full triage → implement → publish → reply → resolve flow.

    Preconditions:
        - ``thread_history`` is ``comment``'s owning thread's full message list
          in chronological order (root through ``comment`` itself), or at
          minimum ``[comment]`` when no fuller history is available — never
          empty, so triage/planning always have at least the comment itself
          to ground on.
    Postconditions:
        - Returns the :class:`CommentOutcome` recording what happened:
          ``not_an_issue`` (skipped), ``false_positive`` (replied and thread
          resolved only when both succeed), or ``resolved`` (implementation
          workflow completed, the fix was pushed to the PR, then the reply and
          resolution succeeded). Never raises; one comment's failure is recorded.
    """
    base = CommentOutcome(
        comment_id=comment.id,
        path=comment.path,
        line=comment.line,
        html_url=comment.html_url,
        outcome="failed",
    )
    try:
        # _read_cited_code's own contract calls for the PR head SHA (resolvable
        # via the base repo's contents API regardless of which repository the
        # branch itself lives in), NOT the branch short name (pr_head): for a
        # fork-opened PR, `pr_head` names a branch that may not exist in the
        # base repo at all, or may coincidentally collide with an unrelated
        # branch there, either way grounding triage on the wrong (or no) code.
        cited_code = _read_cited_code(client, request.owner, request.repo, comment, pr_head_sha)
        triage = _triage_comment(comment, cited_code, thread_history)

        if not triage.raises_issue:
            base.outcome = "not_an_issue"
            base.detail = triage.issue_summary
            return base

        if triage.is_false_positive:
            # Real-looking comment, but the codebase shows the concern does not hold.
            # Reply explaining why and resolve the thread — no code change is owed.
            # Only report success when BOTH the reply and the resolve land, so a
            # silently-open thread is never advertised as a handled false positive.
            reply = (
                "After reviewing the referenced code, this appears to be a false positive: "
                f"{triage.issue_summary}"
            )
            ok = _reply_and_resolve(client, request, comment, thread, reply, thread_history)
            base.outcome = "false_positive" if ok else "failed"
            base.detail = triage.issue_summary if ok else "Reply/resolve step failed."
            return base

        # Real issue: requirements → top-3 scored solutions → plan the best one.
        plan = _plan_resolution(comment, cited_code, thread_history)
        if plan is None:
            base.detail = "Could not produce a resolution plan."
            return base

        # Re-check the thread's LIVE state right before dispatching the
        # implementation workflow — not just before the reply/resolve step
        # that follows it. Triage and planning above can themselves take a
        # while (LLM round-trips), and a reviewer may post a follow-up (e.g.
        # a different desired approach) on this same thread in that window.
        # _reply_and_resolve's own freshness check runs too late to prevent
        # this: by then the workflow would have already implemented and
        # pushed a fix for the stale snapshot. Skip dispatch entirely here so
        # nothing lands on the PR branch for an already-superseded concern;
        # the thread stays open and the next run's _unresolved_comments picks
        # up the reviewer's actual latest message.
        if thread is not None and _thread_has_new_reviewer_feedback(
            client, request.owner, request.repo, request.pr_number, thread.id, comment.id, thread_history
        ):
            base.detail = (
                "Newer reviewer feedback appeared on this thread before implementation "
                "was dispatched; skipped to avoid pushing a fix for a superseded comment."
            )
            return base

        child_job_id = _dispatch_implementation(
            job_id,
            request,
            comment,
            plan,
            pr_head,
            pr_base,
            pr_url,
            pr_remote,
            token,
        )
        reply = (
            f"Addressed by the software-engineering team in job `{child_job_id}`. "
            f"{plan.chosen_plan}"
        )
        ok = _reply_and_resolve(client, request, comment, thread, reply, thread_history)
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


# ---------------------------------------------------------------------------
# Public API for route consumption
# ---------------------------------------------------------------------------
# The route module (``api/routes/reviews.py``) is a separate module, so it must
# not reach into this module's underscore-prefixed internals. These thin public
# aliases are the stable surface the route depends on; the underscore functions
# remain the package-internal implementation (and the monkeypatch surface tests
# use). Keeping the alias lets the internals be refactored without touching the
# route.


def unresolved_comments(
    client: Any, owner: str, repo: str, pr_number: int
) -> tuple[List[ReviewComment], Dict[int, ReviewThread], List[str]]:
    """Public entry point for :func:`_unresolved_comments` (see its contract)."""
    return _unresolved_comments(client, owner, repo, pr_number)


def start_address_comments_thread(job_id: str, request: AddressCommentsRequest, token: str) -> None:
    """Public entry point for :func:`_start_address_comments_thread` (see its contract)."""
    _start_address_comments_thread(job_id, request, token)


def _run_address_comments(job_id: str, request: AddressCommentsRequest, token: str) -> None:
    """Background hook: address every unresolved review comment on the PR.

    Postconditions:
        - On success the job ends ``completed`` with a ``review_summary`` listing
          each comment's outcome, and the PR is labelled "waiting for review".
        - On an error that prevents the flow from running (e.g. the initial GitHub
          reads fail), the job ends ``failed`` with a scrubbed error. Per-comment
          failures are recorded as outcomes and do NOT fail the job.
        - Runs a continuous background heartbeat for the job's whole duration (see
          ``_REVIEW_HEARTBEAT_INTERVAL_S``), matching the ordinary PR-review
          worker: ``_dispatch_implementation`` can now block for hours waiting on
          — and reattaching to — a single comment's implementation workflow (see
          ``execute_coding_team_workflow``'s ``reattach_on_timeout``), and without
          a continuous beat ``_running_review_for_pr`` would see this job's
          heartbeat go stale and admit a duplicate run on the same per-PR
          checkout while this one is still legitimately working.
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

        # Continuous liveness beat for the admission guard (mirrors
        # _run_pr_review's review_hb): job updates only land at phase
        # transitions, and dispatching one comment's implementation can block
        # for a very long time — without this, a perfectly healthy run would
        # look heartbeat-stale to _running_review_for_pr. The context manager
        # guarantees the beat stops on every exit path; on_error keeps a
        # job-service blip from killing the beat thread (or the run).
        from shared.concurrency import BackgroundHeartbeat  # noqa: I001, PLC0415 - keep module import light

        address_hb = BackgroundHeartbeat(
            lambda: _main.heartbeat_job(job_id),
            _main._REVIEW_HEARTBEAT_INTERVAL_S,
            name=f"address-comments-heartbeat-{job_id}",
            beat_first=True,
            on_error=lambda exc: logger.warning(
                "address-comments heartbeat error for job %s: %s",
                job_id,
                scrub_token_from_text(str(exc)),
            ),
        )
        with address_hb, _main.GitHubClient(token=token) as client:
            pr = client.get_pull_request(owner, repo, pr_number)
            pr_remote = _pr_head_remote(owner, repo, pr)
            unresolved, thread_by_comment_id, retry_resolve_threads, thread_history_by_comment_id = (
                _unresolved_comments(client, owner, repo, pr_number)
            )

            # A thread already carrying a Khala-generated reply was already
            # implemented and published; only the resolve mutation is retried
            # here — never re-triage/re-implement it. But the snapshot backing
            # `retry_resolve_threads` can go stale between when it was taken
            # and this loop actually running (an earlier retry/comment in this
            # same run can take a while), so re-verify the thread's LIVE state
            # right before resolving — the same freshness check `_reply_and_
            # resolve` runs before a fresh reply — rather than blindly
            # resolving over a reviewer's follow-up. resolve_review_thread is
            # itself best-effort (never raises, returns False on failure), so
            # a still-failing retry just leaves the thread open for the next
            # run to retry again.
            retry_resolve_ok = True
            for thread_id, khala_reply_id in retry_resolve_threads:
                if _thread_has_new_reviewer_feedback(
                    client, owner, repo, pr_number, thread_id, khala_reply_id
                ):
                    logger.info(
                        "address-comments: skipping resolve-retry on thread %s — newer "
                        "reviewer feedback appeared since generated reply %s was snapshotted",
                        thread_id,
                        khala_reply_id,
                    )
                    retry_resolve_ok = False
                    continue
                if not client.resolve_review_thread(thread_id):
                    retry_resolve_ok = False
                    logger.warning(
                        "address-comments: retry-resolve failed for thread %s", thread_id
                    )

            outcomes: List[CommentOutcome] = []
            for comment in unresolved:
                # Refresh PR metadata before each comment: an earlier comment's
                # real-issue workflow may have already pushed a new head commit,
                # and grounding this comment's triage on the stale `pr.head_sha`
                # captured before the loop would cite code from before that push.
                # Best-effort — a refresh failure degrades to the last known `pr`
                # rather than failing comments that haven't even started yet.
                try:
                    pr = client.get_pull_request(owner, repo, pr_number)
                except Exception as e:  # noqa: BLE001 - refresh is best-effort
                    logger.warning(
                        "address-comments: could not refresh PR metadata before comment %s: %s",
                        comment.id,
                        scrub_token_from_text(str(e)),
                    )
                outcome = _handle_comment(
                    client,
                    job_id,
                    request,
                    comment,
                    thread_by_comment_id.get(comment.id),
                    thread_history_by_comment_id.get(comment.id, [comment]),
                    pr.head,
                    pr.head_sha,
                    pr.base,
                    pr.html_url,
                    pr_remote,
                    token,
                )
                outcomes.append(outcome)

            # Every comment handled without failure AND every retry-resolve
            # succeeded: nothing is still owed to the reviewer, AS FAR AS THIS
            # RUN'S INITIAL SNAPSHOT SAW.
            all_succeeded = retry_resolve_ok and all(o.outcome != "failed" for o in outcomes)
            if all_succeeded:
                # A reviewer can open a brand-new thread — or resolve/reply to an
                # existing one — while an earlier comment's implementation
                # workflow was still running, after this run's initial
                # `_unresolved_comments` snapshot was taken. Such a thread
                # appears in neither `outcomes` nor `retry_resolve_threads`, so
                # the check above alone would declare the run fully successful
                # over feedback that was never triaged. Re-list live state as
                # the actual authority for "nothing left owed" before labelling
                # the PR ready or reclaiming its checkout.
                #
                # A comment this run deliberately triaged as `not_an_issue` is
                # NEVER resolved (there is nothing to fix or reply to), so it
                # legitimately reappears in the fresh re-list every time —
                # that is expected, not "still owed", and must not block
                # success (or the PR could never reach waiting-for-review, and
                # every future run would re-triage the same non-issue
                # forever). Only count a fresh-unresolved comment as blocking
                # when it is NOT one of this run's own known non-issues with
                # an UNCHANGED body — a reviewer editing that comment (or
                # posting genuinely new feedback) still blocks, same as ever.
                not_an_issue_ids = {o.comment_id for o in outcomes if o.outcome == "not_an_issue"}
                triaged_bodies = {c.id: c.body for c in unresolved}
                try:
                    fresh_unresolved, _tbc, fresh_retry, _hist = _unresolved_comments(
                        client, owner, repo, pr_number
                    )
                    blocking = [
                        c
                        for c in fresh_unresolved
                        if not (c.id in not_an_issue_ids and c.body == triaged_bodies.get(c.id))
                    ]
                    all_succeeded = not blocking and not fresh_retry
                except Exception as e:  # noqa: BLE001 - fail closed: unverifiable state must not read as success
                    logger.warning(
                        "address-comments: could not re-list unresolved threads before "
                        "declaring the run successful: %s",
                        scrub_token_from_text(str(e)),
                    )
                    all_succeeded = False
            # Move the PR to "waiting for review" only on a fully successful run —
            # a failed comment or a still-open retried thread means work is owed,
            # so the PR is not yet ready for another look. A run consisting SOLELY
            # of successful resolve-only retries (outcomes empty, retry_resolve_
            # thread_ids non-empty) still did real work and must be labelled too —
            # only a true no-op run (neither outcomes nor retries) skips this,
            # matching the original intent for a PR that never had comments.
            if (outcomes or retry_resolve_threads) and all_succeeded:
                _mark_waiting_for_review(client, owner, repo, pr_number)

        # Drop the per-PR clone only on a clean completion (nothing failed, no
        # unresolved comments left owed) — mirrors the issue-driven flow's
        # _publish_merged_work: cleanup runs BEFORE the terminal status update
        # so the job stays in list_jobs(active_only=True) during the rmtree,
        # and a quick same-PR retry is rejected by the admission guard instead
        # of racing a fresh clone into a directory mid-rmtree.
        if all_succeeded and request.cleanup_checkout_on_success:
            _main._cleanup_issue_checkout(request.repo_path)

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
        - Deliberately covers ``outcomes`` only: a successful resolve-only retry
          (see ``retry_resolve_threads`` in ``_unresolved_comments``) never
          produces a ``CommentOutcome`` — a retry has only a ``thread_id``, not
          the comment metadata (path/line/html_url) ``CommentOutcome`` requires —
          so it is invisible to ``counts``/``total_comments`` here even though it
          did real work. That real work is still surfaced separately: a
          successful retry-only run still moves the PR to "waiting for review"
          (see ``_run_address_comments``'s ``all_succeeded``/``retry_resolve_ok``
          handling).
    """
    counts: Dict[str, int] = {}
    for o in outcomes:
        counts[o.outcome] = counts.get(o.outcome, 0) + 1
    total = len(outcomes)
    # "Handled" = everything the job acted on without failing.
    handled = (
        counts.get("resolved", 0) + counts.get("false_positive", 0) + counts.get("not_an_issue", 0)
    )
    status_text = (
        f"Handled {handled}/{total} unresolved comment(s)"
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
