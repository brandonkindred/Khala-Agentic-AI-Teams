"""Temporal activities for the market_research team — one per pipeline stage.

The fine-grained ``MarketResearchWorkflow`` fans the pipeline out into these
activities: a single-shot ``market_research_prepare`` opens the run, then
``market_research_ingest`` loads transcripts, ``market_research_ux_one`` fans
out **one activity per transcript**, and single-shot ``psychology`` /
``consistency`` / ``viability`` / ``scripts`` stages fan in. Every specialist
call delegates to the shared ``MarketResearchOrchestrator.<stage>`` methods, so
the exact same agent logic runs in both the thread path (``orchestrator.run``)
and Temporal mode (durable, individually retryable activities visible in the
Temporal UI). ``market_research_finalize`` assembles the ``TeamOutput`` and
writes COMPLETED.

Import hygiene: top-level imports stay light (``temporalio``, typing,
``phase_models``, ``shared.concurrency``); heavy imports (``orchestrator``,
``pipeline``, ``models``, ``job_store``) are lazy inside function bodies, and
``os.getenv`` is only ever reached at call time — never at import — so the
temporalio workflow sandbox that re-imports sibling modules during workflow
registration is never tripped.

Job-store status ownership (the retry-safe contract, mirrored from sales_team):
    - RUNNING is written once, by ``market_research_prepare``.
    - Per-stage progress is written by ``market_research_report_progress``,
      which also reports whether the job is still active so the workflow stops
      spending on a cancelled job.
    - COMPLETED is written only by ``market_research_finalize``.
    - FAILED is written only by ``market_research_mark_failed``, which the
      WORKFLOW invokes after a fatal error has exhausted its retries. Activities
      never write FAILED themselves — an activity that recorded FAILED mid-retry
      would trip its own terminal-status guard on the next attempt and defeat
      the retry policy.
    - Terminal-state semantics are status-aware: a CANCELLED/INTERRUPTED (or
      already-COMPLETED) job short-circuits cleanly, while a FAILED or missing
      job at prepare/finalize RAISES — a real failure (or an unreadable store)
      must surface as a failed Temporal workflow, never be masked as success.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from market_research_team.temporal.phase_models import MarketResearchRunContext
from shared.concurrency import BackgroundHeartbeat

_DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
# Heartbeat timeout the workflow schedules every long LLM activity with. Owned
# here (next to the beat interval) so the two knobs cannot drift across modules:
# the interval is clamped to a third of this value, guaranteeing at least ~3
# beats per timeout window regardless of operator configuration.
HEARTBEAT_TIMEOUT_S = 180.0


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _heartbeat_interval_s() -> float:
    """Heartbeat cadence (seconds) for the long LLM activities.

    Preconditions:
        - None (environment may be unset or hold garbage).
    Postconditions:
        - Returns ``MARKET_RESEARCH_TEMPORAL_HEARTBEAT_INTERVAL_S`` clamped to
          ``[1, HEARTBEAT_TIMEOUT_S / 3]`` (garbage/unset → the 30s default);
          the ceiling guarantees beats always outpace the activity heartbeat
          timeout, so a mis-set interval can never spuriously fail activities.
    """
    from shared.env_config import env_float

    return env_float(
        "MARKET_RESEARCH_TEMPORAL_HEARTBEAT_INTERVAL_S",
        _DEFAULT_HEARTBEAT_INTERVAL_S,
        floor=1.0,
        ceiling=HEARTBEAT_TIMEOUT_S / 3.0,
    )


def _beating() -> BackgroundHeartbeat:
    """Background beater keeping a long LLM activity alive across blocking calls.

    Preconditions:
        - Called from inside a running activity body (the constructor snapshots
          the calling thread's context; beat errors outside an activity context
          — e.g. unit tests — are swallowed by the beater).
    Postconditions:
        - Returns an unstarted context manager; entering it starts the daemon
          beater, exiting stops and joins it.
    """
    return BackgroundHeartbeat(
        activity.heartbeat,
        _heartbeat_interval_s(),
        name="market-research-heartbeat",
        copy_context=True,
        join_timeout=5.0,
    )


_STATUS_SETS: Optional[tuple[frozenset[str], frozenset[str]]] = None


def _status_sets() -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(terminal, clean_terminal)`` job-status sets.

    Preconditions:
        - None.
    Postconditions:
        - ``terminal`` = completed/failed/cancelled/interrupted; ``clean`` is
          ``terminal`` minus FAILED (a cancel/interrupt/completed replay ends a
          run cleanly, whereas FAILED must surface as a failed workflow).
        - Computed once and memoized: ``_job_stopped`` calls this on every
          activity/guard, so the constants import + frozenset build must not
          repeat per call.
    """
    global _STATUS_SETS
    if _STATUS_SETS is None:
        from job_service_client import (
            JOB_STATUS_CANCELLED,
            JOB_STATUS_COMPLETED,
            JOB_STATUS_FAILED,
            JOB_STATUS_INTERRUPTED,
        )

        terminal = frozenset(
            {JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED, JOB_STATUS_INTERRUPTED}
        )
        clean = frozenset({JOB_STATUS_COMPLETED, JOB_STATUS_CANCELLED, JOB_STATUS_INTERRUPTED})
        _STATUS_SETS = (terminal, clean)
    return _STATUS_SETS


