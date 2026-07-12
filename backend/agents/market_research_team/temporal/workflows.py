"""Temporal workflow for the market_research team — fine-grained per-stage orchestration.

``MarketResearchWorkflow`` runs the pipeline as a graph of Temporal activities
(defined in :mod:`activities`): a single-shot ``market_research_prepare`` opens
the run, ``market_research_ingest`` loads transcripts, the UX stage **fans out
one activity per transcript** via ``asyncio.gather``, ``scripts`` runs as an
independent parallel branch, ``psychology`` and (split mode) ``consistency`` run
concurrently, ``viability`` fans in, and ``market_research_finalize`` assembles
the ``TeamOutput`` and writes COMPLETED. Every specialist agent invocation is a
durable, individually-retryable Temporal activity, visible in the Temporal UI —
the whole point of the fine-grained decomposition.

Failure contract (mirrored from sales_team): activities never write FAILED
(doing so mid-retry would trip their own terminal guards and defeat the retry
policy). Instead the workflow body is wrapped in a catch-all that, after a fatal
error has exhausted its retries, records FAILED via the dedicated
``market_research_mark_failed`` activity and re-raises — so the Temporal
workflow outcome always mirrors the job store: FAILED job ⇔ failed workflow,
COMPLETED job ⇔ succeeded workflow, and a cancelled/interrupted job ends the
workflow cleanly without a COMPLETED write.

The workflow body is **deterministic**: no clock, no randomness, no I/O — only
pure dict/string computation (the split-topology flag, per-stage progress
gating, exception filtering of the UX fan-out) plus scheduling of activities.
Long LLM activities use per-attempt ``start_to_close`` timeouts with heartbeats;
the cheap job-store activities also use ``start_to_close`` (not
``schedule_to_close``, which counts queue time — a queued progress write on a
worker whose slots are saturated by long LLM activities must wait for a slot,
not die of starvation).

This package ``__init__`` and this module stay free of import-time side effects
(no worker boot, no ``os.getenv``) — the temporalio sandbox replays them during
workflow registration (guarded by ``test_temporal_bootstrap``).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from market_research_team.temporal import activities as _act

# Per-stage LLM calls are non-idempotent, but a single call is cheap to re-run
# and the ``llm_service`` layer also retries transient provider errors; a small
# bounded retry gives crash/transient durability — the point of the fine-grained
# decomposition (vs. the old whole-pipeline single attempt).
LLM_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)
# Cheap, idempotent job-store activities: safe to retry aggressively so a
# transient job-service blip never fails the whole workflow.
IO_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)

# Per-attempt execution timeouts (queue wait excluded — see module docstring).
_IO_TIMEOUT = timedelta(minutes=2)
_INGEST_TIMEOUT = timedelta(minutes=5)
_FINALIZE_TIMEOUT = timedelta(minutes=5)
_STAGE_TIMEOUT = timedelta(minutes=30)
_HEARTBEAT_TIMEOUT = timedelta(seconds=_act.HEARTBEAT_TIMEOUT_S)

# ``TeamTopology.SPLIT`` value. Compared against the JSON-round-tripped request
# dict, so a plain string literal keeps the workflow body free of model imports.
_SPLIT_TOPOLOGY = "split"

# Temporal patch marker for the single-activity → per-stage-DAG decomposition.
# New runs are patched (take the DAG); pre-decomposition histories replay the
# unpatched drain-out branch, which re-schedules the legacy whole-pipeline
# activity with byte-identical options so those in-flight runs stay
# deterministic and survive the deploy.
_PATCH_FINE_GRAINED = "market-research-fine-grained-activities"
# Byte-identical to the pre-decomposition workflow's single execute_activity call.
_LEGACY_ACTIVITY_TIMEOUT = timedelta(hours=2)
_LEGACY_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn(name="MarketResearchWorkflow")
class MarketResearchWorkflow:
    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Durable entrypoint: run the market-research pipeline for ``job_id``.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store.
            - ``request`` is the serialized ``RunMarketResearchRequest``
              (``payload.model_dump()``).

        Postconditions:
            - Pre-decomposition (unpatched) histories replay the legacy
              single-activity path so in-flight runs survive the deploy; new
              (patched) runs take the per-stage DAG.
            - On success returns ``{"job_id": job_id}`` with the job-store row
              COMPLETED (or left at its clean terminal state after a cancel).
            - On a fatal pipeline error the job is marked FAILED (best-effort,
              via ``market_research_mark_failed``) and the error re-raises so the
              Temporal workflow fails — a real failure is never reported as a
              succeeded workflow.
        """
        if not workflow.patched(_PATCH_FINE_GRAINED):
            # Drain-out branch: a history that started before the decomposition
            # replays here and must deterministically re-schedule the original
            # whole-pipeline activity with byte-identical options. The legacy
            # activity stays registered until every such run drains; then this
            # branch and ``run_pipeline_activity`` can be deleted.
            return await workflow.execute_activity(
                _act.run_pipeline_activity,
                args=[job_id, request],
                start_to_close_timeout=_LEGACY_ACTIVITY_TIMEOUT,
                retry_policy=_LEGACY_ACTIVITY_RETRY,
            )

        try:
            return await self._pipeline(job_id, request)
        except Exception as exc:
            try:
                await workflow.execute_activity(
                    _act.mark_failed_activity,
                    args=[job_id, str(exc)],
                    start_to_close_timeout=_IO_TIMEOUT,
                    retry_policy=IO_RETRY,
                )
            except Exception:
                workflow.logger.warning(
                    "market_research job %s: failed to record FAILED after pipeline error", job_id
                )
            raise

    async def _pipeline(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Schedule the pipeline stages; deterministic control flow only.

        Preconditions: same as :meth:`run` (its sole caller).
        Postconditions: returns ``{"job_id": job_id}``; raises on the first fatal
            activity error (handled by :meth:`run`'s catch-all).
        """
        # prepare only validates + returns the transcripts-stripped ctx; it does
        # not need the transcript bodies (ingest loads them from the full
        # request). Passing a stripped request keeps inline transcripts out of
        # prepare's activity input in workflow history — they are already
        # recorded once as the workflow start input, and flow to ingest.
        prepare_request = {**request, "transcripts": [], "transcript_folder_path": None}
        ctx = await workflow.execute_activity(
            _act.prepare_activity,
            args=[job_id, prepare_request],
            start_to_close_timeout=_IO_TIMEOUT,
            retry_policy=IO_RETRY,
        )
        if ctx["stopped"]:
            return {"job_id": job_id}

        split = request.get("topology") == _SPLIT_TOPOLOGY

        # Gate before the expensive fan-out: if the job already went terminal
        # (cancel / stale-job monitor), stop cleanly without spending on LLM
        # activities. Downstream activities also self-guard, so a cancel landing
        # mid-run still short-circuits (each becomes a cheap no-op) and finalize
        # never writes COMPLETED.
        if not await self._progress(job_id, "ingest", 10):
            return {"job_id": job_id}

        # ``scripts`` needs only the mission, so it runs as an independent branch
        # concurrently with ingest + UX (mirrors the graph's parallel entry).
        scripts_handle = workflow.start_activity(
            _act.scripts_activity,
            args=[ctx],
            start_to_close_timeout=_STAGE_TIMEOUT,
            heartbeat_timeout=_HEARTBEAT_TIMEOUT,
            retry_policy=LLM_RETRY,
        )

        # Ingest persists transcripts to the shared per-job store and returns
        # only lightweight refs ({"index", "source"}); the transcript bodies
        # never enter workflow history (ux_one loads each from the store).
        refs = await workflow.execute_activity(
            _act.ingest_activity,
            args=[job_id, request],
            start_to_close_timeout=_INGEST_TIMEOUT,
            retry_policy=IO_RETRY,
        )

        # UX: one activity per transcript, fanned out concurrently. A transcript
        # whose activity fails after its retries is dropped (return_exceptions),
        # not fatal to the run — the Temporal upgrade over the thread path's
        # whole-run failure. An outer cancellation still propagates out of gather.
        ux_results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    _act.ux_one_activity,
                    args=[ctx, ref],
                    start_to_close_timeout=_STAGE_TIMEOUT,
                    heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                    retry_policy=LLM_RETRY,
                )
                for ref in refs
            ],
            return_exceptions=True,
        )
        insights = [i for i in ux_results if not isinstance(i, BaseException)]

        active = await self._progress(job_id, "analysis", 45)
        # Transcripts were loaded but EVERY UX analysis was dropped: if the job is
        # still active (not a cancel — a cancel makes ux_one raise, which gather
        # captures too), this is a total analysis failure. Surface it as a failed
        # run rather than silently emitting an "insufficient evidence / collect
        # more interviews" result from zero insights (which would misrepresent a
        # run that had data but couldn't analyze it).
        if active and refs and not insights:
            raise RuntimeError(f"All {len(refs)} transcript analyses failed")

        # Psychology and (split mode) consistency run concurrently after UX.
        psych_coro = workflow.execute_activity(
            _act.psychology_activity,
            args=[ctx, insights],
            start_to_close_timeout=_STAGE_TIMEOUT,
            heartbeat_timeout=_HEARTBEAT_TIMEOUT,
            retry_policy=LLM_RETRY,
        )
        if split:
            cons_coro = workflow.execute_activity(
                _act.consistency_activity,
                args=[ctx, insights],
                start_to_close_timeout=_STAGE_TIMEOUT,
                heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                retry_policy=LLM_RETRY,
            )
            psych_signals, cons_signals = await asyncio.gather(psych_coro, cons_coro)
            signals = psych_signals + cons_signals
        else:
            signals = await psych_coro

        await self._progress(job_id, "viability", 75)

        recommendation = await workflow.execute_activity(
            _act.viability_activity,
            args=[ctx, signals, len(insights)],
            start_to_close_timeout=_STAGE_TIMEOUT,
            heartbeat_timeout=_HEARTBEAT_TIMEOUT,
            retry_policy=LLM_RETRY,
        )

        scripts = await scripts_handle

        await workflow.execute_activity(
            _act.finalize_activity,
            args=[ctx, insights, signals, recommendation, scripts],
            start_to_close_timeout=_FINALIZE_TIMEOUT,
            retry_policy=IO_RETRY,
        )
        return {"job_id": job_id}

    async def _progress(self, job_id: str, stage: str, pct: int) -> bool:
        """Write stage progress via the report-progress activity.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store;
              ``stage`` labels the current stage and ``pct`` is a 0-100 hint.
        Postconditions:
            - Returns ``True`` when the job is still active (progress written),
              ``False`` when it has gone terminal (nothing written) so the caller
              can stop scheduling further work.
        """
        return await workflow.execute_activity(
            _act.report_progress_activity,
            args=[job_id, stage, pct],
            start_to_close_timeout=_IO_TIMEOUT,
            retry_policy=IO_RETRY,
        )
