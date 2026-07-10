"""Temporal worker bootstrap for the SOC2 compliance team.

Exposes a no-arg ``start_soc2_temporal_worker_thread`` that the generic
team_service entrypoint invokes at boot via the ``TEAM_TEMPORAL_WORKER_MODULE`` /
``TEAM_TEMPORAL_WORKER_FUNC`` env vars, so the Temporal worker (and its connected
client) is ready before uvicorn starts accepting requests. Delegates to the
shared :func:`shared_temporal.start_team_worker`, which installs the shared
sandbox passthroughs and gzip payload codec and is idempotent per team.
"""

from __future__ import annotations

import logging

from shared_temporal import is_temporal_enabled, start_team_worker
from soc2_compliance_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS

logger = logging.getLogger(__name__)

# Team key for the per-team worker registry. Matches the slug registered in
# ``shared_temporal.teams_registry`` so double-starts are idempotent.
TEAM_KEY = "soc2_compliance"


def start_soc2_temporal_worker_thread() -> bool:
    """Start the SOC2 Temporal worker (no-op when disabled).

    Postconditions:
        - Returns ``True`` if a worker thread is running (or already running),
          ``False`` when Temporal is disabled (``TEMPORAL_ADDRESS`` unset).

    Safe to call multiple times — the underlying ``start_team_worker`` is
    idempotent per team.
    """
    if not is_temporal_enabled():
        return False
    return start_team_worker(
        TEAM_KEY,
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )
