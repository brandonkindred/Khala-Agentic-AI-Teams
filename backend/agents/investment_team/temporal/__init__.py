"""Temporal workflows + worker wiring for the investment team.

Mirrors ``agent_team_studio.user_agent_founder.temporal``: workflow/activity definitions live in
:mod:`workflows` (sandbox-safe), and worker startup lives in :mod:`worker`,
invoked by the generic team_service entrypoint at boot via the
``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC`` env vars.

Importing this package has NO side effects. In particular it must not call
``is_temporal_enabled()`` / ``os.getenv`` at module level: doing so both races
the first request (the worker connects its client asynchronously) and trips the
temporalio workflow sandbox when it re-imports the module to register the
workflows. Worker startup is therefore the entrypoint's job, never an
import-time side effect.
"""

from __future__ import annotations

from investment_team.temporal.advisory import (
    ADVISORY_ACTIVITIES,
    ADVISORY_TASK_QUEUE,
    ADVISORY_WORKFLOW_ID_PREFIX,
    ADVISORY_WORKFLOWS,
)
from investment_team.temporal.paper_trading import (
    PaperTradingWorkflow,
    mark_paper_trading_stopped_activity,
    run_paper_trading_activity,
)
from investment_team.temporal.workflows import (
    InvestmentBacktestWorkflow,
    mark_backtest_job_cancelled_activity,
    run_backtest_activity,
)

# The Strategy Lab batch run moved to the fine-grained
# ``investment_team.strategy_lab.temporal`` package (``strategy-lab-queue``); this
# coarse ``investment-queue`` now serves the ad hoc single-backtest workflow and
# the long-running paper-trading workflow (both coarse, one long activity each).
WORKFLOWS = [InvestmentBacktestWorkflow, PaperTradingWorkflow]
ACTIVITIES = [
    run_backtest_activity,
    mark_backtest_job_cancelled_activity,
    run_paper_trading_activity,
    mark_paper_trading_stopped_activity,
]
TASK_QUEUE = "investment-queue"
WORKFLOW_ID_PREFIX = "investment-"

__all__ = [
    "ACTIVITIES",
    "ADVISORY_ACTIVITIES",
    "ADVISORY_TASK_QUEUE",
    "ADVISORY_WORKFLOWS",
    "ADVISORY_WORKFLOW_ID_PREFIX",
    "InvestmentBacktestWorkflow",
    "PaperTradingWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "mark_backtest_job_cancelled_activity",
    "mark_paper_trading_stopped_activity",
    "run_backtest_activity",
    "run_paper_trading_activity",
]
