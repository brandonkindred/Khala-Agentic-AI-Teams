"""Shared helpers for the blogging FastAPI unit tests.

Centralizes the loading of ``api/main.py`` under synthetic module names (so the
app binds to ``FakeJobServiceClient`` and the heavy ``run_pipeline`` can be
patched), plus small utilities reused across the API test modules: a no-op
``Thread`` stand-in and a job-creation helper.

The three API test modules (``test_api_unit``, ``test_api_temporal_and_501s``,
``test_api_extra``) and ``test_blogging_api`` import ``api_main``/``app`` from
here so the module is loaded once and shared; the ``patched_client``/``client``
fixtures live in ``conftest.py`` and reuse the same objects.

``test_medium_stats_api`` deliberately does *not* share that instance: its
tests submit real jobs through the un-mocked ``/medium-stats-async`` route,
which spins up ``api/main.py``'s real daemon worker pool against its
module-level ``_ASYNC_JOB_QUEUE``. ``test_api_unit`` separately pokes that
same queue directly (bypassing the pool) on the assumption that no real
worker is draining it concurrently. Sharing one module instance between the
two would race a live background worker against that direct queue
manipulation — observed as tests hanging on a ``queue.get()`` that never
sees its expected sentinel. ``test_medium_stats_api`` calls
``load_api_module()`` with its own synthetic name to get an independent
module (independent globals, independent worker pool) while still reusing
this file's loader instead of duplicating the importlib boilerplate.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

_blogging_root = Path(__file__).resolve().parent.parent
if str(_blogging_root) not in sys.path:
    sys.path.insert(0, str(_blogging_root))

_MODULE_NAME = "blogging_api_main_unit"


def load_api_module(module_name: str) -> Any:
    """Load ``api/main.py`` under ``module_name`` and cache it in ``sys.modules``.

    This deliberately avoids ``from api.main import app`` / a plain
    ``importlib.import_module("api.main")``. Every team under ``backend/agents/``
    (``blogging``, ``planning_team``, ``software_engineering_team``, the legacy
    ``backend/agents/api``, and about a dozen others) ships its own top-level
    ``api`` package, and each team's ``conftest.py`` puts that team's own root on
    ``sys.path`` rather than importing via a shared package prefix. If this
    module imported ``api.main`` by its natural name, whichever team's ``api``
    package a test session touched *first* would win the ``sys.modules["api"]``
    slot for the rest of the run — a different team's routes/models could get
    bound silently depending on collection order. Loading ``api/main.py`` by
    explicit file path under a synthetic name sidesteps that collision
    entirely, at the cost of module-caching/relative-import quirks normal
    imports don't have (hence this one central loader instead of per-file
    imports).

    Preconditions:
        - ``api/main.py`` exists under the blogging package root.
        - ``module_name`` is unique to whatever isolation the caller needs: reuse
          ``_MODULE_NAME`` (via ``_load_api_main``) to share the one instance the
          unit/extra/temporal/artifact test modules bind fixtures against; pass a
          different name to get an independent module with its own module-level
          state (e.g. a private async-worker queue — see ``test_medium_stats_api``).
    Postconditions:
        - Returns the imported module. Repeat calls with the same ``module_name``
          return the same cached object; different names return independent
          module instances.
    """
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, _blogging_root / "api" / "main.py")
    assert spec is not None and spec.loader is not None, (
        "failed to build an import spec for api/main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    # `_rebuild_api_models()` runs at import and resolves every model defined in
    # api/main (request DTOs included), so no per-class rebuild is needed here.
    return module


def _load_api_main() -> Any:
    return load_api_module(_MODULE_NAME)


api_main = _load_api_main()
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
    from shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, brief)
    if fields:
        bjs.update_blog_job(job_id, **fields)
    return job_id