def _job_status(job_id: str) -> Optional[str]:
    """Read the job's current status from the job store.

    Preconditions:
        - ``job_id`` is a job-store id (the row may or may not exist).
    Postconditions:
        - Returns the status string, or ``None`` when the row is missing —
          callers decide whether missing is a clean skip or an error.
    """
    from market_research_team.shared.job_store import get_job

    job = get_job(job_id)
    return job.get("status") if job else None


def _job_stopped(job_id: str) -> bool:
    """Whether pipeline work must not proceed for ``job_id``.

    Preconditions:
        - ``job_id`` is a job-store id (the row may or may not exist).
    Postconditions:
        - Returns ``True`` when the job is missing or in any terminal state — a
          queued/running activity must never do work (or write progress) for a
          job that has already ended.
    """
    terminal, _ = _status_sets()
    status = _job_status(job_id)
    return status is None or status in terminal


def _check_not_terminal(job_id: str, stage: str) -> Optional[str]:
    """Guard a lifecycle activity against a missing/FAILED/clean-terminal job.

    Shared by ``prepare_activity`` and ``finalize_activity`` — the two
    lifecycle activities that must RAISE (not silently no-op) on a missing or
    already-FAILED job, but cleanly short-circuit on a clean terminal state.
    Each caller builds its own short-circuit return payload.

    Preconditions:
        - ``job_id`` is a job-store id; ``stage`` labels the caller (used only
          in raised/logged messages, e.g. ``"prepare"``/``"finalize"``).
    Postconditions:
        - Job missing → raises ``RuntimeError`` (retryable — a transient store
          read glitch is retried by the workflow's IO policy).
        - Job FAILED → raises a non-retryable ``ApplicationError`` (never
          resurrect a failed job).
        - Job in a clean-terminal state (COMPLETED/CANCELLED/INTERRUPTED) →
          logs and returns that status string.
        - Otherwise (job active) → returns ``None``.
    """
    from market_research_team.shared.job_store import JOB_STATUS_FAILED

    _, clean_terminal = _status_sets()
    status = _job_status(job_id)
    if status is None:
        raise RuntimeError(f"Market research job {job_id} not found at {stage}")
    if status == JOB_STATUS_FAILED:
        raise ApplicationError(
            f"Market research job {job_id} was already FAILED at {stage}", non_retryable=True
        )
    if status in clean_terminal:
        activity.logger.info(
            "Market research job %s terminal (%s) at %s; short-circuiting", job_id, status, stage
        )
        return status
    return None


def _orch():
    """Construct a fresh ``MarketResearchOrchestrator`` (lazy import).

    Preconditions:
        - None.
    Postconditions:
        - Returns a new orchestrator whose specialist agents are lazily built on
          first use (and resolve their LLM client lazily), so constructing it per
          activity is cheap and only the stage this activity invokes builds an
          agent or touches the provider.
    """
    from market_research_team.orchestrator import MarketResearchOrchestrator

    return MarketResearchOrchestrator()


def _mission_and_review(sctx: MarketResearchRunContext):
    """Rebuild ``(mission, human_review)`` from the carrier's stripped request.

    Preconditions:
        - ``sctx.request`` is the transcripts-stripped run request from
          ``market_research_prepare``; the non-ingest stages need only its
          mission + human-review fields.
    Postconditions:
        - Returns ``(ResearchMission, HumanReview)`` derived from the carrier.
    """
    from market_research_team.pipeline import prepare

    return prepare(sctx.request)


