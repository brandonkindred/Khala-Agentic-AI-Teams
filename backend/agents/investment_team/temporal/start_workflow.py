"""Start investment Temporal workflows from synchronous API code.

Thin wrappers over ``shared_temporal.start_workflow_sync`` — the shared
sync→async bridge that waits for the worker's connected client, then schedules
``client.start_workflow`` on the worker loop. These helpers do NOT touch the job
store: the API handlers own their own run/job bookkeeping (active-run registry,
``_persist_run_state`` / ``_bt_create_job``).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from investment_team.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
    InvestmentBacktestWorkflow,
)
from shared_temporal import start_workflow_sync

logger = logging.getLogger(__name__)

# The Strategy Lab run is now started via
# ``investment_team.strategy_lab.temporal.start_workflow.start_strategy_lab_batch_workflow``
# (the fine-grained ``StrategyLabBatchWorkflow`` on ``strategy-lab-queue``); the
# old coarse ``start_strategy_lab_workflow`` here has been removed in the cutover.


def start_backtest_workflow(
    job_id: str,
    strategy: Any,
    config: Any,
    submitted_by: str,
    notes: list[str],
) -> None:
    """Start :class:`InvestmentBacktestWorkflow` for a backtest job.

    Preconditions:
        - ``job_id`` is non-empty and already created in the job store.
        - ``strategy`` / ``config`` are ``StrategySpec`` / ``BacktestConfig``
          instances (expose ``model_dump``).
        - Temporal is enabled and the investment worker is running.

    Postconditions:
        - The workflow is started under id ``investment-bt-{job_id}`` on the
          investment task queue. Raises ``RuntimeError`` if the worker client
          never becomes available.
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}bt-{job_id}"
    start_workflow_sync(
        InvestmentBacktestWorkflow.run,
        job_id,
        strategy.model_dump(mode="json"),
        config.model_dump(mode="json"),
        submitted_by,
        notes,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started InvestmentBacktestWorkflow id=%s", workflow_id)


def start_paper_trading_workflow(session_id: str, payload: dict[str, Any]) -> None:
    """Start :class:`PaperTradingWorkflow` for a paper-trading session (fire-and-forget).

    Preconditions:
        - ``session_id`` names a session already created in ``running`` status.
        - ``payload`` satisfies ``run_paper_trading_activity``'s preconditions.
        - Temporal is enabled and the investment worker is running.

    Postconditions:
        - The workflow is started under id ``investment-pt-{session_id}`` on the
          investment task queue. Raises ``RuntimeError`` if the worker client
          never becomes available.
    """
    from investment_team.temporal import TASK_QUEUE, WORKFLOW_ID_PREFIX
    from investment_team.temporal.paper_trading import PaperTradingWorkflow
    from shared_temporal import start_workflow_sync

    workflow_id = f"{WORKFLOW_ID_PREFIX}pt-{session_id}"
    start_workflow_sync(
        PaperTradingWorkflow.run,
        payload,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started InvestmentPaperTradingWorkflow id=%s", workflow_id)


def signal_paper_trading_stop(session_id: str) -> None:
    """Signal :class:`PaperTradingWorkflow` to stop a running session (idempotent).

    Preconditions:
        - ``session_id`` names a session whose ``PaperTradingWorkflow`` was
          started via :func:`start_paper_trading_workflow`.

    Postconditions:
        - The ``stop`` signal is delivered to workflow id
          ``investment-pt-{session_id}``; the running session terminates at the
          next bar. Raises ``RuntimeError`` if the worker client is unavailable.
    """
    from investment_team.temporal import WORKFLOW_ID_PREFIX
    from shared_temporal import signal_workflow_sync

    workflow_id = f"{WORKFLOW_ID_PREFIX}pt-{session_id}"
    signal_workflow_sync(workflow_id, "stop")
    logger.info("Signalled stop to InvestmentPaperTradingWorkflow id=%s", workflow_id)


# Maps the logical advisory operation to its workflow class. Kept as string keys
# so the FastAPI routes stay decoupled from the workflow classes (which pull in
# temporalio); resolved lazily inside :func:`execute_advisory_workflow`.
_ADVISORY_OPS = (
    "create_proposal",
    "validate_proposal",
    "create_strategy",
    "validate_strategy",
    "promotion_decision",
    "committee_memo",
    "advisor_start",
    "advisor_message",
    "advisor_complete",
)


def execute_advisory_workflow(op: str, payload: dict[str, Any], *, key: str) -> dict[str, Any]:
    """Execute an interactive advisory workflow and return its result (execute-and-wait).

    Preconditions:
        - ``op`` is one of :data:`_ADVISORY_OPS`.
        - ``payload`` satisfies the corresponding activity's preconditions.
        - ``key`` is a caller-supplied label for this logical operation
          (proposal/strategy/session id, or a request-derived key) used only for
          the workflow id's human-readable prefix — it is NOT relied on for
          uniqueness (see below).
        - Temporal is enabled and the advisory worker is running.

    Postconditions:
        - Runs ``Investment<Op>Workflow`` on ``investment-advisory-queue`` under
          id ``investment-adv-{op}-{key}-{uuid8}`` — a fresh random suffix is
          appended on every call so two calls for the same ``(op, key)`` (e.g.
          two chat messages in the same advisor session, or a client retry)
          never collide on a live workflow id and raise
          ``WorkflowAlreadyStartedError``; ``execute_workflow_sync`` documents
          this uniqueness requirement. Raises ``ValueError`` for an unknown
          ``op``; propagates the workflow's failure (e.g. a wrapped
          ``ApplicationError``) on error.
    """
    from investment_team.temporal.advisory import (
        _ADVISORY_TIMEOUT,
        ADVISORY_TASK_QUEUE,
        ADVISORY_WORKFLOW_ID_PREFIX,
        AdvisorCompleteWorkflow,
        AdvisorMessageWorkflow,
        AdvisorStartWorkflow,
        CommitteeMemoWorkflow,
        CreateProposalWorkflow,
        CreateStrategyWorkflow,
        PromotionDecisionWorkflow,
        ValidateProposalWorkflow,
        ValidateStrategyWorkflow,
    )
    from shared_temporal import execute_workflow_sync

    workflows = {
        "create_proposal": CreateProposalWorkflow,
        "validate_proposal": ValidateProposalWorkflow,
        "create_strategy": CreateStrategyWorkflow,
        "validate_strategy": ValidateStrategyWorkflow,
        "promotion_decision": PromotionDecisionWorkflow,
        "committee_memo": CommitteeMemoWorkflow,
        "advisor_start": AdvisorStartWorkflow,
        "advisor_message": AdvisorMessageWorkflow,
        "advisor_complete": AdvisorCompleteWorkflow,
    }
    workflow_cls = workflows.get(op)
    if workflow_cls is None:
        raise ValueError(f"unknown advisory op: {op}")
    workflow_id = f"{ADVISORY_WORKFLOW_ID_PREFIX}{op}-{key}-{uuid.uuid4().hex[:8]}"
    # The activity's own start_to_close_timeout already bounds a single attempt
    # (_ADVISORY_RETRY caps retries at 1); give the execute-and-wait call a
    # modest buffer above that ceiling rather than the shared 300s default, so
    # a genuinely hung worker fails this interactive call well under 5 minutes.
    execute_timeout_s = _ADVISORY_TIMEOUT.total_seconds() + 60.0
    return execute_workflow_sync(
        workflow_cls.run,
        payload,
        workflow_id=workflow_id,
        task_queue=ADVISORY_TASK_QUEUE,
        execute_timeout_s=execute_timeout_s,
    )
