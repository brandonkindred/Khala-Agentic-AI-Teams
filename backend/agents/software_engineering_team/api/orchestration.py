"""coding_team API — orchestrator-thread lifecycle: plan wiring, run/resume/hook thread spawning, auto-resume, and the github-hook run flow.

Monkeypatched collaborators are dereferenced through the ``main`` module object
at call time so ``monkeypatch.setattr(main, ...)`` keeps taking effect after the
split; models are imported directly.
"""

from __future__ import annotations

import logging
import os
import threading
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared.env_config import env_bool
from shared.git.git_utils import DEVELOPMENT_BRANCH
from shared.temporal.runner import signal_workflow_sync
from software_engineering_team import hitl
from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import (
    RunFromGitHubRequest,
)
from software_engineering_team.api.coding_team_state import (
    _ANSWER_WAIT_HEARTBEAT_STALE_S,
)
from software_engineering_team.github_source import (
    GitHubAPIError,
    Issue,
    scrub_token_from_text,
)
from software_engineering_team.job_store import (
    DEFAULT_CACHE_DIR,
    RESUME_CLAIM_TTL_S,
)
from software_engineering_team.models import CodingTeamPlanInput, JobStatus
from software_engineering_team.temporal.coding_team_constants import WORKFLOW_ID_PREFIX
from software_engineering_team.token_crypto import decrypt_token

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
) -> Optional[Dict[str, Any]]:
    """Run the coding-team orchestrator for *job_id* with the standard job-store wiring.

    Single source of the ``(update_job_fn, get_job_fn, cache_dir)`` wiring shared
    by the POST /run background thread, the resume path, and the Temporal
    activity, so it cannot drift between them. The github-source path wires a
    custom ``update_job_fn`` (+ ``on_pause``) and deliberately does not use this.

    ``pause_strategy``/``acknowledged_resume_token`` are forwarded unchanged into
    ``run_coding_team_orchestrator`` — see that function's docstring for the full
    contract. Every existing caller of this function (``_start_orchestrator_thread``,
    thread-mode resume) passes neither, so it keeps requesting ``"block"`` (today's
    behavior, unchanged) by default; only the Temporal activity path
    (``run_pipeline_activity``) passes ``pause_strategy="return"``.

    Preconditions:
        - ``job_id`` names an existing job in the process job store; ``plan`` is a
          validated ``CodingTeamPlanInput`` whose ``repo_path`` equals *repo_path*.
    Postconditions:
        - ``pause_strategy="block"``: returns ``None``, unchanged from every caller's
          behavior before this parameter existed. The orchestrator has run to
          completion (or raised); job state is persisted through ``update_job``.
          Propagates the orchestrator's exceptions unchanged — callers own their own
          failure handling.
        - ``pause_strategy="return"``: returns the orchestrator's
          ``{"outcome": "paused", ...}`` dict when a HITL gate paused, or ``None`` when
          the pipeline instead reached a terminal state.
    """
    return _main.run_coding_team_orchestrator(
        job_id,
        repo_path,
        plan,
        update_job_fn=lambda **kw: _main.update_job(job_id, **kw),
        get_job_fn=_main.get_job,
        cache_dir=DEFAULT_CACHE_DIR,
        pause_strategy=pause_strategy,
        acknowledged_resume_token=acknowledged_resume_token,
    )


def _spawn_run_thread(
    job_id: str,
    run_body: Callable[[], None],
    on_failure: Callable[[Exception], None],
) -> None:
    """Spawn a claim-lifecycle-managed daemon run-thread that executes *run_body*.

    The shared skeleton behind ``_start_orchestrator_thread`` and
    ``_start_github_resume_thread``: run-thread registration lives inside the
    thread's ``try`` so the ``finally`` always releases the claim (even if
    registration itself raises), and a thread-start failure releases the claim
    here — the thread's ``finally`` never ran — before re-raising so the job
    stays resumable. The two callers differ only in *run_body* (the work) and
    *on_failure* (the log line + failed-status write), which are passed in.

    Preconditions:
        - The caller holds the run-thread claim for *job_id*.
        - *run_body* performs the job's work and may raise; *on_failure* records
          the failure (log + ``update_job(status="failed", ...)``) and must not
          itself raise.
    Postconditions:
        - A daemon thread is running *run_body*. The claim is released by the
          thread's ``finally`` on completion or *run_body* failure, or here on
          thread-start failure (in which case the exception propagates).
        - A *run_body* exception is routed to *on_failure*; a thread-start
          failure is not (the work never began) and propagates to the caller.
    """

    def run() -> None:
        try:
            # Registration is inside the try so the finally always releases the claim — even if
            # _register_run_thread itself fails — instead of leaving it wedged in _starting_run_jobs.
            _main._register_run_thread(job_id)
            run_body()
        except Exception as e:
            on_failure(e)
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


