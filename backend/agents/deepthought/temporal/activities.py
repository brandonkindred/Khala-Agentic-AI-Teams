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

import functools
import logging
from typing import Any

from temporalio import activity

from deepthought.temporal.constants import (
    ANALYSE_ACTIVITY,
    CLASSIFY_STRATEGY_ACTIVITY,
    DELIBERATE_ACTIVITY,
    FINALIZE_JOB_ACTIVITY,
    FORCE_DIRECT_ANSWER_ACTIVITY,
    IS_CANCELLED_ACTIVITY,
    RUN_PIPELINE_ACTIVITY,
    START_JOB_ACTIVITY,
    SYNTHESISE_ACTIVITY,
)

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _cached_model() -> Any:  # pragma: no cover - real provider wiring; patched/bypassed in tests
    """Resolve the deepthought strands model once per worker process.

    ``get_strands_model`` re-resolves the Postgres provider list on every call, and
    a decomposed run issues one activity per LLM boundary (up to ~150 per run), so
    the resolution is cached. The returned model / ``FailoverLLMClient`` is the
    piece that is *designed* to be shared (it remembers which providers are
    rate-limited, exactly as thread mode shares one model across a run's parallel
    nodes). Provider-config changes are picked up on the next worker restart.
    """
    from llm_service import get_strands_model

    return get_strands_model("deepthought")


def _build_llm() -> (
    Any
):  # pragma: no cover - real provider wiring (mirrors DeepthoughtOrchestrator.__init__); patched out in tests
    """Build a FRESH strands ``Agent`` (wrapping the cached model) per activity.

    Only the expensive model resolution is cached (:func:`_cached_model`); each
    activity gets its own ``Agent`` so no per-``Agent`` completion state can leak
    across the concurrent jobs sharing a worker. Per-call ``system_prompt``
    overrides make the constructor prompt irrelevant, but we keep it identical to
    ``DeepthoughtOrchestrator.__init__`` for parity.
    """
    from strands import Agent

    from deepthought.prompts import CLASSIFY_QUESTION_SYSTEM_PROMPT

    return Agent(model=_cached_model(), system_prompt=CLASSIFY_QUESTION_SYSTEM_PROMPT)


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
    # Pass the shared (cached) client so classify does not build its own.
    return DeepthoughtOrchestrator(llm=_build_llm())._resolve_strategy(req).value


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
        knowledge_summary=p.knowledge_summary,
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
        knowledge_summary=p.knowledge_summary,
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


@activity.defn(name=IS_CANCELLED_ACTIVITY)
def is_cancelled_activity(job_id: str) -> bool:
    """Report whether the job has been cancelled (cheap job-store read).

    The workflow polls this between decomposition fan-outs so a job cancelled via
    ``/deepthought/jobs/{job_id}/cancel`` (which only marks the store) stops
    spawning further specialists instead of running the whole tree to completion.

    Postconditions:
        - Returns ``True`` iff the job is in a cancelled state; never raises for a
          missing job (treated as not-cancelled by the job store).
    """
    from deepthought.shared.job_store import is_job_cancelled

    return is_job_cancelled(job_id)


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
    is_cancelled_activity,
    finalize_job_activity,
    run_pipeline_activity,
]
