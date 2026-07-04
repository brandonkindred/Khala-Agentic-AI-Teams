"""Temporal worker boot for the coding team.

``start_coding_team_temporal_worker_thread`` is the no-arg contract the generic
team_service entrypoint invokes via ``TEAM_TEMPORAL_WORKER_MODULE`` /
``TEAM_TEMPORAL_WORKER_FUNC`` (matching ``agent_provisioning_team`` and
``user_agent_founder``). It runs once per uvicorn worker process, after the
``coding_team_service`` composition root has installed the ``CodeEngineProvider``
into that process — so activities executed by this worker find a provider.

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
    from coding_team.temporal import ACTIVITIES, WORKFLOWS
    from coding_team.temporal.constants import TASK_QUEUE
    from shared_temporal import is_temporal_enabled, start_team_worker

    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "coding_team",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
