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

from planning_team.temporal.constants import RETRYABLE_MAX_ATTEMPTS, SINGLE_ATTEMPT

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
        - Models become ``model_dump(mode="json")`` dicts; lists and dict values
          are converted element-wise (recursively, so a model nested inside a dict
          is normalized too); everything else is returned unchanged. The result is
          JSON-serializable so it can cross the Temporal activity boundary under
          the default data converter.
    """
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
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


def _is_final_attempt(max_attempts: int) -> bool:
    """Return True when the current activity attempt is the last one allowed.

    Preconditions:
        - ``max_attempts`` is the ``maximum_attempts`` of the phase's RetryPolicy
          (>= 1). It MUST match the policy the workflow assigns this activity, or
          the FAILED marking fires on the wrong attempt.
    Postconditions:
        - Inside a Temporal worker, returns ``activity.info().attempt >= max_attempts``
          — i.e. Temporal will not retry after this attempt.
        - Outside a worker (direct call in unit tests), returns True so a failure
          still surfaces the FAILED marking rather than being silently swallowed.
    """
    if not activity.in_activity():
        return True
    return activity.info().attempt >= max_attempts


def _guarded(
    job_id: str,
    phase: str,
    progress: int,
    status_text: str,
    work: Callable[[], Any],
    *,
    max_attempts: int,
    status: Optional[str] = None,
) -> Any:
    """Report phase progress, run ``work``, and mark the job FAILED on final failure.

    Preconditions:
        - ``job_id`` refers to an existing job; ``progress`` is 0..100; ``work`` is
          a zero-arg callable performing the phase's work and returning its result.
        - ``max_attempts`` matches the phase's Temporal RetryPolicy (see
          ``constants.RETRYABLE_MAX_ATTEMPTS`` / ``SINGLE_ATTEMPT``).
    Postconditions:
        - Updates ``current_phase``/``progress``/``status_text``, and writes
          ``status`` only when supplied (only intake supplies it, PENDING → RUNNING).
        - The progress write is inside the guard, so a failing progress write still
          marks the job FAILED rather than leaving it stuck non-terminal.
        - On error, the job is marked FAILED only on the *final* Temporal attempt
          (``_is_final_attempt``); a retry that later succeeds therefore never leaves
          a transient FAILED status or a stale ``error`` behind. The exception is
          always re-raised so Temporal's RetryPolicy governs re-attempts.
    """
    from planning_team.shared.job_store import update_job

    fields: Dict[str, Any] = {
        "current_phase": phase,
        "progress": progress,
        "status_text": status_text,
    }
    if status is not None:
        # Only intake supplies status (PENDING → RUNNING); later phases leave it
        # untouched so they never clobber a concurrent ``cancelled``. Accepted narrow
        # race: a cancel landing in the create_job→intake window is overwritten — no
        # planning caller cancels jobs today, and a real canceller should also send a
        # Temporal cancel to the workflow.
        fields["status"] = status
    try:
        # Progress is written BEFORE work(), inside the guard, so a failing progress
        # write is still marked FAILED. Trade-off: on terminal failure
        # current_phase/progress point at the phase that was *attempted* when it
        # failed (a best-effort hint, not a completed-phase record).
        update_job(job_id, **fields)
        return work()
    except Exception as exc:
        if _is_final_attempt(max_attempts):
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

    # First phase: flip PENDING → RUNNING. Later phases leave status untouched.
    return _guarded(
        job_id,
        Phase.INTAKE.value,
        5,
        "Intake",
        _work,
        max_attempts=RETRYABLE_MAX_ATTEMPTS,
        status=JOB_STATUS_RUNNING,
    )


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

    return _guarded(
        job_id, Phase.DISCOVERY.value, 15, "Discovery", _work, max_attempts=RETRYABLE_MAX_ATTEMPTS
    )


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

    return _guarded(
        job_id,
        Phase.REQUIREMENTS.value,
        25,
        "Requirements",
        _work,
        max_attempts=RETRYABLE_MAX_ATTEMPTS,
    )


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

    # Market research is a step of the synthesis phase (the orchestrator reports it
    # under a single SYNTHESIS update too), so report the same phase/progress as
    # synthesis_activity rather than inventing a distinct sub-phase. Submitting a
    # research request is non-idempotent → single attempt.
    return _guarded(
        job_id, Phase.SYNTHESIS.value, 35, "Synthesis", _work, max_attempts=SINGLE_ATTEMPT
    )


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

    return _guarded(
        job_id, Phase.SYNTHESIS.value, 35, "Synthesis", _work, max_attempts=RETRYABLE_MAX_ATTEMPTS
    )


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
          poll is not mistaken for a stalled worker. Persists the handoff (which
          carries the full spec/PRD content) to the durable job store and returns a
          *slim* ``{repo_path}`` result, so the large handoff never crosses a
          Temporal activity boundary / blob limit. Also persists the actual
          ``open_questions``/``resolved_questions`` as their own top-level job
          fields (separate from the deliberately-empty copies inside the
          handoff) so ``finalize_planning_activity``'s audit write has a real
          source for them.
        - PRA clarification questions are auto-answered with defaults (parity with
          the current HTTP Temporal path: no user ``answer_callback``); the
          architecture step is not run here (it is a gated non-HTTP feature).
    """
    from planning_team.adapters import (
        run_product_analysis,
        wait_for_product_analysis_completion,
    )
    from planning_team.models import Phase
    from planning_team.orchestrator import resolve_pra_answers
    from planning_team.phases import run_document_production
    from shared.concurrency import BackgroundHeartbeat

    def _work() -> Dict[str, Any]:
        def _pra_answer_cb(questions: list) -> list:
            # No user callback across the Temporal boundary; auto-answer with
            # defaults, exactly as run_workflow_background does for the HTTP path.
            return resolve_pra_answers(questions, None, True)

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
        # Carry any *externally-resolved* questions onto the handoff, mirroring the
        # orchestrator exactly. This is intentionally a ``setdefault`` no-op today:
        # HandoffPackage seeds both keys with ``[]``, and that empty handoff is
        # load-bearing — the SE orchestrator pauses the whole run for user input
        # when ``handoff.open_questions`` is non-empty, and the requirements phase
        # always emits (default) questions, so populating them here would pause
        # every SE-driven run. Kept identical to the thread path for parity.
        handoff = merged.get("handoff_package")
        if isinstance(handoff, dict):
            handoff.setdefault("open_questions", list(merged.get("open_questions") or []))
            handoff.setdefault("resolved_questions", list(merged.get("resolved_questions") or []))
            # Persist the handoff to the durable job store NOW (Postgres JSONB — no
            # Temporal blob limit). The handoff carries the full validated-spec/PRD
            # content, which for a large plan can exceed Temporal's activity-result
            # payload limit; returning it as an activity result would strand the job
            # (files already written, but the workflow can't advance to finalize).
            # So the handoff never crosses a Temporal boundary — only the job store.
            # open_questions/resolved_questions are ALSO persisted as their own
            # top-level job fields (separate from the deliberately-empty copies
            # inside handoff above) so finalize_planning_activity's audit write
            # can read the actual discovery questions. context/merged is already
            # JSON-native here via _json_safe, so no dump step is needed.
            from planning_team.shared.job_store import update_job

            update_job(
                job_id,
                handoff_package=handoff,
                open_questions=list(merged.get("open_questions") or []),
                resolved_questions=list(merged.get("resolved_questions") or []),
            )
        # Slim result: downstream phases read only repo_path; the handoff lives in
        # the job store from here on.
        return {"repo_path": merged.get("repo_path")}

    return _guarded(
        job_id,
        Phase.DOCUMENT_PRODUCTION.value,
        45,
        "Document production",
        _work,
        max_attempts=SINGLE_ATTEMPT,
    )


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
          attaches the resulting blueprint to the job-store handoff (which document
          production persisted; it is not threaded through the activity result).
    """
    from planning_team.adapters import (
        start_ai_systems_build,
        wait_for_ai_systems_build_completion,
    )
    from planning_team.models import Phase
    from planning_team.phases import run_sub_agent_provisioning
    from shared.concurrency import BackgroundHeartbeat

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
        if blueprint:
            # The handoff lives in the job store (persisted by document production),
            # not in the context. Read it back, attach the blueprint, and re-persist.
            # This read-modify-write is NOT atomic with the activity completion: a
            # crash between get_job and update_job would drop the blueprint (a retry
            # re-does it — the attachment is idempotent), and a concurrent writer to
            # handoff_package could be lost. Acceptable today — this phase is
            # SINGLE_ATTEMPT and there is no other concurrent writer of the handoff.
            from planning_team.shared.job_store import get_job, update_job

            job = get_job(job_id) or {}
            handoff = job.get("handoff_package")
            if isinstance(handoff, dict):
                handoff["sub_agent_blueprint"] = blueprint
                update_job(job_id, handoff_package=handoff)
        return merged

    return _guarded(
        job_id,
        Phase.SUB_AGENT_PROVISIONING.value,
        90,
        "Sub-agent provisioning (optional)",
        _work,
        max_attempts=SINGLE_ATTEMPT,
    )


@activity.defn(name="planning_finalize")
def finalize_planning_activity(job_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """Finalize: mark the job completed at 100% and record a best-effort audit row.

    Preconditions:
        - The ``handoff_package`` and the actual ``open_questions``/
          ``resolved_questions`` have already been persisted to the job store by
          ``document_production_activity`` (``context`` is the slim ``{repo_path}``).
    Postconditions:
        - Marks the job COMPLETED at 100% with a summary, WITHOUT passing
          ``handoff_package`` (a partial-update merge, so the already-persisted
          handoff is preserved, not clobbered). Then, best-effort and fully isolated
          from finalization, re-reads the job to derive the ``planning_runs`` audit
          columns — ``open_questions``/``resolved_questions`` from the job's own
          top-level fields, the rest from the persisted handoff — and calls
          ``record_planning_run``. Unlike
          ``record_planning_run`` (which never raises), the ``get_job`` re-read is a
          live job-service call that can raise on an operational failure — that
          exception, and any other failure in the audit block, is caught here so it
          can never escape ``_work``, retry the finalize activity via ``_guarded``, or
          overwrite the already-completed job with a failure on retry exhaustion.
          Returns ``{"success": True, ...}`` regardless of the audit outcome. This is
          the sole terminal-success writer for the Temporal path.
    """
    from planning_team.models import Phase
    from planning_team.postgres.writer import record_planning_run
    from planning_team.shared.job_store import get_job, mark_job_completed

    summary = "Planning completed; handoff package ready."

    def _work() -> Dict[str, Any]:
        mark_job_completed(job_id, summary=summary)
        try:
            job = get_job(job_id) or {}
            handoff = job.get("handoff_package") or {}
            # open_questions/resolved_questions are sourced from the job record's
            # own top-level fields (persisted by document_production_activity), not
            # from handoff: handoff's copies are deliberately left empty — see the
            # matching comment in document_production_activity.
            record_planning_run(
                job_id,
                client_name=(handoff.get("client_context") or {}).get("client_name"),
                summary=summary,
                handoff_summary=handoff.get("summary") or "",
                open_questions=job.get("open_questions") or [],
                resolved_questions=job.get("resolved_questions") or [],
            )
        except Exception:
            # Audit-only: the job is already durably marked completed above, and a
            # failure re-reading it (or writing the audit row) must never retry
            # finalize or turn a completed run into a failed one.
            logger.debug("failed to record planning_run audit for job %s", job_id, exc_info=True)
        return {"success": True, "summary": summary}

    # current_phase stays at the last real phase (sub_agent_provisioning); the
    # completion signal is status=completed + status_text="Complete". This matches
    # the thread-mode orchestrator, so both paths report an identical terminal state.
    return _guarded(
        job_id,
        Phase.SUB_AGENT_PROVISIONING.value,
        100,
        "Complete",
        _work,
        max_attempts=RETRYABLE_MAX_ATTEMPTS,
    )


@activity.defn(name="run_planning_activity")
def run_planning_activity(
    job_id: str,
    repo_path: str,
    client_name: Optional[str],
    initial_brief: Optional[str],
    spec_content: Optional[str],
    use_product_analysis: bool,
    use_market_research: bool,
) -> None:
    """LEGACY single-activity path, retained ONLY for rollout compatibility.

    Runs the whole planning pipeline in one activity via ``run_workflow_background``
    (the pre-decomposition behavior). New executions run the per-phase activities;
    this exists so a ``PlanningWorkflow`` history recorded *before* the per-phase
    migration still replays deterministically (see ``PlanningWorkflow.run``'s
    ``workflow.patched`` gate) and can complete after a worker rolls forward.

    Preconditions:
        - Only scheduled by the legacy branch of ``PlanningWorkflow.run`` while
          replaying a pre-migration history; the activity name / args / timeouts
          must stay byte-for-byte what those histories recorded.
    Postconditions:
        - Runs intake→…→finalize in-process and updates the job store (the same
          function the thread-mode ``/run`` endpoint uses). Re-raises on error so
          the legacy RetryPolicy governs re-attempts. Remove once no pre-migration
          executions remain open.
    """
    try:
        from planning_team.api.main import run_workflow_background

        run_workflow_background(
            job_id,
            repo_path,
            client_name,
            initial_brief,
            spec_content,
            use_product_analysis,
            use_market_research,
        )
    except Exception:
        logger.exception("Planning legacy activity failed for job %s", job_id)
        raise
