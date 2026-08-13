"""Ownership tests for the platform registry package.

Preconditions:
    * ``backend/agents`` is on ``sys.path`` (pytest.ini ``pythonpath = agents .``).
Postconditions:
    * The platform façade imports; the old top-level path does not.
"""

from __future__ import annotations

import importlib

import pytest


def test_platform_registry_get_registry_is_importable() -> None:
    from agent_platform.registry import get_registry

    assert callable(get_registry)


def test_old_agent_registry_path_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_registry")
