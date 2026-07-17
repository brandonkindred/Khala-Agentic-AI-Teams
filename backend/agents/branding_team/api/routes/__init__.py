"""Concern-grouped ``APIRouter`` modules for the branding team API.

Each module here declares a bare ``router = APIRouter()`` and attaches its
handlers; ``api.main`` imports them last (after the app + shared globals are
defined) and mounts them with ``app.include_router(...)``. Route paths are
absolute and unchanged from the pre-split monolith.

Handlers dereference the monkeypatched collaborators (``orchestrator``,
``branding_store``, ``assistant_agent``/``_get_assistant_agent``,
``_run_executor``, …) through ``from branding_team.api import main as _main`` at
call time, so ``monkeypatch.setattr(main, …)`` in the test suite keeps working.
"""
