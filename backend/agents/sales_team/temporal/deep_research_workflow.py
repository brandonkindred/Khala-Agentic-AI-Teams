"""Temporal workflow for the deep-research prospecting pipeline.

``DeepResearchWorkflow`` runs company → decision-maker → dossier as a graph of
activities: a single-shot ``deep_research_prepare`` opens the run, the company
shortlist and ranking are single activities, and the decision-maker mapping and
dossier building **fan out one activity per item** via ``asyncio.gather`` in
place of the thread pools of the synchronous ``deep_research_only`` path. A
single-shot ``deep_research_finalize`` persists the result and writes COMPLETED.

Failure/terminal contract mirrors ``SalesWorkflow``: activities never write
FAILED; the workflow's catch-all records FAILED (via ``sales_mark_failed``)
after a fatal error exhausts its retries and re-raises, so the workflow outcome
always mirrors the job store. The body is deterministic — only dict/list
computation plus activity scheduling; all LLM/DB/job-store work is in activities.
Reuses the main pipeline's ``sales_report_progress`` / ``sales_mark_failed``
activities and its retry policies.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from sales_team.temporal import activities as _act
    from sales_team.temporal import deep_research_activities as _dr
    from sales_team.temporal.workflows import IO_RETRY, LLM_RETRY

_IO_TIMEOUT = timedelta(minutes=2)
_FINALIZE_TIMEOUT = timedelta(minutes=5)
_COMPANIES_TIMEOUT = timedelta(minutes=30)
_MAP_TIMEOUT = timedelta(minutes=15)
_DOSSIER_TIMEOUT = timedelta(minutes=20)
_HEARTBEAT_TIMEOUT = timedelta(seconds=_act.HEARTBEAT_TIMEOUT_S)

# Progress band (entry, exit) per deep-research stage — the run's coarse bar.
_COMPANIES_PCT = 10
_MAP_PCT = (20, 45)
_RANK_PCT = 50
_DOSSIER_PCT = (55, 90)


@workflow.defn(name="DeepResearchWorkflow")
class DeepResearchWorkflow:
    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Durable entrypoint: run the deep-research pipeline for ``job_id``.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store.
            - ``request`` is the serialized ``DeepResearchRequest``.

        Postconditions:
            - On success returns ``{"job_id": job_id}`` with the job COMPLETED
              (or left at its clean terminal state after a cancel).
            - On a fatal error the job is marked FAILED (best-effort) and the
              error re-raises so the Temporal workflow fails.
        """
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
                    "deep-research job %s: failed to record FAILED after error", job_id
                )
            raise

    async def _pipeline(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Schedule the deep-research stages; deterministic control flow only.

        Preconditions: same as :meth:`run` (its sole caller).
        Postconditions: returns ``{"job_id": job_id}``; raises on the first
            fatal activity error (handled by :meth:`run`'s catch-all).
        """
        ctx = await workflow.execute_activity(
            _dr.prepare_deep_research_activity,
            args=[job_id, request],
            start_to_close_timeout=_IO_TIMEOUT,
            retry_policy=IO_RETRY,
        )
        if ctx["stopped"]:
            return {"job_id": job_id}

        async def _gate(stage: str, pct: int) -> bool:
            return await workflow.execute_activity(
                _act.report_progress_activity,
                args=[job_id, stage, pct],
                start_to_close_timeout=_IO_TIMEOUT,
                retry_policy=IO_RETRY,
            )

        async def _finalize(
            final_prospects: list, dossiers: list, extra_notes: list
        ) -> dict[str, Any]:
            await workflow.execute_activity(
                _dr.finalize_deep_research_activity,
                args=[ctx, final_prospects, dossiers, extra_notes],
                start_to_close_timeout=_FINALIZE_TIMEOUT,
                retry_policy=IO_RETRY,
            )
            return {"job_id": job_id}

        # Stage 1 — company shortlist
        if not await _gate("companies", _COMPANIES_PCT):
            return await _finalize([], [], [])
        companies = await workflow.execute_activity(
            _dr.companies_activity,
            args=[ctx],
            start_to_close_timeout=_COMPANIES_TIMEOUT,
            heartbeat_timeout=_HEARTBEAT_TIMEOUT,
            retry_policy=LLM_RETRY,
        )
        if not companies:
            return await _finalize([], [], ["No companies returned by the prospector agent."])

        # Stage 2 — decision-maker mapping (fan out one activity per company)
        if not await _gate("decision_makers", _MAP_PCT[0]):
            return await _finalize([], [], [])
        map_results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    _dr.map_company_one_activity,
                    args=[ctx, company],
                    start_to_close_timeout=_MAP_TIMEOUT,
                    heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                    retry_policy=LLM_RETRY,
                )
                for company in companies
            ],
            return_exceptions=True,
        )
        mapped = [pair for res in map_results if not isinstance(res, BaseException) for pair in res]
        await _gate("decision_makers", _MAP_PCT[1])
        if not mapped:
            return await _finalize(
                [], [], ["No decision-makers identified across the company shortlist."]
            )

        # Stage 3 — cap + rank + trim + assign ids
        if not await _gate("ranking", _RANK_PCT):
            return await _finalize([], [], [])
        final_prospects = await workflow.execute_activity(
            _dr.rank_activity,
            args=[ctx, mapped],
            start_to_close_timeout=_IO_TIMEOUT,
            retry_policy=IO_RETRY,
        )
        if not final_prospects:
            return await _finalize([], [], [])

        # Stage 4 — dossier building (fan out one activity per prospect)
        if not await _gate("dossiers", _DOSSIER_PCT[0]):
            return await _finalize([], [], [])
        dossier_results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    _dr.build_dossier_one_activity,
                    args=[ctx, prospect],
                    start_to_close_timeout=_DOSSIER_TIMEOUT,
                    heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                    retry_policy=LLM_RETRY,
                )
                for prospect in final_prospects
            ],
            return_exceptions=True,
        )
        dossiers = [d for d in dossier_results if not isinstance(d, BaseException)]
        await _gate("dossiers", _DOSSIER_PCT[1])

        # Stage 5 — assemble + persist + COMPLETED
        return await _finalize(final_prospects, dossiers, [])
