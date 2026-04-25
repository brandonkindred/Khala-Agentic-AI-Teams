"""Background workflow orchestrator for the founder agent.

Runs the full lifecycle: spec generation -> product analysis -> team build,
answering all questions autonomously through the founder persona.

Team-specific HTTP coupling lives in ``user_agent_founder.targets``;
the orchestrator only knows the ``TargetTeamAdapter`` Protocol shape.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

import httpx

from job_service_client import JobServiceClient
from llm_service import LLMJsonParseError, LLMSchemaValidationError
from user_agent_founder.agent import FounderAgent
from user_agent_founder.store import FounderRunStore
from user_agent_founder.targets import StartFailed, TargetTeamAdapter, get_adapter

logger = logging.getLogger(__name__)

_job_client = JobServiceClient(team="user_agent_founder")

ANALYSIS_POLL_INTERVAL = int(os.environ.get("FOUNDER_ANALYSIS_POLL_SECONDS", "15"))
EXECUTION_POLL_INTERVAL = int(os.environ.get("FOUNDER_EXECUTION_POLL_SECONDS", "30"))
MAX_POLL_ATTEMPTS = int(os.environ.get("FOUNDER_MAX_POLL_ATTEMPTS", "480"))  # ~4h at 30s
MAX_ANSWER_RETRIES = int(os.environ.get("FOUNDER_MAX_ANSWER_RETRIES", "2"))
ANSWER_POST_RETRIES = 3
ANSWER_POST_BACKOFF_BASE = 2  # seconds


def _sync_job_status(run_id: str, status: str, *, phase: str = "", error: str = "") -> None:
    """Mirror a run status update into the centralized job service."""
    try:
        _job_client.update_job(run_id, status=status, current_phase=phase, error=error or None)
    except Exception:
        logger.debug("Job service sync failed for %s (non-fatal)", run_id, exc_info=True)


def _heartbeat(run_id: str) -> None:
    """Touch the heartbeat so the stale-job monitor doesn't kill a waiting job."""
    try:
        _job_client.heartbeat(run_id)
    except Exception:
        pass


# Interval between spec-generation heartbeats. Module-level so tests can shorten it.
SPEC_HEARTBEAT_INTERVAL = float(os.environ.get("FOUNDER_SPEC_HEARTBEAT_SECONDS", "30"))


def _generate_spec_with_heartbeat(agent: FounderAgent, run_id: str) -> str:
    """Run agent.generate_spec() while a background thread heartbeats the job.

    Spec generation commonly takes 60-180s; without this, the centralised
    job-service stale-job monitor reaps the run as dead even though Phases 2
    and 3 still succeed.
    """
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(SPEC_HEARTBEAT_INTERVAL):
            _heartbeat(run_id)

    hb_thread = threading.Thread(
        target=_beat,
        name=f"founder-spec-hb-{run_id[:12]}",
        daemon=True,
    )
    hb_thread.start()
    try:
        return agent.generate_spec()
    finally:
        stop.set()
        hb_thread.join(timeout=1)


