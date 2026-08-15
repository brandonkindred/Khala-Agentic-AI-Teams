"""Regression test for the ``agent_platform.studio`` public façade.

The package exposes four public names via a PEP 562 lazy ``__getattr__`` so that
importing the package (or one of its lightweight submodules) does not eagerly pull
``routes`` / ``runtime`` — those construct process singletons at import time. This
test pins that contract: the four names still resolve, but only on access.
"""

from __future__ import annotations

import importlib
import sys


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


def _snapshot_studio_modules() -> dict[str, object]:
    """Capture live ``agent_platform.studio*`` modules so a purge can be reversed.

    Later tests in this process (and pydantic models they already imported) must
    keep seeing the same module objects. ``normalize_agent_states`` re-imports
    ``AgentState`` at call time; leaving a post-purge copy in ``sys.modules``
    splits class identity and fails clone/refine route tests.
    """
    return {
        name: mod
        for name, mod in sys.modules.items()
        if name == "agent_platform.studio" or name.startswith("agent_platform.studio.")
    }


def _restore_studio_modules(saved: dict[str, object]) -> None:
    _purge("agent_platform.studio")
    sys.modules.update(saved)
    # ``sys.modules.update`` does not rebind ``parent.child`` attributes. Import
    # after a purge leaves ``agent_platform.studio`` pointing at a *new* package
    # object; later tests (and monkeypatch dotted paths) would then patch the
    # orphan while the restored module lives on in ``sys.modules``.
    for name, mod in saved.items():
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and child:
            setattr(parent, child, mod)


def test_facade_exports_resolve_lazily():
    """The four public names still resolve from ``agent_platform.studio``, but
    only when accessed — importing the package itself must not load
    ``routes`` / ``runtime``.
    """
    saved = _snapshot_studio_modules()
    _purge("agent_platform.studio")
    try:
        studio = importlib.import_module("agent_platform.studio")
        assert "agent_platform.studio.routes" not in sys.modules
        assert "agent_platform.studio.runtime" not in sys.modules
        assert studio.__all__ == [
            "get_studio_service",
            "build_studio_agent_manifest",
            "clone_from_manifest",
            "router",
        ]

        from agent_platform.studio import (
            build_studio_agent_manifest,
            clone_from_manifest,
            get_studio_service,
            router,
        )

        assert router.prefix == "/api/agent-studio"
        assert callable(get_studio_service)
        assert callable(build_studio_agent_manifest)
        assert callable(clone_from_manifest)
    finally:
        _restore_studio_modules(saved)
