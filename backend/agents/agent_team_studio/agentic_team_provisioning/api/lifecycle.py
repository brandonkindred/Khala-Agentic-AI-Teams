"""ASGI startup hook for the agentic team provisioning app.

Passed to ``create_team_app`` by ``main`` as ``on_startup``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _startup() -> None:
    """Start the Temporal worker backstop, then run one-time service initialization.

    The team_service entrypoint normally starts the worker via
    ``TEAM_TEMPORAL_WORKER_MODULE`` before uvicorn accepts requests; this backstop
    covers running the app standalone (``uvicorn ...:app``).

    Preconditions:
        - None (safe to call once at app startup).

    Postconditions:
        - Starts the worker thread when Temporal is enabled; a no-op when
          ``TEMPORAL_ADDRESS`` is unset. Never raises — any failure is logged as a
          warning so it cannot abort app boot (this runs as an ``on_startup`` hook).
        - Calls :func:`initialize_service`, which performs retroactive team
          provisioning/registry registration and orphaned pipeline-run reaping.
          Also never raises (see its own contract).
    """
    try:
        from agent_team_studio.agentic_team_provisioning.temporal.worker import (
            start_agentic_team_provisioning_temporal_worker_thread,
        )

        start_agentic_team_provisioning_temporal_worker_thread()
    except Exception:
        logger.warning(
            "agentic_team_provisioning Temporal worker start (lifespan backstop) failed",
            exc_info=True,
        )
    from agent_team_studio.agentic_team_provisioning.api.state import initialize_service

    initialize_service()
