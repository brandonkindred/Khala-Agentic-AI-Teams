"""Temporal activities for the sales_team — one per specialist agent invocation.

The fine-grained ``SalesWorkflow`` fans out **one activity per prospect per
stage** (outreach, qualification, nurture, discovery, proposal, negotiation),
plus a handful of single-shot activities (prepare, prospect, load-dossiers,
coach, progress, mark-failed, finalize). Per-prospect activities delegate to the
shared ``SalesPodOrchestrator.<stage>_one`` methods, so the exact same
agent-call logic runs in both the thread path (bounded thread pool) and
Temporal mode (durable, individually retryable activities).

Import hygiene: top-level imports stay light (``temporalio``, typing,
``shared.concurrency``, ``phase_models``); heavy imports (``orchestrator``,
``job_runner``, ``models``, ``outcome_store``) are lazy inside function bodies,
and ``os.getenv`` is only ever called at call time — never at import — so the
temporalio workflow sandbox that re-imports sibling modules is never tripped.

Job-store status ownership (the retry-safe contract):
    - RUNNING is written once, by ``sales_prepare``.
    - Per-stage progress is written by ``sales_report_progress`` at each stage
      entry/exit boundary; it also reports whether the job is still active so
      the workflow stops spending on a cancelled job.
    - COMPLETED is written only by ``sales_finalize``.
    - FAILED is written only by ``sales_mark_failed``, which the WORKFLOW
      invokes after a fatal error has exhausted its retries. Activities never
      write FAILED themselves — an activity that recorded FAILED mid-retry
      would trip its own terminal-status guard on the next attempt and defeat
      the retry policy.
    - Terminal-state semantics are status-aware: a CANCELLED/INTERRUPTED (or
      already-COMPLETED) job short-circuits cleanly, while a FAILED or missing
      job at prepare/finalize RAISES — a real failure (or an unreadable store)
      must surface as a failed Temporal workflow, never be masked as success.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Any, Callable, Literal, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sales_team.temporal.phase_models import SalesRunContext
from shared.concurrency import BackgroundHeartbeat

_DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
# Heartbeat timeout the workflow schedules every long LLM activity with. Owned
# here (next to the beat interval) so the two knobs cannot drift apart across
# modules: the interval is clamped to a third of this value, guaranteeing at
# least ~3 beats per timeout window regardless of operator configuration.
HEARTBEAT_TIMEOUT_S = 180.0


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _heartbeat_interval_s() -> float:
    """Heartbeat cadence (seconds) for the long LLM activities.

    Preconditions:
        - none (environment may be unset or garbage).
    Postconditions:
        - Returns ``SALES_TEMPORAL_HEARTBEAT_INTERVAL_S`` clamped to
          ``[1, HEARTBEAT_TIMEOUT_S / 3]`` (garbage/unset → the 30s default);
          the ceiling guarantees beats always outpace the activity heartbeat
          timeout, so a mis-set interval can never spuriously fail activities.
    """
    from shared.env_config import env_float

    return env_float(
        "SALES_TEMPORAL_HEARTBEAT_INTERVAL_S",
        _DEFAULT_HEARTBEAT_INTERVAL_S,
        floor=1.0,
        ceiling=HEARTBEAT_TIMEOUT_S / 3.0,
    )


def _beating() -> BackgroundHeartbeat:
    """Background beater keeping a long LLM activity alive across blocking calls.

    Preconditions:
        - Called from inside a running activity body (the constructor snapshots
          the calling thread's context so the beater can reach the Temporal
          activity handle; beat errors outside an activity context — e.g. unit
          tests — are swallowed by the beater).
    Postconditions:
        - Returns an unstarted context manager; entering it starts the daemon
          beater, exiting stops and joins it.
    """
    return BackgroundHeartbeat(
        activity.heartbeat,
        _heartbeat_interval_s(),
        name="sales-heartbeat",
        copy_context=True,
        join_timeout=5.0,
    )


def _job_status(job_id: str) -> Optional[str]:
    """Read the job's current status from the job store.

    Preconditions:
        - ``job_id`` is a job-store id (the row may or may not exist).
    Postconditions:
        - Returns the status string, or ``None`` when the row is missing —
          callers decide whether missing is a clean skip or an error.
    """
    from sales_team.job_runner import job_manager

    job = job_manager.get_job(job_id)
    return job.get("status") if job else None


def _job_stopped(job_id: str) -> bool:
    """Whether pipeline work must not proceed for ``job_id``.

    Preconditions:
        - ``job_id`` is a job-store id.
    Postconditions:
        - Returns ``True`` when the job is missing or in any terminal state —
          a queued/running activity must never do work (or write progress) for
          a job that has already ended.
    """
    from sales_team.job_runner import TERMINAL_STATUSES

    status = _job_status(job_id)
    return status is None or status in TERMINAL_STATUSES


class _GuardOutcome(Enum):
    """Sentinel returned by ``_terminal_guard`` — never serialized."""

    PROCEED = auto()
    STOP = auto()


_TerminalPhase = Literal[
    "sales_prepare", "sales_finalize", "deep_research_prepare", "deep_research_finalize"
]

_FAILED_MESSAGES: dict[_TerminalPhase, str] = {
    "sales_prepare": "Sales pipeline job {job_id} was already FAILED before start",
    "sales_finalize": "Sales pipeline job {job_id} was marked FAILED during the run",
    "deep_research_prepare": "Deep-research job {job_id} was already FAILED before start",
    "deep_research_finalize": "Deep-research job {job_id} was marked FAILED during the run",
}

_STOP_LOG_MESSAGES: dict[_TerminalPhase, str] = {
    "sales_prepare": "Sales job %s already terminal (%s) at prepare; stopping run",
    "sales_finalize": "Sales job %s terminal (%s) at finalize; not writing COMPLETED",
    "deep_research_prepare": "Deep-research job %s already terminal (%s) at prepare; stopping",
    "deep_research_finalize": "Deep-research job %s terminal (%s) at finalize; not writing COMPLETED",
}


def _terminal_guard(job_id: str, *, phase: _TerminalPhase, missing_msg: str) -> _GuardOutcome:
    """Check whether a job is terminal and decide whether the caller may proceed.

    Preconditions:
        - ``job_id`` is a job-store id (the row may or may not exist).
        - ``phase`` identifies the domain+call-site combination (selects the
          FAILED-message and short-circuit log wording); ``missing_msg`` is
          the exact message to raise verbatim when the job row is missing.
    Postconditions:
        - Job missing -> raises ``RuntimeError(missing_msg)`` (retryable -- a
          transient store read glitch is retried by the workflow's IO policy).
        - Job FAILED -> raises non-retryable ``ApplicationError`` (never
          resurrect a failed job; surface as a failed workflow).
        - Job in ``CLEAN_TERMINAL_STATUSES`` -> logs at info level and returns
          ``_GuardOutcome.STOP`` (a cancel/interrupt/already-complete job must
          short-circuit cleanly; this function does not prescribe what the
          caller returns for that case).
        - Otherwise returns ``_GuardOutcome.PROCEED``.
    """
    from job_service_client import JOB_STATUS_FAILED
    from sales_team.job_runner import CLEAN_TERMINAL_STATUSES

    status = _job_status(job_id)
    if status is None:
        raise RuntimeError(missing_msg)
    if status == JOB_STATUS_FAILED:
        raise ApplicationError(_FAILED_MESSAGES[phase].format(job_id=job_id), non_retryable=True)
    if status in CLEAN_TERMINAL_STATUSES:
        activity.logger.info(_STOP_LOG_MESSAGES[phase], job_id, status)
        return _GuardOutcome.STOP
    return _GuardOutcome.PROCEED


def _orch_and_ctx(sctx: SalesRunContext):
    """Reconstruct a fresh orchestrator + ``_RunContext`` from the carrier.

    Preconditions:
        - ``sctx`` was produced by ``sales_prepare`` (validated request,
          insights loaded).
    Postconditions:
        - Returns ``(orchestrator, run_ctx)``; agents are lazy properties on
          the orchestrator, so only those the caller actually invokes resolve
          an LLM client. Progress is reported out-of-band
          (``sales_report_progress``), so the run-context carries the no-op
          update callback — the non-serializable callback never crosses a
          Temporal boundary.
    """
    from sales_team.orchestrator import SalesPodOrchestrator, build_run_context

    orch = SalesPodOrchestrator(config=sctx.request.config)
    run_ctx = build_run_context(sctx.request, sctx.job_id, sctx.insights_ctx)
    return orch, run_ctx


# ---------------------------------------------------------------------------
# Single-shot activities
# ---------------------------------------------------------------------------


@activity.defn(name="sales_prepare")
def prepare_sales_pipeline_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Open the run: validate the request, guard terminal state, load insights.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``request`` is the serialized ``SalesPipelineRequest``.

    Postconditions:
        - Invalid ``request`` → raises a non-retryable ``ApplicationError``
          (deterministic — retrying cannot help); the workflow's catch-all
          marks the job FAILED so it never sits stuck PENDING.
        - Job missing → raises (retryable — a transient store read glitch is
          retried by the workflow's IO policy; a genuinely missing job fails
          the workflow rather than being masked as success).
        - Job already FAILED → raises non-retryably (never resurrect; surface
          as a failed workflow).
        - Job CANCELLED/INTERRUPTED/COMPLETED → returns ``stopped=True``
          without writing RUNNING (a queued workflow cannot resurrect a
          finished job).
        - Otherwise writes RUNNING, loads learning insights once, and returns
          the ``SalesRunContext`` carrier. The carried request has
          ``existing_prospects`` stripped: prospects flow between activities as
          explicit arguments, and carrying up to 100 of them inside every
          activity's ctx input would silently amplify workflow history.
    """
    from job_service_client import JOB_STATUS_RUNNING
    from sales_team.job_runner import job_manager
    from sales_team.learning_engine import format_insights_for_prompt
    from sales_team.models import SalesPipelineRequest
    from sales_team.outcome_store import load_current_insights

    try:
        pipeline_request = SalesPipelineRequest(**request)
    except Exception as exc:
        raise ApplicationError(
            f"Invalid SalesPipelineRequest for job {job_id}: {exc}", non_retryable=True
        ) from exc

    carried = pipeline_request.model_copy(update={"existing_prospects": []})

    guard = _terminal_guard(
        job_id,
        phase="sales_prepare",
        missing_msg=f"Sales pipeline job {job_id} not found at prepare",
    )
    if guard is _GuardOutcome.STOP:
        return SalesRunContext(request=carried, job_id=job_id, stopped=True).model_dump(mode="json")

    job_manager.update_job(
        job_id,
        status=JOB_STATUS_RUNNING,
        current_stage="initializing",
        progress=2,
        eta_hint="Starting pipeline...",
    )

    insights = load_current_insights()
    return SalesRunContext(
        request=carried,
        job_id=job_id,
        insights_ctx=format_insights_for_prompt(insights),
        insights_version=insights.insights_version if insights else None,
        insights_total_outcomes=insights.total_outcomes_analyzed if insights else 0,
    ).model_dump(mode="json")


@activity.defn(name="sales_prospect")
def prospect_activity(
    ctx: dict[str, Any], existing_prospects: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Generate (or adopt supplied) prospects. Single-shot.

    Preconditions:
        - ``ctx`` is the ``sales_prepare`` carrier; ``existing_prospects`` is
          the request's supplied-leads list (possibly empty).

    Postconditions:
        - Job terminal/missing → returns ``[]`` (finalize surfaces the state).
        - Non-empty ``existing_prospects`` → returns them validated/normalized
          (mirrors the thread path's supplied-leads passthrough).
        - Otherwise generates prospects via the orchestrator. On failure the
          error propagates unmarked so the workflow's retry policy actually
          re-runs the work; only exhausted retries reach the workflow's
          catch-all FAILED write.
    """
    from sales_team.models import Prospect

    sctx = SalesRunContext.model_validate(ctx)
    if _job_stopped(sctx.job_id):
        return []
    if existing_prospects:
        return [Prospect.model_validate(p).model_dump(mode="json") for p in existing_prospects]
    try:
        orch, run_ctx = _orch_and_ctx(sctx)
        with _beating():
            prospects = orch._run_prospecting(run_ctx)
        return [p.model_dump(mode="json") for p in prospects]
    except Exception:
        activity.logger.exception("sales_prospect failed for job %s", sctx.job_id)
        raise


@activity.defn(name="sales_load_dossiers")
def load_dossiers_activity(prospects: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Batch-load saved dossiers for ``prospects``, keyed by prospect id.

    Preconditions:
        - ``prospects`` are serialized ``Prospect`` dicts.

    Postconditions:
        - Returns ``{prospect_id: dossier_dict}`` for prospects with a saved
          dossier (others absent), mirroring the thread path.
        - Never raises: any failure returns ``{}`` (logged) — the same
          "no dossiers = no personalization basis" contract as the module
          function, and the workflow re-attempts the load at the proposal
          boundary exactly like the thread path does.
    """
    try:
        from sales_team.models import Prospect
        from sales_team.orchestrator import load_dossiers_for_prospects

        objs = [Prospect.model_validate(p) for p in prospects]
        dmap = load_dossiers_for_prospects(objs)
        return {pid: d.model_dump(mode="json") for pid, d in dmap.items()}
    except Exception:
        activity.logger.exception("sales_load_dossiers failed; continuing without dossiers")
        return {}


@activity.defn(name="sales_coach")
def coach_activity(
    ctx: dict[str, Any], prospects: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Generate the pipeline coaching report — fully best-effort.

    Preconditions:
        - ``ctx`` is the ``sales_prepare`` carrier.

    Postconditions:
        - Returns the coaching report dict, or ``None`` on ANY failure
          (validation, agent construction, or the review itself) — coaching
          never fails a pipeline run, matching the thread path. Constructs
          only the coach agent, not the full orchestrator.
    """
    try:
        from sales_team.models import Prospect
        from sales_team.orchestrator import coach_review

        sctx = SalesRunContext.model_validate(ctx)
        objs = [Prospect.model_validate(p) for p in prospects]
        with _beating():
            report = coach_review(objs, sctx.request.product_name, sctx.insights_ctx)
        return report.model_dump(mode="json") if report is not None else None
    except Exception:
        activity.logger.exception("sales_coach failed; skipping coaching report (best-effort)")
        return None


@activity.defn(name="sales_report_progress")
def report_progress_activity(job_id: str, stage: str, pct: int) -> bool:
    """Write stage progress and report whether the job is still active.

    Preconditions:
        - ``job_id`` is a job-store id; ``stage``/``pct`` come from the shared
          ``routing.STAGE_PROGRESS`` bands.

    Postconditions:
        - Job missing/terminal → returns ``False`` and writes nothing, letting
          the workflow stop scheduling work for a finished job.
        - Otherwise writes progress (shared field set with the thread path's
          ``on_update``) and returns ``True``.
    """
    from sales_team.job_runner import write_job_progress

    if _job_stopped(job_id):
        return False
    write_job_progress(job_id, stage, pct)
    return True


@activity.defn(name="sales_mark_failed")
def mark_failed_activity(job_id: str, error: str) -> None:
    """Record the terminal FAILED state — the single writer of FAILED.

    Invoked only by the workflow's catch-all after a fatal pipeline error has
    exhausted its retries, so failure marking can never defeat an activity's
    own retry policy.

    Preconditions:
        - ``error`` is the stringified fatal error.

    Postconditions:
        - Job missing or already terminal → no-op (a cancel/interrupt/earlier
          terminal state is never clobbered).
        - Otherwise the row ends FAILED with ``error`` recorded.
    """
    from sales_team.job_runner import write_job_failed

    if _job_stopped(job_id):
        activity.logger.info(
            "Sales job %s missing/terminal at mark-failed; leaving status untouched", job_id
        )
        return
    write_job_failed(job_id, error)


@activity.defn(name="sales_finalize")
def finalize_sales_pipeline_activity(ctx: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Record outcomes, assemble the summary, and write COMPLETED.

    The only writer of COMPLETED. Works on the raw result dict — the payload
    was assembled purely from per-activity ``model_dump`` outputs, so it is
    NOT re-validated here: re-running model validators outside their original
    construction context (e.g. the outreach confidence gate, built with the
    request's configured threshold) would silently mutate already-validated
    data.

    Preconditions:
        - ``ctx`` is the ``sales_prepare`` carrier; ``result`` is the
          workflow-assembled pipeline result (JSON-shaped).

    Postconditions:
        - Job CANCELLED/INTERRUPTED/COMPLETED → returns without writing (a
          cancel wins; a replay after a successful write is a clean no-op).
        - Job FAILED → raises non-retryably; job missing → raises (retryable)
          — a failure or unreadable store is surfaced as a failed workflow,
          never masked as success (the pre-decomposition invariant).
        - No-prospects run → COMPLETED with the shared "Pipeline halted"
          summary; outcomes are not recorded.
        - Otherwise records one prospecting outcome per prospect (deterministic
          outcome ids — replays overwrite rather than duplicate) and writes
          COMPLETED with the summary attached. Errors propagate unmarked so
          the workflow's IO retry can actually re-attempt the write.
    """
    from sales_team.job_runner import write_job_completed
    from sales_team.models import Prospect
    from sales_team.orchestrator import (
        NO_PROSPECTS_SUMMARY,
        build_pipeline_summary,
        record_prospecting_outcomes,
    )

    sctx = SalesRunContext.model_validate(ctx)
    job_id = sctx.job_id
    missing_msg = f"Sales pipeline job {job_id} not found at finalize"

    if (
        _terminal_guard(job_id, phase="sales_finalize", missing_msg=missing_msg)
        is _GuardOutcome.STOP
    ):
        return {"job_id": job_id}

    prospects_raw = result.get("prospects") or []
    if not prospects_raw:
        summary = NO_PROSPECTS_SUMMARY
    else:
        prospect_objs = [Prospect.model_validate(p) for p in prospects_raw]
        record_prospecting_outcomes(prospect_objs, job_id)
        summary = build_pipeline_summary(
            str(result.get("entry_stage", "")),
            prospects=len(prospects_raw),
            outreach_sequences=len(result.get("outreach_sequences") or []),
            qualified_leads=len(result.get("qualified_leads") or []),
            nurture_sequences=len(result.get("nurture_sequences") or []),
            discovery_plans=len(result.get("discovery_plans") or []),
            proposals=len(result.get("proposals") or []),
            closing_strategies=len(result.get("closing_strategies") or []),
            insights_version=sctx.insights_version,
            insights_total_outcomes=sctx.insights_total_outcomes,
        )
    final_result = {**result, "summary": summary}

    # A cancel can land while we assembled the summary; don't clobber it.
    if (
        _terminal_guard(job_id, phase="sales_finalize", missing_msg=missing_msg)
        is _GuardOutcome.STOP
    ):
        return {"job_id": job_id}

    write_job_completed(job_id, final_result)
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# Per-prospect fan-out activities
#
# Each delegates to the shared ``SalesPodOrchestrator.<stage>_one`` method via
# one runner so the scaffolding (validation, terminal short-circuit, heartbeat,
# dump, log-then-raise) cannot drift across the six stages. Errors re-raise so
# Temporal retries; the workflow's ``gather(return_exceptions=True)`` drops a
# prospect only after its retries are exhausted. None of these write job
# FAILED — a single prospect's failure is not fatal to the run (identical to
# the thread path's per-prospect skip).
# ---------------------------------------------------------------------------


def _stage_one(
    ctx: dict[str, Any],
    prospect: dict[str, Any],
    log_tag: str,
    invoke: Callable[[Any, Any, Any], Any],
) -> dict[str, Any]:
    """Shared per-prospect activity body.

    Preconditions:
        - ``invoke(orch, prospect_obj, run_ctx)`` performs exactly one
          prospect's stage call and returns a Pydantic model.

    Postconditions:
        - Job terminal/missing → raises a non-retryable ``ApplicationError``,
          so a cancel stops further LLM spend within the current fan-out (the
          workflow drops the item without retrying it).
        - On success returns the model dumped to a JSON dict; on failure logs
          with the prospect id and re-raises for Temporal's retry policy.
    """
    from sales_team.models import Prospect

    sctx = SalesRunContext.model_validate(ctx)
    p = Prospect.model_validate(prospect)
    if _job_stopped(sctx.job_id):
        raise ApplicationError(
            f"Sales job {sctx.job_id} is terminal; skipping {log_tag}", non_retryable=True
        )
    try:
        orch, run_ctx = _orch_and_ctx(sctx)
        with _beating():
            model = invoke(orch, p, run_ctx)
        return model.model_dump(mode="json")
    except ApplicationError:
        raise
    except Exception:
        activity.logger.exception("%s failed prospect_id=%s", log_tag, p.id)
        raise


@activity.defn(name="sales_outreach_one")
def outreach_one_activity(
    ctx: dict[str, Any], prospect: dict[str, Any], dossier: dict[str, Any]
) -> dict[str, Any]:
    """Generate one prospect's critic-gated outreach sequence.

    Preconditions:
        - ``dossier`` is non-``None`` (the workflow only schedules this for
          prospects that have a dossier).
    Postconditions:
        - See :func:`_stage_one`.
    """
    from sales_team.models import ProspectDossier

    dossier_obj = ProspectDossier.model_validate(dossier)
    return _stage_one(
        ctx,
        prospect,
        "sales_outreach_one",
        lambda orch, p, rc: orch.outreach_one(p, dossier_obj, rc),
    )


@activity.defn(name="sales_qualify_one")
def qualify_one_activity(ctx: dict[str, Any], prospect: dict[str, Any]) -> dict[str, Any]:
    """Qualify one prospect (BANT/MEDDIC).

    Postconditions: see :func:`_stage_one`.
    """
    return _stage_one(
        ctx, prospect, "sales_qualify_one", lambda orch, p, rc: orch.qualify_one(p, rc)
    )


@activity.defn(name="sales_nurture_one")
def nurture_one_activity(ctx: dict[str, Any], prospect: dict[str, Any]) -> dict[str, Any]:
    """Build one prospect's nurture sequence.

    Postconditions: see :func:`_stage_one`.
    """
    return _stage_one(
        ctx, prospect, "sales_nurture_one", lambda orch, p, rc: orch.nurture_one(p, rc)
    )


@activity.defn(name="sales_discovery_one")
def discovery_one_activity(
    ctx: dict[str, Any],
    prospect: dict[str, Any],
    qual: Optional[dict[str, Any]],
    dossier: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Prepare one prospect's discovery plan (``qual``/``dossier`` may be ``None``).

    Postconditions: see :func:`_stage_one`.
    """
    from sales_team.models import ProspectDossier, QualificationScore

    qual_obj = QualificationScore.model_validate(qual) if qual is not None else None
    dossier_obj = ProspectDossier.model_validate(dossier) if dossier is not None else None
    return _stage_one(
        ctx,
        prospect,
        "sales_discovery_one",
        lambda orch, p, rc: orch.discovery_one(p, qual_obj, rc, dossier_obj),
    )


@activity.defn(name="sales_proposal_one")
def proposal_one_activity(
    ctx: dict[str, Any],
    prospect: dict[str, Any],
    dossier: Optional[dict[str, Any]],
    qual: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Write one prospect's critic-gated proposal (``dossier``/``qual`` optional).

    Postconditions: see :func:`_stage_one`.
    """
    from sales_team.models import ProspectDossier, QualificationScore

    dossier_obj = ProspectDossier.model_validate(dossier) if dossier is not None else None
    qual_obj = QualificationScore.model_validate(qual) if qual is not None else None
    return _stage_one(
        ctx,
        prospect,
        "sales_proposal_one",
        lambda orch, p, rc: orch.proposal_one(p, dossier_obj, qual_obj, rc),
    )


@activity.defn(name="sales_close_one")
def close_one_activity(
    ctx: dict[str, Any],
    prospect: dict[str, Any],
    proposal: Optional[dict[str, Any]],
    dossier: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Develop one prospect's closing strategy (``proposal``/``dossier`` may be ``None``).

    Postconditions: see :func:`_stage_one`.
    """
    from sales_team.models import ProspectDossier, SalesProposal

    proposal_obj = SalesProposal.model_validate(proposal) if proposal is not None else None
    dossier_obj = ProspectDossier.model_validate(dossier) if dossier is not None else None
    return _stage_one(
        ctx,
        prospect,
        "sales_close_one",
        lambda orch, p, rc: orch.close_one(p, proposal_obj, rc, dossier_obj),
    )
