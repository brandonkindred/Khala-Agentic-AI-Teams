"""coding_team API — orchestrator wiring and the github-hook run flow.

Monkeypatched collaborators are dereferenced through the ``main`` module object
at call time so ``monkeypatch.setattr(main, ...)`` keeps taking effect after the
split; models are imported directly.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from shared.env_config import env_bool
from shared.git.git_utils import DEVELOPMENT_BRANCH
from software_engineering_team import hitl
from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import (
    RunFromGitHubRequest,
)
from software_engineering_team.github_source import (
    GitHubAPIError,
    Issue,
    scrub_token_from_text,
)
from software_engineering_team.job_store import DEFAULT_CACHE_DIR
from software_engineering_team.models import CodingTeamPlanInput, JobStatus

logger = logging.getLogger(__name__)


def plan_from_input(plan_input: Dict[str, Any], repo_path: str) -> CodingTeamPlanInput:
    """Validate a raw plan dict into a ``CodingTeamPlanInput``, binding *repo_path*.

    Single source of the "merge the request's repo_path into the plan" convention:
    the ``repo_path`` from the request authoritatively overrides any ``repo_path``
    embedded in the plan payload, so the orchestrator always runs against the
    checkout the caller named.

    Preconditions: ``plan_input`` is a mapping (a plan payload); ``repo_path`` is
    the request's repository path.
    Postconditions: returns a validated ``CodingTeamPlanInput`` whose ``repo_path``
    is *repo_path*. Raises ``pydantic.ValidationError`` on an invalid payload.
    """
    return CodingTeamPlanInput.model_validate({**plan_input, "repo_path": repo_path})


def run_orchestrator_wired(
    job_id: str,
    repo_path: str,
    plan: CodingTeamPlanInput,
    *,
    pause_strategy: str = "block",
    acknowledged_resume_token: Optional[str] = None,
    update_job_fn: Optional[Callable[..., None]] = None,
) -> Optional[Dict[str, Any]]:
    """Run the coding-team orchestrator for *job_id* with the standard job-store wiring.

    Single source of the ``(update_job_fn, get_job_fn, cache_dir)`` wiring shared by
    the Temporal pipeline activity (and any remaining direct callers), so it cannot
    drift between them. The github-source path wires a custom ``update_job_fn``
    (+ ``on_pause``) and deliberately does not use this; the Temporal GitHub path
    passes ``_defer_terminal_success(job_id)`` via ``update_job_fn`` so publish
    still sees a non-terminal job.

    ``pause_strategy``/``acknowledged_resume_token`` are forwarded unchanged into
    ``run_coding_team_orchestrator`` — see that function's docstring for the full
    contract. Callers that omit both keep requesting ``"block"`` (legacy thread-mode
    default); the Temporal activity path (``run_pipeline_activity``) passes
    ``pause_strategy="return"``.

    Preconditions:
        - ``job_id`` names an existing job in the process job store; ``plan`` is a
          validated ``CodingTeamPlanInput`` whose ``repo_path`` equals *repo_path*.
        - ``update_job_fn``, when supplied, is a callable accepting keyword job
          field updates (same contract as ``update_job`` for ``job_id``).
    Postconditions:
        - ``pause_strategy="block"``: returns ``None``, unchanged from every caller's
          behavior before this parameter existed. The orchestrator has run to
          completion (or raised); job state is persisted through ``update_job``.
          Propagates the orchestrator's exceptions unchanged — callers own their own
          failure handling.
        - ``pause_strategy="return"``: returns the orchestrator's
          ``{"outcome": "paused", ...}`` dict when a HITL gate paused, or ``None`` when
          the pipeline instead reached a terminal state.
        - Uses ``update_job_fn`` when provided; otherwise the default
          ``lambda **kw: update_job(job_id, **kw)`` wiring.
    """
    return _main.run_coding_team_orchestrator(
        job_id,
        repo_path,
        plan,
        update_job_fn=update_job_fn or (lambda **kw: _main.update_job(job_id, **kw)),
        get_job_fn=_main.get_job,
        cache_dir=DEFAULT_CACHE_DIR,
        pause_strategy=pause_strategy,
        acknowledged_resume_token=acknowledged_resume_token,
    )


def _running_job_for_issue(owner: str, repo: str, issue_number: int) -> Optional[str]:
    """Return the job_id of any non-terminal job already working this issue.

    Owner/repo compare case-insensitively — GitHub treats them as case-insensitive, so two
    casings of the same repository are the same repository here too.

    Performance: this is an O(active-jobs) linear scan over the non-terminal set on each
    run-from-issue request. That set is small in practice (a handful of concurrent runs), so the
    scan is acceptable; if active-job volume ever grows materially, add an owner/repo/issue filter
    to ``list_jobs`` (or an in-memory index) rather than scanning here.
    """
    for j in _main.list_jobs(active_only=True):
        ctx = (j or {}).get("github_context") or {}
        if (
            str(ctx.get("owner") or "").casefold() == owner.casefold()
            and str(ctx.get("repo") or "").casefold() == repo.casefold()
            and ctx.get("issue_number") == issue_number
        ):
            return j.get("job_id")
    return None


# ---------------------------------------------------------------------------
# GitHub-hook run flow (moved from git_ops for cohesion): drives an
# orchestrator run for an issue and publishes/reports the outcome.
# ---------------------------------------------------------------------------


def _record_failure(
    client: _main.GitHubClient, owner: str, repo: str, num: int, job_id: str, error: str
) -> None:
    """Mark the job failed, capture the error, and post a (scrubbed) comment.

    Used for every post-orchestrator failure so callers polling /status see a
    consistent ``status="failed"`` instead of stale ``status="completed"``.
    """
    safe = scrub_token_from_text(error)
    # status_text/current_activity are reset so a failed job cannot keep claiming
    # mid-review progress (e.g. a frozen "Reviewing PR #7 (85%)" line) forever.
    # Unlike _record_review_outage, this deliberately does NOT set phase="completed":
    # it is the generic failure recorder used across the pipeline, so it leaves the
    # job's last-known phase intact for diagnosis. The review-outage path is a
    # terminal post-review state, so it marks the phase completed to match the
    # success/provider-abort paths.
    _main.update_job(
        job_id, status=JobStatus.FAILED.value, error=safe, status_text=None, current_activity=None
    )
    # No-op for non-review jobs (no matching code_review_runs row); persists the
    # failure for review jobs so the Code Review page shows the failed outcome.
    _main.update_review(job_id, status=JobStatus.FAILED.value, error=safe, completed=True)
    _main._safe_comment(client, owner, repo, num, f"Coding team job `{job_id}` failed: {safe}")


# Neutral, non-blocking note posted (at most once) when an automated review could
# not complete. Deliberately carries no exception text, class name, or job id —
# a reviewer-side outage is not a code defect, so the PR gets a calm "re-run it"
# message while the real detail lives in the job/review store.
_REVIEW_OUTAGE_NOTICE = (
    "Automated code review could not complete and did not post findings; it can be re-run."
)


def _post_outage_notice_enabled() -> bool:
    """Whether a review outage posts the neutral PR note (default: on).

    Postconditions:
        - Returns ``False`` only for an explicit falsy ``PR_REVIEW_POST_OUTAGE_NOTICE``
          (``false``/``0``/``no``/``off``); unset or anything else is ``True``.
          Setting it off makes a review outage completely silent on the PR (the
          failure is still recorded in the job/review store).
    """
    return env_bool("PR_REVIEW_POST_OUTAGE_NOTICE", default=True)


def _record_review_outage(
    client: _main.GitHubClient, owner: str, repo: str, num: int, job_id: str, error: str
) -> None:
    """Mark a review job failed for a reviewer-side outage without posting the raw error.

    The graceful-degradation counterpart to ``_record_failure``: instead of
    posting the scrubbed error text as a ``Coding team job X failed: ...`` PR
    comment, it records the real detail only in the job/review store — where
    operators and the Code Review page can still see it — and posts at most a
    single neutral, non-blocking note to the PR (gated by
    ``PR_REVIEW_POST_OUTAGE_NOTICE``). Used for transient reviewer outages (the
    LLM unavailable, a reasoning-only exhaustion the reviewer could not recover,
    or a reviewer that returned no output) so a tooling hiccup never surfaces as a
    raw exception / "job failed" comment on the pull request.

    Postconditions:
        - The job and review row are marked ``failed`` with the scrubbed ``error``
          captured for diagnosis; ``phase`` is set to the terminal ``completed``
          (matching the success/provider-abort paths) and
          ``status_text``/``current_activity`` are reset (as in ``_record_failure``)
          so the failed job cannot keep claiming a mid-review phase or progress. A
          neutral PR note is posted iff ``PR_REVIEW_POST_OUTAGE_NOTICE`` is enabled;
          the raw error is never posted to the PR.
    """
    safe = scrub_token_from_text(error)
    _main.update_job(
        job_id,
        status=JobStatus.FAILED.value,
        phase="completed",
        error=safe,
        status_text=None,
        current_activity=None,
    )
    _main.update_review(job_id, status=JobStatus.FAILED.value, error=safe, completed=True)
    if _post_outage_notice_enabled():
        _main._safe_comment(client, owner, repo, num, _REVIEW_OUTAGE_NOTICE)


def _has_merged_tasks(job: Dict[str, Any]) -> bool:
    """True iff the job landed at least one REAL merge — a task that is MERGED and actually changed
    code. Tasks the Tech Lead adjudicated as already-done (``resolved_without_changes``) are MERGED
    but landed no diff on ``development``, so they do not count: a job whose only merged tasks are
    such no-op resolutions has nothing to publish, and treating them as publishable would push an
    empty branch / open a no-op PR instead of reporting that no real work landed."""
    return any(
        (t or {}).get("status") == "merged" and not (t or {}).get("resolved_without_changes")
        for t in (job.get("task_graph_snapshot") or [])
    )


def _failed_tasks(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Tasks that reached the terminal FAILED state (rejected past the revision cap, blocked by a
    failed dependency, or an unrecoverable implementation/review error)."""
    return [
        t for t in (job.get("task_graph_snapshot") or []) if (t or {}).get("status") == "failed"
    ]


