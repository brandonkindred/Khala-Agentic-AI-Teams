"""Regression: old Studio authoring import roots must not resolve.

After the platform move, live imports go through ``agent_platform.studio``.
The previous packages (``agent_team_studio.agent_studio`` and
``unified_api.routes.agent_studio``) were hard-removed with no compatibility
shim, so importing them must fail.
"""

from __future__ import annotations

import pytest


def test_old_authoring_package_does_not_import() -> None:
    """``agent_team_studio.agent_studio`` is no longer an import root.

    Preconditions:
        * ``agent_team_studio`` itself may still exist (other subpackages).
    Postconditions:
        * Importing ``agent_team_studio.agent_studio`` raises ``ModuleNotFoundError``.
    """
    with pytest.raises(ModuleNotFoundError):
        __import__("agent_team_studio.agent_studio")


def test_old_unified_api_route_module_does_not_import() -> None:
    """``unified_api.routes.agent_studio`` was moved into the platform package.

    Postconditions:
        * Importing ``unified_api.routes.agent_studio`` raises ``ModuleNotFoundError``.
    """
    with pytest.raises(ModuleNotFoundError):
        __import__("unified_api.routes.agent_studio")


def test_platform_studio_is_the_live_import_root() -> None:
    """Callers reach Studio via ``agent_platform.studio``.

    Postconditions:
        * ``agent_platform.studio`` imports, and the façade exposes ``router``.
    """
    import agent_platform.studio as studio

    assert studio.router.prefix == "/api/agent-studio"
