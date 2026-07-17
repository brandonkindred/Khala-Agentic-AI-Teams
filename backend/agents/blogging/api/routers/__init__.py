"""Concern-grouped ``APIRouter`` modules for the blogging team API.

Each module here declares a bare ``router = APIRouter()`` and attaches its
handlers; ``api.main`` imports them last (after the app + shared globals are
defined) and mounts them with ``app.include_router(...)``. Route paths are
absolute and unchanged from the pre-split monolith.

Handlers dereference monkeypatched collaborators (the blog_job_store helpers,
``RUN_ARTIFACTS_BASE``, ``_submit_async_job``, etc.) through
``from agents.blogging.api import main as _main`` at call time, so
``monkeypatch.setattr(main, ...)`` in the test suite keeps working.
"""
