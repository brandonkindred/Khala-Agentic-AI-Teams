"""Shared helpers for the blogging FastAPI unit tests.

The three API test modules (``test_api_unit``, ``test_api_temporal_and_501s``,
``test_api_extra``) and ``test_blogging_api`` import ``api_main``/``app`` from
here so the module is loaded once and shared (via Python's own import cache);
the ``patched_client``/``client`` fixtures live in ``conftest.py`` and reuse
the same objects.

Imported via its fully-qualified ``agents.blogging.api.main`` path rather than
the bare ``api.main`` a synthetic-module loader used to work around: every team
under ``backend/agents/`` ships its own top-level ``api`` package, so a bare
``api.main`` import risks binding whichever team's package a test session
touches first for the rest of the run. The fully-qualified path is cached in
``sys.modules`` under its own unique key per team, so that collision cannot
happen here regardless of collection order.

``test_medium_stats_api`` deliberately does *not* import ``api_main`` from
here: its tests submit real jobs through the un-mocked ``/medium-stats-async``
route, which spins up ``api/main.py``'s real daemon worker pool against its
module-level ``_ASYNC_JOB_QUEUE``. ``test_api_unit`` separately pokes that
same queue directly (bypassing the pool) on the assumption that no real
worker is draining it concurrently. Sharing the one cached
``agents.blogging.api.main`` instance between the two would race a live
background worker against that direct queue manipulation — observed as tests
hanging on a ``queue.get()`` that never sees its expected sentinel. Instead,
``test_medium_stats_api`` calls ``load_isolated_api_main()`` here to get an
independent module (independent globals, independent worker pool) built from
the same file, without duplicating the importlib boilerplate itself.
"""

from __future__ import annotations

import uuid
from typing import Any

from agents.blogging.api import main as api_main

app = api_main.app


def load_isolated_api_main(module_name: str) -> Any:
    """Load a private copy of ``api/main.py`` under ``module_name``, independent of
    the shared ``api_main`` above.

    Preconditions:
        - ``api/main.py`` exists under the blogging package root.
        - ``module_name`` is not already used by an unrelated caller in the same
          test session (it becomes the ``sys.modules`` cache key).
    Postconditions:
        - Returns the imported module, with its own module-level globals (e.g. its
          own async-job queue and worker-started flag) distinct from ``api_main``.
          Repeat calls with the same ``module_name`` return the same cached object.
    """
    import importlib.util
    import sys
    from pathlib import Path

    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    blogging_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(module_name, blogging_root / "api" / "main.py")
    assert spec is not None and spec.loader is not None, (
        "failed to build an import spec for api/main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class NoOpThread:
    """Drop-in ``threading.Thread`` replacement whose target never runs.

    Endpoint code dispatches the pipeline via ``Thread(target=..., ...).start()``.
    Patching ``threading.Thread`` with this keeps the request handler synchronous
    and prevents the (heavy, LLM-backed) target from executing in tests.
    """

    def __init__(
        self, target: Any = None, args: Any = (), daemon: bool = False, **kwargs: Any
    ) -> None:
        pass

    def start(self) -> None:
        pass


def create_job(brief: str = "brief", **fields: Any) -> str:
    """Create a blog job in the store and return its id.

    Preconditions:
        - The ``blog_job_store`` client is patched to the in-memory fake (see the
          ``patched_client`` fixture in ``conftest.py``).
    Postconditions:
        - A job exists with a fresh 8-char id; any ``fields`` are applied via
          ``update_blog_job``.
    """
    from agents.blogging.shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, brief)
    if fields:
        bjs.update_blog_job(job_id, **fields)
    return job_id