def _start_orchestrator_thread(job_id: str, repo_path: str, plan: CodingTeamPlanInput) -> None:
    """Spawn the daemon orchestrator thread for a job whose run-thread claim is held.

    Preconditions:
        - The caller holds the run-thread claim for ``job_id`` (via ``_claim_run_thread``).
    Postconditions:
        - A daemon thread is running the orchestrator; the claim is released by the thread's
          ``finally`` (or here, if the thread never started — in which case the exception
          propagates so the job stays resumable).
    """

    def _run_body() -> None:
        _main.run_orchestrator_wired(job_id, repo_path, plan)

    def _on_failure(e: Exception) -> None:
        logger.exception("Coding team orchestrator resume failed: %s", e)
        _main.update_job(job_id, status=JobStatus.FAILED.value, error=str(e), current_activity=None)

    _spawn_run_thread(job_id, _run_body, _on_failure)


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

    def _run_body() -> None:
        # Advance the job out of waiting_for_user BEFORE the GitHub network I/O. The
        # cross-worker resume claim (claim_resume) has a TTL of RESUME_CLAIM_TTL_S; if the
        # issue fetch or branch prep takes longer than that, another worker could treat the
        # expired claim as abandoned and spawn a second hook path. Moving the status to
        # "running" here makes _try_auto_resume and resume_job decline (they only proceed for
        # waiting_for_user), so the re-claiming window closes before the slow I/O begins.
        _main.update_job(
            job_id, status=JobStatus.RUNNING.value, status_text="Resuming via GitHub hook…"
        )
        with _main.GitHubClient(token=token) as client:
            issue = client.get_issue(request.owner, request.repo, int(ctx["issue_number"]))
        _main._run_with_github_hooks(job_id, request, plan, issue, token)

    def _on_failure(e: Exception) -> None:
        logger.exception("GitHub-path resume failed for job %s: %s", job_id, e)
        _main.update_job(job_id, status=JobStatus.FAILED.value, error=f"resume failed: {e}")

    _spawn_run_thread(job_id, _run_body, _on_failure)


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


def _recover_resume_plan(
    job_id: str, plan_raw: Dict[str, Any], repo_path: Optional[str]
) -> Optional[Tuple[str, CodingTeamPlanInput]]:
    """Validate a job record's ``(plan_raw, repo_path)`` into a resume plan, or ``None`` if unusable.

    The caller owns deriving ``plan_raw``/``repo_path`` from the job record (each caller already
    needs ``plan_raw`` for its own non-dict ``plan_input`` handling — the route rejects it, auto-resume
    coerces it to ``{}`` — so this function takes the already-derived values instead of re-deriving
    them and blurring why a ``None`` was returned).

    Preconditions:
        - ``plan_raw`` is a dict; ``repo_path`` is the job's resume repo_path, or falsy if the
          record carries none.
    Postconditions:
        - Returns ``(repo_path, plan)`` when ``repo_path`` is truthy and ``plan_raw`` validates;
          ``None`` when ``repo_path`` is falsy (not logged — the caller can distinguish this from
          a validation failure by checking ``repo_path`` itself) or when validation fails (logged).
          Never raises.
    """
    if not repo_path:
        return None
    try:
        plan = plan_from_input(plan_raw, repo_path)
    except Exception:
        logger.exception("Resume for job %s skipped: invalid plan_input.", job_id)
        return None
    return repo_path, plan


def _resolve_github_job_token(
    job_id: str, data: Dict[str, Any]
) -> Optional[Tuple[bool, Dict[str, Any], Optional[str]]]:
    """Classify a resume as GitHub-issue or plain and resolve its token.

    Preconditions:
        - ``data`` is the job record for ``job_id``.
    Postconditions:
        - Returns ``(is_github_job, github_context, token)``: a plain job yields
          ``(False, ctx, None)``; a GitHub-issue job with a usable token (persisted
          encrypted at creation, else ``GITHUB_TOKEN``) yields ``(True, ctx, token)``.
        - Returns ``None`` when a GitHub-issue job has no usable token — the publish
          flow (PR, issue comments) cannot be resumed, so the caller must bail with
          the manual-resume hint rather than silently complete without a PR. Never
          raises.
    """
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
        logger.warning("Resume for GitHub job %s skipped: no GitHub token available.", job_id)
        return None
    return is_github_job, ctx, token


