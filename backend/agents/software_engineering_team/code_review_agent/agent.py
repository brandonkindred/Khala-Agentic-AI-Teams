"""Code Review agent: reviews code against spec, standards, and conventions.

``CodeReviewAgent.run`` always delegates to the map-reduce coordinator
(`coordinator.run_coordinator`), which bounds every LLM call independently of
input size, re-anchors line numbers from split segments, re-checks each genuine
finding against the whole submission to drop false positives the bounded chunk
review could not have caught (`filter_false_positives`), and applies the
deterministic approval gate with its anti-loop safety nets. A chunk that
cannot be reviewed after recovery degrades gracefully rather than aborting the
run: by default it is surfaced only non-blockingly (never posted, never
affecting ``approved``) via ``CodeReviewOutput.not_reviewed_ranges`` on the
in-process path (``coordinator.run_coordinator``'s
``CODE_REVIEW_BLOCK_ON_UNREVIEWED``, unset by default); the Temporal-dispatched
path (this class's default when not constructed with ``force_in_process=True``)
currently always folds not-reviewed ranges into the approval gate as a blocking
``high`` finding regardless of that env var, so the same input can produce a
different ``approved`` verdict depending on dispatch mode — see
``temporal/activities.py``'s reduce activity. An infrastructure failure or a
run in which no chunk could be reviewed at all raises
``CodeReviewUnavailableError``, and an unexpected reviewer defect propagates
unchanged (fails closed).
"""

from __future__ import annotations

import logging
import uuid

from llm_service import get_client, llm_attribution

from .coordinator import run_coordinator
from .models import (
    CodeReviewInput,
    CodeReviewOutput,
    CodeReviewUnavailableError,
    ReviewProgressCallback,
    notify_review_progress,
)
from .repo_reader import RepoReader, disk_repo_reader_from_root

logger = logging.getLogger(__name__)

# Bounded walk so a cyclic/adversarial cause chain can never loop forever.
_MAX_CAUSE_DEPTH = 12

# RuntimeError message substrings that mean dispatch never actually started —
# the only conditions ``_run_via_temporal`` may reclassify as
# dispatch-unavailable; any other ``RuntimeError`` must propagate unchanged
# rather than being silently downgraded into an in-process fallback.
#   - "Temporal client not available": ``_await_client``
#     (``shared/temporal/runner.py``) raises this when no worker client became
#     available within its wait window.
#   - "Event loop is closed": the worker can exit and close its loop in the
#     window between ``_await_client`` returning it and
#     ``execute_code_review_workflow_sync`` scheduling the workflow coroutine
#     onto it (``asyncio.run_coroutine_threadsafe`` raises this synchronously
#     when the target loop is closed) — see ``shared/temporal/worker.py``'s
#     shutdown handling of this same race.
_CLIENT_UNAVAILABLE_MARKERS = ("Temporal client not available", "Event loop is closed")


def _reports_review_unavailable(exc: BaseException, marker: str) -> bool:
    """Walk an exception's cause chain for the code-review "unavailable" marker.

    Temporal surfaces a workflow failure differently depending on where the
    marker was raised: the workflow's own total-failure guard puts the
    ``ApplicationError`` at the top of the chain, while an infra failure a
    map/verify activity raised is nested one level deeper under an
    ``ActivityError``. Both spell the marker as ``ApplicationError.type`` (or the
    original exception's class name), so this walks ``cause``/``__cause__``/
    ``__context__`` up to a bounded depth.

    Postconditions:
        - Returns ``True`` iff some node in the chain carries ``type == marker``
          or is itself named ``marker``. Never raises; bounded and cycle-safe.
    """
    seen: set[int] = set()
    node: BaseException | None = exc
    depth = 0
    while node is not None and id(node) not in seen and depth < _MAX_CAUSE_DEPTH:
        seen.add(id(node))
        depth += 1
        if getattr(node, "type", None) == marker or type(node).__name__ == marker:
            return True
        node = getattr(node, "cause", None) or node.__cause__ or node.__context__
    return False


def _code_review_temporal_enabled() -> bool:
    """Whether to dispatch reviews to Temporal, defaulting off if the layer is absent.

    Importing the temporal package pulls in ``temporalio``; if it (or the temporal
    layer) cannot be imported for any reason, code review must still work in
    thread mode, so any import failure resolves to ``False``.
    """
    try:
        from .temporal.config import code_review_temporal_enabled
    except Exception:  # noqa: BLE001 - a missing/broken temporal layer must not break reviews
        logger.debug("Code review temporal layer unavailable; using in-process mode", exc_info=True)
        return False
    return code_review_temporal_enabled()