def _format_failed_tasks(failed: List[Dict[str, Any]]) -> str:
    """Render a markdown bullet list of failed tasks for a PR body / issue comment."""
    return "\n".join(
        f"- `{(t.get('id') or '?')}`: {((t.get('title') or '').strip() or 'untitled')}"
        for t in failed
    )


def _truncate_title(title: str, issue_num: int, limit: int = 256) -> str:
    suffix = f" (closes #{issue_num})"
    head = title[: max(0, limit - len(suffix))].rstrip()
    return f"{head}{suffix}" if head else f"Issue #{issue_num}{suffix}"


def _defer_terminal_success(job_id: str):
    """Build an ``update_job_fn`` that holds the job non-terminal until publish.

    The orchestrator marks its job ``completed`` when the code work finishes,
    but the GitHub hook keeps mutating the shared checkout afterwards
    (fast-forward, push, PR creation, marker clear) and the busy-checkout
    guard keys liveness off the job store's non-terminal statuses. Mapping the orchestrator's
    terminal success to ``(running, publishing)`` keeps the job visible to
    the guard for that whole window; ``_run_with_github_hooks`` sets the real
    terminal status only once it is fully done with the checkout. Failure
    statuses pass through unchanged — every post-orchestrator failure path
    stops touching the checkout.

    Postconditions:
        - The returned callable forwards every update to ``update_job`` for
          ``job_id``, rewriting only ``status="completed"`` updates.
    """

    def _update(**kw: Any) -> None:
        if kw.get("status") in hitl.TERMINAL_SUCCESS_STATUSES:
            kw = {**kw, "status": JobStatus.RUNNING.value, "phase": "publishing"}
        _main.update_job(job_id, **kw)

    return _update


