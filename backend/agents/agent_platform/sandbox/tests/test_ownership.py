"""Ownership tests for the platform sandbox package.

Preconditions:
    * ``backend/agents`` is on ``sys.path`` (pytest.ini ``pythonpath = agents .``).
Postconditions:
    * The platform façade imports; the old provisioning path does not.
"""

from __future__ import annotations

import importlib
import pytest


def test_platform_sandbox_acquire_is_importable() -> None:
    from agent_platform.sandbox import acquire

    assert callable(acquire)


def test_old_provisioning_sandbox_path_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_team_studio.agent_provisioning_team.sandbox")
