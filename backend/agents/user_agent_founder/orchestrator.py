"""Background workflow orchestrator for the founder agent.

Runs the full lifecycle: spec generation -> product analysis -> SE team execution,
answering all questions autonomously through the founder persona.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx

from job_service_client import JobServiceClient
from llm_service import LLMJsonParseError, LLMSchemaValidationError
from user_agent_founder.agent import FounderAgent
from user_agent_founder.store import FounderRunStore

logger = logging.getLogger(__name__)

_job_client = JobServiceClient(team="user_agent_founder")

UNIFIED_API_BASE = os.environ.get("UNIFIED_API_BASE_URL", "http://localhost:8080")
SE_PREFIX = "/api/software-engineering"

ANALYSIS_POLL_INTERVAL = int(os.environ.get("FOUNDER_ANALYSIS_POLL_SECONDS", "15"))
EXECUTION_POLL_INTERVAL = int(os.environ.get("FOUNDER_EXECUTION_POLL_SECONDS", "30"))
MAX_POLL_ATTEMPTS = int(os.environ.get("FOUNDER_MAX_POLL_ATTEMPTS", "480"))  # ~4h at 30s
MAX_ANSWER_RETRIES = int(os.environ.get("FOUNDER_MAX_ANSWER_RETRIES", "2"))
ANSWER_POST_RETRIES = 3
ANSWER_POST_BACKOFF_BASE = 2  # seconds

# httpx timeout: generous because SE team operations can be slow
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _se_url(path: str) -> str:
    return f"{UNIFIED_API_BASE}{SE_PREFIX}{path}"


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
    client: httpx.Client,
    agent: FounderAgent,
    store: FounderRunStore,
    run_id: str,
    job_id: str,
    questions: list[dict[str, Any]],
    endpoint_prefix: str,
) -> bool:
    """Use the founder agent to answer all pending questions and submit them.

    Returns True if answers were successfully submitted, False on failure.
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
            metadata={"question_id": q["id"], "selected_option_id": result.get("selected_option_id")},
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
        resp = client.post(
            _se_url(f"{endpoint_prefix}/{job_id}/answers"),
            json={"answers": answers},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code < 400:
            logger.info("Successfully submitted %d answers for job %s", len(answers), job_id)
            return True
        logger.warning(
            "Answer submission attempt %d/%d failed for job %s: %s %s",
            attempt + 1,
            ANSWER_POST_RETRIES,
            job_id,
            resp.status_code,
            resp.text[:500],
        )
        if attempt < ANSWER_POST_RETRIES - 1:
            time.sleep(ANSWER_POST_BACKOFF_BASE ** (attempt + 1))

    store.add_chat_message(
        run_id=run_id,
        role="system",
        content=f"Failed to submit answers to SE team after {ANSWER_POST_RETRIES} attempts.",
        message_type="status_update",
    )
    return False


def _run_product_analysis(
    client: httpx.Client,
    agent: FounderAgent,
    store: FounderRunStore,
    run_id: str,
    spec_content: str,
) -> str | None:
    """Submit spec for product analysis and poll until complete. Returns repo_path or None."""
    store.update_run(run_id, status="submitting_analysis")
    _sync_job_status(run_id, "running", phase="submitting_analysis")

    resp = client.post(
        _se_url("/product-analysis/start-from-spec"),
        json={"project_name": f"user-agent-founder-{run_id}", "spec_content": spec_content},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code >= 400:
        store.update_run(
            run_id,
            status="failed",
            error=f"Failed to start analysis: {resp.status_code} {resp.text[:500]}",
        )
        _sync_job_status(run_id, "failed", error="Failed to start analysis")
        return None

    data = resp.json()
    analysis_job_id = data.get("job_id")
    store.update_run(run_id, analysis_job_id=analysis_job_id, status="polling_analysis")
    _sync_job_status(run_id, "running", phase="polling_analysis")
    logger.info("Product analysis started: job_id=%s", analysis_job_id)
    store.add_chat_message(run_id, "system", f"Product analysis started (job: {analysis_job_id})", "status_update")

    failed_question_sets: dict[frozenset[str], int] = {}  # qset -> attempt count

    for _ in range(MAX_POLL_ATTEMPTS):
        time.sleep(ANALYSIS_POLL_INTERVAL)
        _heartbeat(run_id)

        resp = client.get(
            _se_url(f"/product-analysis/status/{analysis_job_id}"),
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.warning("Analysis poll error: %s", resp.status_code)
            continue

        status_data = resp.json()
        status = status_data.get("status", "")

        # Answer pending questions
        if status_data.get("waiting_for_answers") and status_data.get("pending_questions"):
            pending = status_data["pending_questions"]
            qset = frozenset(q.get("id", "") for q in pending)
            prior_failures = failed_question_sets.get(qset, 0)

            if prior_failures > MAX_ANSWER_RETRIES:
                err = f"Answer submission failed {prior_failures} times for analysis questions. Aborting."
                logger.error(err)
                store.update_run(run_id, status="failed", error=err)
                _sync_job_status(run_id, "failed", error=err)
                store.add_chat_message(run_id, "system", err, "status_update")
                return None

            store.update_run(run_id, status="answering_analysis_questions")
            store.add_chat_message(
                run_id, "system", f"SE team has {len(pending)} question(s) during analysis.",
                "question_received", metadata={"question_ids": list(qset)},
            )
            success = _answer_pending_questions(
                client, agent, store, run_id, analysis_job_id, pending, "/product-analysis",
            )
            if not success:
                failed_question_sets[qset] = prior_failures + 1
                logger.warning("Answer attempt %d failed for analysis questions", prior_failures + 1)
            continue

        if status == "completed":
            repo_path = status_data.get("repo_path")
            store.update_run(run_id, repo_path=repo_path)
            logger.info("Product analysis completed: repo_path=%s", repo_path)
            store.add_chat_message(
                run_id, "system", "Analysis complete. Starting SE team build.", "status_update",
            )
            return repo_path

        if status == "failed":
            err = f"Product analysis failed: {status_data.get('error', 'unknown')}"
            store.update_run(run_id, status="failed", error=err)
            _sync_job_status(run_id, "failed", error="Product analysis failed")
            store.add_chat_message(run_id, "system", err, "status_update")
            return None

    store.update_run(run_id, status="failed", error="Product analysis timed out")
    _sync_job_status(run_id, "failed", error="Product analysis timed out")
    store.add_chat_message(run_id, "system", "Product analysis timed out.", "status_update")
    return None


def _run_se_team(
    client: httpx.Client,
    agent: FounderAgent,
    store: FounderRunStore,
    run_id: str,
    repo_path: str,
) -> bool:
    """Start the SE team build and poll until complete. Returns True on success."""
    store.update_run(run_id, status="submitting_build")
    _sync_job_status(run_id, "running", phase="submitting_build")

    resp = client.post(
        _se_url("/run-team"),
        json={"repo_path": repo_path},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code >= 400:
        store.update_run(
            run_id,
            status="failed",
            error=f"Failed to start SE team: {resp.status_code} {resp.text[:500]}",
        )
        _sync_job_status(run_id, "failed", error="Failed to start SE team")
        return False

    data = resp.json()
    se_job_id = data.get("job_id")
    store.update_run(run_id, se_job_id=se_job_id, status="polling_build")
    _sync_job_status(run_id, "running", phase="polling_build")
    logger.info("SE team build started: job_id=%s", se_job_id)
    store.add_chat_message(run_id, "system", f"SE team build started (job: {se_job_id})", "status_update")

    failed_question_sets: dict[frozenset[str], int] = {}

    for _ in range(MAX_POLL_ATTEMPTS):
        time.sleep(EXECUTION_POLL_INTERVAL)
        _heartbeat(run_id)

        resp = client.get(
            _se_url(f"/run-team/{se_job_id}"),
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.warning("Build poll error: %s", resp.status_code)
            continue

        status_data = resp.json()
        status = status_data.get("status", "")

        # Answer pending questions
        if status_data.get("waiting_for_answers") and status_data.get("pending_questions"):
            pending = status_data["pending_questions"]
            qset = frozenset(q.get("id", "") for q in pending)
            prior_failures = failed_question_sets.get(qset, 0)

            if prior_failures > MAX_ANSWER_RETRIES:
                err = f"Answer submission failed {prior_failures} times for build questions. Aborting."
                logger.error(err)
                store.update_run(run_id, status="failed", error=err)
                _sync_job_status(run_id, "failed", error=err)
                store.add_chat_message(run_id, "system", err, "status_update")
                return False

            store.update_run(run_id, status="answering_build_questions")
            store.add_chat_message(
                run_id, "system", f"SE team has {len(pending)} question(s) during build.",
                "question_received", metadata={"question_ids": list(qset)},
            )
            success = _answer_pending_questions(
                client, agent, store, run_id, se_job_id, pending, "/run-team",
            )
            if not success:
                failed_question_sets[qset] = prior_failures + 1
                logger.warning("Answer attempt %d failed for build questions", prior_failures + 1)
            continue

        if status == "completed":
            logger.info("SE team build completed for run %s", run_id)
            store.add_chat_message(run_id, "system", "Build completed successfully.", "status_update")
            return True

        if status == "failed":
            err = f"SE team build failed: {status_data.get('error', 'unknown')}"
            store.update_run(run_id, status="failed", error=err)
            _sync_job_status(run_id, "failed", error="SE team build failed")
            store.add_chat_message(run_id, "system", err, "status_update")
            return False

        if status == "cancelled":
            store.update_run(run_id, status="failed", error="SE team build was cancelled")
            _sync_job_status(run_id, "failed", error="SE team build cancelled")
            store.add_chat_message(run_id, "system", "SE team build was cancelled.", "status_update")
            return False

    store.update_run(run_id, status="failed", error="SE team build timed out")
    _sync_job_status(run_id, "failed", error="SE team build timed out")
    store.add_chat_message(run_id, "system", "SE team build timed out.", "status_update")
    return False


def run_workflow(run_id: str, store: FounderRunStore, agent: FounderAgent) -> None:
    """Execute the full founder workflow: spec -> analysis -> build.

    This function is designed to run in a background thread.
    """
    logger.info("Starting founder workflow: run_id=%s", run_id)

    try:
        # Phase 1: Generate the product spec
        store.update_run(run_id, status="generating_spec")
        _sync_job_status(run_id, "running", phase="generating_spec")
        store.add_chat_message(run_id, "system", "Generating product specification...", "status_update")
        spec_content = _generate_spec_with_heartbeat(agent, run_id)
        store.update_run(run_id, spec_content=spec_content)
        logger.info("Spec generated for run %s (%d chars)", run_id, len(spec_content))
        store.add_chat_message(
            run_id, "assistant",
            f"Product spec generated ({len(spec_content)} chars). Submitting to SE team for analysis.",
            "status_update",
        )

        with httpx.Client() as client:
            # Phase 2: Product analysis
            repo_path = _run_product_analysis(client, agent, store, run_id, spec_content)
            if repo_path is None:
                return  # status already set to failed

            # Phase 3: SE team build
            success = _run_se_team(client, agent, store, run_id, repo_path)
            if success:
                store.update_run(run_id, status="completed")
                _sync_job_status(run_id, "completed", phase="completed")
                logger.info("Founder workflow completed successfully: run_id=%s", run_id)

    except Exception as exc:
        logger.exception("Founder workflow crashed: run_id=%s", run_id)
        store.update_run(run_id, status="failed", error=str(exc)[:1000])
        _sync_job_status(run_id, "failed", error=str(exc)[:500])
        store.add_chat_message(run_id, "system", f"Workflow failed: {str(exc)[:500]}", "status_update")
