"""Shared helpers for the blogging FastAPI unit tests.

All API test modules (``test_api_unit``, ``test_api_temporal_and_501s``,
``test_api_extra``, ``test_medium_stats_api``) and ``test_blogging_api`` import
``api_main``/``app`` from here so the module is loaded once and shared (via
Python's own import cache); the ``patched_client``/``client`` fixtures live in
``conftest.py`` and reuse the same objects.

Imported via its fully-qualified ``agents.blogging.api.main`` path rather than
the bare ``api.main`` a synthetic-module loader used to work around: every team
under ``backend/agents/`` ships its own top-level ``api`` package, so a bare
``api.main`` import risks binding whichever team's package a test session
touches first for the rest of the run. The fully-qualified path is cached in
``sys.modules`` under its own unique key per team, so that collision cannot
happen here regardless of collection order.

Every test module shares this one ``api_main``/``app``: since the router split,
route handlers are singleton dotted-path functions (``agents.blogging.api.routers.*``)
that always dereference ``agents.blogging.api.main`` at call time, so a private
``importlib``-reloaded copy of just ``main.py`` no longer observes any HTTP
traffic through it — monkeypatching such a copy would silently do nothing. Tests
that need to drive the real async-job queue/worker internals directly (rather
than through an HTTP request) target ``agents.blogging.api.job_workers`` and, if
they push/drain queue items themselves, substitute a fresh test-local queue via
monkeypatch to avoid racing a live background worker — see ``test_api_unit.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

from agents.blogging.api import main as api_main

app = api_main.app


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
