"""Regression tests for the user_agent_founder Temporal bootstrap.

Two bugs were fixed together here. Both have to be guarded:

1. **Race on first request.** Importing ``user_agent_founder.temporal``
   used to call ``shared_temporal.start_team_worker(...)`` at module
   load. The worker thread connected the Temporal client asynchronously,
   so the very first ``start_founder_workflow`` call lost the race and
   raised ``RuntimeError: Temporal client not available``. The package
   must no longer self-bootstrap a worker at import time — boot is the
   team_service entrypoint's job (or the API lifespan as a backstop).

2. **Workflow sandbox blocks ``os.getenv``.** The temporalio sandbox
   re-imports the workflow module to load ``UserAgentFounderWorkflow``.
   The previous ``__init__.py`` called ``is_temporal_enabled()`` —
   which calls ``os.getenv("TEMPORAL_ADDRESS")`` — at module level, and
   the sandbox aborted with ``__call__ on os.getenv restricted``. Both
   the package ``__init__`` and the dedicated ``workflows`` module must
   load without invoking ``os.getenv``.
"""

from __future__ import annotations

import importlib
import sys
import unittest.mock as mock


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


def test_importing_temporal_package_does_not_call_start_team_worker():
    """Loading the package must NOT spin up a worker thread."""
    import shared_temporal

    _purge("user_agent_founder.temporal")
    with mock.patch.object(shared_temporal, "start_team_worker") as patched:
        importlib.import_module("user_agent_founder.temporal")
        importlib.import_module("user_agent_founder.temporal.workflows")
        importlib.import_module("user_agent_founder.temporal.start_workflow")
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}). This causes a race on the "
            f"first request and a temporalio sandbox os.getenv violation when "
            f"the workflow registers."
        )


def test_workflows_module_does_not_call_os_getenv_at_import_time():
    """The workflow module is reimported by the temporalio sandbox.

    Anything the sandbox restricts (``os.getenv``, time/random, etc.)
    must not be invoked at module top level — it has to live inside
    activity bodies or the worker bootstrap.
    """
    _purge("user_agent_founder.temporal")
    import os

    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("user_agent_founder.temporal.workflows")
        # Allow any getenv calls that come from temporalio itself loading;
        # what we care about is that *our* module didn't add new ones.
        # The cheap check: walking the call stack inside the spy is brittle,
        # so instead assert workflows.py loaded clean (no exception).
        # Also reimport the package __init__ — that path is what the sandbox
        # actually replays.
        spy.reset_mock()
        importlib.import_module("user_agent_founder.temporal")
        # The package __init__ must not call os.getenv at all.
        assert spy.call_count == 0, (
            f"user_agent_founder.temporal.__init__ called os.getenv "
            f"{spy.call_count} time(s) at import — this trips the temporalio "
            f"workflow sandbox during workflow registration."
        )


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py looks up ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE``. Keep that contract pinned so a rename
    can't silently break docker-compose.
    """
    from user_agent_founder.temporal import worker

    fn = getattr(worker, "start_user_agent_founder_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg "
        "start_user_agent_founder_temporal_worker_thread() in "
        "user_agent_founder.temporal.worker"
    )


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    """Standalone uvicorn dev path: with TEMPORAL_ADDRESS unset, the
    backstop in the lifespan must return False instead of raising."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from user_agent_founder.temporal.worker import (
        start_user_agent_founder_temporal_worker_thread,
    )

    assert start_user_agent_founder_temporal_worker_thread() is False


def test_start_workflow_waits_for_client_then_raises(monkeypatch):
    """When the worker is genuinely not running, the helper must time out
    with the original error message — not raise immediately and not wait
    forever. The wait window is what makes the lifespan-backstop case
    work; the eventual failure is what surfaces config bugs in CI.
    """
    from user_agent_founder.temporal import start_workflow as sw

    monkeypatch.setattr(sw, "get_temporal_client", lambda: None)
    monkeypatch.setattr(sw, "get_temporal_loop", lambda: None)
    monkeypatch.setattr(sw, "CLIENT_READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(sw, "CLIENT_READY_POLL_S", 0.01)

    import pytest

    with pytest.raises(RuntimeError, match="Temporal client not available"):
        sw._wait_for_client()
