"""Temporal workflows + worker wiring for the investment team.

Mirrors ``user_agent_founder.temporal``: workflow/activity definitions live in
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

from investment_team.temporal.workflows import (
    InvestmentBacktestWorkflow,
    InvestmentStrategyLabWorkflow,
    run_backtest_activity,
    run_strategy_lab_activity,
)

WORKFLOWS = [InvestmentStrategyLabWorkflow, InvestmentBacktestWorkflow]
ACTIVITIES = [run_strategy_lab_activity, run_backtest_activity]
TASK_QUEUE = "investment-queue"
WORKFLOW_ID_PREFIX = "investment-"

__all__ = [
    "ACTIVITIES",
    "InvestmentBacktestWorkflow",
    "InvestmentStrategyLabWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "run_backtest_activity",
    "run_strategy_lab_activity",
]
