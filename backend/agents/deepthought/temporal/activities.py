"""Temporal activities for the deepthought decomposed pipeline.

Every LLM boundary of the recursive reasoning tree is its own ``@activity.defn``.
Each activity is a thin, durable wrapper around an *existing* method on
``DeepthoughtAgent`` / ``DeepthoughtOrchestrator`` — no reasoning logic is
re-implemented here. Heavy imports (``strands``, ``llm_service``, the
orchestrator, the job store) are deferred into the function bodies so importing
this module for worker registration stays cheap and sandbox-safe.

The workflow (:mod:`deepthought.temporal.workflows`) owns the recursion, the
knowledge base, the budget, and the events as deterministic state; these
activities only perform the non-deterministic work (LLM calls, job-store I/O).

``start_job_activity`` / ``finalize_job_activity`` own the same job-store status
transitions the thread path (``deepthought.api.main._run_deepthought_background``)
performs, so ``/status/{job_id}`` polling is identical across runtimes.
"""

from __future__ import annotations

import logging
from typing import Any

from temporalio import activity

from deepthought.temporal.constants import (
    ANALYSE_ACTIVITY,
    CLASSIFY_STRATEGY_ACTIVITY,
    DELIBERATE_ACTIVITY,
    FINALIZE_JOB_ACTIVITY,
    FORCE_DIRECT_ANSWER_ACTIVITY,
    RUN_PIPELINE_ACTIVITY,
    START_JOB_ACTIVITY,
    SYNTHESISE_ACTIVITY,
)

logger = logging.getLogger(__name__)


def _build_llm() -> (
    Any
):  # pragma: no cover - real provider wiring (mirrors DeepthoughtOrchestrator.__init__); patched out in tests
    """Construct the strands LLM client the reasoning methods call.

    Mirrors ``DeepthoughtOrchestrator.__init__`` so every activity talks to the
    same provider the thread path would. Per-call ``system_prompt`` overrides make
    the constructor prompt irrelevant, but we keep it identical for parity.
    """
    from strands import Agent

    from deepthought.prompts import CLASSIFY_QUESTION_SYSTEM_PROMPT
    from llm_service import get_strands_model

    return Agent(
        model=get_strands_model("deepthought"),
        system_prompt=CLASSIFY_QUESTION_SYSTEM_PROMPT,
    )


def _kb_from_entries(entries: list[Any]) -> Any:
    """Rebuild a :class:`SharedKnowledgeBase` seeded from the workflow's entries.

    The seeded KB reproduces exactly what a node would have read in thread mode:
    the reasoning methods call ``summary_for_prompt``/``find_similar`` on it.
    """
    from deepthought.knowledge_base import SharedKnowledgeBase

    kb = SharedKnowledgeBase()
    for entry in entries:
        kb.add(entry)
    return kb


# --------------------------------------------------------------------------- #
# LLM reasoning activities
# --------------------------------------------------------------------------- #


@activity.defn(name=CLASSIFY_STRATEGY_ACTIVITY)
def classify_strategy_activity(request: dict[str, Any]) -> str:
    """Resolve the decomposition strategy for a request (LLM-classified on AUTO).

    Wraps ``DeepthoughtOrchestrator._resolve_strategy``.

    Preconditions:
        - ``request`` is a ``DeepthoughtRequest.model_dump()`` payload.
    Postconditions:
        - Returns a valid ``DecompositionStrategy`` value string (falls back to
          ``"auto"`` on any classification error — the method never raises).
    """
    from deepthought.models import DeepthoughtRequest
    from deepthought.orchestrator import DeepthoughtOrchestrator

    req = DeepthoughtRequest(**request)
    return DeepthoughtOrchestrator()._resolve_strategy(req).value


