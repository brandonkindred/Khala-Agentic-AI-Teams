"""Thread-mode driver for the SOC2 compliance audit.

Runs the decomposed pipeline (:mod:`soc2_compliance_team.pipeline`) in-process:
load the repo, audit the five Trust Service Criteria concurrently, then
synthesize the report. The Temporal workflow drives the exact same pipeline
steps as durable activities — this orchestrator is the non-Temporal path over
the identical logic.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Callable, TypeVar

from . import pipeline
from .models import SOC2AuditResult

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Thread-mode has no Temporal `start_to_close_timeout` to bound a stalled LLM
# call, so these mirror the equivalent Temporal-mode ceilings
# (temporal/workflows.py's AUDIT_TIMEOUT / REPORT_TIMEOUT) rather than the
# tighter 180s the deleted Strands graph used, which risked false timeouts on
# legitimately long (e.g. thinking-mode) LLM calls.
_CRITERIA_TIMEOUT_SECONDS = 30 * 60
_REPORT_TIMEOUT_SECONDS = 30 * 60


def _run_with_timeout(fn: Callable[[], T], timeout_seconds: float, timeout_message: str) -> T:
    """Run ``fn()`` in a worker thread with a hard wall-clock deadline.

    Postconditions:
        - Returns ``fn()``'s result if it completes within ``timeout_seconds``.
        - Raises ``TimeoutError(timeout_message)`` if it doesn't. Python has no
          safe way to forcibly kill a running thread, so the underlying call
          keeps running in the background and its result is discarded — this
          bounds the CALLER's wait (unblocking the request), not the
          in-flight LLM call itself (which is separately bounded by
          ``llm_service``'s own per-request timeout).
    """
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="soc2-pipeline-step")
    future = pool.submit(fn)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError:
        raise TimeoutError(timeout_message) from None
    finally:
        pool.shutdown(wait=False)


class SOC2AuditOrchestrator:
    """Runs a full SOC2 compliance audit over the shared pipeline steps.

    Five TSC specialist agents run in parallel (via
    :func:`pipeline.run_all_criteria`), then a report writer synthesizes all
    findings into either a compliance report or a next-steps certification
    document.

    Invariants:
        - ``run`` always returns a ``SOC2AuditResult`` (never raises); failures
          surface as ``status="failed"`` with an ``error`` message.
    """

    def run(self, repo_path: str | Path) -> SOC2AuditResult:
        """Execute the full audit on the given repository path.

        Preconditions:
            - ``repo_path`` refers to an existing directory.
        Postconditions:
            - Returns a ``SOC2AuditResult``: ``status="completed"`` with
              per-criterion results and a report/next-steps document on success,
              or ``status="failed"`` with ``error`` set on failure. A failure
              after the criteria audits already succeeded (report synthesis
              failed or timed out) still carries those completed results
              rather than discarding them.
        """
        repo_path = Path(repo_path).resolve()
        logger.info("SOC2 audit starting for repo: %s", repo_path)

        try:
            context = pipeline.load_context(repo_path)
        except Exception as e:
            logger.exception("Failed to load repo context")
            return pipeline.failed_result(repo_path, str(e))

        try:
            tsc_results = _run_with_timeout(
                lambda: pipeline.run_all_criteria(context),
                _CRITERIA_TIMEOUT_SECONDS,
                f"SOC2 criteria audit exceeded {_CRITERIA_TIMEOUT_SECONDS}s",
            )
        except Exception as e:
            logger.exception("SOC2 criteria audit failed")
            return pipeline.failed_result(repo_path, str(e))

        try:
            compliance_report, next_steps_document = _run_with_timeout(
                lambda: pipeline.write_report(str(repo_path), tsc_results),
                _REPORT_TIMEOUT_SECONDS,
                f"SOC2 report synthesis exceeded {_REPORT_TIMEOUT_SECONDS}s",
            )
        except Exception as e:
            logger.exception("SOC2 report synthesis failed")
            return pipeline.failed_result(repo_path, str(e), tsc_results=tsc_results)

        return pipeline.assemble_result(
            str(repo_path), tsc_results, compliance_report, next_steps_document
        )


def run_soc2_audit(repo_path: str | Path) -> SOC2AuditResult:
    """One-shot SOC2 audit over the shared pipeline.

    Preconditions:
        - ``repo_path`` refers to an existing directory. Each pipeline step
          resolves its own LLM client from the provider list, so no client is
          injected here.
    Postconditions:
        - Returns a ``SOC2AuditResult`` (see :meth:`SOC2AuditOrchestrator.run`).
    """
    return SOC2AuditOrchestrator().run(repo_path)
