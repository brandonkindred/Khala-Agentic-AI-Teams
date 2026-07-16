"""Shared helpers for the blogging FastAPI unit tests.

Centralizes the one-off load of ``api/main.py`` under a synthetic module name (so
the app binds to ``FakeJobServiceClient`` and the heavy ``run_pipeline`` can be
patched), plus small utilities reused across the API test modules: a no-op
``Thread`` stand-in and a job-creation helper.

The three API test modules (``test_api_unit``, ``test_api_temporal_and_501s``,
``test_api_extra``) import ``api_main``/``app`` from here so the module is loaded
once and shared; the ``patched_client``/``client`` fixtures live in
``conftest.py`` and reuse the same objects.
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


def _load_api_main() -> Any:
    """Load ``api/main.py`` once under a synthetic name and cache it in ``sys.modules``.

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
    explicit file path under the synthetic name ``_MODULE_NAME`` sidesteps that
    collision entirely, at the cost of module-caching/relative-import quirks
    normal imports don't have (hence this one central loader instead of
    per-file imports).

    Preconditions:
        - ``api/main.py`` exists under the blogging package root.
    Postconditions:
        - Returns the imported module. Repeat calls return the same object
          (cached under ``_MODULE_NAME``), so every importer shares one module.
    """
    existing = sys.modules.get(_MODULE_NAME)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _blogging_root / "api" / "main.py")
    assert spec is not None and spec.loader is not None, (
        "failed to build an import spec for api/main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    # `_rebuild_api_models()` runs at import and resolves every model defined in
    # api/main (request DTOs included), so no per-class rebuild is needed here.
    return module


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
