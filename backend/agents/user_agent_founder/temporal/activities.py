"""Temporal activities for the user_agent_founder (Testing Personas) team.

The fine-grained :class:`UserAgentFounderWorkflow` drives the founder lifecycle
as a graph of durable, individually-retryable Temporal activities instead of the
old single monolithic activity that ran the whole orchestrator with blocking
``time.sleep`` poll loops buried inside. Each specialist step — begin, spec
generation, phase start, one poll tick, one answer batch, finalize, mark-failed —
is its own ``@activity.defn`` so a worker restart re-runs only the unfinished
step and every step is visible in the Temporal UI.

Reuse: the genuinely intricate logic (autonomous question answering with
retry/backoff and the 409-terminal guard, and heartbeated spec generation) is
delegated verbatim to the orchestrator helpers ``_answer_pending_questions`` /
``_generate_spec_with_heartbeat`` — the exact same code the thread path runs — so
Temporal mode and thread mode stay behavior-equivalent. Only the *loop control*
moves out of the activity and into the deterministic workflow body.

Import hygiene: top-level imports stay light (``temporalio``, typing); every
heavy import (``orchestrator``, ``store``, ``agent``, ``targets``, ``httpx``) and
any ``os.getenv`` read is lazy inside function bodies, so the temporalio workflow
sandbox that re-imports sibling modules during workflow registration is never
tripped (guarded by ``tests/test_temporal_bootstrap.py``).

Serialization boundary: every activity takes/returns JSON-native values keyed on
``run_id`` — the store, agent, and adapter are non-serializable and are rebuilt
inside each activity from the run row (the same rule the pre-decomposition
activity already followed).

Job-store status ownership (retry-safe contract, mirroring ``sales_team``):
    - RUNNING is written by ``begin_run`` (and refreshed at each phase boundary).
    - COMPLETED is written only by ``finalize_run``.
    - FAILED is written only by ``mark_failed``, which the WORKFLOW invokes from
      its catch-all after a fatal error has exhausted the activity's retries — so
      an activity never marks FAILED mid-retry (which would defeat the retry
      policy) and a user cancel (which already wrote CANCELLED) is never
      clobbered.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

# ---------------------------------------------------------------------------
# Dependency reconstruction (lazy — nothing heavy at module import)
# ---------------------------------------------------------------------------


def _store_and_run(run_id: str):
    """Return ``(store, run)`` for ``run_id`` (``run`` is ``None`` if absent).

    Preconditions:
        - ``run_id`` is a founder-run id (the row may or may not exist).
    Postconditions:
        - Returns the shared ``FounderRunStore`` and the ``StoredRun`` (or
          ``None``); no side effects.
    """
    from user_agent_founder.store import get_founder_store

    store = get_founder_store()
    return store, store.get_run(run_id)


def _agent_for(run):
    """Build a ``FounderAgent`` for ``run`` (persona-customized when set).

    Preconditions:
        - ``run`` is a non-``None`` ``StoredRun``.
    Postconditions:
        - Returns a ``FounderAgent`` seeded with the run's persona prompts when a
          persona is attached, else the default persona.
    """
    from user_agent_founder.agent import FounderAgent
    from user_agent_founder.store import get_persona_store

    persona = get_persona_store().get_persona(run.persona_id) if run.persona_id else None
    if persona is not None:
        return FounderAgent(
            system_prompt=persona.system_prompt,
            spec_generation_prompt=persona.spec_generation_prompt,
        )
    return FounderAgent()


def _adapter_for(run):
    """Resolve the target-team adapter for ``run``.

    Preconditions:
        - ``run`` is a non-``None`` ``StoredRun`` with a resolvable
          ``target_team_key``.
    Postconditions:
        - Returns a ``TargetTeamAdapter``; ``process_id``/``spec`` are threaded
          through for agentic-team targets and ignored for the static targets.
    """
    from user_agent_founder.targets import get_adapter

    return get_adapter(run.target_team_key, process_id=run.process_id, spec=run.spec_content)


def _require_run(run_id: str, where: str):
    """Load ``(store, run)`` and raise non-retryably when the run row is missing.

    Preconditions:
        - ``run_id`` is a founder-run id; ``where`` names the call site for the
          error message.
    Postconditions:
        - Returns ``(store, run)`` with a non-``None`` ``run``; a genuinely
          absent row raises a non-retryable ``ApplicationError`` (retrying a
          missing row can never succeed and must surface as a failed workflow,
          not silently no-op every downstream store write).
    """
    store, run = _store_and_run(run_id)
    if run is None:
        raise ApplicationError(f"Founder run {run_id} not found at {where}", non_retryable=True)
    return store, run


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@activity.defn(name="user_agent_founder_begin_run")
def begin_run_activity(run_id: str) -> dict[str, Any]:
    """Open the run: mark it RUNNING and return the resume/config snapshot.

    Preconditions:
        - ``run_id`` refers to a run row already created by ``/start``.
    Postconditions:
        - Missing run row → non-retryable ``ApplicationError`` (see
          :func:`_require_run`).
        - Otherwise the central job row is marked RUNNING and a JSON snapshot is
          returned carrying the deterministic decisions the workflow body needs:
          which phases to short-circuit (``skip_spec``/``skip_analysis`` from the
          checkpoint columns), the existing per-phase job ids for resume, the
          persisted ``repo_path``, display/label metadata, and the env-derived
          poll intervals + attempt/answer-retry ceilings (read here so the
          deterministic workflow never touches the environment).
    """
    from user_agent_founder import orchestrator

    store, run = _require_run(run_id, "begin_run")
    adapter = _adapter_for(run)
    orchestrator._sync_job_status(run_id, "running", phase="starting")

    # Preserve the thread path's resume chat breadcrumbs.
    if run.spec_content:
        store.add_chat_message(run_id, "system", "Resuming with existing spec.", "status_update")
    if run.repo_path:
        store.add_chat_message(
            run_id, "system", "Resuming with existing analysis output.", "status_update"
        )

    return {
        "skip_spec": bool(run.spec_content),
        "skip_analysis": bool(run.repo_path),
        "analysis_job_id": run.analysis_job_id,
        "build_job_id": run.se_job_id,
        "repo_path": run.repo_path,
        "project_name": run.project_name or f"user-agent-founder-{run_id}",
        "target_team_key": run.target_team_key,
        "adapter_display_name": adapter.display_name,
        "analysis_poll_interval": orchestrator.ANALYSIS_POLL_INTERVAL,
        "build_poll_interval": orchestrator.EXECUTION_POLL_INTERVAL,
        "max_poll_attempts": orchestrator.MAX_POLL_ATTEMPTS,
        "max_answer_retries": orchestrator.MAX_ANSWER_RETRIES,
    }


@activity.defn(name="user_agent_founder_generate_spec")
def generate_spec_activity(run_id: str) -> dict[str, Any]:
    """Phase 1 — generate the product spec (heartbeated) and persist it.

    Preconditions:
        - ``run_id`` refers to an existing run; the workflow only schedules this
          when ``skip_spec`` is false, but the activity is idempotent either way.
    Postconditions:
        - Missing run → non-retryable ``ApplicationError``.
        - Spec already present → returns ``{"chars", "skipped": True}`` without a
          second LLM call (crash/retry idempotency).
        - Otherwise the spec is generated via the shared
          ``_generate_spec_with_heartbeat`` (job heartbeated so the stale-job
          monitor doesn't reap a long generation), persisted to
          ``spec_content``, and a chat breadcrumb is recorded.
    """
    from user_agent_founder import orchestrator

    store, run = _require_run(run_id, "generate_spec")
    if run.spec_content:
        return {"chars": len(run.spec_content), "skipped": True}

    agent = _agent_for(run)
    adapter_name = _adapter_for(run).display_name

    store.update_run(run_id, status="generating_spec")
    orchestrator._sync_job_status(run_id, "running", phase="generating_spec")
    store.add_chat_message(run_id, "system", "Generating product specification...", "status_update")

    spec_content = orchestrator._generate_spec_with_heartbeat(agent, run_id)
    store.update_run(run_id, spec_content=spec_content)
    store.add_chat_message(
        run_id,
        "assistant",
        f"Product spec generated ({len(spec_content)} chars). "
        f"Submitting to {adapter_name} for analysis.",
        "status_update",
    )
    return {"chars": len(spec_content), "skipped": False}


@activity.defn(name="user_agent_founder_start_phase")
def start_phase_activity(run_id: str, phase: str) -> dict[str, Any]:
    """Start one target-team phase (``analysis`` or ``build``) and record its job id.

    Preconditions:
        - ``run_id`` refers to an existing run; ``phase`` is ``"analysis"`` or
          ``"build"``. For ``analysis`` the run's ``spec_content`` is set; for
          ``build`` the run's ``repo_path`` is the analysis→build handoff (a real
          path for the SE target, ``None`` for the agentic target whose
          ``start_build`` ignores it).
    Postconditions:
        - ``StartFailed`` (or an unknown ``phase``) → non-retryable
          ``ApplicationError`` so the workflow's catch-all marks the run FAILED
          without burning retries on a deterministic failure.
        - Otherwise the target job is started, its id is persisted to the
          matching checkpoint column (``analysis_job_id``/``se_job_id``), the run
          transitions to ``polling_<phase>``, and ``{"job_id": ...}`` is returned.
    """
    import httpx

    from user_agent_founder import orchestrator
    from user_agent_founder.targets import StartFailed

    store, run = _require_run(run_id, "start_phase")
    adapter = _adapter_for(run)

    store.update_run(run_id, status=f"submitting_{phase}")
    orchestrator._sync_job_status(run_id, "running", phase=f"submitting_{phase}")
    project_name = run.project_name or f"user-agent-founder-{run_id}"

    try:
        with httpx.Client() as client:
            if phase == "analysis":
                job_id = adapter.start_from_spec(client, project_name, run.spec_content or "")
            elif phase == "build":
                job_id = adapter.start_build(client, run.repo_path)
            else:
                raise ApplicationError(f"Unknown phase {phase!r}", non_retryable=True)
    except StartFailed as exc:
        raise ApplicationError(f"Failed to start {phase}: {exc}", non_retryable=True) from exc

    if phase == "analysis":
        store.update_run(run_id, analysis_job_id=job_id)
        label = "Product analysis"
    else:
        store.update_run(run_id, se_job_id=job_id)
        label = f"{adapter.display_name} build"
    store.update_run(run_id, status=f"polling_{phase}", error=None)
    orchestrator._sync_job_status(run_id, "running", phase=f"polling_{phase}")
    store.add_chat_message(run_id, "system", f"{label} started (job: {job_id})", "status_update")
    return {"job_id": job_id}


@activity.defn(name="user_agent_founder_poll_phase")
def poll_phase_activity(run_id: str, phase: str, job_id: str) -> dict[str, Any]:
    """Poll one target-team phase once and normalize the verdict for the workflow.

    Preconditions:
        - ``run_id`` refers to an existing run; ``phase`` is ``"analysis"`` or
          ``"build"``; ``job_id`` is the target job started for that phase.
    Postconditions:
        - Missing run / unknown phase → non-retryable ``ApplicationError``.
        - Touches the job heartbeat (so the stale-job monitor doesn't reap a
          long-polling run) and returns the normalized single-poll verdict:
          ``{"status", "poll_error", "waiting", "pending_questions", "repo_path",
          "error"}``. On a terminal ``completed`` analysis poll the analysis→build
          handoff ``repo_path`` is persisted (idempotent) so the build phase can
          read it from the run row.
    """
    import httpx

    from user_agent_founder import orchestrator

    store, run = _require_run(run_id, "poll_phase")
    adapter = _adapter_for(run)

    orchestrator._heartbeat(run_id)
    with httpx.Client() as client:
        if phase == "analysis":
            status_data = adapter.poll_analysis(client, job_id)
        elif phase == "build":
            status_data = adapter.poll_build(client, job_id)
        else:
            raise ApplicationError(f"Unknown phase {phase!r}", non_retryable=True)

    poll_error = status_data.get("_poll_error")
    status = status_data.get("status", "")
    waiting = bool(status_data.get("waiting_for_answers") and status_data.get("pending_questions"))
    repo_path = status_data.get("repo_path")

    if phase == "analysis" and status == "completed" and not poll_error:
        store.update_run(run_id, repo_path=repo_path)
        store.add_chat_message(
            run_id, "system", "Analysis complete. Starting target-team build.", "status_update"
        )

    return {
        "status": status,
        "poll_error": poll_error,
        "waiting": waiting,
        "pending_questions": status_data.get("pending_questions") or [],
        "repo_path": repo_path,
        "error": status_data.get("error"),
    }


@activity.defn(name="user_agent_founder_answer_questions")
def answer_questions_activity(
    run_id: str, phase: str, job_id: str, questions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Autonomously answer + submit one batch of target-team questions.

    Preconditions:
        - ``run_id`` refers to an existing run; ``phase`` is ``"analysis"`` or
          ``"build"``; ``questions`` is the target's pending-questions batch.
    Postconditions:
        - Missing run / unknown phase → non-retryable ``ApplicationError``.
        - Delegates verbatim to ``orchestrator._answer_pending_questions`` (the
          same persona-driven answering, decision/chat recording, submit
          retry/backoff, and 409-terminal guard the thread path uses) and returns
          ``{"ok": bool}``. ``ok=False`` (a handled submission failure) is a
          normal return, not an exception, so Temporal does not retry the batch —
          the workflow tracks the failed-set counter and re-surfaces on the next
          poll, exactly like the thread path.
    """
    import httpx

    from user_agent_founder import orchestrator

    store, run = _require_run(run_id, "answer_questions")
    if phase not in ("analysis", "build"):
        raise ApplicationError(f"Unknown phase {phase!r}", non_retryable=True)
    agent = _agent_for(run)
    adapter = _adapter_for(run)

    store.update_run(run_id, status=f"answering_{phase}_questions")
    store.add_chat_message(
        run_id,
        "system",
        f"Target team has {len(questions)} question(s) during {phase}.",
        "question_received",
        metadata={"question_ids": [q.get("id", "") for q in questions]},
    )

    with httpx.Client() as client:
        if phase == "analysis":

            def _submit(answers: list[dict[str, Any]]) -> None:
                adapter.submit_analysis_answers(client, job_id, answers)
        else:

            def _submit(answers: list[dict[str, Any]]) -> None:
                adapter.submit_build_answers(client, job_id, answers)

        ok = orchestrator._answer_pending_questions(
            agent, store, run_id, job_id, questions, _submit
        )
    return {"ok": bool(ok)}


