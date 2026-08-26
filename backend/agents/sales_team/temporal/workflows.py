"""Temporal workflow for the sales_team — fine-grained per-agent orchestration.

``SalesWorkflow`` runs the full sales pipeline as a graph of Temporal
activities: a single-shot ``sales_prepare`` opens the run, then **each pipeline
stage fans out one activity per prospect** (outreach, qualification, nurture,
discovery, proposal, negotiation) via ``asyncio.gather``, replacing the thread
pools of the in-process orchestrator. Every specialist agent invocation is a
durable, individually-retryable Temporal activity, visible in the Temporal UI.
A single-shot ``sales_finalize`` records outcomes and writes COMPLETED.

Failure contract: activities never write FAILED (doing so mid-retry would trip
their own terminal guards and defeat the retry policy). Instead the workflow
body is wrapped in a catch-all that, after a fatal error has exhausted its
retries, records FAILED via the dedicated ``sales_mark_failed`` activity and
re-raises — so the Temporal workflow outcome always mirrors the job store:
FAILED job ⇔ failed workflow, COMPLETED job ⇔ succeeded workflow, and a
cancelled/interrupted job ends the workflow cleanly without a COMPLETED write.

The workflow body is **deterministic**: no clock, no randomness, no I/O — only
pure dict/string computation (stage gating, advance/nurture routing, per-id
lookups — all shared with the thread path via ``sales_team.routing``) plus
scheduling of activities. Timeouts: long LLM activities use per-attempt
``start_to_close`` timeouts with heartbeats; the cheap job-store activities
also use ``start_to_close`` (NOT ``schedule_to_close``, which counts queue
time — on a worker whose slots are saturated by long LLM activities, a queued
progress write must wait for a slot, not die of starvation).

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
from temporalio.exceptions import is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from sales_team.models import PipelineStage
    from sales_team.routing import (
        STAGE_PROGRESS,
        index_dicts_by_prospect_id,
        partition_qualified_dicts,
        stage_should_run,
    )
    from sales_team.temporal import activities as _act

# Per-prospect + coach + prospecting are non-idempotent LLM calls, but a single
# call is cheap to re-run and the ``llm_service`` layer also retries transient
# provider errors; a small bounded retry gives crash/transient durability — the
# whole point of the fine-grained decomposition.
LLM_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)
# Cheap, idempotent job-store / DB activities: safe to retry aggressively so a
# transient job-service blip never fails the whole workflow.
IO_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)

# Per-attempt execution timeouts (queue wait excluded — see module docstring).
_IO_TIMEOUT = timedelta(minutes=2)
_FINALIZE_TIMEOUT = timedelta(minutes=5)
_PROSPECTING_TIMEOUT = timedelta(minutes=45)
# Per-prospect stages that run a critic (outreach / proposal) now issue two
# sequential LLM calls per critique — a think=True reasoning pass then a
# think=False formatting pass, via complete_validated_via_reasoning — and the
# outreach critic runs inside a refinement loop, so a stage can make several
# such pairs. Doubled from the original single-call 30-minute budget so two
# individually healthy calls can't trip the ceiling; on timeout the
# ``gather(return_exceptions=True)`` fan-out silently drops that prospect, so
# a too-tight budget costs work rather than surfacing an error.
_PER_PROSPECT_TIMEOUT = timedelta(minutes=60)
_HEARTBEAT_TIMEOUT = timedelta(seconds=_act.HEARTBEAT_TIMEOUT_S)


@workflow.defn(name="SalesWorkflow")
class SalesWorkflow:
    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Durable entrypoint: run the sales pipeline for ``job_id``.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store.
            - ``request`` is the serialized ``SalesPipelineRequest``
              (``payload.model_dump(mode="json")``).

        Postconditions:
            - On success returns ``{"job_id": job_id}`` with the job-store row
              COMPLETED (or left at its clean terminal state after a cancel).
            - On a fatal pipeline error the job is marked FAILED (best-effort,
              via ``sales_mark_failed``) and the error re-raises so the
              Temporal workflow fails — a real failure is never reported as a
              succeeded workflow.
            - On workflow/activity cancellation the error re-raises without
              scheduling ``sales_mark_failed`` (cancelled jobs must not be
              recorded as FAILED).
        """
        try:
            return await self._pipeline(job_id, request)
        except Exception as exc:
            # Temporal cancellation (bare CancelledError or ActivityError with
            # a CancelledError cause) must propagate without mutating job
            # state — matching the module docstring's cancel contract.
            if is_cancelled_exception(exc):
                raise
            try:
                await workflow.execute_activity(
                    _act.mark_failed_activity,
                    args=[job_id, str(exc)],
                    start_to_close_timeout=_IO_TIMEOUT,
                    retry_policy=IO_RETRY,
                )
            except Exception:
                workflow.logger.warning(
                    "sales job %s: failed to record FAILED after pipeline error", job_id
                )
            raise

    async def _pipeline(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Schedule the pipeline stages; deterministic control flow only.

        Preconditions: same as :meth:`run` (its sole caller).
        Postconditions: returns ``{"job_id": job_id}``; raises on the first
            fatal activity error (handled by :meth:`run`'s catch-all).
        """
        entry = request.get("entry_stage", PipelineStage.PROSPECTING.value)

        ctx = await workflow.execute_activity(
            _act.prepare_sales_pipeline_activity,
            args=[job_id, request],
            start_to_close_timeout=_IO_TIMEOUT,
            retry_policy=IO_RETRY,
        )
        if ctx["stopped"]:
            return {"job_id": job_id}

        result: dict[str, Any] = {
            "job_id": job_id,
            "entry_stage": entry,
            "product_name": request["product_name"],
            "prospects": [],
            "outreach_sequences": [],
            "qualified_leads": [],
            "nurture_sequences": [],
            "discovery_plans": [],
            "proposals": [],
            "closing_strategies": [],
            "coaching_report": None,
            "summary": "",
        }

        async def _gate(stage: str, pct: int) -> bool:
            """Write stage progress; return False if the job has gone terminal."""
            return await workflow.execute_activity(
                _act.report_progress_activity,
                args=[job_id, stage, pct],
                start_to_close_timeout=_IO_TIMEOUT,
                retry_policy=IO_RETRY,
            )

        async def _entry_gate(stage: str) -> bool:
            return await _gate(stage, STAGE_PROGRESS[stage][0])

        async def _exit_gate(stage: str) -> None:
            # Exit-pct write keeps the progress bar moving and last_updated_at
            # fresh between stages; its active/terminal verdict is irrelevant
            # here (the next stage's entry gate re-checks).
            await _gate(stage, STAGE_PROGRESS[stage][1])

        async def _load_dossiers(items: list[dict[str, Any]]) -> dict[str, Any]:
            return await workflow.execute_activity(
                _act.load_dossiers_activity,
                args=[items],
                start_to_close_timeout=_IO_TIMEOUT,
                retry_policy=IO_RETRY,
            )

        async def _finalize() -> dict[str, Any]:
            await workflow.execute_activity(
                _act.finalize_sales_pipeline_activity,
                args=[ctx, result],
                start_to_close_timeout=_FINALIZE_TIMEOUT,
                retry_policy=IO_RETRY,
            )
            return {"job_id": job_id}

        async def _fan(act, items, arg_fn):
            """Run ``act`` once per item concurrently, dropping items that fail.

            ``gather(return_exceptions=True)`` mirrors the thread path's
            per-prospect skip: a prospect whose activity fails after its
            retries (or is skipped because the job went terminal) is dropped,
            not fatal to the run. An outer workflow cancellation still
            propagates — asyncio re-raises CancelledError out of a gather
            whose own future is cancelled, regardless of return_exceptions.
            """
            tasks = [
                workflow.execute_activity(
                    act,
                    args=arg_fn(item),
                    start_to_close_timeout=_PER_PROSPECT_TIMEOUT,
                    heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                    retry_policy=LLM_RETRY,
                )
                for item in items
            ]
            out = await asyncio.gather(*tasks, return_exceptions=True)
            return [r for r in out if not isinstance(r, BaseException)]

        # Stage 1 — Prospecting (single activity). Supplied leads flow as an
        # explicit argument (the ctx carrier deliberately drops them); when the
        # stage is gated off (entry past prospecting) they are used directly.
        prospects = request.get("existing_prospects", [])
        if stage_should_run(PipelineStage.PROSPECTING, entry) and await _entry_gate("prospecting"):
            prospects = await workflow.execute_activity(
                _act.prospect_activity,
                args=[ctx, request.get("existing_prospects", [])],
                start_to_close_timeout=_PROSPECTING_TIMEOUT,
                heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                retry_policy=LLM_RETRY,
            )
            await _exit_gate("prospecting")
        result["prospects"] = prospects

        if not prospects:
            return await _finalize()

        dossier_map = await _load_dossiers(prospects)

        # Stage 2 — Outreach (only prospects that have a dossier; the rest have
        # no personalization basis, exactly as the thread path skips them).
        if stage_should_run(PipelineStage.OUTREACH, entry) and await _entry_gate("outreach"):
            targets = [p for p in prospects if dossier_map.get(p["id"]) is not None]
            result["outreach_sequences"] = await _fan(
                _act.outreach_one_activity,
                targets,
                lambda p: [ctx, p, dossier_map[p["id"]]],
            )
            await _exit_gate("outreach")

        # Stage 3 — Qualification + advance/nurture routing
        qualified: list[dict[str, Any]] = []
        if stage_should_run(PipelineStage.QUALIFICATION, entry) and await _entry_gate(
            "qualification"
        ):
            qualified = await _fan(_act.qualify_one_activity, prospects, lambda p: [ctx, p])
            result["qualified_leads"] = qualified
            await _exit_gate("qualification")

        nurture_prospects, qualified_prospects = partition_qualified_dicts(qualified, prospects)

        # Stage 4 — Nurturing
        if (
            stage_should_run(PipelineStage.NURTURING, entry)
            and nurture_prospects
            and await _entry_gate("nurturing")
        ):
            result["nurture_sequences"] = await _fan(
                _act.nurture_one_activity, nurture_prospects, lambda p: [ctx, p]
            )
            await _exit_gate("nurturing")

        qual_by_id = index_dicts_by_prospect_id(qualified)

        # Stage 5 — Discovery
        if (
            stage_should_run(PipelineStage.DISCOVERY, entry)
            and qualified_prospects
            and await _entry_gate("discovery")
        ):
            if not dossier_map:
                # Thread-path parity: re-attempt the dossier load at the
                # discovery boundary so a transient store outage at the first
                # load doesn't strip grounding from every discovery plan in
                # the run.
                dossier_map = await _load_dossiers(qualified_prospects)
            result["discovery_plans"] = await _fan(
                _act.discovery_one_activity,
                qualified_prospects,
                lambda p: [ctx, p, qual_by_id.get(p["id"]), dossier_map.get(p["id"])],
            )
            await _exit_gate("discovery")

        # Stage 6 — Proposal
        if (
            stage_should_run(PipelineStage.PROPOSAL, entry)
            and qualified_prospects
            and await _entry_gate("proposal")
        ):
            if not dossier_map:
                # Thread-path parity: re-attempt the dossier load at the
                # proposal boundary so a transient store outage at the first
                # load doesn't strip grounding from every proposal in the run.
                dossier_map = await _load_dossiers(qualified_prospects)
            result["proposals"] = await _fan(
                _act.proposal_one_activity,
                qualified_prospects,
                lambda p: [ctx, p, dossier_map.get(p["id"]), qual_by_id.get(p["id"])],
            )
            await _exit_gate("proposal")

        prop_by_id = index_dicts_by_prospect_id(result["proposals"])

        # Stage 7 — Negotiation / Closing
        if (
            stage_should_run(PipelineStage.NEGOTIATION, entry)
            and qualified_prospects
            and await _entry_gate("negotiation")
        ):
            result["closing_strategies"] = await _fan(
                _act.close_one_activity,
                qualified_prospects,
                lambda p: [ctx, p, prop_by_id.get(p["id"])],
            )
            await _exit_gate("negotiation")

        # Coaching — best-effort in BOTH layers: the activity itself returns
        # None on any error, and an infrastructure failure (e.g. heartbeat
        # timeout after retries) is also absorbed here, matching the thread
        # path where coaching can never fail the run.
        if await _entry_gate("coaching"):
            try:
                result["coaching_report"] = await workflow.execute_activity(
                    _act.coach_activity,
                    args=[ctx, prospects],
                    start_to_close_timeout=_PER_PROSPECT_TIMEOUT,
                    heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                    retry_policy=LLM_RETRY,
                )
            except Exception:
                workflow.logger.warning(
                    "sales job %s: coaching activity failed; continuing without report", job_id
                )

        return await _finalize()