def _finish_already_complete(
    client: Any,
    job_id: str,
    request: RunFromGitHubRequest,
    issue: Issue,
    job_after: Dict[str, Any],
) -> None:
    """Report an already-complete no-op run: recommend closing the issue, clean up, mark done.

    The team determined the issue's work was already done (planning recognized it,
    or every task resolved as already-satisfied with no real diff), so no PR is
    opened. This is a clean no-op success, so it runs the SAME checkout cleanup as
    the merged-work path — otherwise it leaves the active-issue marker set (a later
    same-issue retry would treat stale local state as interrupted progress) and
    leaks the per-issue clone when ``cleanup_checkout_on_success`` is set.

    Preconditions:
        - ``client`` is an open ``GitHubClient``; ``job_after`` is the post-run job
          record whose ``already_complete`` flag is set.
    Postconditions:
        - Posts the close-recommendation comment, clears the active-issue marker,
          runs the optional checkout cleanup BEFORE the terminal write (so the job
          stays in ``list_jobs(active_only=True)`` during the rmtree), then marks
          the job ``already_complete``.
    """
    owner, repo, num = request.owner, request.repo, issue.number
    evidence = str(job_after.get("completion_evidence") or "").strip()
    body = f"Coding team job `{job_id}`: this work appears to be already complete"
    if evidence:
        body += f" — {evidence}"
    body += f"\n\nNo changes were needed. Recommend closing #{num}."
    _main._safe_comment(client, owner, repo, num, body)
    _main._clear_active_issue_if_matches(request.repo_path, num)
    if request.cleanup_checkout_on_success:
        _main._cleanup_issue_checkout(request.repo_path)
    _main.update_job(
        job_id,
        status=JobStatus.ALREADY_COMPLETE.value,
        phase="completed",
        status_text="Work already complete; no changes needed",
    )


