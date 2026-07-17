"""Filing GitHub issues from a completed PR review's pending findings.

Split out of ``pr_review.py``: everything here serves ``create_review_issues``
— the exceptions it raises, the per-job locking that serializes it within a
process and (via a Postgres advisory lock) across worker processes, and the
context load/merge/persist helpers that reconcile the job-service and durable
``code_review_runs`` copies of a review's proposals. The run-review flow stays
in ``pr_review.py``; nothing there calls into this module.

Invariants:
    - A proposal only ever transitions from unfiled to filed (``issue_url``
      set), never back; every code path below preserves that direction.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, NamedTuple, Optional

from software_engineering_team.coding_team.api import main as _main
from software_engineering_team.coding_team.github_source import (
    GitHubAPIError,
    build_issue_from_proposal,
    scrub_token_from_text,
)

logger = logging.getLogger(__name__)


class ReviewNotFoundError(LookupError):
    """Raised when no review (live job or persisted row) exists for a job id."""


class RepoMismatchError(ValueError):
    """Raised when the caller's expected owner/repo disagree with the stored review.

    Guards against filing issues into a repository other than the one that was
    actually reviewed — e.g. after the integration is repointed, or if a job id
    from a different (PAT-accessible) repository is submitted.
    """


class MultipleIssueCreationErrors(GitHubAPIError):
    """Raised when concurrently filing issues for multiple proposals fails for more than one.

    A ``GitHubAPIError`` subclass so the existing ``except GitHubAPIError`` branch
    in the route handler (→ HTTP 502) still catches it without any route change,
    while carrying every individual failure -- not just the one that happens to be
    re-raised -- so the caller's error message reflects the true extent of the
    failures rather than misleadingly naming only one proposal.
    """

    def __init__(self, failures: Dict[str, BaseException]) -> None:
        """Preconditions: ``failures`` is non-empty (proposal id -> the exception it raised)."""
        self.failures = dict(failures)
        summary = "; ".join(f"{pid}: {err}" for pid, err in failures.items())
        super().__init__(status=502, body=f"{len(failures)} proposal(s) failed to file: {summary}")


# Per-job locks serializing ``create_review_issues`` within this process, so two
# concurrent requests for the same review (two browser tabs, a double-click)
# cannot both load a proposal as unfiled and open duplicate GitHub issues. A
# WeakValueDictionary so a job's lock is evicted automatically once no request is
# using it, instead of accumulating one entry per job for the life of the process.
# The process-local lock alone does NOT serialize across the multiple worker
# processes a production deployment runs (see ``make deploy``) — cross-worker
# mutual exclusion is extended by the Postgres advisory lock in
# ``_issue_creation_lock``, mirroring ``_pr_review_admission``.
_ISSUE_CREATION_LOCKS: "weakref.WeakValueDictionary[str, threading.Lock]" = (
    weakref.WeakValueDictionary()
)
_ISSUE_CREATION_LOCKS_GUARD = threading.Lock()

# Max concurrent GitHub issue-creation calls when filing several proposals at
# once, mirroring _HEAD_FETCH_PARALLELISM's bound for this module's other
# independent-I/O fan-out.
_ISSUE_CREATION_PARALLELISM = 8


def _issue_creation_process_lock(job_id: str) -> threading.Lock:
    """Return the process-wide lock serializing issue creation for ``job_id``.

    Preconditions: ``_ISSUE_CREATION_LOCKS_GUARD`` protects the get-or-create
        check against a race between two callers for the same ``job_id``.
    Postconditions: returns the SAME ``Lock`` object to every caller currently
        holding (or waiting on) it for this ``job_id``; once no caller references
        it, the ``WeakValueDictionary`` entry is garbage-collected, so the
        registry never grows past the number of jobs with in-flight requests.
    """
    with _ISSUE_CREATION_LOCKS_GUARD:
        lock = _ISSUE_CREATION_LOCKS.get(job_id)
        if lock is None:
            lock = threading.Lock()
            _ISSUE_CREATION_LOCKS[job_id] = lock
        return lock


@contextlib.contextmanager
def _issue_creation_lock(job_id: str):
    """Mutual exclusion for filing GitHub issues from one review's proposals.

    Preconditions: ``job_id`` names the review whose issue-filing is being serialized.
    Postconditions: while the ``with`` body runs, no other issue-filing request for
        this ``job_id`` can run — in this process via
        :func:`_issue_creation_process_lock`, and across worker processes via a
        Postgres transaction-scoped advisory lock (``pg_advisory_xact_lock`` keyed
        on ``job_id``) when Postgres is configured, exactly mirroring
        ``_pr_review_admission``. Degrades to the process-local lock alone
        (logged) when Postgres is unconfigured or the lock cannot be taken —
        single-worker serialization stays intact, and the residual cross-worker
        window is the pre-lock behavior, never worse.
    """
    with _issue_creation_process_lock(job_id), contextlib.ExitStack() as stack:
        try:
            from shared_postgres import (  # noqa: PLC0415 - optional dep path
                get_conn,
                is_postgres_enabled,
            )

            if is_postgres_enabled():
                conn = stack.enter_context(get_conn())
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    ("coding_team_issue_creation", job_id),
                )
        except Exception:  # noqa: BLE001 - degrade to process-local locking, never block issue filing
            stack.pop_all().close()
            logger.warning(
                "could not take cross-worker issue-creation lock for job %s; "
                "falling back to process-local locking only",
                job_id,
                exc_info=True,
            )
        yield


class _ReviewIssueContext(NamedTuple):
    """A completed review's coordinates plus its (mutable) review summary.

    ``summary["pending_issue_proposals"]`` is the single source of truth for a
    review's proposals: callers read and mutate it there directly rather than
    through a second aliased field, so there is no aliasing invariant to
    maintain (or accidentally break) between two fields.
    """

    owner: str
    repo: str
    pr_number: int
    pr_url: str
    status: str
    summary: Dict[str, Any]


def _proposals_copy(summary: Any) -> List[Dict[str, Any]]:
    """Return an independent, mutable copy of a summary's pending issue proposals.

    Postconditions:
        - Returns a list of dict copies of ``summary["pending_issue_proposals"]``
          (each a fresh dict so mutating it never aliases the stored record), or
          ``[]`` when the field is absent or malformed. Never raises.
    """
    raw = summary.get("pending_issue_proposals") if isinstance(summary, dict) else None
    if not isinstance(raw, list):
        return []
    return [dict(p) for p in raw if isinstance(p, dict)]


def _merge_filed_proposals(
    preferred: List[Dict[str, Any]], other: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Merge two copies of a review's proposals, favoring whichever already filed an issue.

    Preconditions:
        - ``preferred`` and ``other`` are proposal-dict lists for the SAME review
          (matching ``id`` values), typically the job-service and durable-Postgres
          copies of ``pending_issue_proposals``.
    Postconditions:
        - Returns one entry per id in ``preferred``. A proposal only ever
          transitions from unfiled to filed, never back — so when
          ``preferred``'s copy of an id is still unfiled but ``other``'s copy
          already carries ``issue_url``, ``other``'s copy wins. This closes the
          race where one store's post-creation write succeeded while the
          other's failed (or simply has not been read since): whichever store
          IS up to date always overrides the one that is not.
    """
    other_by_id = {str(p.get("id")): p for p in other if p.get("id") is not None}
    merged: List[Dict[str, Any]] = []
    for p in preferred:
        other_p = other_by_id.get(str(p.get("id")))
        if other_p and other_p.get("issue_url") and not p.get("issue_url"):
            merged.append(dict(other_p))
        else:
            merged.append(dict(p))
    return merged


