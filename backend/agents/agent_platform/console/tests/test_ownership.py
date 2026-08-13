"""Ownership tests for the platform console package.

Preconditions:
    * ``backend/agents`` is on ``sys.path`` (pytest.ini ``pythonpath = agents .``).
Postconditions:
    * The platform façade imports; the old top-level path does not.
"""

from __future__ import annotations

import importlib

import pytest


def test_platform_console_get_store_is_importable() -> None:
    from agent_platform.console import get_store

    assert callable(get_store)


def test_old_agent_console_path_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_console")
