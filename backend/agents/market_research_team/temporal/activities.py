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
``phase_models``, ``shared_concurrency``); heavy imports (``orchestrator``,
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

from typing import Any, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from market_research_team.temporal.phase_models import MarketResearchRunContext
from shared_concurrency import BackgroundHeartbeat

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

    Postconditions:
        - Returns ``MARKET_RESEARCH_TEMPORAL_HEARTBEAT_INTERVAL_S`` clamped to
          ``[1, HEARTBEAT_TIMEOUT_S / 3]`` (garbage/unset → the 30s default);
          the ceiling guarantees beats always outpace the activity heartbeat
          timeout, so a mis-set interval can never spuriously fail activities.
    """
    from shared_env_config import env_float

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


def _status_sets() -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(terminal, clean_terminal)`` job-status sets.

    Postconditions:
        - ``terminal`` = completed/failed/cancelled/interrupted; ``clean`` is
          ``terminal`` minus FAILED (a cancel/interrupt/completed replay ends a
          run cleanly, whereas FAILED must surface as a failed workflow).
    """
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
    return terminal, clean


def _job_status(job_id: str) -> Optional[str]:
    """Read the job's current status from the job store (``None`` if missing)."""
    from market_research_team.shared.job_store import get_job

    job = get_job(job_id)
    return job.get("status") if job else None


def _job_stopped(job_id: str) -> bool:
    """Whether pipeline work must not proceed for ``job_id``.

    Postconditions:
        - Returns ``True`` when the job is missing or in any terminal state — a
          queued/running activity must never do work (or write progress) for a
          job that has already ended.
    """
    terminal, _ = _status_sets()
    status = _job_status(job_id)
    return status is None or status in terminal


def _orch():
    """Construct a fresh ``MarketResearchOrchestrator`` (lazy import).

    Postconditions:
        - Each specialist agent resolves its LLM client lazily on first call, so
          constructing the orchestrator per activity is cheap and only the
          stage this activity invokes touches the provider.
    """
    from market_research_team.orchestrator import MarketResearchOrchestrator

    return MarketResearchOrchestrator()


def _mission_and_review(sctx: MarketResearchRunContext):
    """Rebuild ``(mission, human_review)`` from the carrier's stripped request.

    Preconditions:
        - ``sctx.request`` is the transcripts-stripped run request from
          ``market_research_prepare``; the non-ingest stages need only its
          mission + human-review fields.
    """
    from market_research_team.pipeline import prepare

    return prepare(sctx.request)


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
    from market_research_team.shared.job_store import (
        JOB_STATUS_FAILED,
        JOB_STATUS_RUNNING,
        update_job,
    )

    try:
        req = RunMarketResearchRequest(**request)
    except Exception as exc:
        raise ApplicationError(
            f"Invalid RunMarketResearchRequest for job {job_id}: {exc}", non_retryable=True
        ) from exc

    carried = req.model_copy(update={"transcripts": [], "transcript_folder_path": None})

    _, clean_terminal = _status_sets()
    status = _job_status(job_id)
    if status is None:
        raise RuntimeError(f"Market research job {job_id} not found at prepare")
    if status == JOB_STATUS_FAILED:
        raise ApplicationError(
            f"Market research job {job_id} was already FAILED before start", non_retryable=True
        )
    if status in clean_terminal:
        activity.logger.info(
            "Market research job %s already terminal (%s) at prepare; stopping run", job_id, status
        )
        return MarketResearchRunContext(request=carried, job_id=job_id, stopped=True).model_dump(
            mode="json"
        )

    update_job(job_id, status=JOB_STATUS_RUNNING)
    return MarketResearchRunContext(request=carried, job_id=job_id).model_dump(mode="json")


@activity.defn(name="market_research_ingest")
def ingest_activity(job_id: str, request: dict[str, Any]) -> list[list[str]]:
    """Load transcript text (inline + folder) for the run. Single-shot, pure I/O.

    Preconditions:
        - ``request`` is the FULL serialized request (transcripts included) — the
          workflow passes its own input here rather than the stripped ctx.

    Postconditions:
        - Job terminal/missing → returns ``[]`` (finalize surfaces the state).
        - Otherwise returns ``[[source, text], ...]`` (tuples serialize as lists).
    """
    from market_research_team.models import RunMarketResearchRequest
    from market_research_team.pipeline import build_mission

    if _job_stopped(job_id):
        return []
    mission = build_mission(RunMarketResearchRequest(**request))
    loaded = _orch().ingest(mission)
    return [[source, text] for source, text in loaded]


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

    Postconditions:
        - Job missing or already terminal → no-op (a cancel/interrupt/earlier
          terminal state is never clobbered).
        - Otherwise the row ends FAILED with ``error`` recorded.
    """
    from market_research_team.shared.job_store import JOB_STATUS_FAILED, update_job

    if _job_stopped(job_id):
        activity.logger.info(
            "Market research job %s missing/terminal at mark-failed; leaving status untouched",
            job_id,
        )
        return
    update_job(job_id, status=JOB_STATUS_FAILED, error=error)


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
    """
    from market_research_team.models import (
        InterviewInsight,
        MarketSignal,
        ViabilityRecommendation,
    )
    from market_research_team.shared.job_store import (
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
        update_job,
    )

    sctx = MarketResearchRunContext.model_validate(ctx)
    job_id = sctx.job_id
    _, clean_terminal = _status_sets()

    def _terminal_short_circuit() -> Optional[dict[str, Any]]:
        status = _job_status(job_id)
        if status is None:
            raise RuntimeError(f"Market research job {job_id} not found at finalize")
        if status == JOB_STATUS_FAILED:
            raise ApplicationError(
                f"Market research job {job_id} was marked FAILED during the run",
                non_retryable=True,
            )
        if status in clean_terminal:
            activity.logger.info(
                "Market research job %s terminal (%s) at finalize; not writing COMPLETED",
                job_id,
                status,
            )
            return {"job_id": job_id}
        return None

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


# ---------------------------------------------------------------------------
# Per-stage LLM activities (delegate to the shared orchestrator seam)
# ---------------------------------------------------------------------------


@activity.defn(name="market_research_ux_one")
def ux_one_activity(ctx: dict[str, Any], source: str, transcript: str) -> dict[str, Any]:
    """Extract one interview's ``InterviewInsight``. Per-transcript fan-out unit.

    Preconditions:
        - ``ctx`` is the ``market_research_prepare`` carrier; ``(source,
          transcript)`` is one loaded interview.

    Postconditions:
        - Job terminal/missing → raises a non-retryable ``ApplicationError`` so a
          cancel stops further LLM spend within the fan-out (the workflow's
          ``gather`` drops the item without retrying it).
        - Otherwise returns the insight dumped to a JSON dict; on failure logs
          and re-raises for Temporal's retry policy.
    """
    sctx = MarketResearchRunContext.model_validate(ctx)
    if _job_stopped(sctx.job_id):
        raise ApplicationError(
            f"Market research job {sctx.job_id} is terminal; skipping ux_one", non_retryable=True
        )
    try:
        with _beating():
            insight = _orch().ux_one(source, transcript)
        return insight.model_dump(mode="json")
    except ApplicationError:
        raise
    except Exception:
        activity.logger.exception("market_research_ux_one failed source=%s", source)
        raise


@activity.defn(name="market_research_psychology")
def psychology_activity(
    ctx: dict[str, Any], insights: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Derive adoption/behavior signals from the collected insights. Single-shot.

    Postconditions:
        - Job terminal/missing → returns ``[]`` (finalize surfaces the state).
        - Otherwise returns the signal dicts (agent guarantees at least two).
    """
    from market_research_team.models import InterviewInsight

    sctx = MarketResearchRunContext.model_validate(ctx)
    if _job_stopped(sctx.job_id):
        return []
    insight_objs = [InterviewInsight.model_validate(i) for i in insights]
    with _beating():
        signals = _orch().psychology(insight_objs)
    return [s.model_dump(mode="json") for s in signals]


@activity.defn(name="market_research_consistency")
def consistency_activity(
    ctx: dict[str, Any], insights: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Score cross-interview theme consistency (split mode only). Single-shot.

    Postconditions:
        - Job terminal/missing → returns ``[]`` (finalize surfaces the state).
        - Empty ``insights`` → the deterministic fallback signal (no LLM call);
          otherwise the consistency agent's single-signal assessment.
    """
    from market_research_team.models import InterviewInsight

    sctx = MarketResearchRunContext.model_validate(ctx)
    if _job_stopped(sctx.job_id):
        return []
    insight_objs = [InterviewInsight.model_validate(i) for i in insights]
    with _beating():
        signals = _orch().consistency(insight_objs)
    return [s.model_dump(mode="json") for s in signals]


@activity.defn(name="market_research_viability")
def viability_activity(
    ctx: dict[str, Any], signals: list[dict[str, Any]], insight_count: int
) -> dict[str, Any]:
    """Produce the viability verdict from the derived signals. Single-shot.

    Postconditions:
        - Job terminal/missing → returns the deterministic zero-evidence
          recommendation (no LLM call); finalize discards it on the terminal
          short-circuit.
        - Otherwise returns the recommendation dumped to a JSON dict.
    """
    from market_research_team.models import MarketSignal

    sctx = MarketResearchRunContext.model_validate(ctx)
    orch = _orch()
    mission, _ = _mission_and_review(sctx)
    if _job_stopped(sctx.job_id):
        # Deterministic (insight_count=0 short-circuit) — no LLM spend on cancel.
        return orch.viability(mission, [], 0).model_dump(mode="json")
    signal_objs = [MarketSignal.model_validate(s) for s in signals]
    with _beating():
        recommendation = orch.viability(mission, signal_objs, insight_count)
    return recommendation.model_dump(mode="json")


@activity.defn(name="market_research_scripts")
def scripts_activity(ctx: dict[str, Any]) -> list[str]:
    """Generate the research scripts/templates for the mission. Single-shot.

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