def _load_review_issue_context(job_id: str) -> Optional[_ReviewIssueContext]:
    """Load a completed review's repo coordinates and pending issue proposals.

    Reads the in-memory job first (present for the life of the session) for
    coordinates and status, then merges in the durable ``code_review_runs``
    row's proposals (survives restarts when Postgres is configured) — falling
    back to the row alone when the job has aged out.

    Postconditions:
        - Returns a context carrying the reviewed repository's owner/repo, the PR
          number/url, the review's terminal status, and a mutable review summary
          whose ``pending_issue_proposals`` is the merge of both stores' copies
          (see :func:`_merge_filed_proposals`), so neither store's lagging write
          can make an already-filed proposal look unfiled; or None when neither
          store knows the job.
    """
    job = _main.get_job(job_id)
    row = _main.get_review(job_id)

    if job:
        ctx = job.get("github_context") or {}
        owner = str(ctx.get("owner") or "")
        repo = str(ctx.get("repo") or "")
        pr_number = ctx.get("pr_number")
        if owner and repo and pr_number is not None:
            summary = dict(job.get("review_summary") or {})
            proposals = _proposals_copy(summary)
            if row:
                proposals = _merge_filed_proposals(
                    proposals, _proposals_copy(row.get("review_summary") or {})
                )
            summary["pending_issue_proposals"] = proposals
            return _ReviewIssueContext(
                owner=owner,
                repo=repo,
                pr_number=int(pr_number),
                pr_url=str(ctx.get("pr_url") or ""),
                status=str(job.get("status") or "completed"),
                summary=summary,
            )
    if row:
        pr_number = row.get("pr_number")
        summary = dict(row.get("review_summary") or {})
        summary["pending_issue_proposals"] = _proposals_copy(summary)
        return _ReviewIssueContext(
            owner=str(row.get("owner") or ""),
            repo=str(row.get("repo") or ""),
            # pr_number is NOT NULL in code_review_runs and record_review_start
            # always inserts a real int, so `is None` here cannot happen from a
            # legitimately-written row; the fallback is unreachable defense, not
            # a real "unknown PR" case.
            pr_number=int(pr_number) if pr_number is not None else 0,  # pragma: no cover
            pr_url=str(row.get("pr_url") or ""),
            status=str(row.get("status") or "completed"),
            summary=summary,
        )
    return None


