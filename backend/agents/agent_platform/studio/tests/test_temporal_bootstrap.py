"""Regression tests for the Agent Studio Temporal bootstrap.

Guards the failure modes the wiring is designed to avoid:

1. **Self-bootstrap at import time.** Importing the package must NOT spin up a worker
   thread. Boot is the unified-API lifespan's job.
2. **Empty authoring surface.** The 1-activity workflows are gone; ``WORKFLOWS`` and
   ``ACTIVITIES`` stay empty, and the worker starter is a no-op (an empty Temporal
   Worker is invalid).
3. **Parent façade must stay lazy.** Importing ``agent_platform.studio.temporal``
   also executes ``agent_platform.studio.__init__``. Eager re-exports of ``routes`` /
   ``runtime`` would construct process singletons at import time.
"""

from __future__ import annotations

import importlib
import sys
import unittest.mock as mock

import pytest


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
    # orphan while activities import the restored module from ``sys.modules``.
    for name, mod in saved.items():
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and child:
            setattr(parent, child, mod)


def test_importing_temporal_package_does_not_call_start_team_worker():
    """Loading the package (and its submodules) must NOT spin up a worker thread."""
    import shared.temporal

    _purge("agent_platform.studio.temporal")
    with mock.patch.object(shared.temporal, "start_team_worker") as patched:
        importlib.import_module("agent_platform.studio.temporal")
        importlib.import_module("agent_platform.studio.temporal.dispatch")
        importlib.import_module("agent_platform.studio.temporal.worker")
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count})."
        )


def test_importing_temporal_package_does_not_load_routes_runtime_or_call_getenv():
    """Importing the Temporal package must not pull ``routes`` / ``runtime`` or
    call ``os.getenv`` at import time.
    """
    import os

    importlib.import_module("agent_platform.studio.temporal")
    saved = _snapshot_studio_modules()
    _purge("agent_platform.studio")
    try:
        with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
            importlib.import_module("agent_platform.studio.temporal")
            assert spy.call_count == 0, (
                f"importing agent_platform.studio.temporal (and parent "
                f"studio/__init__) called os.getenv {spy.call_count} time(s)."
            )

        assert "agent_platform.studio.routes" not in sys.modules
        assert "agent_platform.studio.runtime" not in sys.modules
    finally:
        _restore_studio_modules(saved)


def test_facade_exports_resolve_lazily():
    """The four public names still resolve from ``agent_platform.studio``, but
    only when accessed — importing the package itself must not load them.
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


def test_worker_module_exposes_lifespan_entrypoint():
    """The unified-API lifespan calls a no-arg
    ``start_agent_studio_temporal_worker_thread``. Keep that contract pinned."""
    from agent_platform.studio.temporal import worker

    fn = getattr(worker, "start_agent_studio_temporal_worker_thread", None)
    assert callable(fn), (
        "the unified-API lifespan expects a no-arg "
        "start_agent_studio_temporal_worker_thread() in agent_platform.studio.temporal.worker"
    )


def test_authoring_workflows_and_activities_are_empty() -> None:
    """1-activity authoring workflows are gone; CRUD must not depend on them."""
    from agent_platform.studio.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS

    assert WORKFLOWS == []
    assert ACTIVITIES == []
    assert TASK_QUEUE == "agent-studio-queue"


def test_authoring_workflow_module_is_gone() -> None:
    """The 1-activity workflow module must not resolve."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_platform.studio.temporal.workflows")


def test_worker_start_is_noop() -> None:
    """The lifespan entrypoint must not boot a worker now that CRUD has no workflows."""
    from agent_platform.studio.temporal import worker as worker_mod

    assert worker_mod.start_agent_studio_temporal_worker_thread() is False
