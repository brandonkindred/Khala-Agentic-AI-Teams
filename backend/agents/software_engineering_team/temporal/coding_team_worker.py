"""Temporal worker boot for the coding team.

``start_coding_team_temporal_worker_thread`` is invoked from SE's own
``_se_startup()`` hook (``software_engineering_team/api/lifecycle.py``), after
that same hook has installed the ``CodeEngineProvider`` into the process — so
activities executed by this worker find a provider. It runs on its own
``coding_team-queue``, independent of SE's main Temporal worker, following the
same pattern as ``software_engineering_team.code_review_agent.temporal.worker``.
It also registers GitHub issue grooming's ``IssueGroomingWorkflow`` and
``run_issue_grooming_activity`` (``temporal.issue_grooming_workflow``) on this
same worker/queue, since grooming has no dedicated boot hook of its own.

Boot lives here (not as a ``temporal/__init__`` import-time side effect) so the
package can be imported by the temporalio workflow sandbox without spinning a
worker thread or calling ``os.getenv`` at module load.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def start_coding_team_temporal_worker_thread() -> bool:
    """Start the coding team Temporal worker (no-op when disabled).

    Preconditions:
        - None. Safe to call multiple times — ``start_team_worker`` is
          idempotent per team, so repeated calls (e.g. across uvicorn workers)
          are harmless.
    Postconditions:
        - Returns True if a worker thread is running (or already running) for
          this team, False when Temporal is disabled (``TEMPORAL_ADDRESS``
          unset).
    """
    from shared.temporal import start_team_worker
    from software_engineering_team.temporal.coding_team_constants import TASK_QUEUE
    from software_engineering_team.temporal.coding_team_workflow import ACTIVITIES, WORKFLOWS
    from software_engineering_team.temporal.issue_grooming_workflow import (
        ACTIVITIES as GROOMING_ACTIVITIES,
    )
    from software_engineering_team.temporal.issue_grooming_workflow import (
        WORKFLOWS as GROOMING_WORKFLOWS,
    )

    return start_team_worker(
        "coding_team",
        WORKFLOWS + GROOMING_WORKFLOWS,
        ACTIVITIES + GROOMING_ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