def _persist_review_proposals(job_id: str, status: str, summary: Dict[str, Any]) -> None:
    """Write the updated review summary back to both stores (best-effort each).

    Postconditions:
        - Attempts ``update_job`` (in-memory; may have aged out) and
          ``update_review`` (durable). A failure of either is logged and
          swallowed — the newly-created GitHub issues already exist regardless of
          whether the local record is updated, so a store hiccup must not surface
          as a failed request. Never raises.
    """
    try:
        _main.update_job(job_id, review_summary=summary)
    except Exception:  # noqa: BLE001 - job may have aged out; the review row is the durable copy
        logger.warning(
            "could not update job %s review_summary after issue creation", job_id, exc_info=True
        )
    try:
        _main.update_review(job_id, status=status, review_summary=summary)
    except Exception:  # noqa: BLE001 - persistence is best-effort; the issues already exist
        logger.warning("could not update review row %s after issue creation", job_id, exc_info=True)


def create_review_issues(
    job_id: str,
    proposal_ids: List[str],
    token: str,
    *,
    expected_owner: Optional[str] = None,
    expected_repo: Optional[str] = None,
) -> Dict[str, Any]:
    """Open GitHub issues for the selected pre-existing findings of a review.

    Preconditions:
        - ``job_id`` names a completed PR review; ``proposal_ids`` are ids drawn
          from that review's ``pending_issue_proposals``; ``token`` is a GitHub
          PAT with issue-write scope on the reviewed repository.
        - ``expected_owner``/``expected_repo``, when supplied, are the repository
          the caller believes the review belongs to (the Code Review page passes
          the review row's own owner/repo). They are validated against the stored
          review so a mismatched or forged ``job_id`` cannot file issues into a
          different (PAT-accessible) repository than the one reviewed.
    Postconditions:
        - Runs under a per-``job_id`` lock (process-local AND, when Postgres is
          configured, a cross-worker Postgres advisory lock — see
          :func:`_issue_creation_lock`), so concurrent requests for the same
          review — even from different worker processes — are serialized and
          cannot both open an issue for one proposal.
        - For each requested proposal that exists and has not already been filed,
          opens one GitHub issue (carrying the finding's full detail, token-
          scrubbed) in the reviewed repository — fanned out concurrently — records
          the created issue's number/url on the proposal, and persists the
          updated proposals to both the job store and the durable review row.
          Idempotent: a proposal already carrying an ``issue_url`` is skipped, so
          a repeated request never opens a duplicate; an unknown id is ignored.
          ``issue_url`` may already be set before this function ever runs -- when
          ``annotate_duplicate_proposals`` matched the finding to a pre-existing
          open issue at review time -- and such a proposal is skipped exactly the
          same way, so a matched finding can never be filed as a second, duplicate
          issue.
          Returns ``{"job_id", "created", "proposals"}`` where ``created`` lists
          each newly-opened issue and ``proposals`` is the full, updated
          proposal list.
        - Raises :class:`ReviewNotFoundError` when neither store knows ``job_id``,
          and :class:`RepoMismatchError` when the expected owner/repo disagree with
          the stored review (owner/repo compared case-insensitively, as GitHub
          treats them). Raises ``GitHubAPIError`` when GitHub rejects an issue
          creation — every proposal's creation is attempted independently, so one
          rejection never stops another's, and any issue opened before the raise
          is still recorded and persisted. When exactly one proposal fails, its own
          exception is re-raised unchanged; when more than one fails, raises
          :class:`MultipleIssueCreationErrors` (a ``GitHubAPIError`` subclass)
          carrying every individual failure, so the caller's error is never
          misleadingly attributed to just one proposal when several actually failed.
    """
    # Serialize the whole load → create → persist section per job. The context is
    # loaded INSIDE the lock so a second same-process request that ran after the
    # first persisted an issue url re-reads it and skips the already-filed proposal.
    with _issue_creation_lock(job_id):
        ctx = _load_review_issue_context(job_id)
        if ctx is None:
            raise ReviewNotFoundError(job_id)

        if (
            expected_owner is not None
            and expected_repo is not None
            and (
                ctx.owner.casefold() != expected_owner.casefold()
                or ctx.repo.casefold() != expected_repo.casefold()
            )
        ):
            raise RepoMismatchError(
                f"review {job_id} belongs to {ctx.owner}/{ctx.repo}, "
                f"not the requested {expected_owner}/{expected_repo}"
            )

        proposals = ctx.summary["pending_issue_proposals"]
        # A proposal's id always comes from proposal_from_findings's f"p{index}"
        # (never None); the `is not None` filter is defense-in-depth against a
        # malformed stored record so a missing id can never collide under the
        # shared string key "None".
        by_id = {str(p.get("id")): p for p in proposals if p.get("id") is not None}
        # dict.fromkeys dedupes while preserving order: proposal_ids can repeat the
        # same id (a malformed/direct request, or a doubled UI click that lands as
        # one request), and each unique proposal must be filed exactly once — the
        # concurrent creates below have no other guard against two tasks for the
        # SAME proposal both observing issue_url unset before either writes it.
        needed = list(
            dict.fromkeys(
                pid for pid in proposal_ids if pid in by_id and not by_id[pid].get("issue_url")
            )
        )
        created: List[Dict[str, Any]] = []
        changed = False
        try:
            # Only open the client when there is genuinely something to file (a
            # requested proposal that has not already been filed), so a redundant
            # or all-unknown request makes no GitHub call.
            if needed:
                with _main.GitHubClient(token=token) as client:

                    def _file_one(pid: str) -> Optional[Dict[str, Any]]:
                        proposal = by_id[pid]
                        if proposal.get("issue_url"):
                            # Race-only defense: ``needed`` already filtered filed
                            # proposals and dedupes ids, so this is unreachable
                            # except when a concurrent writer files the proposal
                            # between that filter and this task running.
                            return None  # pragma: no cover
                        title, body = build_issue_from_proposal(
                            proposal, pr_number=ctx.pr_number, pr_url=ctx.pr_url
                        )
                        # The finding text is LLM output over the reviewed code and
                        # can echo a secret from it, exactly like the PR comments —
                        # scrub both title and body before anything reaches GitHub.
                        scrubbed_title = scrub_token_from_text(title)
                        issue = client.create_issue(
                            ctx.owner,
                            ctx.repo,
                            title=scrubbed_title,
                            body=scrub_token_from_text(body),
                        )
                        proposal["issue_number"] = issue.number
                        proposal["issue_url"] = issue.html_url
                        return {
                            "proposal_id": pid,
                            "issue_number": issue.number,
                            "issue_url": issue.html_url,
                            # The scrubbed title, matching what was actually filed —
                            # never the raw one, which can still carry a secret.
                            "title": scrubbed_title,
                        }

                    # Each proposal's issue-creation call is independent (a distinct
                    # proposal, no shared mutable state until its own result is
                    # folded in below), so fan them out concurrently instead of
                    # paying one sequential GitHub round-trip per proposal — the
                    # same pattern this module already uses for _fetch_head_files.
                    # Every future is drained (successes and failures alike)
                    # before any exception is re-raised, so one proposal's GitHub
                    # rejection never stops another's independent creation.
                    workers = min(_ISSUE_CREATION_PARALLELISM, len(needed))
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        futures = {executor.submit(_file_one, pid): pid for pid in needed}
                        errors: Dict[str, BaseException] = {}
                        for future in futures:
                            pid = futures[future]
                            try:
                                result = future.result()
                            except Exception as e:  # noqa: BLE001 - collected; re-raised below after every proposal has had its chance
                                errors[pid] = e
                                continue
                            if result is not None:
                                changed = True
                                created.append(result)
                    if errors:
                        # Log every failure, not just the one re-raised below — an
                        # operator debugging "why didn't proposal p3 get filed"
                        # must not lose its detail just because p1's error happened
                        # to be the one that propagated to the HTTP response.
                        for pid in needed:
                            if pid in errors:
                                logger.warning(
                                    "create_review_issues: proposal %s failed for job %s: %s",
                                    pid,
                                    job_id,
                                    errors[pid],
                                )
                        if len(errors) == 1:
                            # A single failure: re-raise it as-is so its own type
                            # (e.g. a specific GitHubAPIError subclass) still
                            # reaches the caller unchanged.
                            raise next(iter(errors.values()))
                        # More than one proposal failed: a plain re-raise of
                        # whichever happened to be first would misleadingly
                        # report only one failure. Wrap every failure into a
                        # single composite error instead, so the caller's error
                        # message reflects the true extent of the failures.
                        raise MultipleIssueCreationErrors(errors)
        finally:
            # Persist whatever was created — even when some proposals failed — so
            # a partially-successful request never loses the issues it did open.
            if changed:
                _persist_review_proposals(job_id, ctx.status, ctx.summary)
        return {"job_id": job_id, "created": created, "proposals": proposals}
