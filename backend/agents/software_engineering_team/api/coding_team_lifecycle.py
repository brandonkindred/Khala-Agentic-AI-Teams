"""ASGI startup probe for the coding_team app.

Makes a mis-wired deployment (no ``CodeEngineProvider``) visible at boot rather
than per-job. Passed to ``create_team_app`` by ``main``.

Invariants:
    - The probe reads ``get_engine_provider`` through the ``main`` hub so that a
      test which patches ``main.get_engine_provider`` still governs the boot
      check exactly as it did before the api/main.py split.
"""

import logging

from software_engineering_team.api import coding_team_main as _main

logger = logging.getLogger(__name__)


def _warn_if_no_engine_provider() -> None:
    """Startup probe: make a mis-wired deployment visible at boot, not per-job.

    SE's own ``_se_startup()`` hook installs the SE-backed engine provider
    before this app handles traffic. A process importing this module directly
    (tests, embedded uses) still boots without one, but every /run and
    /review-pr job will fail without a provider — so say it loudly once at
    startup.

    Postconditions: logs an ERROR when no provider is installed; never raises.
    """
    if _main.get_engine_provider() is None:
        logger.error(
            "No CodeEngineProvider installed at startup: /run and /review-pr jobs will fail. "
            "Ensure software_engineering_team.api.lifecycle._se_startup() ran, or call "
            "software_engineering_team.engine_provider.set_engine_provider() "
            "before serving traffic."
        )


def _start_temporal_worker_backstop() -> None:
    """Start the Temporal worker from the app lifespan (no-op when disabled).

    SE's own ``_se_startup()`` hook already starts this worker for the normal
    deployment. This backstop covers every other way this app is served — a
    bare ``uvicorn software_engineering_team.api.coding_team_main:app``
    dev/CI run, or embedding — so that with ``TEMPORAL_ADDRESS`` set a ``/run``
    dispatch always has a worker to reach instead of timing out. ``start_team_worker``
    is idempotent, so overlapping with SE's own startup hook is harmless.

    Postconditions: attempts to start the worker; logs a warning and never
    raises if startup fails (a broken worker must not block serving traffic).
    """
    try:
        from software_engineering_team.temporal.coding_team_worker import (
            start_coding_team_temporal_worker_thread,
        )

        start_coding_team_temporal_worker_thread()
    except Exception:
        logger.warning(
            "coding_team Temporal worker start (lifespan backstop) failed", exc_info=True
        )


def _startup() -> None:
    """Composite ASGI startup hook: logging setup + engine-provider probe + Temporal
    worker backstop.

    Configuring logging here (rather than at module import time) means this app's
    default log format is only applied once serving begins, so it can't clobber a
    host process's own logging setup (e.g. SE's ``_se_startup``, or a test's log
    capture) just by importing this module.

    Postconditions: runs all startup steps; none raise into app startup.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _warn_if_no_engine_provider()
    _start_temporal_worker_backstop()
