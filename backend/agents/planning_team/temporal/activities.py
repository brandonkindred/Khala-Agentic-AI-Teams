"""Temporal activities for the Planning team — one per pipeline phase.

Each phase of the in-process orchestrator (``planning_team.orchestrator.run_workflow``)
is wrapped here as its own ``@activity.defn`` so the durable workflow
(:mod:`.workflows`) can drive the plan phase by phase, with independent
timeouts, retries and heartbeats instead of one opaque black-box activity:

- :func:`intake_activity` — build the initial ``ClientContext`` (wraps ``run_intake``).
- :func:`discovery_activity` — LLM discovery extraction (wraps ``run_discovery``).
- :func:`requirements_activity` — LLM open-question derivation (wraps ``run_requirements``).
- :func:`market_research_activity` — optional cross-team Market Research call.
- :func:`synthesis_activity` — fold evidence into context (wraps ``run_synthesis``).
- :func:`document_production_activity` — write artifacts + PRA (wraps ``run_document_production``).
- :func:`sub_agent_provisioning_activity` — optional AI-Systems build (wraps ``run_sub_agent_provisioning``).
- :func:`finalize_planning_activity` — mark the job completed with its handoff package.

Each activity is a plain **sync** function (run in the worker's thread-pool
executor) whose heavy imports live inside the body, keeping this module — which
the workflow sandbox reuses via ``imports_passed_through`` — cheap and side-effect
free at import. The mutable planning ``context`` crosses the activity boundary as
a **JSON-native dict** (Temporal's default converter is used repo-wide, so no
pydantic value may cross): each activity re-uses the phase functions (which
re-hydrate models from dicts defensively) and normalizes any returned
``ClientContext``/``HandoffPackage`` back to a dict via :func:`_json_safe` on the
way out. Job-store bookkeeping (progress, RUNNING → COMPLETED, FAILED-on-error)
is written to the durable ``JobServiceClient`` store so a completed run survives a
worker/process restart and the API can keep polling status.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from temporalio import activity

logger = logging.getLogger(__name__)

#: Background-heartbeat cadence (seconds) for the long external-poll phases
#: (PRA / AI-Systems). Kept well under those activities' ``heartbeat_timeout`` so
#: a live poll is never mistaken for a stalled worker.
_POLL_HEARTBEAT_INTERVAL_S = 30.0


def _json_safe(value: Any) -> Any:
    """Return ``value`` with any pydantic model rendered to a JSON-native dict.

    Preconditions:
        - ``value`` is a phase ``context`` value: a scalar, list, dict, or a
          pydantic model (e.g. ``ClientContext``/``HandoffPackage``/``OpenQuestion``).
    Postconditions:
        - Models become ``model_dump(mode="json")`` dicts and lists are converted
          element-wise; everything else is returned unchanged. The result is
          JSON-serializable so it can cross the Temporal activity boundary under
          the default data converter.
    """
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _merge_context(context: Dict[str, Any], context_update: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a phase's ``context_update`` into ``context``, JSON-normalizing values.

    Preconditions:
        - ``context`` is the JSON-native context dict received by the activity.
        - ``context_update`` is the first element of a phase function's
          ``(context_update, artifacts)`` return.
    Postconditions:
        - Returns a new dict = ``context`` overlaid with ``context_update``, with
          every overlaid value passed through :func:`_json_safe` so the returned
          context stays JSON-serializable (no pydantic objects survive).
    """
    merged = dict(context)
    for key, value in context_update.items():
        merged[key] = _json_safe(value)
    return merged


def _fail(job_id: str, exc: BaseException) -> None:
    """Mark the planning job FAILED after an activity error (before re-raising).

    Preconditions:
        - ``job_id`` refers to an existing job record; ``exc`` is the caught error.
    Postconditions:
        - The job-store row for ``job_id`` is marked FAILED with ``str(exc)``. The
          caller re-raises so the failure also surfaces as a failed Temporal
          activity/workflow rather than a silently-"completed" one.
    """
    from planning_team.shared.job_store import mark_job_failed

    activity.logger.exception("Planning activity failed for job %s", job_id)
    mark_job_failed(job_id, error=str(exc))


def _guarded(
    job_id: str,
    phase: str,
    progress: int,
    status_text: str,
    work: Callable[[], Any],
    *,
    status: Optional[str] = None,
) -> Any:
    """Report phase progress, run ``work``, and mark the job FAILED on error.

    Preconditions:
        - ``job_id`` refers to an existing job; ``progress`` is 0..100; ``work`` is
          a zero-arg callable performing the phase's work and returning its result.
    Postconditions:
        - The job row's ``current_phase``/``progress``/``status_text`` (and
          ``status`` when supplied — the first activity flips it to RUNNING) are
          updated, then ``work()`` runs and its result is returned.
        - If ``work`` raises, the job is marked FAILED and the exception is
          re-raised unchanged (so the workflow's RetryPolicy governs re-attempts).
    """
    from planning_team.shared.job_store import update_job

    fields: Dict[str, Any] = {
        "current_phase": phase,
        "progress": progress,
        "status_text": status_text,
    }
    if status is not None:
        fields["status"] = status
    update_job(job_id, **fields)
    try:
        return work()
    except Exception as exc:
        _fail(job_id, exc)
        raise