class CodeReviewAgent:
    """
    Code review agent that reviews code produced by coding agents
    against the project specification, coding standards, and conventions.

    Returns approval or a list of issues that must be resolved.

    Invariants:
        - Every ``run`` call goes through the map-reduce coordinator, so review
          prompts stay bounded regardless of how much code is submitted.
    """

    def __init__(self, llm_client=None, *, force_in_process: bool = False) -> None:
        # The chunk reviewer resolves its own strands model per call; this
        # client is used for context sizing and shared-context compaction.
        self.llm = llm_client if llm_client is not None else get_client("code_review")
        # When True, run() always uses the in-process coordinator — required for
        # callers already inside a Temporal activity so review never nests a
        # child workflow on the same worker (nested-workflow deadlock risk).
        self._force_in_process = bool(force_in_process)

    def run(
        self,
        input_data: CodeReviewInput,
        progress_callback: ReviewProgressCallback | None = None,
        repo_reader: RepoReader | None = None,
    ) -> CodeReviewOutput:
        """Review code and return approval or issues.

        Preconditions:
            - ``input_data`` carries the code under review via ``files`` or ``code``.
            - ``progress_callback`` is None or satisfies the
              ``ReviewProgressCallback`` contract (non-raising, accepts
              ``(step, detail, fraction)``).
            - ``repo_reader`` is None or a ``repo_reader.RepoReader`` giving the
              false-positive verifier whole-repo read access (so it can confirm a
              file/module a finding calls missing already exists outside the diff).
              When it is None and ``input_data.repo_root`` names a disk checkout,
              the in-process path rebuilds a ``DiskRepoReader`` from that path so
              a live reader and a serialized ``repo_root`` grant the same access.

        Postconditions:
            - Returns the coordinator's merged verdict covering every submitted
              line; ``approved is False`` implies at least one critical/high issue.
              A chunk unreviewable after recovery degrades gracefully rather than
              failing the run: on the in-process path it is non-blocking by
              default (surfaced only via ``not_reviewed_ranges``; set
              ``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` to restore the legacy blocking
              ``high`` "not reviewed" finding), while the Temporal-dispatched
              path (the default unless this instance was constructed with
              ``force_in_process=True``) currently always treats it as blocking
              regardless of that env var — see the module docstring.
            - Findings that the verifier confirms are false positives (judged
              against the full codebase) are absent from the result; the
              not-reviewed coverage findings are never removed this way.
            - When ``progress_callback`` is provided, it is invoked with
              non-decreasing fractions ending at 1.0 (step ``done``) on every
              successful return; the review result is identical whether or not
              a callback is provided.
            - When this instance was constructed with ``force_in_process=True``,
              never starts a Temporal worker or child workflow; always uses the
              in-process coordinator.

        Raises:
            CodeReviewUnavailableError: when the review could not be completed —
                the model was unavailable (an infrastructure failure), or no
                chunk could be reviewed at all. Callers must treat this as a
                failed review run — never as review feedback for the coding agent.
            TimeoutError: when dispatched to Temporal (the default) and the
                synchronous wait for the durable workflow's result exceeds
                ``CODE_REVIEW_EXECUTE_TIMEOUT_S`` — see ``_run_via_temporal``.
        """
        code_size = sum(len(c) for c in input_data.files.values())
        logger.info(
            "CodeReview: reviewing %s chars of %s code | task=%s | has_spec=%s | has_architecture=%s | acceptance_criteria=%s",
            code_size,
            input_data.language,
            input_data.task_description[:80] if input_data.task_description else "",
            bool(input_data.spec_content),
            input_data.architecture is not None,
            len(input_data.acceptance_criteria),
        )
        # Temporal is the default execution mode for the code review agent (see
        # ``temporal/config.py``). Dispatch the durable ``CodeReviewWorkflow`` when
        # enabled; fall back to the in-process coordinator only when Temporal is
        # explicitly disabled (address sentinel), force_in_process is set
        # (Temporal activity callers), or Temporal dispatch is unavailable.
        if not self._force_in_process and _code_review_temporal_enabled():
            try:
                return self._run_via_temporal(input_data, progress_callback)
            except CodeReviewUnavailableError:
                # A real review failure — never silently downgrade to thread mode,
                # or a resubmit could mask the failure as feedback.
                raise
            except _TemporalDispatchUnavailable as exc:
                logger.warning(
                    "CodeReview: Temporal dispatch unavailable (%s); "
                    "falling back to in-process review",
                    exc,
                )
        # A live reader wins; otherwise rebuild one from the serializable
        # ``repo_root`` so the in-process path grants the same off-diff read access
        # as the Temporal activities (which reconstruct from the same field). Both
        # channels are fail-safe — a missing path yields None (keep-more).
        effective_reader = repo_reader
        if effective_reader is None:
            effective_reader = disk_repo_reader_from_root(input_data.repo_root)
        # Binds job_id for the whole in-process run so every LLM call site can
        # record its prompt/response into that job's durable transcript (see
        # ``transcript.record_transcript_entry``); ``shared.concurrency.parallel_map``
        # propagates this context into the map phase's and tail passes' worker
        # threads by default, so no call site below needs job_id threaded through
        # its own parameters. A blank ``input_data.job_id`` (no caller-tracked job)
        # makes every record a no-op. Not honored on the Temporal-dispatched path:
        # attribution is a contextvar and does not cross the activity boundary.
        with llm_attribution(
            job_id=input_data.job_id, team="software_engineering_team", agent_key="code_review"
        ):
            return run_coordinator(
                self.llm,
                input_data,
                progress_callback=progress_callback,
                repo_reader=effective_reader,
            )

    def _run_via_temporal(
        self,
        input_data: CodeReviewInput,
        progress_callback: ReviewProgressCallback | None,
    ) -> CodeReviewOutput:
        """Execute the review as a durable ``CodeReviewWorkflow`` and return its output.

        Preconditions:
            - Temporal is enabled for the code review agent
              (``code_review_temporal_enabled()``).

        Postconditions:
            - Returns the workflow's ``CodeReviewOutput``. ``progress_callback``, a
              live in-process object that cannot cross the Temporal boundary, is
              invoked at the coarse start/end milestones so its contract
              (non-decreasing, ends at 1.0) still holds; fine-grained progress
              lives on the workflow's ``progress`` query. ``repo_reader`` is
              likewise not forwarded — the false-positive pass runs without
              out-of-diff read access, a strictly keep-more (fail-safe) behavior.

        Raises:
            CodeReviewUnavailableError: the workflow reported it could not review
                the submission (mapped from its ``ApplicationError`` marker).
            _TemporalDispatchUnavailable: dispatch never actually started — the
                worker/client never became available, or the worker's loop closed
                out from under the dispatch call (see ``_CLIENT_UNAVAILABLE_MARKERS``)
                — and the caller falls back to in-process review. Raised only for
                those conditions; any other ``RuntimeError`` from the workflow
                dispatch path propagates unchanged instead of being misclassified.
            TimeoutError: this call's own wait for the workflow result exceeded
                the configured ceiling (``CODE_REVIEW_EXECUTE_TIMEOUT_S``); the
                workflow may still be running, or may have completed, server-side
                (message includes the configured duration — see below).
        """
        from temporalio.client import WorkflowFailureError

        from .temporal.config import WORKFLOW_ID_PREFIX, resolve_execute_timeout_s
        from .temporal.start_workflow import execute_code_review_workflow_sync
        from .temporal.worker import start_code_review_temporal_worker_thread
        from .temporal.workflows import CODE_REVIEW_UNAVAILABLE_TYPE

        notify_review_progress(progress_callback, "preparing", "dispatching durable review", 0.02)
        # Ensure a worker (and the shared client) is running in this process;
        # idempotent per team, so repeated reviews reuse the same worker.
        start_code_review_temporal_worker_thread()

        workflow_id = f"{WORKFLOW_ID_PREFIX}{uuid.uuid4().hex}"
        execute_timeout_s = resolve_execute_timeout_s()
        try:
            result = execute_code_review_workflow_sync(
                input_data.model_dump(mode="json"),
                workflow_id=workflow_id,
                execute_timeout_s=execute_timeout_s,
            )
        except RuntimeError as exc:
            if not any(marker in str(exc) for marker in _CLIENT_UNAVAILABLE_MARKERS):
                # Not a known "dispatch never started" condition — an unexpected
                # failure inside workflow execution must propagate unchanged
                # rather than being misclassified as dispatch-unavailable and
                # silently downgraded to the in-process fallback.
                raise
            # ``_await_client`` raises the "client not available" message when no
            # worker client is available; the worker's own shutdown race can raise
            # "Event loop is closed" instead (see ``_CLIENT_UNAVAILABLE_MARKERS``).
            raise _TemporalDispatchUnavailable(str(exc)) from exc
        except WorkflowFailureError as exc:
            # The unavailable marker may sit at the top of the cause chain (the
            # workflow's own total-failure guard) OR be nested under an
            # ``ActivityError`` (an infra failure a map/verify activity raised),
            # so walk the chain rather than checking only the top-level cause —
            # otherwise an expected reviewer-infrastructure outage would leak as
            # an unexpected ``WorkflowFailureError`` instead of the
            # ``CodeReviewUnavailableError`` callers fail-close on.
            if _reports_review_unavailable(exc, CODE_REVIEW_UNAVAILABLE_TYPE):
                raise CodeReviewUnavailableError(str(exc)) from exc
            raise
        except TimeoutError as exc:
            # The bare TimeoutError concurrent.futures.Future.result raises (==
            # the builtin TimeoutError on Python >= 3.11, the deployed runtime)
            # carries no message (str(exc) == ""). Attach real context here, at
            # the source, so it survives to whatever catches this (pr_review.py's
            # _run_reviewer today) instead of logging as "code review failed: ".
            raise TimeoutError(
                f"Durable code review wait timed out after {execute_timeout_s:.0f}s "
                "waiting for the Temporal workflow result "
                "(code_review_agent/temporal/start_workflow.py); the workflow may "
                "still be running, or may have completed, on the server. This is "
                "a client-side wait timeout, not a reviewer content failure."
            ) from exc

        notify_review_progress(progress_callback, "done", "durable review complete", 1.0)
        return CodeReviewOutput.model_validate(result)


class _TemporalDispatchUnavailable(RuntimeError):
    """The Temporal worker/client was not available to dispatch the review.

    Distinct from ``CodeReviewUnavailableError`` (a real review failure): this
    means the durable path could not even start, so ``CodeReviewAgent.run`` falls
    back to the in-process coordinator rather than failing the review.
    """
