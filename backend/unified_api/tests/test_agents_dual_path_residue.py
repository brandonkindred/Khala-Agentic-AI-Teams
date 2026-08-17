"""Regression: /api/agents stays on the platform packages with no dual path.

After the registry and console moves, live imports go through
``agent_platform.registry`` and ``agent_platform.console``. The previous
top-level packages were hard-removed with no compatibility shim.

Preconditions:
    * ``backend/agents`` and ``backend`` are on ``sys.path``.
Postconditions:
    * Old top-level packages do not import.
    * Catalog routes on ``/api/agents`` still function.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from textwrap import dedent

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from agent_platform.registry import loader
from agent_platform.registry.loader import AgentRegistry
from unified_api.routes.agents import router as agents_router

# Former top-level package dirs. Live code is under agent_platform/ or
# agent_team_studio/; these names must not reappear as source trees.
_RESIDUE_TOP_LEVEL = (
    "agent_registry",
    "agent_console",
    "agent_studio",
    "agent_provisioning_team",
    "agentic_team_provisioning",
    "user_agent_founder",
)

# Bare import roots that used to live on PYTHONPATH. Domain apps still import
# as agent_team_studio.<name>, so those names are not in this list.
_RESIDUE_IMPORT_ROOTS = ("agent_registry", "agent_console", "agent_studio")


def test_old_registry_and_console_packages_do_not_import() -> None:
    """Bare former platform package roots are gone.

    Postconditions:
        * Importing each residue root raises ``ModuleNotFoundError``.
    """
    for name in _RESIDUE_IMPORT_ROOTS:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)


def test_residue_top_level_dirs_have_no_python_source() -> None:
    """Empty leftover ``backend/agents/agent_*`` package dirs stay gone.

    Preconditions:
        * ``_agents`` is the ``backend/agents`` tree.
    Postconditions:
        * None of the former top-level package dirs contain ``.py`` files.
    """
    leftover = []
    for name in _RESIDUE_TOP_LEVEL:
        root = _agents / name
        leftover.extend(sorted(p for p in root.rglob("*.py") if p.is_file()))
    assert leftover == []


def test_agents_route_loads_platform_packages_not_old_paths() -> None:
    """``unified_api.routes.agents`` binds to the platform façades.

    Postconditions:
        * Importing the route module loads ``agent_platform.registry`` and
          ``agent_platform.console``.
        * ``agent_registry`` and ``agent_console`` stay absent from ``sys.modules``.
    """
    import unified_api.routes.agents as agents_route_mod

    assert agents_route_mod.router.prefix == "/api/agents"
    assert "agent_platform.registry" in sys.modules
    assert "agent_platform.console" in sys.modules
    assert "agent_registry" not in sys.modules
    assert "agent_console" not in sys.modules


def _write_manifest(dir_: Path, team: str, filename: str, body: str) -> None:
    d = dir_ / team / "agent_console" / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(dedent(body).lstrip(), encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_dynamic_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this smoke test off the dynamic Postgres overlay."""
    monkeypatch.setattr(AgentRegistry, "_dynamic_store", lambda self: None)


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """Isolated catalog client backed by one tmp-dir manifest.

    Preconditions:
        * ``tmp_path`` is writable.
    Postconditions:
        * Yields a ``TestClient`` whose ``GET /api/agents`` reads only the
          tmp-dir catalog; the process-wide registry singleton is restored.
    """
    _write_manifest(
        tmp_path,
        "blogging",
        "planner.yaml",
        """
        schema_version: 1
        id: blogging.planner
        team: blogging
        name: Planner
        summary: Plans posts
        source:
          entrypoint: x:y
        """,
    )
    loader.get_registry.cache_clear()
    rebuilt = AgentRegistry.load(tmp_path)
    original = loader.get_registry
    loader.get_registry = lambda: rebuilt  # type: ignore[assignment]

    import unified_api.routes.agents as agents_route_mod

    agents_route_mod.get_registry = lambda: rebuilt  # type: ignore[assignment]

    app = FastAPI()
    app.include_router(agents_router)
    try:
        yield TestClient(app)
    finally:
        loader.get_registry = original  # type: ignore[assignment]
        agents_route_mod.get_registry = original  # type: ignore[assignment]
        loader.get_registry.cache_clear()


def test_list_agents_still_serves_catalog(client: TestClient) -> None:
    """``GET /api/agents`` still lists the catalog after the package moves.

    Postconditions:
        * Response is 200 with the tmp-dir agent id.
    """
    resp = client.get("/api/agents")
    assert resp.status_code == 200
    assert {item["id"] for item in resp.json()} == {"blogging.planner"}


def test_list_teams_still_groups_catalog(client: TestClient) -> None:
    """``GET /api/agents/teams`` still groups the catalog after the package moves.

    Postconditions:
        * Response is 200 and includes the tmp-dir team slug.
    """
    resp = client.get("/api/agents/teams")
    assert resp.status_code == 200
    assert {item["team"]: item["agent_count"] for item in resp.json()} == {"blogging": 1}