def _answer_pending_questions(
    agent: FounderAgent,
    store: FounderRunStore,
    run_id: str,
    job_id: str,
    questions: list[dict[str, Any]],
    submit_fn: Callable[[list[dict[str, Any]]], None],
) -> bool:
    """Use the founder agent to answer all pending questions and submit them.

    ``submit_fn`` posts the answers (e.g. ``adapter.submit_analysis_answers``
    bound to ``client`` and ``job_id``). Returns True if answers were
    successfully submitted, False on failure.
    """
    answerable = [q for q in questions if q.get("id")]
    if not answerable:
        logger.error("All %d questions lack an 'id' field — cannot answer", len(questions))
        return False

    answers = []
    for q in answerable:
        try:
            result = agent.answer_question(q)
        except (LLMJsonParseError, LLMSchemaValidationError):
            # Self-correction retry inside generate_structured already ran and
            # failed — with bounded per-question schemas this should be rare.
            # Skip just this question so a single LLM glitch doesn't kill the
            # whole batch; the outer poll loop will re-surface unanswered
            # required questions on the next tick.
            logger.warning(
                "LLM validation failed for question %s after self-correction retry; skipping",
                q["id"],
            )
            continue
        except Exception:
            logger.exception("Unexpected error answering question %s", q["id"])
            return False
        answer_text = result.get("other_text") or result.get("selected_option_id", "")
        rationale = result.get("rationale", "")
        store.add_decision(
            run_id=run_id,
            question_id=q["id"],
            question_text=q.get("question_text", ""),
            answer_text=answer_text,
            rationale=rationale,
        )
        store.add_chat_message(
            run_id=run_id,
            role="assistant",
            content=f"Q: {q.get('question_text', '')}\nA: {answer_text}\nRationale: {rationale}",
            message_type="answer_given",
            metadata={
                "question_id": q["id"],
                "selected_option_id": result.get("selected_option_id"),
            },
        )
        answer_payload: dict[str, Any] = {
            "question_id": q["id"],
            "selected_option_id": result["selected_option_id"],
        }
        if result["selected_option_id"] == "other" and result.get("other_text"):
            answer_payload["other_text"] = result["other_text"]
        answers.append(answer_payload)

    if not answers:
        return False

    # Submit with retry + backoff for transient failures
    for attempt in range(ANSWER_POST_RETRIES):
        try:
            submit_fn(answers)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Answer submission attempt %d/%d failed for job %s: %s %s",
                attempt + 1,
                ANSWER_POST_RETRIES,
                job_id,
                exc.response.status_code,
                exc.response.text[:500],
            )
        except Exception:
            logger.exception(
                "Answer submission attempt %d/%d crashed for job %s",
                attempt + 1,
                ANSWER_POST_RETRIES,
                job_id,
            )
        else:
            logger.info("Successfully submitted %d answers for job %s", len(answers), job_id)
            return True
        if attempt < ANSWER_POST_RETRIES - 1:
            time.sleep(ANSWER_POST_BACKOFF_BASE ** (attempt + 1))

    store.add_chat_message(
        run_id=run_id,
        role="system",
        content=f"Failed to submit answers to target team after {ANSWER_POST_RETRIES} attempts.",
        message_type="status_update",
    )
    return False


def _run_phase(
    *,
    client: httpx.Client,
    agent: FounderAgent,
    store: FounderRunStore,
    run_id: str,
    phase: str,
    poll_interval: int,
    start_fn: Callable[[], str],
    poll_fn: Callable[[str], dict[str, Any]],
    submit_answers_fn: Callable[[str, list[dict[str, Any]]], None],
    on_started: Callable[[str], None],
    existing_job_id: str | None,
    questions_status: str,
    failure_label: str,
) -> tuple[bool, dict[str, Any] | None]:
    """Run one start + poll + answer phase against a target-team adapter.

    Returns ``(ok, final_status_data)``. ``ok=False`` means the phase
    failed/cancelled/timed out and the run row has already been marked
    ``failed``; the caller should abort. ``final_status_data`` is the
    last status payload returned by ``poll_fn`` on success.

    All four terminal states (completed / failed / cancelled / timeout)
    are handled in this single helper, eliminating the historic
    asymmetry where the analysis phase ignored ``cancelled`` and burned
    ~4 hours of polling before timing out.
    """
    submit_phase_status = f"submitting_{phase}"
    polling_phase_status = f"polling_{phase}"

    if existing_job_id is None:
        store.update_run(run_id, status=submit_phase_status)
        _sync_job_status(run_id, "running", phase=submit_phase_status)
        try:
            job_id = start_fn()
        except StartFailed as exc:
            err = f"Failed to start {phase}: {exc}"
            store.update_run(run_id, status="failed", error=err)
            _sync_job_status(run_id, "failed", error=f"Failed to start {phase}")
            return False, None
        on_started(job_id)
        store.update_run(run_id, status=polling_phase_status)
        _sync_job_status(run_id, "running", phase=polling_phase_status)
        logger.info("%s started: job_id=%s", failure_label, job_id)
        store.add_chat_message(
            run_id, "system", f"{failure_label} started (job: {job_id})", "status_update"
        )
    else:
        job_id = existing_job_id
        store.update_run(run_id, status=polling_phase_status, error=None)
        _sync_job_status(run_id, "running", phase=polling_phase_status)
        logger.info("Resuming %s poll: job_id=%s", failure_label, job_id)
        store.add_chat_message(
            run_id,
            "system",
            f"Resuming {failure_label} poll (job: {job_id})",
            "status_update",
        )

    failed_question_sets: dict[frozenset[str], int] = {}

    for _ in range(MAX_POLL_ATTEMPTS):
        time.sleep(poll_interval)
        _heartbeat(run_id)

        status_data = poll_fn(job_id)
        if status_data.get("_poll_error"):
            logger.warning("%s poll error: %s", failure_label, status_data.get("_poll_error"))
            continue
        status = status_data.get("status", "")

        # Answer pending questions
        if status_data.get("waiting_for_answers") and status_data.get("pending_questions"):
            pending = status_data["pending_questions"]
            qset = frozenset(q.get("id", "") for q in pending)
            prior_failures = failed_question_sets.get(qset, 0)

            if prior_failures > MAX_ANSWER_RETRIES:
                err = (
                    f"Answer submission failed {prior_failures} times "
                    f"for {phase} questions. Aborting."
                )
                logger.error(err)
                store.update_run(run_id, status="failed", error=err)
                _sync_job_status(run_id, "failed", error=err)
                store.add_chat_message(run_id, "system", err, "status_update")
                return False, None

            store.update_run(run_id, status=questions_status)
            store.add_chat_message(
                run_id,
                "system",
                f"Target team has {len(pending)} question(s) during {phase}.",
                "question_received",
                metadata={"question_ids": list(qset)},
            )
            success = _answer_pending_questions(
                agent,
                store,
                run_id,
                job_id,
                pending,
                lambda answers, _jid=job_id: submit_answers_fn(_jid, answers),
            )
            if not success:
                failed_question_sets[qset] = prior_failures + 1
                logger.warning(
                    "Answer attempt %d failed for %s questions",
                    prior_failures + 1,
                    phase,
                )
            continue

        if status == "completed":
            return True, status_data

        if status == "failed":
            err = f"{failure_label} failed: {status_data.get('error', 'unknown')}"
            store.update_run(run_id, status="failed", error=err)
            _sync_job_status(run_id, "failed", error=f"{failure_label} failed")
            store.add_chat_message(run_id, "system", err, "status_update")
            return False, None

        if status == "cancelled":
            err = f"{failure_label} was cancelled"
            store.update_run(run_id, status="failed", error=err)
            _sync_job_status(run_id, "failed", error=f"{failure_label} cancelled")
            store.add_chat_message(run_id, "system", f"{err}.", "status_update")
            return False, None

    err = f"{failure_label} timed out"
    store.update_run(run_id, status="failed", error=err)
    _sync_job_status(run_id, "failed", error=err)
    store.add_chat_message(run_id, "system", f"{err}.", "status_update")
    return False, None