def _signals_stage(
    ctx: dict[str, Any],
    insights: list[dict[str, Any]],
    invoke: Callable[[Any, list[Any]], list[Any]],
) -> list[dict[str, Any]]:
    """Shared body for the single-shot signal stages (psychology, consistency).

    Preconditions:
        - ``ctx`` is the ``market_research_prepare`` carrier; ``insights`` are the
          collected per-transcript UX insight dicts; ``invoke(orch, insight_objs)``
          runs exactly one signal stage and returns ``list[MarketSignal]``.
    Postconditions:
        - Job terminal/missing → returns ``[]`` (finalize surfaces the state).
        - Otherwise returns the stage's signals dumped to JSON dicts.
    """
    from market_research_team.models import InterviewInsight

    sctx = MarketResearchRunContext.model_validate(ctx)
    if _job_stopped(sctx.job_id):
        return []
    insight_objs = [InterviewInsight.model_validate(i) for i in insights]
    with _beating():
        signals = invoke(_orch(), insight_objs)
    return [s.model_dump(mode="json") for s in signals]


# ---------------------------------------------------------------------------
# Single-shot lifecycle activities
# ---------------------------------------------------------------------------


@activity.defn(name="market_research_prepare")
def prepare_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Open the run: validate the request, guard terminal state, write RUNNING.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store (PENDING).
        - ``request`` is the serialized ``RunMarketResearchRequest``.

    Postconditions:
        - Invalid ``request`` → raises a non-retryable ``ApplicationError``
          (deterministic — retrying cannot help); the workflow's catch-all marks
          the job FAILED so it never sits stuck PENDING.
        - Job missing → raises (retryable); job already FAILED → raises
          non-retryably; job CANCELLED/INTERRUPTED/COMPLETED → returns a ctx with
          ``stopped=True`` (a queued workflow cannot resurrect a finished job).
        - Otherwise writes RUNNING and returns the ``MarketResearchRunContext``
          carrier with ``transcripts``/``transcript_folder_path`` stripped.
    """
    from market_research_team.models import RunMarketResearchRequest
    from market_research_team.shared.job_store import JOB_STATUS_RUNNING, update_job

    try:
        req = RunMarketResearchRequest(**request)
    except Exception as exc:
        raise ApplicationError(
            f"Invalid RunMarketResearchRequest for job {job_id}: {exc}", non_retryable=True
        ) from exc

    carried = req.model_copy(update={"transcripts": [], "transcript_folder_path": None})

    if _check_not_terminal(job_id, "prepare") is not None:
        return MarketResearchRunContext(request=carried, job_id=job_id, stopped=True).model_dump(
            mode="json"
        )

    update_job(job_id, status=JOB_STATUS_RUNNING)
    return MarketResearchRunContext(request=carried, job_id=job_id).model_dump(mode="json")


@activity.defn(name="market_research_ingest")
def ingest_activity(job_id: str, request: dict[str, Any]) -> list[dict[str, Any]]:
    """Load transcripts, persist them to the per-job store, return references.

    Preconditions:
        - ``request`` is the FULL serialized request (transcripts included) — the
          workflow passes its own input here rather than the stripped ctx.

    Postconditions:
        - Job terminal/missing → returns ``[]`` (finalize surfaces the state).
        - Otherwise persists each loaded transcript to the shared per-job
          transcript store and returns lightweight refs
          (``[{"index": i, "source": source}, ...]``). The transcript *bodies*
          are deliberately NOT serialized through Temporal workflow history —
          each ``ux_one`` loads its own transcript from the store — so large or
          folder-based corpora can't bloat history or hit payload limits.
    """
    from market_research_team.models import RunMarketResearchRequest
    from market_research_team.pipeline import build_mission
    from market_research_team.shared.transcript_store import save_transcripts

    if _job_stopped(job_id):
        return []
    mission = build_mission(RunMarketResearchRequest(**request))
    loaded = _orch().ingest(mission)
    return save_transcripts(job_id, loaded)


@activity.defn(name="market_research_report_progress")
def report_progress_activity(job_id: str, stage: str, pct: int) -> bool:
    """Write stage progress and report whether the job is still active.

    Preconditions:
        - ``job_id`` is a job-store id; ``stage`` labels the current stage and
          ``pct`` is a 0-100 progress hint.

    Postconditions:
        - Job missing/terminal → returns ``False`` and writes nothing, letting
          the workflow stop scheduling work for a finished job.
        - Otherwise writes ``{progress, current_stage}`` and returns ``True``.
    """
    from market_research_team.shared.job_store import update_job

    if _job_stopped(job_id):
        return False
    update_job(job_id, progress=pct, current_stage=stage)
    return True


@activity.defn(name="market_research_mark_failed")
def mark_failed_activity(job_id: str, error: str) -> None:
    """Record the terminal FAILED state — the single writer of FAILED.

    Invoked only by the workflow's catch-all after a fatal pipeline error has
    exhausted its retries, so failure marking can never defeat an activity's own
    retry policy.

    Preconditions:
        - ``error`` is the stringified fatal error (see ``_root_cause_message``
          in the workflow module, which unwraps Temporal's ``ActivityError``
          wrapper to the real underlying message before calling this).
    Postconditions:
        - Job missing or already terminal → status left untouched (a
          cancel/interrupt/earlier terminal state is never clobbered).
        - Otherwise the row ends FAILED with ``error`` recorded.
        - Always clears the job's persisted transcripts (best-effort) — this is
          the terminal cleanup for failure paths that never reach finalize
          (e.g. the workflow's catch-all after an all-UX-failed error).
    """
    from market_research_team.shared.job_store import JOB_STATUS_FAILED, update_job
    from market_research_team.shared.transcript_store import clear_transcripts

    try:
        if _job_stopped(job_id):
            activity.logger.info(
                "Market research job %s missing/terminal at mark-failed; leaving status untouched",
                job_id,
            )
            return
        update_job(job_id, status=JOB_STATUS_FAILED, error=error)
    finally:
        clear_transcripts(job_id)


@activity.defn(name="market_research_cleanup_transcripts")
def cleanup_transcripts_activity(job_id: str) -> None:
    """Best-effort cleanup of a job's persisted transcripts.

    ``finalize_activity``/``mark_failed_activity`` are the DAG's only other
    cleanup sites (both clear the store on every exit path, including their own
    terminal short-circuits). Neither runs when the workflow's progress gates
    short-circuit on an already-terminal job (cancel / stale-job monitor) —
    those gates return directly without reaching finalize or mark-failed — so
    without this activity a cancelled run's persisted transcript directory
    would sit on disk (potentially indefinitely, on a long-running worker
    that's never restarted) until the next process-startup ``sweep_orphaned``
    sweep.

    Preconditions:
        - ``job_id`` is the run's job id. Transcripts may or may not have been
          persisted for it yet — ``clear_transcripts`` no-ops if the
          directory was never created (e.g. the "ingest" gate stopped before
          ``ingest_activity`` ever ran).
    Postconditions:
        - Removes the job's persisted transcript directory if present. Never
          raises — this only runs on an already-terminal job, so a cleanup
          failure must not turn a clean cancel into a failed workflow.
    """
    from market_research_team.shared.transcript_store import clear_transcripts

    clear_transcripts(job_id)


@activity.defn(name="market_research_finalize")
def finalize_activity(
    ctx: dict[str, Any],
    insights: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    recommendation: dict[str, Any],
    scripts: list[str],
) -> dict[str, Any]:
    """Assemble the ``TeamOutput`` and write COMPLETED — the only COMPLETED writer.

    Preconditions:
        - ``ctx`` is the ``market_research_prepare`` carrier; ``insights`` /
          ``signals`` / ``recommendation`` / ``scripts`` are the workflow-collected
          per-stage outputs (JSON-shaped).

    Postconditions:
        - Job CANCELLED/INTERRUPTED/COMPLETED → returns without writing (a cancel
          wins; a replay after a successful write is a clean no-op).
        - Job FAILED → raises non-retryably; job missing → raises (retryable) —
          a failure or unreadable store surfaces as a failed workflow, never
          masked as success.
        - Otherwise assembles the ``TeamOutput`` (human-review branch + min-2
          signals) and writes COMPLETED with ``result`` attached.
        - Always clears the job's persisted transcripts (best-effort) once it
          runs — finalize is last in the DAG, so every ``ux_one`` has already
          loaded what it needs.
    """
    from market_research_team.models import (
        InterviewInsight,
        MarketSignal,
        ViabilityRecommendation,
    )
    from market_research_team.shared.job_store import JOB_STATUS_COMPLETED, update_job
    from market_research_team.shared.transcript_store import clear_transcripts

    sctx = MarketResearchRunContext.model_validate(ctx)
    job_id = sctx.job_id

    def _terminal_short_circuit() -> Optional[dict[str, Any]]:
        return {"job_id": job_id} if _check_not_terminal(job_id, "finalize") is not None else None

    try:
        early = _terminal_short_circuit()
        if early is not None:
            return early

        mission, human_review = _mission_and_review(sctx)
        output = _orch().assemble(
            mission,
            human_review,
            [InterviewInsight.model_validate(i) for i in insights],
            [MarketSignal.model_validate(s) for s in signals],
            ViabilityRecommendation.model_validate(recommendation),
            list(scripts),
        )

        # A cancel can land while we assembled the output; don't clobber it.
        early = _terminal_short_circuit()
        if early is not None:
            return early

        update_job(job_id, status=JOB_STATUS_COMPLETED, result=output.model_dump())
        return {"job_id": job_id}
    finally:
        # finalize is last in the DAG — the persisted transcripts are no longer
        # needed once it runs, on any outcome (COMPLETED, clean-terminal, or the
        # FAILED/missing re-raise). Never raises, so it can't mask the outcome.
        clear_transcripts(job_id)


# ---------------------------------------------------------------------------
# Per-stage LLM activities (delegate to the shared orchestrator seam)
# ---------------------------------------------------------------------------


@activity.defn(name="market_research_ux_one")
def ux_one_activity(ctx: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    """Extract one interview's ``InterviewInsight``. Per-transcript fan-out unit.

    Preconditions:
        - ``ctx`` is the ``market_research_prepare`` carrier; ``ref`` is one of
          ``market_research_ingest``'s references (``{"index": i, "source": …}``).
          The transcript *body* is loaded from the per-job store — it never
          crosses the workflow boundary as an input.

    Postconditions:
        - Job terminal/missing → raises a non-retryable ``ApplicationError`` so a
          cancel stops further LLM spend within the fan-out (the workflow's
          ``gather`` drops the item without retrying it).
        - Otherwise loads the transcript from the store, returns the insight
          dumped to a JSON dict; on failure logs and re-raises for Temporal's
          retry policy.
    """
    from market_research_team.shared.transcript_store import load_transcript

    sctx = MarketResearchRunContext.model_validate(ctx)
    if _job_stopped(sctx.job_id):
        raise ApplicationError(
            f"Market research job {sctx.job_id} is terminal; skipping ux_one", non_retryable=True
        )
    try:
        source, transcript = load_transcript(sctx.job_id, ref["index"])
        with _beating():
            insight = _orch().ux_one(source, transcript)
        return insight.model_dump(mode="json")
    except ApplicationError:
        raise
    except Exception:
        activity.logger.exception("market_research_ux_one failed ref=%s", ref)
        raise


@activity.defn(name="market_research_psychology")
def psychology_activity(
    ctx: dict[str, Any], insights: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Derive adoption/behavior signals from the collected insights. Single-shot.

    Preconditions:
        - ``ctx`` is the ``market_research_prepare`` carrier; ``insights`` are
          the collected per-transcript UX insight dicts (may be empty).
    Postconditions:
        - Job terminal/missing → returns ``[]`` (finalize surfaces the state).
        - Otherwise returns the signal dicts (agent guarantees at least two).
    """
    return _signals_stage(ctx, insights, lambda orch, objs: orch.psychology(objs))


@activity.defn(name="market_research_consistency")
def consistency_activity(
    ctx: dict[str, Any], insights: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Score cross-interview theme consistency (split mode only). Single-shot.

    Preconditions:
        - ``ctx`` is the ``market_research_prepare`` carrier; ``insights`` are
          the collected per-transcript UX insight dicts (may be empty).
    Postconditions:
        - Job terminal/missing → returns ``[]`` (finalize surfaces the state).
        - Empty ``insights`` → the deterministic fallback signal (no LLM call);
          otherwise the consistency agent's single-signal assessment.
    """
    return _signals_stage(ctx, insights, lambda orch, objs: orch.consistency(objs))


@activity.defn(name="market_research_viability")
def viability_activity(
    ctx: dict[str, Any], signals: list[dict[str, Any]], insight_count: int
) -> dict[str, Any]:
    """Produce the viability verdict from the derived signals. Single-shot.

    Preconditions:
        - ``ctx`` is the ``market_research_prepare`` carrier; ``signals`` are
          the derived signal dicts; ``insight_count`` is the number of
          successfully-analyzed transcripts (``len(insights)``).
    Postconditions:
        - Job terminal/missing → returns the deterministic zero-evidence
          recommendation (no LLM call); finalize discards it on the terminal
          short-circuit.
        - Otherwise returns the recommendation dumped to a JSON dict.
    """
    from market_research_team.models import MarketSignal

    sctx = MarketResearchRunContext.model_validate(ctx)
    if _job_stopped(sctx.job_id):
        # Checked before rebuilding the mission or touching signals — matches
        # the stopped-first ordering every sibling stage activity uses.
        # Deterministic (insight_count=0 short-circuit) — no LLM spend on cancel.
        mission, _ = _mission_and_review(sctx)
        return _orch().viability(mission, [], 0).model_dump(mode="json")
    mission, _ = _mission_and_review(sctx)
    signal_objs = [MarketSignal.model_validate(s) for s in signals]
    with _beating():
        recommendation = _orch().viability(mission, signal_objs, insight_count)
    return recommendation.model_dump(mode="json")


@activity.defn(name="market_research_scripts")
def scripts_activity(ctx: dict[str, Any]) -> list[str]:
    """Generate the research scripts/templates for the mission. Single-shot.

    Preconditions:
        - ``ctx`` is the ``market_research_prepare`` carrier.
    Postconditions:
        - Job terminal/missing → returns ``[]`` (finalize surfaces the state).
        - Otherwise returns the non-empty scripts list.
    """
    sctx = MarketResearchRunContext.model_validate(ctx)
    if _job_stopped(sctx.job_id):
        return []
    mission, _ = _mission_and_review(sctx)
    with _beating():
        return _orch().scripts(mission)


# ---------------------------------------------------------------------------
# Legacy whole-pipeline activity — kept registered for drain-out only
# ---------------------------------------------------------------------------


@activity.defn(name="market_research_run_pipeline")
def run_pipeline_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Legacy single-activity pipeline, kept registered for drain-out ONLY.

    A pre-decomposition ``MarketResearchWorkflow`` history that is still open when
    the fine-grained decomposition deploys replays the workflow's unpatched
    branch (``not workflow.patched(...)``), which re-schedules THIS activity; it
    must stay registered until those in-flight runs drain. New runs take the
    patched per-stage DAG instead. Removal: once every pre-decomposition run has
    drained, delete this activity and the workflow's unpatched branch.

    Fidelity note: this activity delegates to the SAME ``MarketResearchOrchestrator``
    class the per-stage DAG uses, not a frozen pre-decomposition copy. Temporal's
    replay determinism only requires that this activity type is scheduled with
    the same args/options on replay (guaranteed above) — it has no visibility
    into what runs inside the activity body. So while the job-store status
    bookkeeping below genuinely is unchanged, the orchestrator's own analysis
    methodology is NOT frozen: a future change to ``MarketResearchOrchestrator.run``
    changes what a drained-out run actually does, even though the drain-out
    mechanism itself keeps working correctly.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``request`` is the serialized ``RunMarketResearchRequest``.

    Postconditions:
        - RUNNING → COMPLETED with the orchestrator result on success; a cancel
          leaves the row untouched; a genuine failure marks the row FAILED and
          re-raises. This status-bookkeeping contract (not the orchestrator's
          internal analysis behavior — see the fidelity note above) is what
          stays identical to the pre-decomposition activity.
    """
    from market_research_team.models import RunMarketResearchRequest
    from market_research_team.pipeline import prepare, run_pipeline_core
    from market_research_team.shared.job_store import (
        JOB_STATUS_FAILED,
        is_job_cancelled,
        update_job,
    )

    mission, human_review = prepare(RunMarketResearchRequest(**request))
    try:
        run_pipeline_core(job_id, mission, human_review)
    except Exception as e:
        activity.logger.exception("Market research job %s failed", job_id)
        if is_job_cancelled(job_id):
            return {"job_id": job_id}
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
        raise
    return {"job_id": job_id}