@activity.defn(name="planning_intake")
def intake_activity(
    job_id: str,
    repo_path: str,
    client_name: Optional[str],
    initial_brief: Optional[str],
    spec_content: Optional[str],
) -> Dict[str, Any]:
    """Intake phase: build the initial ``ClientContext`` from the request inputs.

    Preconditions:
        - ``job_id`` refers to a pending job; ``repo_path`` is the resolved
          workspace; at least one of ``initial_brief``/``spec_content`` is set.
    Postconditions:
        - Flips the job to RUNNING at 5% and returns the initial JSON-native
          ``context`` (client_context/repo_path/initial_brief/spec_content) that
          seeds the rest of the pipeline.
    """
    from planning_team.models import Phase
    from planning_team.phases import run_intake
    from planning_team.shared.job_store import JOB_STATUS_RUNNING

    def _work() -> Dict[str, Any]:
        context_update, _ = run_intake(
            repo_path=repo_path,
            client_name=client_name,
            initial_brief=initial_brief,
            spec_content=spec_content,
        )
        return _merge_context({}, context_update)

    return _guarded(job_id, Phase.INTAKE.value, 5, "Intake", _work, status=JOB_STATUS_RUNNING)


@activity.defn(name="planning_discovery")
def discovery_activity(job_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """Discovery phase: LLM-extract problem/opportunity/personas/success criteria.

    Preconditions:
        - ``context`` is the JSON-native context from :func:`intake_activity`.
    Postconditions:
        - Returns ``context`` with the discovery-refined ``client_context`` merged
          in (same map-reduce as thread mode; the shared ``get_client("planning")``
          client is resolved inside the worker).
    """
    from llm_service import get_client
    from planning_team.models import Phase
    from planning_team.phases import run_discovery

    def _work() -> Dict[str, Any]:
        context_update, _ = run_discovery(context, get_client("planning"))
        return _merge_context(context, context_update)

    return _guarded(job_id, Phase.DISCOVERY.value, 15, "Discovery", _work)


@activity.defn(name="planning_requirements")
def requirements_activity(job_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """Requirements phase: LLM-derive open questions (RPO/RTO, SLAs, compliance…).

    Preconditions:
        - ``context`` is the JSON-native context from :func:`discovery_activity`.
    Postconditions:
        - Returns ``context`` with ``open_questions`` (a list of JSON-native
          question dicts) merged in.
    """
    from llm_service import get_client
    from planning_team.models import Phase
    from planning_team.phases import run_requirements

    def _work() -> Dict[str, Any]:
        context_update, _ = run_requirements(context, get_client("planning"))
        return _merge_context(context, context_update)

    return _guarded(job_id, Phase.REQUIREMENTS.value, 25, "Requirements", _work)


@activity.defn(name="planning_market_research")
def market_research_activity(job_id: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Optional cross-team Market Research call (only dispatched when requested).

    Preconditions:
        - ``context`` is the JSON-native context; the workflow invokes this only
          when ``use_market_research`` is true.
    Postconditions:
        - Returns the market-research *evidence* dict (to feed
          :func:`synthesis_activity`), or ``None`` when there is nothing to research
          (no problem/users) or the Market Research team returns nothing. Mirrors
          the orchestrator's synthesis-phase market-research block.
    """
    from planning_team.adapters import market_research_to_evidence, request_market_research
    from planning_team.models import Phase

    def _work() -> Optional[Dict[str, Any]]:
        client_ctx = context.get("client_context") or {}
        problem = client_ctx.get("problem_summary") if isinstance(client_ctx, dict) else None
        users = client_ctx.get("target_users", []) if isinstance(client_ctx, dict) else []
        if not (problem or users):
            return None
        mr_data = request_market_research(
            product_concept=problem or "Product",
            target_users=", ".join(users) if users else "End users",
            business_goal="Validate and refine requirements",
        )
        if not mr_data:
            return None
        return market_research_to_evidence(mr_data)

    return _guarded(job_id, Phase.SYNTHESIS.value, 30, "Market research", _work)


@activity.defn(name="planning_synthesis")
def synthesis_activity(
    job_id: str,
    context: Dict[str, Any],
    market_evidence: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Synthesis phase: fold market-research (or other) evidence into the context.

    Preconditions:
        - ``context`` is the JSON-native context; ``market_evidence`` is the
          :func:`market_research_activity` result (or ``None`` when it did not run).
    Postconditions:
        - Returns ``context`` with the evidence merged into ``client_context``
          (a no-op merge when ``market_evidence`` is ``None``).
    """
    from planning_team.models import Phase
    from planning_team.phases import run_synthesis

    def _work() -> Dict[str, Any]:
        context_update, _ = run_synthesis(context, market_research_evidence=market_evidence)
        return _merge_context(context, context_update)

    return _guarded(job_id, Phase.SYNTHESIS.value, 35, "Synthesis", _work)


@activity.defn(name="planning_document_production")
def document_production_activity(
    job_id: str,
    context: Dict[str, Any],
    use_product_analysis: bool,
) -> Dict[str, Any]:
    """Document-production phase: write context doc + spec, optionally run PRA.

    Preconditions:
        - ``context`` is the JSON-native context after synthesis.
    Postconditions:
        - Writes the client-context doc and initial spec under the workspace, and
          (when ``use_product_analysis``) submits the Product Requirements Analysis
          job and blocks on its completion — with a background heartbeat so the long
          poll is not mistaken for a stalled worker. Returns ``context`` with a
          JSON-native ``handoff_package`` merged in.
        - PRA clarification questions are auto-answered with defaults (parity with
          the current HTTP Temporal path: no user ``answer_callback``); the
          architecture step is not run here (it is a gated non-HTTP feature).
    """
    from planning_team.adapters import (
        run_product_analysis,
        wait_for_product_analysis_completion,
    )
    from planning_team.models import Phase
    from planning_team.orchestrator import _resolve_pra_answers
    from planning_team.phases import run_document_production
    from shared_concurrency import BackgroundHeartbeat

    def _work() -> Dict[str, Any]:
        def _pra_answer_cb(questions: list) -> list:
            # No user callback across the Temporal boundary; auto-answer with
            # defaults, exactly as run_workflow_background does for the HTTP path.
            return _resolve_pra_answers(questions, None, True)

        with BackgroundHeartbeat(activity.heartbeat, _POLL_HEARTBEAT_INTERVAL_S, copy_context=True):
            context_update, _ = run_document_production(
                context,
                use_product_analysis=use_product_analysis,
                run_pra=run_product_analysis,
                wait_pra=wait_for_product_analysis_completion,
                answer_callback=_pra_answer_cb,
                run_architecture_fn=None,
            )
        merged = _merge_context(context, context_update)
        # Carry any planning-surfaced questions onto the handoff so a downstream
        # team can escalate unanswered ones instead of auto-deciding (mirrors the
        # orchestrator; typically a no-op today since PRA resolves answers inline).
        handoff = merged.get("handoff_package")
        if isinstance(handoff, dict):
            handoff.setdefault("open_questions", list(merged.get("open_questions") or []))
            handoff.setdefault("resolved_questions", list(merged.get("resolved_questions") or []))
        return merged

    return _guarded(job_id, Phase.DOCUMENT_PRODUCTION.value, 45, "Document production", _work)


@activity.defn(name="planning_sub_agent_provisioning")
def sub_agent_provisioning_activity(
    job_id: str,
    context: Dict[str, Any],
    capability_gap: Optional[str],
) -> Dict[str, Any]:
    """Optional sub-agent provisioning: draft a spec and call the AI-Systems team.

    Preconditions:
        - ``context`` is the JSON-native context after document production.
    Postconditions:
        - When ``capability_gap`` is falsy (the HTTP path never sets it) this is a
          fast no-op returning ``context`` unchanged. Otherwise it writes a spec,
          starts the AI-Systems build and blocks on completion (heartbeated), then
          attaches the resulting blueprint to both ``context`` and the handoff.
    """
    from planning_team.adapters import (
        start_ai_systems_build,
        wait_for_ai_systems_build_completion,
    )
    from planning_team.models import Phase
    from planning_team.phases import run_sub_agent_provisioning
    from shared_concurrency import BackgroundHeartbeat

    def _work() -> Dict[str, Any]:
        with BackgroundHeartbeat(activity.heartbeat, _POLL_HEARTBEAT_INTERVAL_S, copy_context=True):
            context_update, _ = run_sub_agent_provisioning(
                context,
                capability_gap=capability_gap,
                start_build_fn=start_ai_systems_build,
                wait_build_fn=wait_for_ai_systems_build_completion,
            )
        merged = _merge_context(context, context_update)
        blueprint = merged.get("sub_agent_blueprint")
        handoff = merged.get("handoff_package")
        if blueprint and isinstance(handoff, dict):
            handoff["sub_agent_blueprint"] = blueprint
        return merged

    return _guarded(
        job_id,
        Phase.SUB_AGENT_PROVISIONING.value,
        90,
        "Sub-agent provisioning (optional)",
        _work,
    )


@activity.defn(name="planning_finalize")
def finalize_planning_activity(job_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """Finalize: persist the handoff package and mark the job completed at 100%.

    Preconditions:
        - ``context`` carries the ``handoff_package`` produced by document
          production (a JSON-native dict, or ``None`` if it was never built).
    Postconditions:
        - Marks the job COMPLETED at 100% with its ``handoff_package`` and summary,
          and returns ``{"success": True, "summary": ...}``. This is the sole
          terminal-success writer for the Temporal path.
    """
    from planning_team.models import Phase
    from planning_team.shared.job_store import mark_job_completed

    summary = "Planning completed; handoff package ready."

    def _work() -> Dict[str, Any]:
        mark_job_completed(
            job_id,
            handoff_package=context.get("handoff_package"),
            summary=summary,
        )
        return {"success": True, "summary": summary}

    return _guarded(job_id, Phase.SUB_AGENT_PROVISIONING.value, 100, "Complete", _work)