def _run_product_analysis(
    client: httpx.Client,
    agent: FounderAgent,
    store: FounderRunStore,
    run_id: str,
    spec_content: str,
    adapter: TargetTeamAdapter,
    project_name: str,
    *,
    existing_job_id: str | None = None,
) -> str | None:
    """Submit spec for product analysis and poll until complete. Returns repo_path or None."""

    def _on_started(job_id: str) -> None:
        store.update_run(run_id, analysis_job_id=job_id)

    ok, status_data = _run_phase(
        client=client,
        agent=agent,
        store=store,
        run_id=run_id,
        phase="analysis",
        poll_interval=ANALYSIS_POLL_INTERVAL,
        start_fn=lambda: adapter.start_from_spec(client, project_name, spec_content),
        poll_fn=lambda jid: adapter.poll_analysis(client, jid),
        submit_answers_fn=lambda jid, answers: adapter.submit_analysis_answers(
            client, jid, answers
        ),
        on_started=_on_started,
        existing_job_id=existing_job_id,
        questions_status="answering_analysis_questions",
        failure_label="Product analysis",
    )
    if not ok or status_data is None:
        return None

    repo_path = status_data.get("repo_path")
    store.update_run(run_id, repo_path=repo_path)
    logger.info("Product analysis completed: repo_path=%s", repo_path)
    store.add_chat_message(
        run_id,
        "system",
        "Analysis complete. Starting target-team build.",
        "status_update",
    )
    return repo_path