class ResumeSpawnResult(str, Enum):
    """Outcome of ``_claim_and_spawn_resume``. Each caller (the HTTP route, auto-resume) maps
    every member onto its own response/log/scheduling behavior; the helper itself stays silent
    on logging and recheck scheduling since those differ per caller for the same outcome."""

    SPAWNED = "spawned"
    CLAIM_LOST = "claim_lost"
    CLAIM_STORE_ERROR = "claim_store_error"
    NOT_WAITING = "not_waiting"
    POST_CLAIM_READ_ERROR = "post_claim_read_error"
    THREAD_CLAIM_LOST = "thread_claim_lost"
    SPAWN_FAILED = "spawn_failed"


def _claim_and_spawn_resume(
    job_id: str,
    ctx: Dict[str, Any],
    repo_path: str,
    plan: CodingTeamPlanInput,
    token: Optional[str],
    is_github_job: bool,
) -> Tuple[ResumeSpawnResult, Optional[Dict[str, Any]], Optional[Exception]]:
    """Cross-worker claim → post-claim re-read → local run-thread claim → spawn, shared by the
    HTTP ``/resume`` route and ``_try_auto_resume``.

    Single source of the resume-once safety sequence: winning the shared-store claim FIRST (the
    process-local run-thread claim alone cannot stop a different worker process from also
    spawning), re-reading the job post-claim (the job could have transitioned out of
    ``waiting_for_user`` between the caller's snapshot and the claim — claim_resume checks only
    the claim stamp, not status), then the local run-thread claim, then the actual spawn. A
    failed step past the shared claim always releases it so a later attempt can win.

    Preconditions:
        - ``repo_path``/``plan`` are a recovered, validated resume plan (e.g. from
          ``_recover_resume_plan``); ``token``/``ctx``/``is_github_job`` are a resolved GitHub
          classification (e.g. from ``_resolve_github_job_token``); the caller has already
          verified the job is paused (``waiting_for_user``) and not already alive.
    Postconditions:
        - Returns ``(SPAWNED, post_claim_data, None)`` when a thread was started here.
        - Returns ``(CLAIM_LOST | THREAD_CLAIM_LOST, ..., None)`` when another worker/caller
          already owns the resume; the shared claim is released in the ``THREAD_CLAIM_LOST``
          case (this worker's local claim lost, not the shared one) and left with the other
          owner in the ``CLAIM_LOST`` case (never acquired here).
        - Returns ``(NOT_WAITING, post_claim_data_or_None, None)`` and releases the shared claim
          when the post-claim re-read finds the job missing or no longer ``waiting_for_user``.
        - Returns ``(CLAIM_STORE_ERROR | POST_CLAIM_READ_ERROR | SPAWN_FAILED, ..., exc)`` on a
          collaborator exception, releasing the shared claim first except for
          ``CLAIM_STORE_ERROR`` (no claim was won). Never raises.
    """
    try:
        claimed = _main.claim_resume(job_id)
    except Exception as e:
        return ResumeSpawnResult.CLAIM_STORE_ERROR, None, e
    if not claimed:
        return ResumeSpawnResult.CLAIM_LOST, None, None
    try:
        post_claim_data = _main.get_job(job_id)
    except Exception as e:
        _main.release_resume_claim(job_id)
        return ResumeSpawnResult.POST_CLAIM_READ_ERROR, None, e
    if not post_claim_data or post_claim_data.get("status") != hitl.WAITING_STATUS:
        _main.release_resume_claim(job_id)
        return ResumeSpawnResult.NOT_WAITING, post_claim_data, None
    if not _main._claim_run_thread(job_id):
        # The shared claim is ours but this process is already spawning (a racing thread):
        # release the shared claim so the in-flight spawn (or a later retry) isn't blocked.
        _main.release_resume_claim(job_id)
        return ResumeSpawnResult.THREAD_CLAIM_LOST, post_claim_data, None
    try:
        if is_github_job:
            _main._start_github_resume_thread(job_id, ctx, repo_path, plan, token or "")
        else:
            _main._start_orchestrator_thread(job_id, repo_path, plan)
    except Exception as e:
        _main.release_resume_claim(job_id)
        return ResumeSpawnResult.SPAWN_FAILED, post_claim_data, e
    return ResumeSpawnResult.SPAWNED, post_claim_data, None


