"""Temporal workflow for the sales_team — fine-grained per-agent orchestration.

``SalesWorkflow`` runs the full sales pipeline as a graph of Temporal
activities: a single-shot ``sales_prepare`` opens the run, then **each pipeline
stage fans out one activity per prospect** (outreach, qualification, nurture,
discovery, proposal, negotiation) via ``asyncio.gather``, replacing the thread
pools of the in-process orchestrator. Every specialist agent invocation is now
its own durable, individually-retryable Temporal activity, visible in the
Temporal UI. A single-shot ``sales_finalize`` records outcomes and writes the
terminal job status.

The workflow body is **deterministic**: no clock, no randomness, no I/O — only
pure dict/string computation (stage gating, advance/nurture routing, per-id
lookups) plus scheduling of activities. All LLM / Postgres / job-store work
happens inside activities. The activities module and the pure routing helpers
are imported under ``workflow.unsafe.imports_passed_through()`` so the sandbox
does not re-execute them.

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
    from sales_team.models import PipelineStage
    from sales_team.routing import is_advance, is_disqualify, stage_should_run
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

_IO_TIMEOUT = timedelta(minutes=5)
_PREPARE_TIMEOUT = timedelta(minutes=5)
_FINALIZE_TIMEOUT = timedelta(minutes=15)
_PROSPECTING_TIMEOUT = timedelta(minutes=45)
_PER_PROSPECT_TIMEOUT = timedelta(minutes=30)
_HEARTBEAT_TIMEOUT = timedelta(minutes=3)


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
            - Schedules ``sales_prepare`` → per-stage fan-out → ``sales_finalize``.
            - Returns ``{"job_id": job_id}``; the terminal job-store status is
              owned by the activities (RUNNING in prepare, COMPLETED/skip in
              finalize, FAILED on a fatal activity error).
        """
        entry = request.get("entry_stage", PipelineStage.PROSPECTING.value)

        ctx = await workflow.execute_activity(
            _act.prepare_sales_pipeline_activity,
            args=[job_id, request],
            schedule_to_close_timeout=_PREPARE_TIMEOUT,
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
                schedule_to_close_timeout=_IO_TIMEOUT,
                retry_policy=IO_RETRY,
            )

        async def _finalize() -> dict[str, Any]:
            await workflow.execute_activity(
                _act.finalize_sales_pipeline_activity,
                args=[ctx, result],
                schedule_to_close_timeout=_FINALIZE_TIMEOUT,
                retry_policy=IO_RETRY,
            )
            return {"job_id": job_id}

        async def _fan(act, items, arg_fn):
            """Run ``act`` once per item concurrently, dropping items that fail.

            ``gather(return_exceptions=True)`` mirrors the thread path's
            per-prospect skip: a prospect whose activity fails after its retries
            is dropped, not fatal to the run.
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

        # Stage 1 — Prospecting (single activity; resolves existing_prospects
        # internally). Defaults to any supplied existing prospects when the
        # prospecting stage is gated off (entry past prospecting) or skipped
        # because the job was cancelled.
        prospects = request.get("existing_prospects", [])
        if stage_should_run(PipelineStage.PROSPECTING, entry) and await _gate("prospecting", 5):
            prospects = await workflow.execute_activity(
                _act.prospect_activity,
                args=[ctx],
                start_to_close_timeout=_PROSPECTING_TIMEOUT,
                heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                retry_policy=LLM_RETRY,
            )
        result["prospects"] = prospects

        if not prospects:
            return await _finalize()

        dossier_map = await workflow.execute_activity(
            _act.load_dossiers_activity,
            args=[ctx, prospects],
            schedule_to_close_timeout=_IO_TIMEOUT,
            retry_policy=IO_RETRY,
        )

        # Stage 2 — Outreach (only prospects that have a dossier; the rest have
        # no personalization basis, exactly as the thread path skips them).
        if stage_should_run(PipelineStage.OUTREACH, entry) and await _gate("outreach", 20):
            targets = [p for p in prospects if dossier_map.get(p["id"]) is not None]
            result["outreach_sequences"] = await _fan(
                _act.outreach_one_activity,
                targets,
                lambda p: [ctx, p, dossier_map[p["id"]]],
            )

        # Stage 3 — Qualification + advance/nurture routing
        qualified: list[dict[str, Any]] = []
        if stage_should_run(PipelineStage.QUALIFICATION, entry) and await _gate(
            "qualification", 40
        ):
            qualified = await _fan(_act.qualify_one_activity, prospects, lambda p: [ctx, p])
            result["qualified_leads"] = qualified

        if qualified:
            advance = [q for q in qualified if is_advance(q["recommended_action"])]
            nurture_prospects = [
                q["prospect"]
                for q in qualified
                if not is_advance(q["recommended_action"])
                and not is_disqualify(q["recommended_action"])
            ]
            qualified_prospects = [q["prospect"] for q in advance]
        else:
            nurture_prospects, qualified_prospects = [], prospects

        # Stage 4 — Nurturing
        if (
            stage_should_run(PipelineStage.NURTURING, entry)
            and nurture_prospects
            and await _gate("nurturing", 55)
        ):
            result["nurture_sequences"] = await _fan(
                _act.nurture_one_activity, nurture_prospects, lambda p: [ctx, p]
            )

        # Id-keyed lookups deliberately mirror the orchestrator's ``.get(p.id)``
        # (empty-id collision preserved) so behaviour matches the thread path.
        qual_by_id = {q["prospect"]["id"]: q for q in qualified if q["prospect"]["id"]}

        # Stage 5 — Discovery
        if (
            stage_should_run(PipelineStage.DISCOVERY, entry)
            and qualified_prospects
            and await _gate("discovery", 65)
        ):
            result["discovery_plans"] = await _fan(
                _act.discovery_one_activity,
                qualified_prospects,
                lambda p: [ctx, p, qual_by_id.get(p["id"])],
            )

        # Stage 6 — Proposal
        if (
            stage_should_run(PipelineStage.PROPOSAL, entry)
            and qualified_prospects
            and await _gate("proposal", 78)
        ):
            result["proposals"] = await _fan(
                _act.proposal_one_activity,
                qualified_prospects,
                lambda p: [ctx, p, dossier_map.get(p["id"]), qual_by_id.get(p["id"])],
            )

        prop_by_id = {
            pr["prospect"]["id"]: pr for pr in result["proposals"] if pr["prospect"]["id"]
        }

        # Stage 7 — Negotiation / Closing
        if (
            stage_should_run(PipelineStage.NEGOTIATION, entry)
            and qualified_prospects
            and await _gate("negotiation", 90)
        ):
            result["closing_strategies"] = await _fan(
                _act.close_one_activity,
                qualified_prospects,
                lambda p: [ctx, p, prop_by_id.get(p["id"])],
            )

        # Coaching (best-effort) then finalize (records outcomes + COMPLETED).
        if await _gate("coaching", 97):
            result["coaching_report"] = await workflow.execute_activity(
                _act.coach_activity,
                args=[ctx, prospects],
                start_to_close_timeout=_PER_PROSPECT_TIMEOUT,
                heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                retry_policy=LLM_RETRY,
            )

        return await _finalize()