@activity.defn(name=ANALYSE_ACTIVITY)
def analyse_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Analyse a node's focus question (wraps ``DeepthoughtAgent._analyse``).

    Preconditions:
        - ``payload`` validates as :class:`AnalysePayload`.
    Postconditions:
        - Returns a ``QueryAnalysis.model_dump()`` (``_analyse`` degrades to a
          direct-answer analysis on LLM error rather than raising).
    """
    from deepthought.agent import DeepthoughtAgent
    from deepthought.models import DecompositionStrategy
    from deepthought.temporal.phase_models import AnalysePayload

    p = AnalysePayload.model_validate(payload)
    agent = DeepthoughtAgent(
        spec=p.spec,
        llm=_build_llm(),
        parent_question=p.parent_question,
        original_query=p.original_query,
        conversation_history=p.conversation_history,
        decomposition_strategy=DecompositionStrategy(p.decomposition_strategy),
        knowledge_base=_kb_from_entries(p.kb_entries),
    )
    return agent._analyse(p.max_depth).model_dump()


@activity.defn(name=FORCE_DIRECT_ANSWER_ACTIVITY)
def force_direct_answer_activity(payload: dict[str, Any]) -> str:
    """Force a direct answer at the depth limit (wraps ``_force_direct_answer``).

    Preconditions:
        - ``payload`` validates as :class:`ForceDirectAnswerPayload`.
    Postconditions:
        - Returns the answer text (a fallback string on LLM error, never raises).
    """
    from deepthought.agent import DeepthoughtAgent
    from deepthought.temporal.phase_models import ForceDirectAnswerPayload

    p = ForceDirectAnswerPayload.model_validate(payload)
    agent = DeepthoughtAgent(
        spec=p.spec,
        llm=_build_llm(),
        parent_question=p.parent_question,
        original_query=p.original_query,
        knowledge_base=_kb_from_entries(p.kb_entries),
    )
    return agent._force_direct_answer()


@activity.defn(name=DELIBERATE_ACTIVITY)
def deliberate_activity(payload: dict[str, Any]) -> str:
    """Review child results for contradictions/gaps (wraps ``_deliberate``).

    Preconditions:
        - ``payload`` validates as :class:`DeliberatePayload` with >= 2 children
          (the workflow only calls this when there are enough children to
          deliberate over).
    Postconditions:
        - Returns the deliberation notes (``""`` on LLM error, never raises).
    """
    from deepthought.agent import DeepthoughtAgent
    from deepthought.temporal.phase_models import DeliberatePayload

    p = DeliberatePayload.model_validate(payload)
    agent = DeepthoughtAgent(spec=p.spec, llm=_build_llm(), original_query=p.original_query)
    children = [c.to_agent_result() for c in p.children]
    return agent._deliberate(children)


@activity.defn(name=SYNTHESISE_ACTIVITY)
def synthesise_activity(payload: dict[str, Any]) -> str:
    """Merge child results into one answer (wraps ``_synthesise``).

    Preconditions:
        - ``payload`` validates as :class:`SynthesisePayload`.
    Postconditions:
        - Returns the synthesised answer (a concatenation fallback on LLM error).
    """
    from deepthought.agent import DeepthoughtAgent
    from deepthought.temporal.phase_models import SynthesisePayload

    p = SynthesisePayload.model_validate(payload)
    agent = DeepthoughtAgent(spec=p.spec, llm=_build_llm(), original_query=p.original_query)
    children = [c.to_agent_result() for c in p.children]
    return agent._synthesise(children, p.deliberation_notes)


# --------------------------------------------------------------------------- #
# Job-store activities (own the RUNNING / COMPLETED / FAILED transitions)
# --------------------------------------------------------------------------- #


@activity.defn(name=START_JOB_ACTIVITY)
def start_job_activity(job_id: str) -> bool:
    """Flip the job to RUNNING unless it was cancelled before the run started.

    Preconditions:
        - ``job_id`` refers to a job row already created by the API handler.
    Postconditions:
        - Returns ``False`` (workflow short-circuits, no reasoning runs) if the
          job is already cancelled; otherwise sets RUNNING and returns ``True``.
    """
    from deepthought.shared.job_store import (
        JOB_STATUS_RUNNING,
        is_job_cancelled,
        update_job,
    )

    if is_job_cancelled(job_id):
        return False
    update_job(job_id, status=JOB_STATUS_RUNNING)
    return True


@activity.defn(name=FINALIZE_JOB_ACTIVITY)
def finalize_job_activity(
    job_id: str, response: dict[str, Any], success: bool, error: str = ""
) -> None:
    """Record the terminal job status (COMPLETED with result, or FAILED).

    Preconditions:
        - ``job_id`` refers to an existing job row.
        - When ``success`` is True, ``response`` is a ``DeepthoughtResponse``
          dump; when False, ``error`` describes the failure.
    Postconditions:
        - Writes COMPLETED (with ``result=response``) or FAILED (with ``error``),
          unless the job was cancelled meanwhile, in which case it is left as-is
          (parity with the thread path's cancellation checks).
    """
    from deepthought.shared.job_store import (
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
        is_job_cancelled,
        update_job,
    )

    if is_job_cancelled(job_id):
        return
    if success:
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=response)
    else:
        update_job(job_id, status=JOB_STATUS_FAILED, error=error)


# --------------------------------------------------------------------------- #
# Legacy single-activity pipeline (kept for workflow.patched replay only)
# --------------------------------------------------------------------------- #


@activity.defn(name=RUN_PIPELINE_ACTIVITY)
def run_pipeline_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run the whole orchestrator in one activity — the pre-decomposition path.

    Retained unchanged so ``DeepthoughtWorkflow`` histories started before the
    per-step decomposition (which recorded a single ``run_pipeline_activity``
    call) still replay deterministically via ``workflow.patched``. New runs take
    the decomposed path and never invoke this.

    Preconditions:
        - ``job_id`` refers to a job row already created by the caller.
        - ``request`` is a ``DeepthoughtRequest.model_dump()`` payload.
    Postconditions:
        - On success: job status COMPLETED with ``result`` set, result dict
          returned. On failure: job status FAILED, exception re-raised. If the
          job was cancelled before this ran, returns ``{}`` untouched.
    """
    from deepthought.models import DeepthoughtRequest
    from deepthought.orchestrator import DeepthoughtOrchestrator
    from deepthought.shared.job_store import (
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
        JOB_STATUS_RUNNING,
        is_job_cancelled,
        update_job,
    )

    if is_job_cancelled(job_id):
        return {}

    try:
        update_job(job_id, status=JOB_STATUS_RUNNING)
        req = DeepthoughtRequest(**request)
        result = DeepthoughtOrchestrator().process_message(req)
        dump = result.model_dump()
        if is_job_cancelled(job_id):
            return dump
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=dump)
        return dump
    except Exception as e:  # noqa: BLE001 — record then re-raise for Temporal
        logger.exception("Deepthought job %s failed", job_id)
        if not is_job_cancelled(job_id):
            update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
        raise


ALL_ACTIVITIES = [
    classify_strategy_activity,
    analyse_activity,
    force_direct_answer_activity,
    deliberate_activity,
    synthesise_activity,
    start_job_activity,
    finalize_job_activity,
    run_pipeline_activity,
]
