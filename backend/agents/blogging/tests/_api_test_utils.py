"""Shared helpers for the blogging FastAPI unit tests.

The three API test modules (``test_api_unit``, ``test_api_temporal_and_501s``,
``test_api_extra``) import ``api_main``/``app`` from here so the module is loaded
once and shared (via Python's own import cache); the ``patched_client``/``client``
fixtures live in ``conftest.py`` and reuse the same objects.
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