def _try_auto_resume(job_id: str, data: Dict[str, Any]) -> bool:
    """Best-effort resume of a paused job after answers arrived (or a deferred recheck).

    Two paths, told apart by whether ``data`` carries a ``resume_token`` (set only by a
    ``pause_strategy="return"`` pause):

    - **Temporal-native pause** (``resume_token`` present): signal the running
      ``CodingTeamWorkflow`` with ``submit_answers``, carrying ``resume_token`` and
      already-stored ``submitted_answers`` (or ``[]``). No heartbeat deferral, plan
      recovery, GitHub-token resolution, or claim+spawn. Signal delivery failures are
      logged and become ``False`` so this function's never-raises contract holds.
    - **Thread-mode / GitHub-hook pause** (``resume_token`` absent): unchanged — defer to
      a fresh answer-wait heartbeat (with recheck), or claim and spawn the orchestrator
      (hook path for GitHub-issue jobs).

    Preconditions:
        - ``data`` is the job record for ``job_id`` and the caller observed the run thread
          as not alive in this process (thread-mode callers); Temporal callers may invoke
          this with a waiting Temporal-native job as a safety net / recheck.
    Postconditions:
        - Returns True when the run is resuming (Temporal signal accepted; a live wait loop
          heartbeated recently — with a deferred recheck scheduled; a thread was spawned
          here; or another caller holds the start claim).
        - Returns False when the job is terminal, not paused, a Temporal signal failed, the
          record lacks a usable ``repo_path``/``plan_input``, a GitHub-issue job has no
          token, or the thread could not be started.
        - Never raises for Temporal signal failures or any documented ``ResumeSpawnResult``
          outcome; raises ``RuntimeError`` only if ``_claim_and_spawn_resume`` returns an
          unrecognized outcome (exhaustiveness guard).
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
    resume_token = data.get("resume_token")
    if resume_token:
        try:
            signal_workflow_sync(
                f"{WORKFLOW_ID_PREFIX}{job_id}",
                "submit_answers",
                {
                    "resume_token": resume_token,
                    "answers": data.get("submitted_answers") or [],
                },
            )
        except Exception:
            logger.error(
                "Auto-resume for job %s skipped: Temporal submit_answers signal failed.",
                job_id,
                exc_info=True,
            )
            return False
        return True
    if _main._answer_wait_heartbeat_fresh(data):
        _main._schedule_resume_recheck(job_id)
        return True
    plan_raw = data.get("plan_input") or {}
    if not isinstance(plan_raw, dict):
        # A corrupted record could carry a non-dict plan_input; .get() on it would raise
        # AttributeError and break the "Never raises" contract. Treat it as no usable plan.
        plan_raw = {}
    repo_path = data.get("repo_path") or plan_raw.get("repo_path")
    recovered = _recover_resume_plan(job_id, plan_raw, repo_path)
    if recovered is None:
        return False
    repo_path, plan = recovered
    resolved = _resolve_github_job_token(job_id, data)
    if resolved is None:
        return False
    is_github_job, ctx, token = resolved

    result, post_claim_data, err = _claim_and_spawn_resume(
        job_id, ctx, repo_path, plan, token, is_github_job
    )
    if result is ResumeSpawnResult.CLAIM_STORE_ERROR:
        # claim_resume() is the one job-store read-modify-write here and may raise on a transport
        # error; this function promises "Never raises", so degrade a store failure to a False
        # (manual-resume hint) rather than letting it escape into submit_pending_answers after the
        # answers were already stored.
        logger.error(
            "Auto-resume for job %s skipped: resume-claim store error.", job_id, exc_info=err
        )
        return False
    if result is ResumeSpawnResult.CLAIM_LOST:
        logger.info(
            "Auto-resume for job %s skipped: another worker holds the resume claim.", job_id
        )
        # The winner could die after claiming but before advancing the job out of waiting_for_user;
        # its lease then expires (RESUME_CLAIM_TTL_S) with nobody retrying, leaving the job paused
        # until the next user request. Schedule a recheck past the lease TTL: if the job is still
        # waiting with no live thread, that recheck reclaims and resumes it.
        _main._schedule_resume_recheck(job_id, delay=RESUME_CLAIM_TTL_S + 5.0)
        return True
    if result is ResumeSpawnResult.POST_CLAIM_READ_ERROR:
        logger.error(
            "Auto-resume for job %s aborted: could not verify state after acquiring claim.",
            job_id,
            exc_info=err,
        )
        return False
    if result is ResumeSpawnResult.NOT_WAITING:
        logger.warning(
            "Auto-resume for job %s aborted: status is '%s' after claim (no longer waiting).",
            job_id,
            (post_claim_data or {}).get("status"),
        )
        return False
    if result is ResumeSpawnResult.THREAD_CLAIM_LOST:
        return True
    if result is ResumeSpawnResult.SPAWN_FAILED:
        logger.error(
            "Auto-resume for job %s failed to start the orchestrator thread.", job_id, exc_info=err
        )
        return False
    if result is not ResumeSpawnResult.SPAWNED:
        # Exhaustiveness guard: a future ResumeSpawnResult member falling through here would
        # silently report a successful resume for what is actually a new, unhandled outcome.
        raise RuntimeError(f"Unhandled ResumeSpawnResult: {result!r}")
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
