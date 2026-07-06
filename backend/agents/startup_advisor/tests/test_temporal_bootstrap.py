"""Bootstrap contract for the startup-advisor Temporal package.

Guards the two invariants the sandbox and the docker worker hook depend on:
importing ``startup_advisor.temporal`` has no worker side-effect, and
``start_startup_advisor_temporal_worker_thread`` is a no-op (returns False)
when ``TEMPORAL_ADDRESS`` is unset.
"""

from __future__ import annotations

import importlib

import pytest


def test_temporal_package_exports_workflows_and_activities():
    mod = importlib.import_module("startup_advisor.temporal")
    assert [w.__name__ for w in mod.WORKFLOWS] == ["StartupAdvisorWorkflow"]
    assert [a.__name__ for a in mod.ACTIVITIES] == ["run_pipeline_activity"]
    assert mod.TASK_QUEUE == "startup_advisor-queue"


def test_worker_start_is_noop_when_temporal_disabled(monkeypatch):
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: False)
    from startup_advisor.temporal.worker import start_startup_advisor_temporal_worker_thread

    assert start_startup_advisor_temporal_worker_thread() is False


def test_startup_backstop_never_raises(monkeypatch):
    """The ``on_startup`` hook must swallow worker-start failures so a boot-time
    Temporal problem cannot abort app startup."""
    from startup_advisor.api import main as api_main

    def _boom():
        raise RuntimeError("worker boot exploded")

    monkeypatch.setattr(
        "startup_advisor.temporal.worker.start_startup_advisor_temporal_worker_thread",
        _boom,
    )
    # Must not raise.
    api_main._startup()


@pytest.mark.parametrize("attr", ["is_temporal_enabled", "start_team_worker"])
def test_temporal_init_has_no_bootstrap_symbols(attr):
    """The package ``__init__`` must not bind worker-bootstrap symbols at module
    scope (they belong in ``worker.py``), so the sandbox re-import stays clean."""
    mod = importlib.import_module("startup_advisor.temporal")
    assert not hasattr(mod, attr)
