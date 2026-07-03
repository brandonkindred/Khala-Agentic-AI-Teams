"""Standalone coding-team service composition root (the container's ``TEAM_MODULE``).

The coding-team runs as its own container that ``unified_api`` reverse-proxies
(``CODING_TEAM_SERVICE_URL``). It needs ``software_engineering_team``'s
implementation engines, but the ``coding_team`` package must not import
``software_engineering_team``. This module lives OUTSIDE the coding_team package
and is the composition root: it installs the SE-backed ``CodeEngineProvider`` into
coding_team's registry, then re-exposes the coding_team FastAPI ``app``.

Point the container at this module (``TEAM_MODULE=coding_team_service.main``)
instead of ``coding_team.api.main`` so the standalone service has its engines
wired while the coding_team package itself stays SE-free.
"""

from __future__ import annotations

from coding_team.engine_provider import set_engine_provider
from software_engineering_team.coding_engine_provider import SECodeEngineProvider

# Install the engines before the app handles any request. Idempotent and cheap:
# the provider defers every heavy engine import to call time.
set_engine_provider(SECodeEngineProvider())

from coding_team.api.main import app  # noqa: E402  (must follow provider installation)

__all__ = ["app"]
