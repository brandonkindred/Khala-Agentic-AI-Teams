"""coding_team API — orchestrator-thread lifecycle: plan wiring, run/resume/hook thread spawning, auto-resume.

Monkeypatched collaborators are dereferenced through the ``main`` module object
at call time so ``monkeypatch.setattr(main, ...)`` keeps taking effect after the
split; models are imported directly.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

# Ensure backend/agents is on path for coding_team and job_service_client
from coding_team import hitl
from coding_team.api import main as _main
from coding_team.api.models import (
    RunFromGitHubRequest,
)
from coding_team.api.state import (
    _ANSWER_WAIT_HEARTBEAT_STALE_S,
)
from coding_team.github_source import (
    Issue,
)
from coding_team.job_store import (
    DEFAULT_CACHE_DIR,
    RESUME_CLAIM_TTL_S,
)
from coding_team.models import CodingTeamPlanInput
from coding_team.token_crypto import decrypt_token

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


def run_orchestrator_wired(job_id: str, repo_path: str, plan: CodingTeamPlanInput) -> None:
    """Run the coding-team orchestrator for *job_id* with the standard job-store wiring.

    Single source of the ``(update_job_fn, get_job_fn, cache_dir)`` wiring shared
    by the POST /run background thread, the resume path, and the Temporal
    activity, so it cannot drift between them. The github-source path wires a
    custom ``update_job_fn`` (+ ``on_pause``) and deliberately does not use this.

    Preconditions:
        - ``job_id`` names an existing job in the process job store; ``plan`` is a
          validated ``CodingTeamPlanInput`` whose ``repo_path`` equals *repo_path*.
    Postconditions:
        - The orchestrator has run to completion (or raised); job state is
          persisted through ``update_job``. Propagates the orchestrator's
          exceptions unchanged — callers own their own failure handling.
    """
    _main.run_coding_team_orchestrator(
        job_id,
        repo_path,
        plan,
        update_job_fn=lambda **kw: _main.update_job(job_id, **kw),
        get_job_fn=_main.get_job,
        cache_dir=DEFAULT_CACHE_DIR,
    )


def _start_orchestrator_thread(job_id: str, repo_path: str, plan: CodingTeamPlanInput) -> None:
    """Spawn the daemon orchestrator thread for a job whose run-thread claim is held.

    Preconditions:
        - The caller holds the run-thread claim for ``job_id`` (via ``_claim_run_thread``).
    Postconditions:
        - A daemon thread is running the orchestrator; the claim is released by the thread's
          ``finally`` (or here, if the thread never started — in which case the exception
          propagates so the job stays resumable).
    """

    def run() -> None:
        try:
            # Registration is inside the try so the finally always releases the claim — even if
            # _register_run_thread itself fails — instead of leaving it wedged in _starting_run_jobs.
            _main._register_run_thread(job_id)
            _main.run_orchestrator_wired(job_id, repo_path, plan)
        except Exception as e:
            logger.exception("Coding team orchestrator resume failed: %s", e)
            _main.update_job(job_id, status="failed", error=str(e), current_activity=None)
        finally:
            _main._clear_run_thread(job_id)

    try:
        # The dead attempt may have left a mid-review current_activity behind (its
        # finally clears never ran); wipe it so the UI does not render a frozen
        # sub-bar through the resumed run's early phases. This sits INSIDE the
        # claim-releasing try: it is the first job-service write after the claim,
        # and a raise here (store outage) that escaped without releasing would
        # wedge the job — every later /resume would see the claim and no-op.
        _main.update_job(job_id, current_activity=None)
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        # The thread never started, so run()'s finally will never release the claim — release it
        # here so the job stays resumable instead of being wedged in _starting_run_jobs.
        _main._clear_run_thread(job_id)
        raise


def _start_github_resume_thread(
    job_id: str, ctx: Dict[str, Any], repo_path: str, plan: CodingTeamPlanInput, token: str
) -> None:
    """Spawn a resume of a GitHub-issue job through the full hook path (comments, branch prep, PR).

    A plain orchestrator restart would silently drop publication (no PR, no issue comments), so
    GitHub-issue jobs must resume through ``_run_with_github_hooks``. The spawned thread registers
    itself in the run-thread registry immediately — before any GitHub I/O — so liveness checks see
    it and the claim is released.

    Preconditions:
        - The caller holds the run-thread claim for ``job_id``; ``ctx`` carries ``owner``, ``repo``
          and ``issue_number``; ``token`` is a non-empty GitHub token.
    Postconditions:
        - A daemon thread is running the hook-path resume; on thread-start failure the claim is
          released and the exception propagates. A failed issue re-fetch inside the thread marks
          the job failed rather than silently degrading to the hook-less path.
        - The resumed run reproduces the fresh run's checkout-cleanup decision, read from
          ``ctx['cleanup_checkout_on_success']`` (absent for jobs persisted before this field
          existed → ``False``, the safe no-cleanup default).
    """
    request = RunFromGitHubRequest(
        owner=str(ctx["owner"]),
        repo=str(ctx["repo"]),
        repo_path=repo_path,
        issue_number=int(ctx["issue_number"]),
        base_branch=ctx.get("base_branch"),
        remote=str(ctx.get("remote") or "origin"),
        # `is True` (not bool()) so any non-bool persisted value — e.g. a string
        # "False" from a future serialization change, which bool() would read as
        # truthy — fails safe to no-cleanup rather than deleting the checkout.
        cleanup_checkout_on_success=ctx.get("cleanup_checkout_on_success") is True,
    )

    def run() -> None:
        try:
            # Registration is inside the try so the finally always releases the claim — even if
            # _register_run_thread itself fails — instead of leaving it wedged in _starting_run_jobs.
            _main._register_run_thread(job_id)
            # Advance the job out of waiting_for_user BEFORE the GitHub network I/O. The
            # cross-worker resume claim (claim_resume) has a TTL of RESUME_CLAIM_TTL_S; if the
            # issue fetch or branch prep takes longer than that, another worker could treat the
            # expired claim as abandoned and spawn a second hook path. Moving the status to
            # "running" here makes _try_auto_resume and resume_job decline (they only proceed for
            # waiting_for_user), so the re-claiming window closes before the slow I/O begins.
            _main.update_job(job_id, status="running", status_text="Resuming via GitHub hook…")
            with _main.GitHubClient(token=token) as client:
                issue = client.get_issue(request.owner, request.repo, int(ctx["issue_number"]))
            _main._run_with_github_hooks(job_id, request, plan, issue, token)
        except Exception as e:
            logger.exception("GitHub-path resume failed for job %s: %s", job_id, e)
            _main.update_job(job_id, status="failed", error=f"resume failed: {e}")
        finally:
            _main._clear_run_thread(job_id)

    try:
        # Mirror _start_orchestrator_thread: a dead prior attempt may have left a mid-review
        # current_activity behind (its finally never ran), which would render a frozen sub-bar
        # through the resumed run's early phases. Wipe it first. This is the first job-service
        # write after the claim, inside the claim-releasing try, so a store-outage raise here is
        # handled by the except below rather than wedging the job.
        _main.update_job(job_id, current_activity=None)
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        _main._clear_run_thread(job_id)
        raise


# How long after deferring to a fresh heartbeat we re-check that the deferred-to wait loop really
# consumed the answers. Slightly past the staleness window so a loop that died right after its
# last heartbeat is unambiguously dead by the time the recheck runs.

_RESUME_RECHECK_DELAY_S = _ANSWER_WAIT_HEARTBEAT_STALE_S + 5.0


def _schedule_resume_recheck(job_id: str, delay: float = _RESUME_RECHECK_DELAY_S) -> None:
    """Schedule a one-shot recheck for a resume that was deferred to another live owner.

    Two deferral cases share this safety net: deferring to a fresh answer-wait heartbeat (a wait
    loop elsewhere should consume the answers) and deferring to another worker's resume claim. In
    both, the owner could die right after we deferred, leaving the job paused with no resume control
    (the UI shows "resuming" forever). The recheck runs after ``delay``: if the job is still paused
    with no live thread and no fresh heartbeat, it resumes it for real. Callers pass a ``delay`` past
    whichever liveness window applies (the heartbeat staleness window, or the resume-claim TTL).

    Postconditions:
        - A daemon timer is scheduled; its callback is a no-op when the job moved on (status no
          longer waiting), a thread is alive here, or the heartbeat is fresh again (the loop
          really is alive elsewhere). Scheduling failures are logged, never raised.
    """

    def _recheck() -> None:
        try:
            data = _main.get_job(job_id) or {}
            if data.get("status") != hitl.WAITING_STATUS:
                return
            if _main._is_run_thread_alive(job_id) or _main._answer_wait_heartbeat_fresh(data):
                return
            if _try_auto_resume(job_id, data):
                logger.info(
                    "Deferred resume recheck restarted the orchestrator for job %s.", job_id
                )
            else:
                _main.update_job(
                    job_id,
                    status_text="Answers received. Resume the job to continue processing.",
                )
        except Exception:
            logger.exception("Deferred resume recheck failed for job %s.", job_id)

    try:
        t = threading.Timer(delay, _recheck)
        t.daemon = True
        t.start()
    except Exception:
        logger.exception("Could not schedule resume recheck for job %s.", job_id)


def _try_auto_resume(job_id: str, data: Dict[str, Any]) -> bool:
    """Best-effort restart of a dead orchestrator after answers arrived.

    The thread registry is process-local, so "not alive here" does not mean "not alive anywhere":
    a paused wait loop in another worker process heartbeats the job record every poll, and a fresh
    heartbeat means that loop will consume the just-stored answers itself — spawning a second
    orchestrator would double-drive the job and its checkout. GitHub-issue jobs resume through the
    full hook path so publication (PR, issue comments) is preserved.

    Preconditions:
        - ``data`` is the job record for ``job_id`` and the caller observed the run thread
          as not alive in this process.
    Postconditions:
        - Returns True when the run is resuming (a live wait loop heartbeated recently — with a
          deferred recheck scheduled in case that loop died right after its last beat — a thread
          was spawned here, or another caller holds the start claim); False when the job is
          terminal, the record lacks a usable ``repo_path``/``plan_input``, a GitHub-issue job
          has no token to resume its publish flow, or the thread could not be started.
          Never raises.
    """
    if hitl.is_terminal(data):
        logger.warning("Auto-resume for job %s skipped: job is terminal.", job_id)
        return False
    # Only a paused job is safely resumable: a non-paused (e.g. running) job has no heartbeat to
    # prove it dead, so it may be alive in another worker. Every current caller already passes a
    # waiting_for_user record; this is a defensive invariant so the function stays safe if reused.
    if data.get("status") != hitl.WAITING_STATUS:
        logger.warning(
            "Auto-resume for job %s skipped: not paused (status=%s).",
            job_id,
            data.get("status"),
        )
        return False
    if _main._answer_wait_heartbeat_fresh(data):
        _main._schedule_resume_recheck(job_id)
        return True
    plan_raw = data.get("plan_input") or {}
    if not isinstance(plan_raw, dict):
        # A corrupted record could carry a non-dict plan_input; .get() on it would raise
        # AttributeError and break the "Never raises" contract. Treat it as no usable plan.
        plan_raw = {}
    repo_path = data.get("repo_path") or plan_raw.get("repo_path")
    if not repo_path:
        return False
    try:
        plan = plan_from_input(plan_raw, repo_path)
    except Exception:
        logger.exception("Auto-resume for job %s skipped: invalid plan_input.", job_id)
        return False
    ctx = data.get("github_context") or {}
    is_github_job = bool(
        ctx.get("owner") and ctx.get("repo") and ctx.get("issue_number") is not None
    )
    # Prefer the token persisted (encrypted) at job creation; fall back to GITHUB_TOKEN env.
    token = (
        (decrypt_token(data.get("github_token_encrypted")) or os.environ.get("GITHUB_TOKEN"))
        if is_github_job
        else None
    )
    if is_github_job and not token:
        # Without a token the publish flow (PR, issue comments) cannot be resumed; fall back to
        # the explicit-resume hint rather than silently completing without a PR.
        logger.warning("Auto-resume for GitHub job %s skipped: no GitHub token available.", job_id)
        return False
    # Cross-worker claim FIRST: the process-local _claim_run_thread cannot stop a different worker
    # process from also spawning. The shared-store claim is the authoritative gate; only the worker
    # that wins it proceeds to the local claim and spawn. claim_resume() is the one job-store
    # read-modify-write here and may raise on a transport error; this function promises "Never
    # raises", so degrade a store failure to a False (manual-resume hint) rather than letting it
    # escape into submit_pending_answers after the answers were already stored.
    try:
        claimed = _main.claim_resume(job_id)
    except Exception:
        logger.exception("Auto-resume for job %s skipped: resume-claim store error.", job_id)
        return False
    if not claimed:
        logger.info(
            "Auto-resume for job %s skipped: another worker holds the resume claim.", job_id
        )
        # The winner could die after claiming but before advancing the job out of waiting_for_user;
        # its lease then expires (RESUME_CLAIM_TTL_S) with nobody retrying, leaving the job paused
        # until the next user request. Schedule a recheck past the lease TTL: if the job is still
        # waiting with no live thread, that recheck reclaims and resumes it.
        _main._schedule_resume_recheck(job_id, delay=RESUME_CLAIM_TTL_S + 5.0)
        return True
    # Post-claim freshness check: the job could have transitioned out of waiting_for_user between
    # the caller's snapshot and the claim. claim_resume checks only the claim stamp, not the
    # job status, so re-read here. If the job is no longer waiting (terminal OR a wait loop in
    # another worker consumed the answers and moved the job to 'running'), release the claim and
    # abort — spawning here would double-drive a running job or clobber a terminal one. If the
    # read itself fails (store temporarily unavailable), the unknown state is treated conservatively:
    # release the claim and return False so the caller gets the manual-resume hint.
    try:
        post_claim_data = _main.get_job(job_id)
    except Exception:
        logger.exception(
            "Auto-resume for job %s aborted: could not verify state after acquiring claim.", job_id
        )
        _main.release_resume_claim(job_id)
        return False
    if post_claim_data and post_claim_data.get("status") != hitl.WAITING_STATUS:
        _main.release_resume_claim(job_id)
        logger.warning(
            "Auto-resume for job %s aborted: status is '%s' after claim (no longer waiting).",
            job_id,
            post_claim_data.get("status"),
        )
        return False
    if not _main._claim_run_thread(job_id):
        # The cross-worker claim is ours but this process is already spawning (a racing thread):
        # release the shared claim so the in-flight spawn (or a later retry) isn't blocked.
        _main.release_resume_claim(job_id)
        return True
    try:
        if is_github_job:
            _main._start_github_resume_thread(job_id, ctx, repo_path, plan, token or "")
        else:
            _main._start_orchestrator_thread(job_id, repo_path, plan)
    except Exception:
        logger.exception("Auto-resume for job %s failed to start the orchestrator thread.", job_id)
        _main.release_resume_claim(job_id)
        return False
    return True


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


# A live review thread heartbeats every _REVIEW_HEARTBEAT_INTERVAL_S; a review job whose
# last_heartbeat_at is older than this cutoff has no live worker anywhere (its process
# died before the except-path could terminalize it) and must not block new reviews.
# 10 missed beats plus the shared clock-skew tolerance keeps false stales implausible.


def _start_hook_thread(
    job_id: str,
    request: RunFromGitHubRequest,
    plan: CodingTeamPlanInput,
    issue: Issue,
    token: str,
) -> None:
    """Spawn the post-creation hook in a background thread.

    Indirection so tests can monkey-patch this to invoke the hook synchronously.
    """
    t = threading.Thread(
        target=_main._run_with_github_hooks,
        args=(job_id, request, plan, issue, token),
        daemon=True,
    )
    t.start()
