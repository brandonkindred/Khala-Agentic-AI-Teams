"""Lock the unified-API lifespan worker/route catalog in one documented place.

A later cleanup or move must not drop the catalog file, the map links, or the
lifespan docstring pointer without failing CI.
"""

from __future__ import annotations

from pathlib import Path

import unified_api.main as main

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CATALOG = _REPO_ROOT / "docs" / "UNIFIED_API_LIFESPAN.md"

_MAP_FILES = (
    _REPO_ROOT / "CLAUDE.md",
    _REPO_ROOT / "docs" / "ARCHITECTURE.md",
    _REPO_ROOT / "backend" / "unified_api" / "README.md",
    _REPO_ROOT / "backend" / "agents" / "agent_platform" / "README.md",
)


def test_lifespan_catalog_documents_worker_and_route_registration() -> None:
    """The markdown catalog is the single map of lifespan + import-time registration.

    Preconditions:
        * ``docs/UNIFIED_API_LIFESPAN.md`` exists at the repository root.
    Postconditions:
        * The catalog names every numbered lifespan step and the identifiers a
          later owner must not relocate without updating this file.
    """
    assert _CATALOG.is_file(), f"missing lifespan catalog: {_CATALOG}"
    text = _CATALOG.read_text(encoding="utf-8")

    for step in range(9):
        assert f"{step}." in text, f"catalog missing lifespan step {step}"

    for token in (
        "start_agent_platform_sandbox_temporal_worker_thread",
        "SANDBOX_TASK_QUEUE",
        "_register_proxy_routes",
        "include_router",
        "_maybe_register_team_assistants",
        "_start_agent_studio_temporal_worker",
        "run_pruner",
        "UNIFIED_API_SANDBOX_TEMPORAL_WORKER",
        "UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER",
        "agent_platform.studio.router",
        "team_service",
    ):
        assert token in text, f"catalog missing {token!r}"


def test_lifespan_catalog_is_linked_from_maps() -> None:
    """CLAUDE, architecture, and READMEs must point at the catalog, not copy it.

    Preconditions:
        * Each map file in ``_MAP_FILES`` exists.
    Postconditions:
        * Every map file contains the catalog filename so the location stays
          discoverable from the repo's orientation docs.
    """
    for path in _MAP_FILES:
        assert path.is_file(), f"missing map file: {path}"
        text = path.read_text(encoding="utf-8")
        assert "UNIFIED_API_LIFESPAN.md" in text, f"{path} does not reference the lifespan catalog"


def test_lifespan_docstring_points_at_catalog() -> None:
    """The lifespan docstring cites the catalog so code and docs stay coupled.

    Preconditions:
        * ``unified_api.main.lifespan`` has a docstring.
    Postconditions:
        * The docstring names ``UNIFIED_API_LIFESPAN.md``.
    """
    doc = main.lifespan.__doc__
    assert doc
    assert "UNIFIED_API_LIFESPAN.md" in doc
