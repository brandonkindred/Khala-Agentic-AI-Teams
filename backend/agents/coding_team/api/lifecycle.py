"""ASGI startup probe for the coding_team app.

Makes a mis-wired deployment (no ``CodeEngineProvider``) visible at boot rather
than per-job. Passed to ``create_team_app`` by ``main``.

Invariants:
    - The probe reads ``get_engine_provider`` through the ``main`` hub so that a
      test which patches ``main.get_engine_provider`` still governs the boot
      check exactly as it did before the api/main.py split.
"""

import logging

from coding_team.api import main as _main

logger = logging.getLogger(__name__)


def _warn_if_no_engine_provider() -> None:
    """Startup probe: make a mis-wired deployment visible at boot, not per-job.

    The standalone container must run via ``coding_team_service.main`` (its
    ``TEAM_MODULE``), which installs the SE-backed engine provider before this
    app is imported. A process serving this module directly still boots (tests
    and embedded uses rely on that), but every /run and /review-pr job will fail
    without a provider — so say it loudly once at startup.

    Postconditions: logs an ERROR when no provider is installed; never raises.
    """
    if _main.get_engine_provider() is None:
        logger.error(
            "No CodeEngineProvider installed at startup: /run and /review-pr jobs will fail. "
            "Serve the coding team via coding_team_service.main (TEAM_MODULE) or call "
            "coding_team.engine_provider.set_engine_provider() before serving traffic."
        )