def _run_target_team(
    client: httpx.Client,
    agent: FounderAgent,
    store: FounderRunStore,
    run_id: str,
    repo_path: str,
    adapter: TargetTeamAdapter,
    *,
    existing_job_id: str | None = None,
) -> bool:
    """Start the target-team build and poll until complete. Returns True on success."""

    def _on_started(job_id: str) -> None:
        store.update_run(run_id, se_job_id=job_id)

    ok, _status_data = _run_phase(
        client=client,
        agent=agent,
        store=store,
        run_id=run_id,
        phase="build",
        poll_interval=EXECUTION_POLL_INTERVAL,
        start_fn=lambda: adapter.start_build(client, repo_path),
        poll_fn=lambda jid: adapter.poll_build(client, jid),
        submit_answers_fn=lambda jid, answers: adapter.submit_build_answers(client, jid, answers),
        on_started=_on_started,
        existing_job_id=existing_job_id,
        questions_status="answering_build_questions",
        failure_label=f"{adapter.display_name} build",
    )
    if ok:
        logger.info("%s build completed for run %s", adapter.display_name, run_id)
        store.add_chat_message(run_id, "system", "Build completed successfully.", "status_update")
    return ok


def run_workflow(
    run_id: str,
    store: FounderRunStore,
    agent: FounderAgent,
    adapter: TargetTeamAdapter | None = None,
) -> None:
    """Execute the full founder workflow: spec -> analysis -> build.

    ``adapter`` defaults to the team recorded on the run row's
    ``target_team_key`` column (or the default if the column is empty).
    Re-entrant for the resume path: phases whose checkpoint columns are
    already populated on the run row are short-circuited so a `/resume`
    call does not re-pay the cost of completed phases (LLM spec gen,
    multi-hour analysis poll, etc.).

    Designed to run in a background thread.
    """
    logger.info("Starting founder workflow: run_id=%s", run_id)

    try:
        # Read the run row inside the try so a transient store outage during
        # the resume-short-circuit lookup gets caught by the failure handler
        # below rather than escaping the worker thread silently.
        run = store.get_run(run_id)

        if adapter is None:
            team_key = getattr(run, "target_team_key", None) or "software_engineering"
            adapter = get_adapter(team_key)

        project_name = f"user-agent-founder-{run_id}"

        # Phase 1: Generate the product spec (skip if already done)
        if run is not None and run.spec_content:
            spec_content = run.spec_content
            logger.info(
                "Resuming run %s past Phase 1 (spec already generated, %d chars)",
                run_id,
                len(spec_content),
            )
            store.add_chat_message(
                run_id,
                "system",
                "Resuming with existing spec.",
                "status_update",
            )
        else:
            store.update_run(run_id, status="generating_spec")
            _sync_job_status(run_id, "running", phase="generating_spec")
            store.add_chat_message(
                run_id, "system", "Generating product specification...", "status_update"
            )
            spec_content = _generate_spec_with_heartbeat(agent, run_id)
            store.update_run(run_id, spec_content=spec_content)
            logger.info("Spec generated for run %s (%d chars)", run_id, len(spec_content))
            store.add_chat_message(
                run_id,
                "assistant",
                f"Product spec generated ({len(spec_content)} chars). "
                f"Submitting to {adapter.display_name} for analysis.",
                "status_update",
            )

        with httpx.Client() as client:
            # Phase 2: Product analysis (skip entirely if repo_path stored;
            # skip submit only if analysis_job_id stored without repo_path)
            if run is not None and run.repo_path:
                repo_path: str | None = run.repo_path
                logger.info(
                    "Resuming run %s past Phase 2 (analysis already complete, repo_path=%s)",
                    run_id,
                    repo_path,
                )
                store.add_chat_message(
                    run_id,
                    "system",
                    "Resuming with existing analysis output.",
                    "status_update",
                )
            else:
                repo_path = _run_product_analysis(
                    client,
                    agent,
                    store,
                    run_id,
                    spec_content,
                    adapter,
                    project_name,
                    existing_job_id=run.analysis_job_id if run is not None else None,
                )
                if repo_path is None:
                    return  # status already set to failed

            # Phase 3: target-team build (skip submit if se_job_id stored)
            success = _run_target_team(
                client,
                agent,
                store,
                run_id,
                repo_path,
                adapter,
                existing_job_id=run.se_job_id if run is not None else None,
            )
            if success:
                store.update_run(run_id, status="completed")
                _sync_job_status(run_id, "completed", phase="completed")
                logger.info("Founder workflow completed successfully: run_id=%s", run_id)

    except Exception as exc:
        logger.exception("Founder workflow crashed: run_id=%s", run_id)
        store.update_run(run_id, status="failed", error=str(exc)[:1000])
        _sync_job_status(run_id, "failed", error=str(exc)[:500])
        store.add_chat_message(
            run_id, "system", f"Workflow failed: {str(exc)[:500]}", "status_update"
        )
