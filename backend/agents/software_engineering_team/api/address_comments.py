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
import re
import threading
from typing import Any, Dict, List, Literal, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, Field

from shared.concurrency import BackgroundHeartbeat
from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import AddressCommentsRequest
from software_engineering_team.github_source import (
    GitHubClient,
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

# Embedded in every reply this flow posts (in addition to `_KHALA_COMMENT_MARKER`,
# which only proves authorship): records the highest comment id in the thread's
# history that was actually shown to the LLM when this specific reply was
# generated. Comment ids are assigned at creation and don't necessarily match a
# thread's GitHub-response chronological order under a race — a reviewer's
# follow-up (H) can end up with a LOWER id than Khala's own reply (R) even though
# H predates R chronologically. `_unresolved_comments` classifies a thread by its
# chronologically-latest message, so id order alone can't tell a thread where R
# genuinely accounted for everything before it apart from one where H slipped in
# too late to be reflected in R's content but too "early" (by id) to trip an
# id-only "is there anything newer than R" check. Embedding the boundary in the
# reply itself — on GitHub, which already persists it durably per-thread — answers
# that without a new datastore: any non-Khala message with an id greater than this
# value was NOT part of what generated the reply, regardless of chronological
# position relative to the reply itself.
_KHALA_ACCOUNTED_THROUGH_RE = re.compile(r"<!--\s*khala-accounted-through:(\d+)\s*-->")


def _accounted_through_marker(comment_id: int) -> str:
    """Hidden HTML-comment marker recording the id a Khala reply accounted through."""
    return f"<!-- khala-accounted-through:{comment_id} -->"


def _parse_accounted_through(body: str) -> Optional[int]:
    """Extract the id embedded by :func:`_accounted_through_marker`, if present.

    Postconditions:
        - Returns the embedded integer id when ``body`` carries the marker.
        - Returns None when the marker is absent or malformed — notably for
          any reply this flow posted BEFORE this marker existed, so callers
          must treat None as "unknown provenance", not "accounted for nothing".
    """
    match = _KHALA_ACCOUNTED_THROUGH_RE.search(body or "")
    return int(match.group(1)) if match else None


# Cap on cited file content included in an LLM prompt (triage and planning both use
# this), to bound prompt size regardless of the actual file's length.
_MAX_CITED_CODE_CHARS = 20000

# Cap on a rendered thread-history transcript included in an LLM prompt (triage
# and planning both use this, via _format_thread_history), to bound prompt size
# regardless of how many messages (up to 100) or how long any one of them is.
_MAX_THREAD_HISTORY_CHARS = 20000

# Non-terminal coding-team job statuses: a job in one of these states may still be
# actively running (or was, until its worker crashed/restarted without terminalizing
# it). Used by `_dispatch_implementation` to refuse overwriting an existing job for
# the same comment-scoped id rather than blindly resetting possibly-live state.
_ACTIVE_JOB_STATUSES = frozenset(
    {JobStatus.PENDING.value, JobStatus.RUNNING.value, JobStatus.WAITING_FOR_USER.value}
)

# Named pieces of _unresolved_comments' return type, factored out so the function's
# own signature doesn't carry one unreadable nested 5-tuple annotation.
RetryResolveEntry = Tuple[str, int]
# Same shape as RetryResolveEntry (thread_id, khala_reply_comment_id), but for a
# thread `_unresolved_comments` could NOT confirm is safe to retry-resolve (no
# persisted evidence its own resolve mutation failed) — see `ambiguous_threads`
# below. Named separately so a caller can never accidentally feed one of these
# into the retry-resolve path meant only for confirmed RetryResolveEntry values.
AmbiguousThreadEntry = Tuple[str, int]
ThreadHistoryByCommentId = Dict[int, List[ReviewComment]]
UnresolvedCommentsResult = Tuple[
    List[ReviewComment],
    Dict[int, ReviewThread],
    List[RetryResolveEntry],
    ThreadHistoryByCommentId,
    List[AmbiguousThreadEntry],
]


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
    """LLM verdict on whether a review comment raises a real, actionable issue.

    ``raises_issue``/``is_false_positive`` map to exactly three outcomes
    :func:`_handle_comment` acts on: ``raises_issue=False`` — not an issue,
    skipped regardless of ``is_false_positive`` (which is meaningless in that
    case — the caller never inspects it); ``raises_issue=True,
    is_false_positive=True`` — a real-looking comment whose concern the cited
    code disproves; ``raises_issue=True, is_false_positive=False`` — a real
    issue to plan and implement. ``is_false_positive`` is only ever consulted
    when ``raises_issue`` is True, so the two are NOT independent booleans
    despite the flat schema — do not add a mutual-exclusion validator here,
    since ``(True, True)`` is the valid, load-bearing false-positive
    encoding, not a contradiction.
    """

    raises_issue: bool = Field(
        description="True when the comment identifies a concrete problem to fix (a bug, a missing "
        "requirement, a correctness/quality/security concern), as opposed to a question, a "
        "compliment, or a non-actionable remark."
    )
    is_false_positive: bool = Field(
        description="Only meaningful when raises_issue is True. True when, after checking the "
        "cited code, the comment's concern does NOT hold for the actual codebase (the issue it "
        "describes is not real) — this is the false-positive verdict. Ignored when raises_issue "
        "is False (the comment is skipped as not-an-issue regardless of this field)."
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
    chosen_candidate_index: Optional[int] = Field(
        default=None,
        description=(
            "0-based index into candidate_solutions (in the order returned above, before any "
            "re-ranking) identifying which candidate chosen_plan actually implements."
        ),
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
    left_unpublished_work: bool = Field(
        default=False,
        description=(
            "True iff this comment's implementation was dispatched and may have "
            "committed work to the shared `development` branch that never reached a "
            "clean publish — see `_run_address_comments`'s use of this flag to stop "
            "the run rather than let a later comment's branch preparation treat that "
            "leftover, unpublished state as same-work continuation."
        ),
    )


# ---------------------------------------------------------------------------
# Gather unresolved comments
# ---------------------------------------------------------------------------


def _unresolved_comments(
    client: GitHubClient, owner: str, repo: str, pr_number: int
) -> UnresolvedCommentsResult:
    """Return the PR's unresolved review comments plus a comment-id → thread map.

    Preconditions:
        - ``client`` is a live :class:`GitHubClient`; ``pr_number`` names an open PR.
    Postconditions:
        - Returns ``(comments, thread_by_comment_id, retry_resolve_threads,
          thread_history_by_comment_id, ambiguous_threads)``.
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
          reply AND for which :func:`resolve_attempt_store.has_recorded_resolve_
          failure` confirms Khala's OWN resolve mutation for THAT reply is on
          record as having failed — i.e. the reply landed, no reviewer has said
          anything since (as of THIS snapshot), and there is persisted evidence
          the resolve call itself ran and failed (not merely "hasn't run yet" or
          "unknown"). GitHub's read APIs cannot distinguish "our resolve failed"
          from "a reviewer clicked Reopen conversation with no new comment" —
          ``isResolved`` reports the same False in both cases — so without this
          persisted evidence a Khala-marker-ending unresolved thread is treated
          as AMBIGUOUS, not as a retry candidate: it is silently skipped (left
          exactly as found, logged) rather than auto-resolved, which would
          otherwise override a reviewer's deliberate reopen with no chance for
          them to be heard. The caller retries ONLY the resolve step for
          confirmed candidates, never re-triage/re-implementation — but must
          still re-check the thread's LIVE state immediately before resolving
          (:func:`_thread_has_new_reviewer_feedback` with ``khala_reply_comment_id``
          as ``since_comment_id``), since a reviewer can post a follow-up in the
          window between this snapshot and the retry loop actually running.
          "LATEST message is Khala's own reply" alone is NOT sufficient to
          reach this list: a thread only qualifies once every OTHER message
          in it is either Khala's own or at-or-before the id embedded by
          :func:`_accounted_through_marker` in that reply's body — the
          highest comment id the reply was actually generated from. A
          reviewer's follow-up that predates the reply chronologically but
          was assigned a HIGHER id than the boundary the reply was generated
          from (whether or not that id also happens to be lower than the
          reply's own id — GitHub id assignment doesn't strictly track
          response order under a race) was never accounted for and is routed
          to ``comments`` instead, using that follow-up as the representative
          message, exactly like ordinary new-feedback-after-reply handling.
          A reply posted before this marker existed carries no boundary
          (``_parse_accounted_through`` returns None) and is treated under
          the prior, order-only rule.
        - ``ambiguous_threads`` lists ``(thread_id, khala_reply_comment_id)`` for
          every UNRESOLVED thread whose LATEST message is a Khala-generated
          reply with no unaddressed follow-up (the same population
          ``retry_resolve_threads`` is drawn from) but for which
          :func:`resolve_attempt_store.has_recorded_resolve_failure` found NO
          persisted evidence — i.e. exactly the threads logged and skipped as
          "ambiguous reviewer reopen" above, deliberately excluded from both
          ``comments`` (never re-triaged/re-implemented) and
          ``retry_resolve_threads`` (never auto-resolved without evidence).
          A thread landing here is NOT eligible for either action, but it is
          also NOT resolved on GitHub — a reviewer's genuine reopen is still
          open. Callers computing "is anything still owed on this PR" must
          treat a non-empty ``ambiguous_threads`` as blocking completion (see
          ``_run_address_comments``'s fresh re-list check) even though it
          never appears in ``comments`` or ``retry_resolve_threads`` — leaving
          it out of every collection a completion check consults would let a
          run report full success (and label the PR "waiting for review", or
          delete its ephemeral checkout) while a reviewer's reopened
          conversation is still genuinely unresolved.
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
    # authenticated identity. Resolved once per call. Unlike pr_review._fetch_
    # pr_metadata's _get_login (a display-only best-effort read), a failure
    # here must NOT degrade to "" and continue: an empty login makes
    # _is_khala_authored reject even Khala's own genuine prior reply on a
    # thread that DOES have a discoverable thread, so that reply would be fed
    # back into triage as if it were fresh reviewer feedback — risking a
    # duplicate implementation dispatch for an already-fixed comment, or a
    # misclassification that leaves the thread unresolved forever. Fail
    # closed instead, exactly like the other unverifiable-state cases in this
    # function (no discoverable thread, incomplete GraphQL/REST listings).
    try:
        authenticated_login = client.get_authenticated_login()
    except Exception as e:  # noqa: BLE001 - re-raised as a fail-closed error below
        raise ReviewThreadsUnavailableError(
            owner,
            repo,
            pr_number,
            f"could not resolve authenticated login: {scrub_token_from_text(str(e))}",
        ) from e
    if not authenticated_login:
        # get_authenticated_login()'s own contract degrades to "" on a
        # best-effort failure rather than raising (see its docstring) — that
        # must still fail closed here, just via a different signal.
        raise ReviewThreadsUnavailableError(
            owner,
            repo,
            pr_number,
            "could not resolve authenticated login (empty)",
        )

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
    ambiguous_threads: List[Tuple[str, int]] = []
    thread_history_by_comment_id: Dict[int, List[ReviewComment]] = {}
    seen_thread_ids: Set[str] = set()
    for thread in threads:
        if thread.is_resolved or thread.id in seen_thread_ids:
            continue
        seen_thread_ids.add(thread.id)
        messages = thread_messages.get(thread.id) or []
        fetched_ids = {m.id for m in messages}
        missing_ids = set(thread.comment_ids) - fetched_ids
        if not messages or missing_ids:
            # An unresolved thread whose fetched messages don't cover EVERY
            # id in `thread.comment_ids` (zero fetched, or some subset) means
            # thread state is incomplete, not empty — every review thread has
            # at least a root comment, and GraphQL's `comment_ids` is the
            # authoritative membership list. This happens when list_review_
            # comments' REST traversal cap (MAX_REVIEW_COMMENTS_TRAVERSED)
            # truncates before reaching all of this thread's comments while
            # list_review_threads' separate GraphQL pagination still returned
            # the thread (and its full id list) intact. A PARTIAL fetch is
            # just as dangerous as an empty one: an early message inside the
            # cap can still look like a complete, current `messages` list —
            # silently treating it as the latest would ground triage on
            # stale history and let a stale verdict resolve the thread, and
            # dropping the thread from processing would let the caller's
            # final re-list make the same omission and reclaim the checkout
            # with real feedback never handled. Fail closed instead, exactly
            # like the "REST comment has no discoverable thread" case above.
            raise ReviewThreadsUnavailableError(
                owner,
                repo,
                pr_number,
                f"thread {thread.id} is unresolved but only fetched {sorted(fetched_ids)} of its "
                f"{sorted(thread.comment_ids)} known comments (likely truncated by "
                "list_review_comments' traversal cap)",
            )
        latest = messages[-1]
        if _is_khala_authored(latest, authenticated_login):
            # Nothing has been said since Khala's reply — the fix already
            # landed but GitHub still reports the thread unresolved. The
            # reply's own id travels with the thread id so the caller can
            # re-verify freshness immediately before resolving. Also
            # snapshot the thread's full history under the SAME dict retry
            # threads share with genuinely-unresolved ones — keyed by
            # `latest.id` (the reply's id) here — so the caller's freshness
            # re-check can pass it as `since_history` and catch an EARLIER
            # message being edited (not just a new one appearing), which an
            # id-only ">" comparison against the reply's own id could never
            # see: any message that predates the reply always has a lower id
            # — UNLESS a reviewer's follow-up (H) was created before `latest`
            # (R) chose to post but happened to be assigned a lower comment
            # id than R (a GitHub id-vs-creation-order race). `_reply_and_
            # resolve`'s own freshness checks catch H at the time R was
            # generated/resolved (they compare against the much older
            # representative comment's id, not R's), so THIS run never
            # resolves over it — but H still predates R in "chronological
            # response order", so a NEXT run naively picking `messages[-1]`
            # as "latest" would see R here and misclassify the thread as a
            # clean resolve-only retry, silently burying H forever the
            # moment the retry succeeds. Use R's own embedded
            # `_accounted_through_marker` (the highest id R was actually
            # generated from) instead of message order to settle this: any
            # non-Khala message with an id greater than that boundary —
            # REGARDLESS of its position in `messages` — was never accounted
            # for and still needs triage. A reply posted before this marker
            # existed has no boundary to check (`accounted_through` is None)
            # and falls back to the prior, order-only behavior.
            accounted_through = _parse_accounted_through(latest.body or "")
            unaddressed = (
                [
                    m
                    for m in messages
                    if m.id != latest.id
                    and m.id > accounted_through
                    and not _is_khala_authored(m, authenticated_login)
                ]
                if accounted_through is not None
                else []
            )
            if unaddressed:
                representative = max(unaddressed, key=lambda m: m.id)
                unresolved.append(representative)
                thread_history_by_comment_id[representative.id] = messages
                continue
            # No unaddressed follow-up found — but GitHub reporting the
            # thread unresolved is STILL ambiguous on its own: it means
            # EITHER "our resolve mutation failed previously" (safe to
            # retry) OR "a reviewer clicked Reopen conversation with no new
            # comment" (must NOT be silently auto-resolved — the reviewer
            # never gets a chance to have their reopen looked at).
            # `isResolved` reports the same False in both cases and GitHub
            # exposes no history/audit trail to tell them apart, so only
            # PERSISTED evidence that Khala's own resolve call for THIS
            # reply actually ran and failed (`resolve_attempt_store`,
            # written by the resolve step itself) authorizes the
            # retry-resolve path.
            if _main.has_recorded_resolve_failure(owner, repo, pr_number, thread.id, latest.id):
                retry_resolve_threads.append((thread.id, latest.id))
                thread_history_by_comment_id[latest.id] = messages
            else:
                # No persisted evidence either way — could be a genuine first
                # failure whose own record-write also failed (rare double
                # failure), or a reviewer's deliberate reopen. Never guess:
                # leave the thread exactly as found rather than risk silently
                # overriding a reviewer. It surfaces again on the next run,
                # by which point either the evidence has landed (retried) or
                # the reviewer has posted follow-up feedback (re-triaged via
                # the branch above once that feedback becomes visible).
                logger.info(
                    "address-comments: thread %s is unresolved with Khala's own reply as its "
                    "latest message but no recorded resolve-failure evidence — treating as an "
                    "ambiguous reviewer reopen and skipping rather than auto-resolving",
                    thread.id,
                )
                # Not eligible for retry-resolve (no evidence) or re-triage (no
                # unaddressed follow-up), but still genuinely unresolved on
                # GitHub — record it separately so a completion check can see
                # this thread is still blocking without making it eligible for
                # either action. See `ambiguous_threads` in this function's
                # own docstring.
                ambiguous_threads.append((thread.id, latest.id))
            continue
        # `latest` is either the thread's only message so far, or newer
        # feedback a reviewer posted after an earlier Khala reply in the same
        # thread. Either way it is the current concern to triage.
        unresolved.append(latest)
        thread_history_by_comment_id[latest.id] = messages
    return (
        unresolved,
        thread_by_comment_id,
        retry_resolve_threads,
        thread_history_by_comment_id,
        ambiguous_threads,
    )


# ---------------------------------------------------------------------------
# LLM steps (routed through llm_service.generate_structured)
# ---------------------------------------------------------------------------


class CitedCodeUnavailableError(RuntimeError):
    """Raised when the cited file's content could not be fetched (as opposed
    to the file genuinely being empty).

    Distinct from an empty-string result: triage is evidence-grounded, so a
    fetch failure (transient error, deleted file, unreadable fork commit)
    must not be silently converted into "no cited code" and allowed to
    proceed to a false-positive/resolve verdict based on prose alone.
    """


def _read_cited_code(client: GitHubClient, owner: str, repo: str, comment: ReviewComment, ref: str) -> str:
    """Read the file the comment points at, for grounding the LLM.

    Preconditions:
        - ``ref`` is the PR head SHA at which the comment was made — this
          function reads whatever ``ref`` the caller passes; it does not
          verify or canonicalize it.
    Postconditions:
        - Returns the cited file's text at ``ref``, or "" when the comment is
          file-less (a PR-level comment with no ``path``) — that case is not
          a fetch failure, there is simply nothing to fetch.
        - Raises :class:`CitedCodeUnavailableError` when ``comment.path`` is
          set but the content could not be fetched, so the caller can fail
          the comment closed rather than triage it on the comment's prose
          alone. Never silently degrades a real fetch failure to "".
    """
    if not comment.path:
        return ""
    try:
        content = client.get_file_contents(owner, repo, comment.path, ref)
    except Exception as e:  # noqa: BLE001 - reraised as a distinguishable, typed failure
        logger.warning(
            "address-comments: could not read %s@%s: %s",
            comment.path,
            ref,
            scrub_token_from_text(str(e)),
        )
        raise CitedCodeUnavailableError(
            f"could not read cited file {comment.path}@{ref}: {scrub_token_from_text(str(e))}"
        ) from e
    return content or ""


def _format_thread_history(thread_history: List[ReviewComment]) -> str:
    """Render a thread's messages as a chronological conversation transcript.

    Postconditions:
        - Returns one "Reviewer:" or "Khala:" labelled paragraph per message,
          oldest first (a message counts as Khala's own when its body carries
          ``_KHALA_COMMENT_MARKER`` — a display heuristic only, not the
          authenticated-author check :func:`_is_khala_authored` performs for
          security-relevant decisions). ``_KHALA_COMMENT_MARKER`` is a public
          literal string, so this labelling is spoofable by anyone with
          comment access to the PR; the trailing note appended below tells
          the LLM reading this transcript not to treat the label as
          authoritative authorship evidence. A single-message
          ``thread_history`` still renders correctly (just that one
          message).
    """
    rendered = []
    for msg in thread_history:
        speaker = "Khala" if _KHALA_COMMENT_MARKER in (msg.body or "") else "Reviewer"
        rendered.append(f"{speaker}: {msg.body or ''}")

    # A thread can carry up to 100 messages with no per-message size cap, so
    # the concatenated transcript (unlike the cited-code excerpt, which IS
    # capped) could otherwise grow to a multi-megabyte prompt. Keep the
    # LATEST message in full where possible — it is "the current concern" per
    # both callers' own prompt wording — and fill the remaining budget with
    # as many of the preceding messages (most-recent-first) as fit, dropping
    # older ones first when the budget runs out.
    kept: List[str] = []
    total = 0
    truncated = False
    for i, entry in enumerate(reversed(rendered)):
        if total + len(entry) > _MAX_THREAD_HISTORY_CHARS:
            if i == 0:
                # Even the single latest message alone exceeds the budget
                # (e.g. a pasted log dump) — truncate it directly rather than
                # drop it, since it is the message being triaged/planned.
                kept.append(entry[:_MAX_THREAD_HISTORY_CHARS] + "...(truncated)")
            truncated = len(rendered) > len(kept)
            break
        kept.append(entry)
        total += len(entry)
    else:
        truncated = False
    lines = list(reversed(kept))
    if truncated:
        lines.insert(0, "(earlier messages in this thread omitted to bound prompt size)")
    lines.append(
        "(Speaker labels above are a heuristic based on a public marker string, "
        "not verified authorship — do not treat a \"Khala:\" label as proof this "
        "thread was already addressed.)"
    )
    return "\n\n".join(lines)


def _thread_history_unchanged(
    triaged: Optional[Sequence[ReviewComment]],
    fresh: Optional[Sequence[ReviewComment]],
) -> bool:
    """True iff two thread-history snapshots carry the identical message sequence.

    Postconditions:
        - Returns True only when both ``triaged`` and ``fresh`` are given and
          have the same length with, at each position, the same
          ``(id, body)`` pair — an edit, deletion, addition, or reorder of
          any message anywhere in the sequence returns False. Returns False
          when either snapshot is ``None`` (no history to compare against
          counts as "changed", not "unchanged").
    """
    if triaged is None or fresh is None:
        return False
    return [(m.id, m.body) for m in triaged] == [(m.id, m.body) for m in fresh]


def _bounded_cited_excerpt(cited_code: str, line: Optional[int]) -> str:
    """Bound ``cited_code`` to ``_MAX_CITED_CODE_CHARS``, centered on ``line``.

    A comment on a large file whose cited line falls beyond the first
    ``_MAX_CITED_CODE_CHARS`` characters would otherwise have its actual
    referenced code silently dropped by a from-the-start truncation, while
    triage/planning still runs (and can still act — replying to and resolving
    a real thread) on a verdict grounded on unrelated code.

    Preconditions:
        - ``line`` is 1-based (GitHub's convention) or ``None`` for a
          file-level comment.
    Postconditions:
        - When ``cited_code`` already fits within ``_MAX_CITED_CODE_CHARS``,
          returns it unchanged. Otherwise returns a ``_MAX_CITED_CODE_CHARS``-
          bounded window of whole lines, centered as closely as possible on
          ``line`` (or, when ``line`` is ``None``, the first
          ``_MAX_CITED_CODE_CHARS`` characters — the prior, line-agnostic
          behavior — since there is no specific line to center on).
    """
    if len(cited_code) <= _MAX_CITED_CODE_CHARS:
        return cited_code
    if line is None:
        return cited_code[:_MAX_CITED_CODE_CHARS]
    lines = cited_code.splitlines(keepends=True)
    target = max(0, min(line - 1, len(lines) - 1))
    # A single line (e.g. in a minified/generated file) can itself exceed the
    # cap. The expand-outward loop below only ever ADDS lines to the window,
    # so if the target line alone is already over budget, the loop's guard
    # (`total < _MAX_CITED_CODE_CHARS`) is false from the start and the loop
    # body never executes — returning that one oversized line entirely
    # unbounded. Truncate it directly instead of falling into that loop.
    if len(lines[target]) > _MAX_CITED_CODE_CHARS:
        return lines[target][:_MAX_CITED_CODE_CHARS] + "...(truncated)"
    # Expand outward from the target line, alternating forward/backward, until
    # the budget is spent — keeps whole lines only, so the excerpt is never
    # cut mid-line.
    start = end = target
    total = len(lines[target])
    while total < _MAX_CITED_CODE_CHARS and (start > 0 or end < len(lines) - 1):
        if start > 0:
            start -= 1
            total += len(lines[start])
            if total > _MAX_CITED_CODE_CHARS:
                start += 1
                break
        if end < len(lines) - 1:
            end += 1
            total += len(lines[end])
            if total > _MAX_CITED_CODE_CHARS:
                end -= 1
                break
    return "".join(lines[start : end + 1])


def _format_comment_prompt_context(
    comment: ReviewComment,
    cited_code: str,
    thread_history: List[ReviewComment],
    thread_history_note: str,
) -> str:
    """Render the File/Line + discussion-thread + cited-code header shared by
    both :func:`_triage_comment` and :func:`_plan_resolution`'s prompts.

    Preconditions:
        - ``thread_history_note`` is the phase-specific parenthetical describing
          what the thread's LAST message means for THIS call (triage vs.
          planning word it differently) — the two callers intentionally keep
          distinct wording here, only the surrounding structure is shared.
    Postconditions:
        - Returns the header block verbatim as both callers previously built
          it inline, ending after the cited-code section (callers append
          their own task-specific instructions after this).
    """
    # GitHub nulls out `line` once a comment's diff hunk goes outdated (the
    # file changed at that spot since) but keeps `original_line` — the line
    # the comment was actually made against. Centering the excerpt on that
    # is a much better anchor than treating the comment as file-level, even
    # though it may drift from the current head if the file has since shifted
    # around that spot; the "Line:" header is worded to make the distinction
    # explicit to the model rather than silently presenting it as current.
    excerpt_line = comment.line if comment.line is not None else comment.original_line
    if comment.line is not None:
        line_display = str(comment.line)
    elif comment.original_line is not None:
        line_display = f"{comment.original_line} (outdated comment; line at the time it was posted)"
    else:
        line_display = "(file-level)"
    return (
        f"File: {comment.path or '(none)'}\n"
        f"Line: {line_display}\n\n"
        f"## Discussion thread (oldest to newest; {thread_history_note})\n"
        f"{_format_thread_history(thread_history)}\n\n"
        "## Cited file content (may be empty if unavailable)\n"
        f"{_bounded_cited_excerpt(cited_code, excerpt_line) if cited_code else '(unavailable)'}\n\n"
    )


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
        + _format_comment_prompt_context(
            comment,
            cited_code,
            thread_history,
            "the LAST message is the current concern to triage — earlier messages are context, "
            'e.g. what a short follow-up like "still broken" or "use the other approach" is '
            "referring to",
        )
        + "Decide whether the thread's LAST message raises a real, actionable issue, and "
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
        - ``chosen_plan`` is free text the model writes independently of
          ``candidate_solutions`` — nothing in the schema forces it to actually
          describe the candidate ``chosen_candidate_index`` names, let alone the
          top-scoring one. When they disagree (or ``chosen_candidate_index`` is
          missing/out of range while candidates exist), this logs a warning but
          still returns the plan: ``chosen_plan`` — not the candidate list — is
          what :func:`_dispatch_implementation` actually acts on, so a scoring/
          description mismatch is a self-consistency signal worth surfacing, not
          grounds to fail an otherwise usable plan.
    """
    prompt = (
        "## Real issue raised by a review comment\n"
        + _format_comment_prompt_context(
            comment,
            cited_code,
            thread_history,
            "the LAST message is the issue to resolve — earlier messages give it context",
        )
        + "Identify the resolution requirements for the thread's LAST message, the top THREE "
        "candidate solutions (each scored 1-10 on requirement fit, computational performance, "
        "memory usage, and inverted code complexity), which one you are choosing "
        "(chosen_candidate_index, 0-based into the candidate list you return), and a concrete "
        "implementation plan for that SAME chosen candidate — the best-scoring one. Respond as "
        "JSON."
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
    if plan.candidate_solutions:
        top_index = max(
            range(len(plan.candidate_solutions)),
            key=lambda i: plan.candidate_solutions[i].score,
        )
        if plan.chosen_candidate_index != top_index:
            logger.warning(
                "address-comments: comment %s's plan names chosen_candidate_index=%s but "
                "candidate %s scored highest — chosen_plan may not describe the top-scoring "
                "candidate; proceeding with chosen_plan as-is since it, not the candidate "
                "list, is what gets implemented",
                comment.id,
                plan.chosen_candidate_index,
                top_index,
            )
    # Rank candidates best-first for display/logging only — this reorders
    # candidate_solutions but does NOT touch chosen_plan or
    # chosen_candidate_index. chosen_plan is the model's own independent
    # free-text plan and remains the sole source of truth for what
    # _dispatch_implementation actually implements; see the mismatch warning
    # above for when the two disagree.
    plan.candidate_solutions.sort(key=lambda c: c.score, reverse=True)
    return plan


# ---------------------------------------------------------------------------
# Implement, publish to the existing PR, and wait for success
# ---------------------------------------------------------------------------


def _pr_head_remote(owner: str, repo: str, pr: Any, web_host: str = "github.com") -> Optional[str]:
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
        - ``web_host`` is the clone/browse host to build the fork URL against
          (see :attr:`GitHubClient.web_host`) — defaults to ``"github.com"``
          for callers (mostly tests) that don't have a live client handy.
    Postconditions:
        - Returns ``"origin"`` when the head branch lives in ``owner/repo``
          itself (the ordinary, same-repo case) — the checkout's existing
          origin remote is already correct.
        - Returns an HTTPS clone URL (against ``web_host``, so this resolves
          correctly against a GitHub Enterprise Server deployment too, not
          just github.com Cloud) for the head repository when it differs from
          ``owner/repo`` (a fork-opened PR). A URL is valid anywhere git
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
    return f"https://{web_host}/{head_repo_full_name}.git"


def _child_job_id_for_comment(comment_id: int) -> str:
    """Return the deterministic child-implementation job id for a review comment.

    Postconditions:
        - Returns ``f"address-comment:{comment_id}"`` — keyed on the GitHub
          review comment's own globally-unique id, NOT on any per-run
          address-comments parent job id, so the SAME id is produced on
          every run that handles this comment. This is what lets
          :func:`_previously_published_fix` recognize, on a later run, that
          a fix for this exact comment was already implemented and
          published — see its docstring for why a parent-job-scoped id
          cannot do that.
    """
    return f"address-comment:{comment_id}"


def _previously_published_fix(comment_id: int) -> Optional[Tuple[str, str]]:
    """Best-effort check: was a fix for this comment already implemented and published?

    A run can dispatch ``_dispatch_implementation`` successfully (the fix lands
    on the PR branch) and then fail at the reply/resolve step that follows it
    (see ``_handle_comment``) — e.g. the reply POST itself errors. GitHub then
    still reports the thread unresolved with no Khala-authored reply on it, so
    a later run's ``_unresolved_comments`` re-surfaces the SAME original
    comment (its id unchanged, since nothing new was ever posted) and would
    otherwise re-triage and re-dispatch a brand new implementation for a fix
    that may already be on the branch. This is only possible to catch across
    runs because the child job id is derived from ``comment_id`` alone (see
    :func:`_child_job_id_for_comment`) rather than from the per-run parent job
    id: a parent-scoped id (the old ``f"{parent_job_id}:comment:{comment_id}"``
    scheme) is a fresh, different string on every new address-comments run, so
    a later run could never look up an earlier run's child job by id.

    Postconditions:
        - Returns ``(child_job_id, chosen_plan)`` when a child job already
          exists for ``comment_id``, its status is exactly ``"completed"``
          (an exact terminal success — see ``_dispatch_implementation``'s own
          postcondition for why partial results don't count), its
          ``github_context.review_comment_id`` matches ``comment_id`` (defends
          against a hypothetical id collision or corrupted record), and it
          carries a non-empty ``chosen_plan`` field.
        - Returns ``None`` otherwise, including on a lookup failure — this is
          advisory; a failed check must not block the run, it just means the
          comment is triaged fresh as if no prior attempt existed.
    """
    child_job_id = _child_job_id_for_comment(comment_id)
    try:
        job = _main.get_job(child_job_id)
    except Exception as e:  # noqa: BLE001 - best-effort; a failed lookup must not block
        logger.warning(
            "address-comments: could not check for a previously-published fix for "
            "comment %s: %s",
            comment_id,
            scrub_token_from_text(str(e)),
        )
        return None
    if not job or job.get("status") != JobStatus.COMPLETED.value:
        return None
    github_context = job.get("github_context") or {}
    if github_context.get("review_comment_id") != comment_id:
        return None
    chosen_plan = job.get("chosen_plan")
    if not chosen_plan:
        return None
    return child_job_id, chosen_plan


def _dispatch_implementation(
    parent_job_id: str,
    request: AddressCommentsRequest,
    comment: ReviewComment,
    plan: IssueResolutionPlan,
    pr_head: str,
    pr_head_sha: str,
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
        - ``parent_job_id`` is the address-comments run's own job id, stored on
          the child job's ``parent_job_id`` field for traceability and PR-review
          admission exclusion (see ``pr_review._running_review_for_pr``) — it is
          NOT used to derive the child job id itself; that comes from
          :func:`_child_job_id_for_comment` (comment-scoped, stable across runs)
          so a later run can recognize this exact dispatch as already done (see
          :func:`_previously_published_fix`).
        - ``pr_head``/``pr_base`` are the PR's head/base branch SHORT names
          (not SHAs) — passed straight through to the child workflow's branch
          preparation.
        - ``pr_head_sha`` is the PR head SHA the caller's plan was grounded
          on (the same value threaded through ``_handle_comment``'s own
          freshness checks — see ``_pr_became_stale``). Forwarded to the
          child workflow as ``expected_head_sha`` so branch preparation
          (``github_branch_prep_activity`` / ``_prepare_issue_branch``) fails
          closed if the branch moved again between this dispatch and the
          Temporal workflow actually running branch prep — a window this
          function cannot itself observe or wait out, since
          ``execute_coding_team_workflow`` may reattach to a workflow that
          was queued for a while.
        - ``pr_url`` is the PR's web URL, stored on the child job's github
          context for traceability.
        - ``pr_remote`` is the remote to fetch/push the head branch through
          (see :func:`_pr_head_remote`) — ``"origin"`` for a same-repo PR, a
          fork clone URL for a fork PR, or ``None`` when it could not be
          resolved (the fork was deleted). ``None`` raises immediately rather
          than defaulting to "origin", which would silently target the wrong
          repository.
        - ``token`` is a valid GitHub token for ``request.owner``/``request.repo``.
    Postconditions:
        - On success, returns the child job id
          (:func:`_child_job_id_for_comment`\\ ``(comment.id)``) after
          ``execute_coding_team_workflow`` reports an exact ``"completed"``
          status; any other terminal status (including
          ``"completed_with_failures"``) raises ``RuntimeError`` instead of
          returning, so the caller never replies to or resolves the thread
          over a partially-failed implementation. In that case the child job
          row and its Temporal workflow were both created (the workflow ran;
          only its result was unsuccessful) but its status is not
          ``"completed"``, so :func:`_previously_published_fix` correctly
          will not treat it as an already-published fix on a later run. This
          is also how an ``expected_head_sha`` mismatch surfaces: branch prep
          reports ``ok=False``, the workflow terminalizes as a GitHub failure
          notice rather than ``"completed"``, and this function raises the
          same as any other non-success result, leaving the thread open for
          the next run to re-triage against the branch's current head.
        - If ``execute_coding_team_workflow`` itself raises (rather than
          returning a non-``"completed"`` status), that exception propagates
          uncaught — it is not wrapped in a try/except here. The child job
          row was created before that call, but whether the Temporal
          workflow itself was started depends on where inside
          ``execute_coding_team_workflow`` the raise happened.
        - Raises ``RuntimeError`` (before creating/touching anything) instead
          of dispatching when a job already exists for
          :func:`_child_job_id_for_comment`\\ ``(comment.id)`` in an ACTIVE
          state (``"pending"``, ``"running"``, or ``"waiting_for_user"``) —
          because the child job id is now comment-scoped rather than
          parent-job-scoped (see the ``parent_job_id`` precondition above),
          the SAME id can otherwise be reused across two runs. The caller
          (:func:`_handle_comment`) only reaches this function after
          :func:`_previously_published_fix` found no ``"completed"`` job for
          this comment, so any job found here is either a stale/orphaned
          active job (e.g. its worker crashed or the server restarted
          mid-run) or, in principle, a genuinely still-running one — either
          way, blindly overwriting it via ``create_job``'s upsert could
          corrupt or orphan real in-flight work. A job whose status is
          already TERMINAL but not ``"completed"`` (``"failed"``,
          ``"completed_with_failures"``, ``"cancelled"``) is safe to reset
          and retry — that is the common, intended case a comment resurfaces
          for re-dispatch at all.
    """
    if pr_remote is None:
        raise RuntimeError(
            f"cannot determine the head repository for {request.owner}/{request.repo}"
            f"#{request.pr_number}'s branch {pr_head!r} (its fork appears to have been "
            "deleted); the fix cannot be published"
        )
    child_job_id = _child_job_id_for_comment(comment.id)
    try:
        existing_job = _main.get_job(child_job_id)
    except Exception as e:  # noqa: BLE001 - best-effort; a failed lookup must not block dispatch
        logger.warning(
            "address-comments: could not check for an existing job before dispatching "
            "comment %s's implementation: %s",
            comment.id,
            scrub_token_from_text(str(e)),
        )
        existing_job = None
    if existing_job and existing_job.get("status") in _ACTIVE_JOB_STATUSES:
        raise RuntimeError(
            f"a job already exists for comment {comment.id} (id {child_job_id!r}, status "
            f"{existing_job.get('status')!r}); refusing to overwrite a possibly still-running "
            "implementation"
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
    plan_input_dict = plan_input.model_dump()
    _main.create_job(
        job_id=child_job_id,
        repo_path=request.repo_path,
        plan_input=plan_input_dict,
    )
    child_fields: Dict[str, Any] = {
        "parent_job_id": parent_job_id,
        "chosen_plan": plan.chosen_plan,
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
        plan_input_dict,
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
            "expected_head_sha": pr_head_sha,
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
    client: GitHubClient,
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
          the triage that ran never saw — OR (b) ANY id in ``since_history``
          that is now either MISSING from the live comment set (the reviewer
          deleted a message the triage/plan was grounded on — context they
          withdrew is exactly as invalidating as an edit) or present with a
          body that no longer matches that entry's snapshotted body — GitHub
          retains a comment's id across an edit, so a reviewer who edits an
          ALREADY-triaged message in place (rather than posting a reply)
          would otherwise be invisible to the id-only check and get silently
          resolved over, whether that message was the representative comment
          or an earlier one in the same history — OR (c) the LIVE thread is
          already resolved: a reviewer can resolve a thread by hand (via the
          GitHub UI) while triage/planning/implementation for it is still
          running, which supersedes the
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
          the thread is found, still open, and none of (a)/(b)/(c)/(d) hold. Fails
          OPEN (returns True) on any error: resolving a thread whose
          freshness could not be verified risks silently hiding real
          feedback, which is worse than a redundant re-check on the next run.
    """
    try:
        authenticated_login = client.get_authenticated_login()
    except Exception:  # noqa: BLE001 - best-effort; missing identity fails open below
        authenticated_login = ""
    try:
        # Fetch comments BEFORE thread membership, not after: if feedback lands
        # between the two calls, it must land in the LATER fetch so it's
        # visible to at least one of them. With comments fetched first, a
        # comment posted in the gap is missing from `all_comments` but present
        # in `fresh_thread.comment_ids` (fetched after) — the loop below then
        # finds it via `comments_by_id.get(cid) is None` and fails closed
        # (returns True). The reverse order would do the opposite: a comment
        # landing in the gap would be present in `all_comments` but absent
        # from the earlier `comment_ids` snapshot, so the loop would never
        # examine it at all and silently fail open (return False) over
        # feedback that had, in fact, already been posted.
        all_comments = client.list_review_comments(owner, repo, pr_number)
        comments_by_id = {c.id: c for c in all_comments}
        threads = client.list_review_threads(owner, repo, pr_number)
        fresh_thread = next((t for t in threads if t.id == thread_id), None)
        if fresh_thread is None:
            # Deleted (or otherwise no longer discoverable) — treat exactly
            # like an already-resolved thread: superseded, nothing left to
            # protect by proceeding.
            return True
        if fresh_thread.is_resolved:
            return True
        for snapshotted in since_history or ():
            live = comments_by_id.get(snapshotted.id)
            if live is None:
                # A message that WAS part of the triaged history is now gone
                # (the reviewer deleted it, or it fell out of the REST listing
                # mid-run) — the plan/verdict was grounded on context the
                # reviewer withdrew, exactly as invalidating as an edit.
                return True
            if (live.body or "") != (snapshotted.body or ""):
                return True
        for cid in fresh_thread.comment_ids:
            if cid <= since_comment_id:
                continue
            newer = comments_by_id.get(cid)
            if newer is None:
                # GraphQL reports a comment id newer than since_comment_id that
                # the REST listing didn't return (e.g. list_review_comments'
                # MAX_REVIEW_COMMENTS_TRAVERSED cap truncated before reaching
                # it). Its content — and therefore whether it's genuine
                # reviewer feedback — is unverifiable; fail closed rather than
                # silently treating an unfetched comment as if it doesn't
                # exist, which would let stale work proceed over feedback that
                # was never actually seen.
                return True
            if not _is_khala_authored(newer, authenticated_login):
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
    client: GitHubClient,
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
          not only an edit to the representative comment. When OMITTED
          (``None``), the freshness check defaults to ``[comment]`` — it is
          NOT disabled; edits to ``comment`` itself are still detected, only
          edits to earlier messages elsewhere in the thread go unchecked.
    Postconditions:
        - Attempts to post a threaded reply and, when ``thread`` is known and
          the reply succeeds, attempts to resolve the thread — see the
          freshness-check bullet below for when the reply is skipped
          entirely rather than attempted. The reply targets the thread's
          ROOT comment
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
        - Whenever a resolve is attempted, best-effort updates the resolve-
          attempt ledger (:mod:`resolve_attempt_store`): a failure records
          ``(thread.id, reply's comment id)`` so a future run's
          ``_unresolved_comments`` can positively confirm "our own resolve
          failed" rather than treating the still-unresolved thread as an
          ambiguous reviewer reopen; success clears any existing entry.
        - WHEN ``thread`` is known, checks the thread's LIVE state
          (:func:`_thread_has_new_reviewer_feedback`) BEFORE posting anything:
          a reviewer may have posted follow-up feedback on this thread while
          this comment's implementation workflow was running (between when
          ``comment`` was snapshotted and now). When newer, non-Khala
          feedback is found, this call posts NEITHER the reply NOR the
          resolution and reports failure — the check must run before the
          reply, not just before the resolve: a reply posted first would itself
          become the thread's new latest message, and since it carries Khala's
          own marker, the NEXT run's ``_unresolved_comments`` would then route
          the thread down the resolve-only retry path — never re-triaging the
          human feedback that prompted skipping the resolve in the first place.
          Leaving the thread exactly as found lets the next run's latest-message
          check correctly see the human's feedback as the thread's live latest
          message. WHEN ``thread`` is ``None`` (should not normally happen —
          see the reply-target precondition above), there is no thread id to
          re-check against, so the reply is posted directly to ``comment.id``
          with NO live-state verification at all — and the SAME ``comment.id``
          fallback applies when ``thread`` IS known but carries an empty
          ``comment_ids`` (should also not normally happen — a real thread
          always has at least a root comment).
        - The posted reply body carries :func:`_accounted_through_marker` for
          the highest comment id in ``history`` (i.e. the boundary of what
          this specific reply was actually generated from), in addition to
          the client's own authorship marker. A LATER run's
          ``_unresolved_comments`` uses this to tell a thread where nothing
          arrived before this reply was generated apart from one where a
          reviewer's message slipped in during generation but happened to be
          assigned a lower id than the reply itself — see that marker's own
          docstring.
    """
    history = thread_history if thread_history is not None else [comment]
    accounted_through_id = max((m.id for m in history), default=comment.id)
    reply_body = f"{reply_body}\n\n{_accounted_through_marker(accounted_through_id)}"
    if thread is not None and _thread_has_new_reviewer_feedback(
        client,
        request.owner,
        request.repo,
        request.pr_number,
        thread.id,
        comment.id,
        history,
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
    reply_id: Optional[int] = None
    try:
        reply_payload = client.reply_to_review_comment(
            owner=request.owner,
            repo=request.repo,
            number=request.pr_number,
            comment_id=reply_target_id,
            body=scrub_token_from_text(reply_body),
        )
        replied = True
        # Best-effort only: feeds the resolve-attempt ledger below so a
        # subsequent failed resolve can be matched back to THIS reply on the
        # next run's `has_recorded_resolve_failure` check. A missing/odd
        # payload shape just means that ledger entry keys off `None` instead —
        # never fails the reply itself, which already landed.
        reply_id = reply_payload.get("id") if isinstance(reply_payload, dict) else None
    except Exception as e:  # noqa: BLE001 - reply is best-effort
        logger.warning(
            "address-comments: failed to reply to comment %s: %s",
            comment.id,
            scrub_token_from_text(str(e)),
        )

    resolved = True
    if replied and thread is not None:
        # Re-check freshness a SECOND time, right before resolving: reviewer
        # feedback can land in the window between the pre-reply check above
        # and this point (during or just after `reply_to_review_comment`
        # itself). The reply has already been posted by now — best-effort,
        # not undoable — but resolving on top of feedback that arrived in
        # that window would still close the thread over an unaddressed
        # concern, AND (since Khala's own reply is now the latest message)
        # the next run's latest-message check would misread the thread as
        # already handled, silently dropping the reviewer's feedback for
        # good. `since_comment_id=comment.id` still correctly excludes the
        # reply just posted (it postdates comment.id but is Khala's own).
        if _thread_has_new_reviewer_feedback(
            client,
            request.owner,
            request.repo,
            request.pr_number,
            thread.id,
            comment.id,
            history,
        ):
            logger.info(
                "address-comments: skipping resolve on thread %s — newer reviewer "
                "feedback appeared after the reply was posted",
                thread.id,
            )
            return False
        try:
            resolved = client.resolve_review_thread(thread.id)
        except Exception as e:  # noqa: BLE001 - resolve is best-effort; honor "never raises"
            logger.warning(
                "address-comments: failed to resolve thread %s: %s",
                thread.id,
                scrub_token_from_text(str(e)),
            )
            resolved = False
        # Record/clear the resolve-attempt ledger regardless of which branch
        # set `resolved` above — this is the ONLY evidence `_unresolved_
        # comments` will later trust to route this thread down the
        # resolve-only retry path rather than treating a still-unresolved,
        # Khala-marker-ending thread as an ambiguous reviewer reopen.
        if resolved:
            _main.clear_resolve_attempt(request.owner, request.repo, request.pr_number, thread.id)
        else:
            _main.record_resolve_failure(
                request.owner, request.repo, request.pr_number, thread.id, reply_id
            )
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


def _mark_waiting_for_review(client: GitHubClient, owner: str, repo: str, pr_number: int) -> None:
    """Add the "waiting for review" label to the PR (best-effort).

    GitHub has no native "waiting for review" PR state, so this is a label. A PR is
    an issue in GitHub's REST API, so ``update_issue`` applies it; existing labels
    are preserved by merging (``update_issue`` replaces the full label set).

    Postconditions:
        - The PR carries ``WAITING_FOR_REVIEW_LABEL`` in addition to its existing
          labels. Never raises — the label is a convenience signal, so a failure to
          apply it does not fail the job (the comments are already addressed).
        - Read-modify-write, NOT atomic: the fetch and the ``update_issue`` replace
          are two separate API calls, so a label change made by another process
          (or a concurrent run) in between is silently overwritten by whichever
          call lands last. Accepted as a known best-effort limitation, same as
          the rest of this label's convenience-signal contract — GitHub's REST
          API has no compare-and-swap for label sets.
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


def _clear_waiting_for_review(client: GitHubClient, owner: str, repo: str, pr_number: int) -> None:
    """Remove the "waiting for review" label from the PR (best-effort).

    A previous successful run may have applied ``WAITING_FOR_REVIEW_LABEL``.
    If a reviewer then opens new feedback, admitting a run to address it means
    the PR is no longer actually ready for another look — the old label would
    otherwise stay stuck advertising "ready" through however long this new
    run takes, or even after a run that fails outright, since the label is
    only ever ADDED (on success), never removed. Called once, up front, as
    soon as this run knows it has actionable comments to work through — not
    conditioned on this run's own eventual success.

    Postconditions:
        - The PR's labels no longer include ``WAITING_FOR_REVIEW_LABEL``.
          Never raises — the label is a best-effort convenience signal, same
          as :func:`_mark_waiting_for_review`, including its same non-atomic
          read-modify-write limitation (see that function's docstring).
    """
    try:
        pr = client.get_pull_request(owner, repo, pr_number)
        if WAITING_FOR_REVIEW_LABEL not in pr.labels:
            return
        remaining = [label for label in pr.labels if label != WAITING_FOR_REVIEW_LABEL]
        client.update_issue(owner, repo, pr_number, labels=remaining)
    except Exception as e:  # noqa: BLE001 - status label is best-effort
        logger.warning(
            "address-comments: could not clear waiting-for-review label on PR %s/%s#%s: %s",
            owner,
            repo,
            pr_number,
            scrub_token_from_text(str(e)),
        )


def _job_cancelled(job_id: str) -> bool:
    """Best-effort check: has this job been cancelled through the normal job APIs?

    An operator can cancel an address-comments parent job via the generic
    ``POST /api/jobs/{team}/{job_id}/cancel`` route (or the SE team's own
    cancel endpoint), which sets the job's ``status`` to ``"cancelled"`` —
    but the address-comments background loop otherwise never re-reads its
    own job's status while running, so a cancellation request would
    otherwise go completely unnoticed: dispatching further work, then having
    its final terminal-status write silently overwrite "cancelled" with a
    completion status.

    Postconditions:
        - Returns True iff the job's current status is exactly
          ``"cancelled"``. Returns False (never raises) on a lookup failure —
          this is advisory, checked at multiple points in the run, so a
          transient read failure degrades to proceeding rather than
          incorrectly treating an uncancelled run as cancelled.
    """
    try:
        job = _main.get_job(job_id)
    except Exception as e:  # noqa: BLE001 - best-effort; a failed check must not block
        logger.warning(
            "address-comments: could not check job %s for cancellation: %s",
            job_id,
            scrub_token_from_text(str(e)),
        )
        return False
    return bool(job) and job.get("status") == JobStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# Per-comment driver
# ---------------------------------------------------------------------------


def _pr_became_stale(
    client: GitHubClient,
    request: AddressCommentsRequest,
    comment_id: int,
    pr_head_sha: str,
    stage: str,
) -> Optional[str]:
    """Re-check whether the PR moved on (a new head, or closed/merged) since ``pr_head_sha``.

    Preconditions:
        - ``pr_head_sha`` is the head SHA captured before the LLM round-trip
          (triage or planning) named by ``stage`` (used only in log/detail text).
    Postconditions:
        - Returns a ready-to-use ``base.detail`` message when a live re-fetch
          succeeds and finds EITHER the head SHA changed OR the PR is no
          longer open — either one means the verdict/plan this comment is
          about to act on was grounded on state that's no longer current. A
          head-SHA-only check would miss the "closed with no new commit"
          case (e.g. merged as-is, or closed without merging): the SHA
          reported by GitHub for a merged PR does not change, but false-
          positive replies/resolves and real-issue implementation dispatch
          must not proceed against a PR no longer accepting either.
        - Returns ``None`` when neither happened, or when the re-fetch
          itself fails — a failed check is best-effort and must not block
          the run, so it degrades to proceeding on the original snapshot
          rather than raising.
    """
    try:
        current_pr = client.get_pull_request(request.owner, request.repo, request.pr_number)
    except Exception as e:  # noqa: BLE001 - best-effort; a failed re-check must not block the run
        logger.warning(
            "address-comments: could not re-check PR state after comment %s was %s: %s",
            comment_id,
            stage,
            scrub_token_from_text(str(e)),
        )
        return None
    if current_pr.state != "open":
        return (
            f"PR {request.owner}/{request.repo}#{request.pr_number} is no longer open "
            f"(state={current_pr.state}) after this comment was {stage}; skipped so nothing "
            "is replied to, resolved, or published against a closed PR."
        )
    if current_pr.head_sha != pr_head_sha:
        return (
            f"PR head moved from {pr_head_sha} to {current_pr.head_sha} while this comment "
            f"was being {stage}; skipped so the next run re-triages against the current code."
        )
    return None


def _handle_comment(
    client: GitHubClient,
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
        - Returns the :class:`CommentOutcome` recording what happened, with one
          of four outcomes:
          * ``not_an_issue`` — triage decided the comment does not raise a
            real issue; nothing else is attempted.
          * ``false_positive`` — triage decided the concern does not hold,
            and the reply/resolve step succeeded.
          * ``resolved`` — a fix was planned, implemented, and pushed to the
            PR, and the reply/resolve step succeeded. This also covers the
            case where an EARLIER run already did the planning/implementing/
            publishing and only the reply/resolve step is being retried now
            (see ``_previously_published_fix``).
          * ``failed`` — the comment could not be fully handled: the cited
            file's content could not be fetched (triage is evidence-grounded
            and must not proceed on prose alone), the PR's head SHA moved (or
            the PR closed) while triage/planning was in flight, newer
            reviewer feedback appeared on the thread before dispatch, the PR
            closed after implementation was dispatched, or the reply/resolve
            step itself failed. ``detail`` explains which.
          Never raises; one comment's failure is recorded and returned
          rather than propagating.
        - If the PR's head SHA has moved since ``pr_head_sha`` (a push landed
          while triage's LLM call(s) were in flight), acts on NONE of triage's
          verdict — records ``failed`` with an explanatory detail instead, so
          the next run re-triages against the current code rather than
          resolving a false-positive (or planning a fix) against a stale read.
    """
    base = CommentOutcome(
        comment_id=comment.id,
        path=comment.path,
        line=comment.line,
        html_url=comment.html_url,
        outcome="failed",
    )
    try:
        # An earlier run may have already implemented and published a fix for
        # this exact comment and then failed at the reply/resolve step that
        # follows (e.g. the reply POST itself errored) — GitHub still reports
        # the thread unresolved with no Khala reply, so this SAME comment
        # (same id; nothing new was ever posted) resurfaces here again. Skip
        # straight to reply/resolve using the already-published fix instead
        # of re-triaging and re-dispatching a brand new implementation
        # workflow on top of one that may already be on the PR branch — see
        # _previously_published_fix for how this is recognized across runs.
        published = _previously_published_fix(comment.id)
        if published is not None:
            child_job_id, chosen_plan = published
            try:
                post_dispatch_pr = client.get_pull_request(request.owner, request.repo, request.pr_number)
            except Exception as e:  # noqa: BLE001 - best-effort; a failed re-check must not block
                logger.warning(
                    "address-comments: could not re-check PR state before retrying reply/resolve "
                    "for comment %s's already-published fix: %s",
                    comment.id,
                    scrub_token_from_text(str(e)),
                )
            else:
                if post_dispatch_pr.state != "open":
                    base.detail = (
                        f"PR {request.owner}/{request.repo}#{request.pr_number} is no longer open "
                        f"(state={post_dispatch_pr.state}); a fix was already published by job "
                        f"{child_job_id} but the thread was left as-is rather than replying to or "
                        "resolving a conversation on a closed PR."
                    )
                    return base
            reply = f"Addressed by the software-engineering team in job `{child_job_id}`. {chosen_plan}"
            ok = _reply_and_resolve(client, request, comment, thread, reply, thread_history)
            base.outcome = "resolved" if ok else "failed"
            base.detail = chosen_plan if ok else "Reply/resolve step failed."
            return base

        # _read_cited_code's own contract calls for the PR head SHA (resolvable
        # via the base repo's contents API regardless of which repository the
        # branch itself lives in), NOT the branch short name (pr_head): for a
        # fork-opened PR, `pr_head` names a branch that may not exist in the
        # base repo at all, or may coincidentally collide with an unrelated
        # branch there, either way grounding triage on the wrong (or no) code.
        try:
            cited_code = _read_cited_code(client, request.owner, request.repo, comment, pr_head_sha)
        except CitedCodeUnavailableError as e:
            # Triage is meant to be evidence-grounded: proceeding on the
            # comment's prose alone (via a silent "") risks a false-positive
            # verdict resolving a thread that a readable file might have
            # shown to be a real issue. Fail this comment closed instead —
            # it surfaces again (and can be retried) on a later run rather
            # than being auto-resolved on absent evidence.
            base.detail = str(e)
            return base
        triage = _triage_comment(comment, cited_code, thread_history)

        # Re-check the PR's head SHA right after triage: the LLM call(s) above
        # can take a while, and the PR author can push a new commit in that
        # window. `pr_head_sha` is captured once before this call (refreshed
        # per-comment, not per-LLM-round-trip), so a push mid-triage leaves the
        # verdict grounded on code that is no longer current — a false-positive
        # would then resolve the thread over an obsolete read, and a real-issue
        # plan could recommend a change that no longer applies or is already
        # redundant with what the newer commit did. Best-effort: a failed
        # re-fetch degrades to proceeding on the original verdict rather than
        # blocking indefinitely on a transient API issue.
        stale_detail = _pr_became_stale(client, request, comment.id, pr_head_sha, "triaged")
        if stale_detail is not None:
            base.detail = stale_detail
            return base

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

        # Re-check the PR's head SHA again after planning: planning is itself
        # another LLM round-trip on top of triage, so a push can land during
        # this window too, not just during triage. Without this second check,
        # a plan built for pre-triage code could still be dispatched even
        # though the post-triage check above already passed.
        stale_detail = _pr_became_stale(client, request, comment.id, pr_head_sha, "planned")
        if stale_detail is not None:
            base.detail = stale_detail
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

        try:
            child_job_id = _dispatch_implementation(
                job_id,
                request,
                comment,
                plan,
                pr_head,
                pr_head_sha,
                pr_base,
                pr_url,
                pr_remote,
                token,
            )
        except Exception as e:
            # Usually a non-"completed" workflow result surfaced as a
            # RuntimeError (see _dispatch_implementation's own
            # postcondition), meaning a child job row and Temporal workflow
            # WERE created and may have committed work to the shared
            # `development` branch that never reached a clean publish —
            # every comment's branch preparation writes/reads the SAME
            # `khala.active-issue` marker (the bare PR number) for every
            # comment of this PR, so a LATER comment's branch prep would
            # otherwise treat this leftover, unpublished state as same-work
            # continuation and could publish it alongside (or instead of)
            # its own fix. (The other raise path — the PR's fork was
            # deleted, so no remote could be resolved — creates no job at
            # all and poses no such risk, but is flagged the same way here
            # too: erring toward the safe/conservative direction for a rare
            # edge case is cheaper than distinguishing it.)
            #
            # Widened from `except RuntimeError` to `except Exception`:
            # `_dispatch_implementation`'s own postcondition documents that
            # `execute_coding_team_workflow` can raise uncaught — a Temporal
            # RPC error, `WorkflowFailureError`, a cancellation, or anything
            # else — AFTER the child job row (and possibly the Temporal
            # workflow itself) was already created. Every one of those is
            # exactly the same "job row/workflow may exist, may have
            # committed unpublished work" situation as the RuntimeError
            # case, so narrowing to one exception type left the others to
            # propagate uncaught out of this function — skipping the
            # left_unpublished_work flag and the caller's run-stopping
            # safeguard for what is otherwise the identical hazard.
            base.detail = scrub_token_from_text(str(e))
            base.left_unpublished_work = True
            return base
        # `_dispatch_implementation` can block for a long time (an implementation
        # workflow, possibly reattaching across hours), and by the time it returns
        # the child workflow has ALREADY published to the PR's branch — that
        # mutation can't be undone or prevented from here. But the PR can ALSO
        # have been merged or closed during that same window, and without this
        # check the reply/resolve below would still fire against it: posting a
        # "fixed" comment and closing the conversation on a PR that is no longer
        # open. Best-effort — a failed re-check degrades to proceeding, matching
        # every other freshness check in this module.
        try:
            post_dispatch_pr = client.get_pull_request(request.owner, request.repo, request.pr_number)
        except Exception as e:  # noqa: BLE001 - best-effort; a failed re-check must not block
            logger.warning(
                "address-comments: could not re-check PR state after dispatching comment %s's "
                "implementation: %s",
                comment.id,
                scrub_token_from_text(str(e)),
            )
        else:
            if post_dispatch_pr.state != "open":
                base.detail = (
                    f"PR {request.owner}/{request.repo}#{request.pr_number} is no longer open "
                    f"(state={post_dispatch_pr.state}); the fix was published by job "
                    f"{child_job_id} but the thread was left as-is rather than replying to or "
                    "resolving a conversation on a closed PR."
                )
                return base
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


def unresolved_comments(client: GitHubClient, owner: str, repo: str, pr_number: int) -> UnresolvedCommentsResult:
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
        - Checks for cancellation (via :func:`_job_cancelled`, an operator
          using the normal job APIs) before starting, before the resolve-only
          retry step, before each comment's implementation is dispatched, and
          once more before the terminal status is written — stopping further
          work at whichever checkpoint finds it cancelled and, at every one,
          leaving the job's own ``"cancelled"`` status as the authority
          rather than overwriting it with a completion status. Does NOT
          preempt a child implementation workflow already in flight when
          cancellation is detected — only the address-comments run's own
          not-yet-dispatched work stops; an already-dispatched child keeps
          running to its own conclusion.
        - NEVER raises — the daemon thread cannot leave a job wedged.
    """
    owner, repo, pr_number = request.owner, request.repo, request.pr_number
    if _job_cancelled(job_id):
        # An operator can cancel this job (via the normal job APIs) before
        # this background hook even starts running. Writing status="running"
        # unconditionally below would silently undo that cancellation —
        # check first and skip the whole run rather than overwrite it.
        logger.info(
            "address-comments: job %s was already cancelled before starting; nothing to do",
            job_id,
        )
        return
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
            # Computed once and reused for every comment below, unlike
            # `pr`/`pr.head_sha` (refreshed per-comment): `_pr_head_remote`
            # derives ONLY from which repository the head branch lives in
            # (`pr.head_repo_full_name`) — same-repo vs. fork — which is
            # fixed for a PR's whole lifetime, unlike its head SHA. The one
            # exception is the fork being deleted mid-run (this snapshot's
            # non-None resolution going stale), which `_dispatch_implementation`
            # already guards: a `None` `pr_remote` raises there rather than
            # silently reusing a stale non-None value.
            pr_remote = _pr_head_remote(owner, repo, pr, client.web_host)
            (
                unresolved,
                thread_by_comment_id,
                retry_resolve_threads,
                thread_history_by_comment_id,
                ambiguous_threads,
            ) = _unresolved_comments(client, owner, repo, pr_number)

            if unresolved or ambiguous_threads:
                # New, actionable feedback is about to be worked through — a
                # stale WAITING_FOR_REVIEW_LABEL from an earlier successful run
                # would otherwise keep advertising "ready" for however long
                # THIS run takes, or forever if it fails outright (the label
                # is only ever added on success, never removed on failure).
                # Clear it up front rather than conditioning removal on this
                # run's own eventual outcome. `ambiguous_threads` (a reviewer
                # reopened a thread with no new message) never populates
                # `unresolved` — it is neither retried nor re-triaged — but it
                # is still genuinely unresolved on GitHub and would otherwise
                # leave the stale label untouched: `unresolved` and
                # `retry_resolve_threads` empty skips both this clear AND the
                # later `_mark_waiting_for_review` call, so nothing ever
                # updates the label at all for a run that finds only
                # ambiguous threads.
                _clear_waiting_for_review(client, owner, repo, pr_number)

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
            run_stopped_early = False
            if _job_cancelled(job_id):
                # An operator can cancel this job through the normal job APIs
                # at any point — check before this run does anything mutating
                # (resolving threads, dispatching implementations) rather
                # than finding out only at the very end, if at all.
                logger.info("address-comments: job %s was cancelled; stopping before any work", job_id)
                retry_resolve_ok = False
                run_stopped_early = True
            if retry_resolve_threads and not run_stopped_early:
                # This run's PR snapshot at the top of the block can be stale
                # by the time this loop runs (`_unresolved_comments` above can
                # take a while) — a PR that closed in that gap must not have
                # its threads resolved or, later, get labelled "waiting for
                # review": both would be acting on a PR no longer accepting
                # either. Best-effort, same style as this function's other
                # live re-checks: a failed check degrades to proceeding on the
                # last known (possibly stale) state rather than blocking.
                try:
                    retry_pr = client.get_pull_request(owner, repo, pr_number)
                except Exception as e:  # noqa: BLE001 - best-effort; a failed check must not block
                    logger.warning(
                        "address-comments: could not re-check PR state before resolve-only "
                        "retries: %s",
                        scrub_token_from_text(str(e)),
                    )
                else:
                    if retry_pr.state != "open":
                        logger.info(
                            "address-comments: PR %s/%s#%s is no longer open (state=%s); "
                            "skipping %d resolve-only retr%s",
                            owner,
                            repo,
                            pr_number,
                            retry_pr.state,
                            len(retry_resolve_threads),
                            "y" if len(retry_resolve_threads) == 1 else "ies",
                        )
                        retry_resolve_ok = False
                        run_stopped_early = True
                        # address-comments never revisits a closed PR, so any
                        # rows this PR left in the resolve-attempt ledger
                        # would otherwise never be cleared. Best-effort.
                        _main.clear_resolve_attempts_for_pr(owner, repo, pr_number)

            if not run_stopped_early:
                for thread_id, khala_reply_id in retry_resolve_threads:
                    if _thread_has_new_reviewer_feedback(
                        client,
                        owner,
                        repo,
                        pr_number,
                        thread_id,
                        khala_reply_id,
                        thread_history_by_comment_id.get(khala_reply_id),
                    ):
                        logger.info(
                            "address-comments: skipping resolve-retry on thread %s — newer "
                            "reviewer feedback appeared since generated reply %s was snapshotted",
                            thread_id,
                            khala_reply_id,
                        )
                        retry_resolve_ok = False
                        continue
                    if client.resolve_review_thread(thread_id):
                        # The retry succeeded — clear the ledger entry so a
                        # LATER, genuine reviewer reopen of this same thread
                        # (a fresh, unrelated event) is never mistaken for
                        # leftover evidence from this now-resolved attempt.
                        _main.clear_resolve_attempt(owner, repo, pr_number, thread_id)
                    else:
                        retry_resolve_ok = False
                        logger.warning(
                            "address-comments: retry-resolve failed for thread %s", thread_id
                        )
                        _main.record_resolve_failure(owner, repo, pr_number, thread_id, khala_reply_id)

            outcomes: List[CommentOutcome] = []
            for comment in unresolved:
                if run_stopped_early:
                    # Set by the retry-resolve section above (cancellation or
                    # PR closure detected before this loop even started) —
                    # skip straight through without a wasted PR-state
                    # refresh, matching the mid-loop break below.
                    break
                if _job_cancelled(job_id):
                    logger.info(
                        "address-comments: job %s was cancelled; stopping before comment %s "
                        "and any comment after it",
                        job_id,
                        comment.id,
                    )
                    run_stopped_early = True
                    break
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
                else:
                    if pr.state != "open":
                        # The PR was merged or closed by someone else while an
                        # earlier comment's implementation workflow was still
                        # running. Dispatching further workflows now would push
                        # commits to (and reply/resolve/label) a PR that is no
                        # longer accepting them — stop here rather than working
                        # through the rest of `unresolved`. The remaining
                        # comments are simply left unaddressed; they are not
                        # recorded as failures (nothing was attempted for them),
                        # and `all_succeeded` below is forced False so this run
                        # is never mistaken for a clean, complete one.
                        logger.info(
                            "address-comments: PR %s/%s#%s is no longer open (state=%s); "
                            "stopping before comment %s and any comment after it",
                            owner,
                            repo,
                            pr_number,
                            pr.state,
                            comment.id,
                        )
                        run_stopped_early = True
                        break
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
                if outcome.left_unpublished_work:
                    # This comment's implementation may have left partial,
                    # unpublished commits on the shared `development` branch
                    # (see CommentOutcome.left_unpublished_work). Every
                    # comment of this PR shares the SAME `khala.active-issue`
                    # marker, so a LATER comment's branch preparation would
                    # otherwise treat that leftover state as same-work
                    # continuation and could publish it alongside its own
                    # fix. Stop here rather than risk that — the remaining
                    # comments are left unaddressed (not recorded as
                    # failures; nothing was attempted for them) for a future
                    # run to pick up once this leftover state is resolved.
                    logger.info(
                        "address-comments: comment %s's implementation may have left "
                        "unpublished work on the shared development branch; stopping "
                        "before any comment after it",
                        comment.id,
                    )
                    run_stopped_early = True
                    break

            if (unresolved or retry_resolve_threads) and not run_stopped_early:
                # The per-iteration state check above catches the PR closing
                # BETWEEN comments, but not while the FINAL (or only)
                # comment's own `_handle_comment` call was still running —
                # that call can itself block for a long implementation
                # dispatch, and there is no next loop iteration to observe a
                # closure that happens during it. Also covers a retry-only
                # run (`unresolved` empty): the resolve-only retries above
                # already checked PR state before resolving, but the PR can
                # still close in the gap between that check and here. Re-check
                # once more here, after the loop, before this run's outcome
                # is judged successful and the label/cleanup actions below
                # are allowed
                # to run against what may now be a closed PR.
                try:
                    final_pr = client.get_pull_request(owner, repo, pr_number)
                except Exception as e:  # noqa: BLE001 - best-effort; a failed check must not block
                    logger.warning(
                        "address-comments: could not re-check PR state after the comment "
                        "loop finished: %s",
                        scrub_token_from_text(str(e)),
                    )
                else:
                    if final_pr.state != "open":
                        logger.info(
                            "address-comments: PR %s/%s#%s closed (state=%s) while the final "
                            "comment was being processed",
                            owner,
                            repo,
                            pr_number,
                            final_pr.state,
                        )
                        run_stopped_early = True

            # Every comment handled without failure AND every retry-resolve
            # succeeded: nothing is still owed to the reviewer, AS FAR AS THIS
            # RUN'S INITIAL SNAPSHOT SAW. A run that stopped early because the
            # PR closed mid-run is never "succeeded" even if every comment it
            # DID reach came back clean — comments after the stop point were
            # never attempted at all, and labelling/cleanup would be moot (and
            # potentially wrong, e.g. reopened-and-repushed) for a closed PR.
            all_succeeded = (
                retry_resolve_ok
                and not run_stopped_early
                and all(o.outcome != "failed" for o in outcomes)
            )
            if all_succeeded and _job_cancelled(job_id):
                # The per-comment loop's own cancellation check (above) only
                # catches cancellation BETWEEN comments — not one that arrives
                # during the LAST comment's own (possibly long-running)
                # `_handle_comment` call, with no further loop iteration left
                # to observe it, nor in the gap between that and here. Without
                # this, a cancelled parent could still fall through to the
                # label-apply / checkout-delete side effects below and
                # advertise success (and reclaim/delete its own workspace)
                # after cancellation had already taken effect. Same pattern as
                # this function's other `_job_cancelled` checkpoints — a
                # positive result forces `all_succeeded` False so nothing
                # below treats this run as complete.
                logger.info(
                    "address-comments: job %s was cancelled after the comment loop finished; "
                    "skipping label/cleanup side effects",
                    job_id,
                )
                all_succeeded = False
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
                # an UNCHANGED thread history — comparing just the latest
                # message's body is not enough: a reviewer can edit or delete
                # an EARLIER message in the thread (changing the context the
                # `not_an_issue` verdict was actually grounded on) without
                # touching the latest message's id or body at all. Compare
                # the full history snapshotted at triage time against a fresh
                # snapshot, the same way the implementation and false-positive
                # paths already do via `_thread_has_new_reviewer_feedback`.
                not_an_issue_ids = {o.comment_id for o in outcomes if o.outcome == "not_an_issue"}
                triaged_histories = {
                    c.id: thread_history_by_comment_id.get(c.id, [c])
                    for c in unresolved
                    if c.id in not_an_issue_ids
                }

                try:
                    (
                        fresh_unresolved,
                        _tbc,
                        fresh_retry,
                        fresh_history_by_comment_id,
                        fresh_ambiguous,
                    ) = _unresolved_comments(client, owner, repo, pr_number)
                    blocking = [
                        c
                        for c in fresh_unresolved
                        if not (
                            c.id in not_an_issue_ids
                            and _thread_history_unchanged(
                                triaged_histories.get(c.id),
                                fresh_history_by_comment_id.get(c.id),
                            )
                        )
                    ]
                    # An ambiguous thread (Khala's reply is the latest message,
                    # but no persisted evidence justifies auto-resolving it —
                    # see `ambiguous_threads` in `_unresolved_comments`'s
                    # docstring) is neither retried nor re-triaged, but it is
                    # still genuinely unresolved on GitHub: a reviewer's
                    # reopened conversation must keep blocking completion, not
                    # be silently reported as "nothing owed" just because it
                    # appears in neither `fresh_unresolved` nor `fresh_retry`.
                    all_succeeded = not blocking and not fresh_retry and not fresh_ambiguous
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
            # threads non-empty) still did real work and must be labelled too —
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

        if _job_cancelled(job_id):
            # Cancellation can arrive during the LAST comment's own
            # (possibly long-running) _handle_comment call, after the last
            # per-iteration check above — with no further loop iteration to
            # observe it. Overwriting an already-cancelled job's status with
            # a completion status here would silently undo the cancellation
            # (and the label/cleanup above may already have acted as if this
            # run succeeded). Leave the job's own cancelled status as the
            # authority instead of writing over it.
            logger.info(
                "address-comments: job %s was cancelled; leaving its status as-is "
                "rather than overwriting with a completion status",
                job_id,
            )
        else:
            summary = _build_summary(outcomes)
            # `all_succeeded` can be False for reasons that never produce a failed
            # CommentOutcome at all (a resolve-only retry failed, the final re-list
            # found new feedback, thread state became unverifiable, or the PR
            # closed mid-run) — the label/cleanup above are already correctly
            # skipped for those, but the terminal status must say so too, or a
            # caller polling job status sees "completed" with no indication that
            # work is still owed and another run is needed.
            terminal_status = (
                JobStatus.COMPLETED.value if all_succeeded else JobStatus.COMPLETED_WITH_FAILURES.value
            )
            _main.update_job(
                job_id,
                status=terminal_status,
                phase="completed",
                status_text=summary["status_text"],
                github_pr_url=pr.html_url,
                review_summary=summary,
            )
            _main.update_review(
                job_id,
                status=terminal_status,
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