def _publish_merged_work(
    client: Any,
    job_id: str,
    request: RunFromGitHubRequest,
    issue: Issue,
    base: str,
    integration_branch: str,
    token: str,
) -> None:
    """Publish the merged work: fast-forward, push, open/reuse the draft PR, comment, finalize.

    Some tasks may have merged while others reached a terminal FAILED state; the
    merged work is still published, but the PR reference keyword and the terminal
    job status surface the gap rather than presenting incomplete work as a clean
    success (``Refs`` + ``completed_with_failures`` when any task failed, ``Closes``
    + ``completed`` otherwise).

    Preconditions:
        - Called only after the orchestrator produced at least one merged task and
          did not end failed/cancelled/waiting.
    Postconditions:
        - On success the integration branch is fast-forwarded and pushed, a draft
          PR is created or its body refreshed, the active-issue marker is cleared,
          the optional checkout cleanup runs (clean completion only) BEFORE the
          terminal status write, and the job ends ``completed``/
          ``completed_with_failures``. Every failure path records the failure via
          ``_record_failure`` and returns, retaining the marker for a retry.
    """
    owner, repo, num = request.owner, request.repo, issue.number

    ff_ok, ff_err = _main._fast_forward(request.repo_path, integration_branch, DEVELOPMENT_BRANCH)
    if not ff_ok:
        _record_failure(client, owner, repo, num, job_id, f"fast-forward failed: {ff_err}")
        return

    push_ok, push_err = _main._push_branch(
        request.repo_path, request.remote, integration_branch, token
    )
    if not push_ok:
        _record_failure(client, owner, repo, num, job_id, f"git push failed: {push_err}")
        return

    try:
        existing = client.find_existing_pr(owner, repo, integration_branch)
    except GitHubAPIError as e:
        _record_failure(client, owner, repo, num, job_id, f"github find_existing_pr: {e}")
        return

    # Only auto-close the issue when every task landed. A partial result still
    # leaves requested work undone, so use a non-closing reference ("Refs") to
    # avoid closing the issue when the PR merges into the default branch.
    failed = _failed_tasks(_main.get_job(job_id) or {})
    ref_keyword = "Refs" if failed else "Closes"
    pr_body = f"{ref_keyword} #{num}\n\nGenerated by Khala coding team job `{job_id}`."
    if failed:
        pr_body += (
            f"\n\n> ⚠️ {len(failed)} task(s) did not complete and are **not** included in "
            f"this PR:\n{_format_failed_tasks(failed)}"
        )

    if existing is not None:
        pr_url, created = existing.html_url, False
        # Always refresh the reused PR's body so it reflects the latest run: add a
        # partial-failure warning when this run left tasks unfinished, and clear a stale
        # warning (and old job id) from an earlier partial run that a later retry completed.
        try:
            updated = client.update_pull_request(
                owner=owner, repo=repo, number=existing.number, body=pr_body
            )
            pr_url = updated.html_url
        except GitHubAPIError as e:
            # Non-fatal: the warning (if any) is still posted as a comment below.
            logger.warning("Failed to update reused PR #%s body: %s", existing.number, e)
    else:
        try:
            pr = client.create_pull_request(
                owner=owner,
                repo=repo,
                title=_truncate_title(issue.title, num),
                head=integration_branch,
                base=base,
                body=pr_body,
                draft=True,
            )
        except GitHubAPIError as e:
            _record_failure(client, owner, repo, num, job_id, f"github create_pull_request: {e}")
            return
        pr_url, created = pr.html_url, True

    _main.update_job(job_id, github_pr_url=pr_url, integration_branch=integration_branch)
    if created:
        _main._safe_comment(client, owner, repo, num, f"Draft PR opened: {pr_url}")
    else:
        _main._safe_comment(client, owner, repo, num, f"Reusing existing draft PR: {pr_url}")
    if failed:
        _main._safe_comment(
            client,
            owner,
            repo,
            num,
            f"⚠️ {len(failed)} task(s) did not complete and were not merged:\n"
            f"{_format_failed_tasks(failed)}",
        )
    # Publication is the marker's end of life: the work now lives on the remote PR
    # branch, so the checkout no longer holds unpublished work for this issue.
    # Every earlier return retains the marker so a retry continues from
    # development instead of starting over. Scoped to this job's issue: a sibling
    # job for another issue may have re-marked the checkout since this job prepped.
    _main._clear_active_issue_if_matches(request.repo_path, num)

    # Drop the per-issue clone only on a clean completion: every task merged and the
    # work published, so nothing local is unrecoverable. A partial result keeps the
    # checkout so a retry can seed from its local progress. Cleanup runs BEFORE the
    # terminal status update so the job stays in list_jobs(active_only=True) during
    # the rmtree: a quick same-issue retry is then rejected by the duplicate guard
    # in /run-from-github instead of cloning into a directory mid-rmtree.
    if not failed and request.cleanup_checkout_on_success:
        _main._cleanup_issue_checkout(request.repo_path)

    # Terminal status comes last: the busy-checkout guard treats a terminal job as
    # done with the checkout, so this must be the final action after every
    # checkout-touching step above (including the cleanup rmtree). A job that merged
    # some work but also has failed tasks is reported as a partial success.
    _main.update_job(
        job_id,
        status=(JobStatus.COMPLETED_WITH_FAILURES.value if failed else JobStatus.COMPLETED.value),
        phase="completed",
    )


