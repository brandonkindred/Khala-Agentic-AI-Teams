"""Concern-grouped ``APIRouter`` modules for agentic team provisioning HTTP.

Each module declares a bare ``router = APIRouter()``; ``api.main`` imports them
last (after the app + shared globals are defined) and mounts them with
``app.include_router(...)``. Route paths stay absolute and unchanged from the
pre-split monolith.

Handlers / services dereference monkeypatched collaborators through
``from agent_team_studio.agentic_team_provisioning.api import main as _main`` at
call time so ``monkeypatch.setattr(main, …)`` keeps working.
"""
