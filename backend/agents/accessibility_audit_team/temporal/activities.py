"""Temporal activities for the accessibility audit team — one per pipeline phase.

Each phase of the in-process orchestrator is wrapped here as its own
``@activity.defn`` so :class:`~accessibility_audit_team.temporal.workflows.AccessibilityAuditWorkflow`
can drive the audit phase by phase — with independent timeouts, retries, and
heartbeats — instead of one opaque 2-hour black-box activity:

* :func:`intake_activity`            -> build the audit plan (wraps ``run_intake_step``)
* :func:`discovery_activity`         -> WAS/MAS scans + early QA (wraps ``run_discovery_step``)
* :func:`verification_activity`      -> AT/standards/remediation (wraps ``run_verification_step``)
* :func:`report_packaging_activity`  -> final QA + backlog (wraps ``run_report_packaging_step``)
* :func:`finalize_activity`          -> assemble result + mark the job completed
* :func:`retest_activity`            -> re-verify fixed findings (wraps ``run_retest_job``)
* :func:`mark_timed_out_activity`    -> fail the job when the audit exceeds its timebox

State crosses each boundary via the artifact store (``audit_state_{audit_id}``),
not by threading large findings lists through Temporal payloads: every step loads
the accumulated ``AccessibilityAuditResult``, runs its phase, and persists it.
Only a slim ``{"status", "audit_id"}`` dict is returned to the workflow. Heavy
imports live inside the function bodies so importing this module — which the
workflow sandbox reuses via ``imports_passed_through`` — stays cheap and
side-effect free.

Error model (mirrors the pre-decomposition ``run_pipeline_activity`` contract):
a *logical* phase failure (the phase ran but its target was unauditable) sets
``failure_reason`` on the result, so the activity marks the job FAILED and returns
``{"status": "FAIL"}`` to short-circuit the workflow. An *infrastructure* failure
(LLM/store crash) propagates out of the step and the activity so Temporal's retry
policy recovers, rather than being swallowed into a green workflow.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List

from temporalio import activity

from accessibility_audit_team.temporal.constants import (
    ACTIVITY_DISCOVERY,
    ACTIVITY_FINALIZE,
    ACTIVITY_INTAKE,
    ACTIVITY_REPORT_PACKAGING,
    ACTIVITY_RETEST,
    ACTIVITY_RUN_PIPELINE,
    ACTIVITY_TIMEOUT,
    ACTIVITY_VERIFICATION,
)

logger = logging.getLogger(__name__)

#: Background-heartbeat cadence (seconds) for the long LLM/scan phases. Kept well
#: under those activities' ``heartbeat_timeout`` so a live phase is never mistaken
#: for a stalled worker.
_HEARTBEAT_INTERVAL_S = 30.0

# Per-phase job-store progress markers (0..100).
_INTAKE_PROGRESS = 20
_DISCOVERY_PROGRESS = 40
_VERIFICATION_PROGRESS = 60
_REPORT_PROGRESS = 80


def _is_last_attempt() -> bool:
    """True when this is the final Temporal retry attempt (or no activity context).

    Preconditions:
        - Called from within an activity body (or directly, e.g. in tests).
    Postconditions:
        - Returns True when the current attempt is the last one Temporal will make
          for the scheduled retry policy (so the caller should mark the job terminal
          now rather than wait for a retry that will never come), or when called
          outside an activity context (direct/thread use).
        - Returns False when the scheduled policy allows unlimited retries
          (``maximum_attempts <= 0``) — there is no "last attempt" to gate on, so the
          caller keeps deferring to Temporal.
    """
    try:
        info = activity.info()
    except RuntimeError:
        return True
    policy = info.retry_policy
    max_attempts = policy.maximum_attempts if policy is not None else 0
    if max_attempts <= 0:
        return False
    return info.attempt >= max_attempts


def _is_job_terminal(manager: Any, job_id: str) -> bool:
    """True if the job has already reached a terminal (``completed``/``failed``) status.

    Used to guard a phase activity's job-store write against overwriting a terminal
    status a concurrent path (e.g. a timebox timeout) already set while this
    activity was in flight or being cancelled — Temporal activity cancellation is
    cooperative (delivered via heartbeat), so an abandoned activity can otherwise
    keep running and complete after the job is already terminal.
    """
    from accessibility_audit_team.audit_execution import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED

    existing = manager.get_job(job_id)
    return existing is not None and existing.get("status") in (
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
    )


def _heartbeat_activity_and_job(manager: Any, job_id: str) -> None:
    """``BackgroundHeartbeat`` callback: pings both Temporal and the job service.

    The API's stale-job monitor (``api/main.py``'s ``start_stale_job_monitor``) is
    an independent 300-second sweep, completely unrelated to Temporal, that marks
    any job whose ``last_heartbeat_at`` goes stale as FAILED — it has no way to
    know a Temporal activity is still healthily running. A long LLM/scan phase's
    single initial ``RUNNING`` write (the only job-store write for the whole
    phase, which can run up to ``LLM_PHASE_TIMEOUT``) goes stale well before the
    phase itself completes, so heartbeating only Temporal (via
    ``activity.heartbeat``) leaves the job vulnerable to being wrongly marked
    failed mid-phase. Combined with this module's terminal-write guards (which
    correctly refuse to let an *abandoned* activity clobber a legitimately
    decided terminal status), that wrong failure would then also suppress the
    phase's own later, legitimate completion write — leaving a workflow that
    actually succeeded stuck showing failed. Heartbeating the job service here
    keeps ``last_heartbeat_at`` fresh for the whole phase, so the stale monitor
    never fires on a genuinely alive job in the first place.

    Preconditions:
        - Called from the ``BackgroundHeartbeat`` thread (or directly).
    Postconditions:
        - Always calls ``activity.heartbeat()`` (propagates on Temporal-signaled
          cancellation, per ``BackgroundHeartbeat``'s contract). A failure
          heartbeating the job service is logged and swallowed here — a
          transient job-service hiccup must never suppress the Temporal
          heartbeat or kill the background loop.
    """
    activity.heartbeat()
    try:
        manager.heartbeat(job_id)
    except Exception:
        logger.warning("Failed to heartbeat job %s", job_id, exc_info=True)


async def _best_effort_terminal_fields(audit_id: str) -> Dict[str, Any]:
    """Recover progress/result fields for a terminal write when no fresh result exists.

    An exception raised by a phase ``step`` (or by ``finalize_audit_step``) means
    the caller never got an ``AccessibilityAuditResult`` back to report from — so,
    unlike the logical-failure branch, it can't include the phase's own
    ``completed_phases``/``findings_count``/``result`` in the terminal job-store
    write. This reloads the last durably-persisted state (mirrors
    ``mark_audit_timed_out``'s recovery pattern) so an exception-terminated job
    still reflects the best-known progress instead of leaving those fields stale
    from the phase's earlier ``RUNNING`` write.

    Preconditions:
        - ``audit_id`` is the API-supplied audit id used as the persistence key.
    Postconditions:
        - Returns ``{"progress": 100, "completed_phases": [...], "findings_count":
          int, "result": dict | None}``, populated from the persisted audit state
          when one exists, or terminal-but-empty defaults (``[]``/``0``/``None``)
          when nothing was ever persisted (e.g. intake failed before its first
          successful write).
    """
    from accessibility_audit_team.audit_execution import load_audit_state

    loaded = await load_audit_state(audit_id)
    return {
        "progress": 100,
        "completed_phases": (
            [p.value for p in loaded.completed_phases] if loaded is not None else []
        ),
        "findings_count": loaded.total_findings if loaded is not None else 0,
        "result": loaded.model_dump(mode="json") if loaded is not None else None,
    }


async def _run_phase(
    job_id: str,
    audit_id: str,
    phase_name: str,
    progress: int,
    step: Callable[[], Awaitable[Any]],
    *,
    heartbeat: bool = False,
) -> Dict[str, Any]:
    """Report per-phase progress, run ``step``, and translate its result to a status dict.

    Preconditions:
        - ``job_id`` refers to an existing job row; ``step`` is a zero-arg callable
          returning the awaitable phase step (which returns an
          ``AccessibilityAuditResult``).
    Postconditions:
        - If the job has already reached a terminal status when this attempt
          starts — a previous attempt of THIS activity already wrote a logical
          failure or an infra-exception's last-attempt failure, or a concurrent
          path like ``mark_timed_out_activity`` did, but Temporal never received
          the completion signal and retries it — the phase is NOT run at all:
          re-running it would be wasteful, and if this retry's nondeterministic
          outcome happened to differ (e.g. now PASS), reporting that to the
          workflow would let it continue while the job row stays stuck showing
          the earlier terminal failure. Returns ``{"status": "FAIL", ...}``
          immediately without touching the job store.
        - Otherwise re-checks terminality (narrowing, but not fully closing, the
          TOCTOU window against a concurrent path like ``mark_timed_out_activity``
          racing this same job between the check above and this point — the job
          service has no compare-and-swap primitive to make check-then-write
          atomic) and writes ``RUNNING``/``current_phase``/``progress`` before
          running the step. That write is inside the same failure handling as
          ``step`` itself: a transient job-service error on the initial write
          (not just a failure inside ``step``) is also caught below and, on the
          last scheduled attempt, reconciled via the FAILED write rather than
          propagating unhandled and leaving the job stuck at its previous
          pending/running status until the external stale-job sweep catches it.
        - On a *logical* phase failure (``result.failure_reason`` set), the terminal
          job-store write (with the phase's full partial result) is skipped if a
          concurrent path already marked the job terminal (e.g. a timebox timeout
          racing an abandoned, still-running activity); the returned status dict
          always reflects this attempt's own outcome regardless of that guard.
          Returns ``{"status": "FAIL", ...}`` so the workflow short-circuits.
        - On success returns ``{"status": "PASS", "audit_id": audit_id}``.
        - An exception RAISED by the initial job-store write or by ``step`` (an
          infrastructure/plumbing failure, as opposed to a returned
          ``failure_reason``) propagates so Temporal retries; on the LAST
          scheduled attempt it also marks the job FAILED first (guarded against
          clobbering an already-terminal status, and with progress/result
          fields best-effort recovered from persisted state — see
          :func:`_best_effort_terminal_fields`) so the job is never left stranded
          non-terminal, with a stale partial record, once Temporal gives up retrying.
        - When ``heartbeat`` is set the step runs under a background heartbeat
          (Temporal AND the job service — see :func:`_heartbeat_activity_and_job`)
          so a long phase keeps the activity alive, cancellation is deliverable,
          and the job's ``last_heartbeat_at`` stays fresh against the API's
          independent stale-job monitor.
    """
    from accessibility_audit_team.audit_execution import (
        JOB_STATUS_FAILED,
        JOB_STATUS_RUNNING,
        get_job_manager,
    )

    manager = get_job_manager()
    if _is_job_terminal(manager, job_id):
        logger.warning("Skipping %s phase for job %s: job is already terminal", phase_name, job_id)
        return {"status": "FAIL", "audit_id": audit_id, "error": "job already terminal"}

    try:
        # Re-check immediately before writing: a concurrent path (most notably
        # mark_timed_out_activity, racing this same job) can mark the job
        # terminal in the window between the check above and this write — an
        # unconditional RUNNING write here would silently revert that terminal
        # status. This narrows but cannot fully close the race (the job-service
        # client has no compare-and-swap/optimistic-concurrency primitive to make
        # the check-then-write atomic); the downstream logical-failure/exception
        # guards use the same pattern and share the same residual risk.
        if _is_job_terminal(manager, job_id):
            logger.warning(
                "Skipping %s phase for job %s: job became terminal before the RUNNING write",
                phase_name,
                job_id,
            )
            return {"status": "FAIL", "audit_id": audit_id, "error": "job already terminal"}
        manager.update_job(
            job_id, status=JOB_STATUS_RUNNING, current_phase=phase_name, progress=progress
        )
        if heartbeat:
            from shared.concurrency import BackgroundHeartbeat

            with BackgroundHeartbeat(
                lambda: _heartbeat_activity_and_job(manager, job_id),
                _HEARTBEAT_INTERVAL_S,
                copy_context=True,
            ):
                result = await step()
        else:
            result = await step()
    except Exception as exc:
        if _is_last_attempt() and not _is_job_terminal(manager, job_id):
            manager.update_job(
                job_id,
                status=JOB_STATUS_FAILED,
                current_phase=phase_name,
                error=str(exc),
                **await _best_effort_terminal_fields(audit_id),
            )
        raise

    if result.failure_reason:
        if not _is_job_terminal(manager, job_id):
            manager.update_job(
                job_id,
                status=JOB_STATUS_FAILED,
                current_phase=phase_name,
                progress=100,
                completed_phases=[p.value for p in result.completed_phases],
                findings_count=result.total_findings,
                result=result.model_dump(mode="json"),
                error=result.failure_reason,
            )
        return {"status": "FAIL", "audit_id": audit_id, "error": result.failure_reason}
    return {"status": "PASS", "audit_id": audit_id}


@activity.defn(name=ACTIVITY_INTAKE)
async def intake_activity(
    job_id: str, audit_id: str, request_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Intake phase: build the audit plan + coverage matrix and seed audit state.

    Preconditions:
        - ``job_id`` refers to a pending job; ``request_dict`` validates as a
          ``CreateAuditRequest``.
    Postconditions:
        - Flips the job to RUNNING at 20% and returns a PASS/FAIL status dict.
    """
    from accessibility_audit_team.audit_execution import CreateAuditRequest, run_intake_step

    # Rebuild the request OUTSIDE the funnel: a malformed request is a schema/plumbing
    # defect that must fail loudly, not read as a pipeline FAIL.
    request = CreateAuditRequest(**request_dict)
    return await _run_phase(
        job_id,
        audit_id,
        "intake",
        _INTAKE_PROGRESS,
        lambda: run_intake_step(job_id, audit_id, request),
        heartbeat=True,
    )


@activity.defn(name=ACTIVITY_DISCOVERY)
async def discovery_activity(job_id: str, audit_id: str) -> Dict[str, Any]:
    """Discovery phase: WAS/MAS scans + evidence + early QA over the audit plan.

    Preconditions:
        - Intake has persisted state under ``audit_id``.
    Postconditions:
        - Returns a PASS/FAIL status dict; runs under a background heartbeat.
    """
    from accessibility_audit_team.audit_execution import run_discovery_step

    return await _run_phase(
        job_id,
        audit_id,
        "discovery",
        _DISCOVERY_PROGRESS,
        lambda: run_discovery_step(job_id, audit_id),
        heartbeat=True,
    )


@activity.defn(name=ACTIVITY_VERIFICATION)
async def verification_activity(
    job_id: str, audit_id: str, tech_stack: Dict[str, str]
) -> Dict[str, Any]:
    """Verification phase: AT verification, standards mapping, remediation guidance.

    Preconditions:
        - Discovery has persisted state under ``audit_id``; ``tech_stack`` is the
          audit's tech-stack map (``{web, mobile}``).
    Postconditions:
        - Returns a PASS/FAIL status dict; runs under a background heartbeat.
    """
    from accessibility_audit_team.audit_execution import run_verification_step

    return await _run_phase(
        job_id,
        audit_id,
        "verification",
        _VERIFICATION_PROGRESS,
        lambda: run_verification_step(job_id, audit_id, tech_stack),
        heartbeat=True,
    )


@activity.defn(name=ACTIVITY_REPORT_PACKAGING)
async def report_packaging_activity(job_id: str, audit_id: str) -> Dict[str, Any]:
    """Report-packaging phase: final QA, pattern clustering, backlog + roadmap.

    Preconditions:
        - Verification has persisted state under ``audit_id``.
    Postconditions:
        - Returns a PASS/FAIL status dict; runs under a background heartbeat. Final
          assembly (severity counts / completed status) is done by
          :func:`finalize_activity`.
    """
    from accessibility_audit_team.audit_execution import run_report_packaging_step

    return await _run_phase(
        job_id,
        audit_id,
        "report_packaging",
        _REPORT_PROGRESS,
        lambda: run_report_packaging_step(job_id, audit_id),
        heartbeat=True,
    )


@activity.defn(name=ACTIVITY_FINALIZE)
async def finalize_activity(job_id: str, audit_id: str) -> Dict[str, Any]:
    """Finalize: assemble the audit result and write the terminal job-store row.

    Preconditions:
        - Report packaging has persisted state under ``audit_id``.
    Postconditions:
        - If the job has already reached a terminal status when this attempt
          starts (a previous attempt of finalize already wrote ``completed``/
          ``failed``, but Temporal never received the completion signal and
          retries it), ``finalize_audit_step`` is NOT run again — that would be
          wasteful and, if report packaging's own persisted state changed in the
          meantime, could disagree with the already-recorded outcome. Returns a
          status dict reflecting the EXISTING terminal status immediately,
          without touching the job store or running the heartbeat.
        - Otherwise marks the job ``completed`` (severity counts + full result
          dump) at 100%, or ``failed`` if the finalized result is unsuccessful.
          The terminal write is skipped if a concurrent path already marked the
          job terminal in the meantime (e.g. a timebox timeout racing an
          abandoned, still-running activity). Idempotent otherwise: a Temporal
          retry re-writes the same terminal state. Returns a status dict that
          always reflects this attempt's own outcome.
        - An exception raised while assembling the result propagates so Temporal
          retries; on the last scheduled attempt it also marks the job FAILED first
          (guarded against clobbering an already-terminal status, and with
          progress/result fields best-effort recovered from persisted state — see
          :func:`_best_effort_terminal_fields`).
        - Runs under a background heartbeat (Temporal AND the job service — see
          :func:`_heartbeat_activity_and_job`) so a long finalize keeps
          ``last_heartbeat_at`` fresh against the API's independent stale-job
          monitor.
    """
    from accessibility_audit_team.audit_execution import (
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
        finalize_audit_step,
        get_job_manager,
    )
    from shared.concurrency import BackgroundHeartbeat

    manager = get_job_manager()
    existing = manager.get_job(job_id)
    if existing is not None and existing.get("status") in (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED):
        logger.warning("Skipping finalize for job %s: job is already terminal", job_id)
        return {
            "status": "PASS" if existing.get("status") == JOB_STATUS_COMPLETED else "FAIL",
            "audit_id": audit_id,
        }

    try:
        with BackgroundHeartbeat(
            lambda: _heartbeat_activity_and_job(manager, job_id),
            _HEARTBEAT_INTERVAL_S,
            copy_context=True,
        ):
            result = await finalize_audit_step(job_id, audit_id)
    except Exception as exc:
        if _is_last_attempt() and not _is_job_terminal(manager, job_id):
            manager.update_job(
                job_id,
                status=JOB_STATUS_FAILED,
                current_phase="finalize",
                error=str(exc),
                **await _best_effort_terminal_fields(audit_id),
            )
        raise

    if not _is_job_terminal(manager, job_id):
        manager.update_job(
            job_id,
            status=JOB_STATUS_COMPLETED if result.success else JOB_STATUS_FAILED,
            progress=100,
            current_phase=result.current_phase.value,
            completed_phases=[p.value for p in result.completed_phases],
            findings_count=result.total_findings,
            result=result.model_dump(mode="json"),
            error=None if result.success else result.failure_reason,
        )
    return {"status": "PASS" if result.success else "FAIL", "audit_id": audit_id}


@activity.defn(name=ACTIVITY_RETEST)
async def retest_activity(job_id: str, audit_id: str, finding_ids: List[str]) -> Dict[str, Any]:
    """Retest: re-verify fixed findings for an existing audit.

    Preconditions:
        - ``job_id`` refers to a created retest job; ``audit_id`` refers to a
          completed audit whose state is in the artifact store.
    Postconditions:
        - ``run_retest_job`` owns the job-store lifecycle (running -> terminal) and
          propagates infrastructure failures so Temporal retries. Returns a status
          dict once the retest reaches a terminal state. Runs under a background
          heartbeat (Temporal AND the job service — see
          :func:`_heartbeat_activity_and_job`) so a genuinely long retest keeps
          the activity alive within the workflow's ``heartbeat_timeout`` instead
          of being timed out mid-run, and keeps ``last_heartbeat_at`` fresh
          against the API's independent stale-job monitor.
        - An exception raised by ``run_retest_job`` propagates so Temporal retries;
          on the LAST scheduled attempt it also marks the job FAILED first (guarded
          against clobbering an already-terminal status, and with progress/result
          fields best-effort recovered from persisted state — see
          :func:`_best_effort_terminal_fields`) so the job is never left stranded
          RUNNING once Temporal gives up retrying, unlike relying solely on the
          external stale-job monitor's much coarser timeout.
    """
    from accessibility_audit_team.audit_execution import (
        JOB_STATUS_FAILED,
        get_job_manager,
        run_retest_job,
    )
    from shared.concurrency import BackgroundHeartbeat

    manager = get_job_manager()
    try:
        with BackgroundHeartbeat(
            lambda: _heartbeat_activity_and_job(manager, job_id),
            _HEARTBEAT_INTERVAL_S,
            copy_context=True,
        ):
            await run_retest_job(job_id, audit_id, finding_ids)
    except Exception as exc:
        if _is_last_attempt() and not _is_job_terminal(manager, job_id):
            manager.update_job(
                job_id,
                status=JOB_STATUS_FAILED,
                current_phase="retest",
                error=str(exc),
                **await _best_effort_terminal_fields(audit_id),
            )
        raise
    return {"status": "done", "audit_id": audit_id}


@activity.defn(name=ACTIVITY_TIMEOUT)
async def mark_timed_out_activity(job_id: str, audit_id: str, timebox_hours: int) -> Dict[str, Any]:
    """Mark the audit job failed because it exceeded its timebox budget.

    Preconditions:
        - ``job_id``/``audit_id`` are non-empty; the workflow schedules this only
          when the timebox timer wins the race against the phase chain.
    Postconditions:
        - Records the timeout failure on the job (and persisted state) and returns a
          ``{"status": "TIMEOUT"}`` dict.
    """
    from accessibility_audit_team.audit_execution import mark_audit_timed_out

    await mark_audit_timed_out(job_id, audit_id, timebox_hours)
    return {"status": "TIMEOUT", "audit_id": audit_id}


@activity.defn(name=ACTIVITY_RUN_PIPELINE)
async def run_pipeline_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    """LEGACY whole-pipeline activity, retained ONLY for history drain-out.

    Runs the entire audit in one activity via ``run_audit_job`` (the
    pre-decomposition behavior). New executions run the per-phase activities; this
    exists so an ``AccessibilityAuditWorkflow`` history recorded *before* the
    per-phase migration still replays deterministically (see the ``workflow.patched``
    gate in :class:`AccessibilityAuditWorkflow`) and can complete after a worker
    rolls forward. Remove once no pre-migration executions remain open.

    Preconditions:
        - ``payload`` has ``job_id``, ``audit_id``, and a ``request`` dict.
    Postconditions:
        - The job's terminal state is persisted; an infrastructure exception
          propagates (failing the activity) rather than being swallowed.
    """
    from accessibility_audit_team.audit_execution import CreateAuditRequest, run_audit_job

    job_id = payload["job_id"]
    audit_id = payload["audit_id"]
    request = CreateAuditRequest(**payload["request"])
    await run_audit_job(job_id, audit_id, request)
    return {"job_id": job_id, "audit_id": audit_id, "status": "done"}
