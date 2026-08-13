"""Ownership tests for the platform sandbox package.

Preconditions:
    * ``backend/agents`` is on ``sys.path`` (pytest.ini ``pythonpath = agents .``).
Postconditions:
    * The platform façade imports; old provisioning sandbox paths do not.
"""

from __future__ import annotations

import importlib

import pytest


def test_platform_sandbox_acquire_is_importable() -> None:
    """Callers reach sandbox lifecycle via ``agent_platform.sandbox``.

    Postconditions:
        * ``acquire`` is importable and callable.
    """
    from agent_platform.sandbox import acquire

    assert callable(acquire)


def test_platform_sandbox_temporal_is_the_live_export_root() -> None:
    """Sandbox Temporal packaging lives on the platform façade, not provisioning.

    Postconditions:
        * ``agent_platform.sandbox.temporal`` exports ``SANDBOX_WORKFLOWS``,
          ``SANDBOX_ACTIVITIES``, and the renamed worker starter.
    """
    from agent_platform.sandbox import temporal as sandbox_temporal

    assert sandbox_temporal.SANDBOX_WORKFLOWS
    assert sandbox_temporal.SANDBOX_ACTIVITIES
    assert callable(sandbox_temporal.start_agent_platform_sandbox_temporal_worker_thread)


def test_old_provisioning_sandbox_path_is_gone() -> None:
    """``agent_team_studio.agent_provisioning_team.sandbox`` has no shim.

    Postconditions:
        * Importing the old package raises ``ModuleNotFoundError``.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_team_studio.agent_provisioning_team.sandbox")


def test_old_provisioning_sandbox_workflows_path_is_gone() -> None:
    """Old Temporal workflow module must not resolve.

    Postconditions:
        * Importing ``...temporal.sandbox_workflows`` raises ``ModuleNotFoundError``.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(
            "agent_team_studio.agent_provisioning_team.temporal.sandbox_workflows"
        )


def test_old_provisioning_sandbox_activities_path_is_gone() -> None:
    """Old Temporal activity module must not resolve.

    Postconditions:
        * Importing ``...temporal.sandbox_activities`` raises ``ModuleNotFoundError``.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(
            "agent_team_studio.agent_provisioning_team.temporal.sandbox_activities"
        )


def test_old_provisioning_sandbox_worker_starter_is_gone() -> None:
    """The renamed worker starter must not remain on the provisioning worker.

    Preconditions:
        * ``agent_team_studio.agent_provisioning_team.temporal.worker`` still
          exists (it boots provisioning workflows).
    Postconditions:
        * The module has no ``start_agent_provisioning_sandbox_temporal_worker_thread``.
    """
    from agent_team_studio.agent_provisioning_team.temporal import worker as prov_worker

    assert not hasattr(prov_worker, "start_agent_provisioning_sandbox_temporal_worker_thread")