@activity.defn(name="user_agent_founder_finalize_run")
def finalize_run_activity(run_id: str) -> dict[str, Any]:
    """Record the terminal COMPLETED state — the single writer of COMPLETED.

    Preconditions:
        - ``run_id`` refers to an existing run whose build phase completed.
    Postconditions:
        - Missing run → non-retryable ``ApplicationError``.
        - Otherwise the run row + central job are marked COMPLETED and a success
          breadcrumb is recorded; returns ``{"run_id": run_id}``.
    """
    from user_agent_founder import orchestrator

    store, _run = _require_run(run_id, "finalize")
    store.add_chat_message(run_id, "system", "Build completed successfully.", "status_update")
    store.update_run(run_id, status="completed")
    orchestrator._sync_job_status(run_id, "completed", phase="completed")
    return {"run_id": run_id}


@activity.defn(name="user_agent_founder_mark_failed")
def mark_failed_activity(run_id: str, error: str) -> dict[str, Any]:
    """Record the terminal FAILED state — the single writer of FAILED.

    Invoked only by the workflow's catch-all after a fatal error exhausted its
    retries, so failure marking can never defeat an activity's own retry policy.

    Preconditions:
        - ``error`` is the stringified fatal error.
    Postconditions:
        - Central job already CANCELLED (a user cancel wrote the terminal state)
          → no-op, returns ``{"marked": False}`` — the cancel is never clobbered.
        - Otherwise the run row + central job end FAILED with ``error`` recorded
          and a breadcrumb added; returns ``{"marked": True}``.
    """
    from user_agent_founder import orchestrator
    from user_agent_founder.shared import job_store
    from user_agent_founder.store import get_founder_store

    job = job_store.get_job(run_id)
    if job is not None and job.get("status") == job_store.JOB_STATUS_CANCELLED:
        activity.logger.info(
            "Founder run %s already CANCELLED at mark-failed; leaving status untouched", run_id
        )
        return {"marked": False}

    store = get_founder_store()
    store.update_run(run_id, status="failed", error=error)
    orchestrator._sync_job_status(run_id, "failed", error=error)
    store.add_chat_message(run_id, "system", f"Workflow failed: {error}", "status_update")
    return {"marked": True}