def _run_with_github_hooks(
    job_id: str,
    request: RunFromGitHubRequest,
    plan: CodingTeamPlanInput,
    issue: Issue,
    token: str,
) -> None:
    """Wrap the orchestrator with GitHub-side actions: comments, branch prep, push, PR."""
    owner, repo, num = request.owner, request.repo, issue.number
    integration_branch = f"khala/issue-{num}"

    with _main.GitHubClient(token=token) as client:
        # Validate the token via get_repo *before* posting the start-comment
        # so a bad token surfaces a single failure event on the issue rather
        # than a silently-dropped comment + a separate failure later.
        try:
            default_branch = client.get_repo(owner, repo).default_branch
        except GitHubAPIError as e:
            _record_failure(client, owner, repo, num, job_id, f"github get_repo: {e}")
            return
        base = request.base_branch or default_branch

        # Branch prep mutates the shared checkout; never do that under a
        # sibling job that is actively working it. Leftovers from DEAD jobs
        # are recovered below — live work is not a leftover.
        sibling = _main._running_sibling_on_checkout(request.repo_path, job_id)
        if sibling is not None:
            sib_ctx = sibling.get("github_context") or {}
            _record_failure(
                client,
                owner,
                repo,
                num,
                job_id,
                f"checkout busy: job `{sibling.get('job_id')}` "
                f"(issue #{sib_ctx.get('issue_number', '?')}) is still running on this "
                f"checkout; retry after it finishes",
            )
            return

        _main._safe_comment(client, owner, repo, num, f"Coding team started job `{job_id}`.")

        prep_ok, prep_err, prep_notes = _main._prepare_issue_branch(
            request.repo_path, request.remote, base, integration_branch, token, issue_number=num
        )
        if not prep_ok:
            _record_failure(client, owner, repo, num, job_id, f"branch prep failed: {prep_err}")
            return
        for note in prep_notes:
            _main._safe_comment(client, owner, repo, num, note)

        # When the coding team pauses for a user decision, surface the questions on the issue so a
        # human can answer them (via POST /run/{job_id}/answers); the hook thread stays blocked in
        # the orchestrator's wait until they do.
        def _on_pause(questions: List[Dict[str, Any]]) -> None:
            _main._safe_comment(
                client, owner, repo, num, _main._format_questions_comment(questions, job_id)
            )

        _main._register_run_thread(job_id)
        try:
            _main.run_coding_team_orchestrator(
                job_id,
                request.repo_path,
                plan,
                update_job_fn=_defer_terminal_success(job_id),
                get_job_fn=lambda jid: _main.get_job(jid),
                cache_dir=DEFAULT_CACHE_DIR,
                on_pause=_on_pause,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Coding team orchestrator failed: %s", e)
            _record_failure(client, owner, repo, num, job_id, str(e))
            return

        job_after = _main.get_job(job_id) or {}
        # The orchestrator may have already set a terminal/paused status — e.g. a decision pause
        # timed out (status=failed) or is still waiting for the user. Surface that diagnostic rather
        # than overwriting it with the generic "no merged tasks" message, which would hide the real
        # cause (an unanswered question) from the operator.
        if job_after.get("status") in (
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
            JobStatus.WAITING_FOR_USER.value,
        ):
            reason = (
                job_after.get("error") or job_after.get("status_text") or job_after.get("status")
            )
            _main._safe_comment(
                client, owner, repo, num, f"Coding team job `{job_id}` did not complete: {reason}"
            )
            return

        if job_after.get("already_complete"):
            # The team determined the issue's work was already done (planning
            # recognized it, or every task resolved as already-satisfied with no
            # real diff). Recommend closing the issue; do NOT open a no-op PR.
            _finish_already_complete(client, job_id, request, issue, job_after)
            return

        if not _has_merged_tasks(job_after):
            _main.update_job(
                job_id,
                status=JobStatus.FAILED.value,
                error="orchestrator produced no merged tasks",
            )
            _main._safe_comment(
                client,
                owner,
                repo,
                num,
                f"Coding team job `{job_id}` finished but produced no merged tasks.",
            )
            return

        _publish_merged_work(client, job_id, request, issue, base, integration_branch, token)
