"""Shared fixtures for branding_team tests."""

from __future__ import annotations

from typing import Any

import pytest

from branding_team.models import BrandingMission


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_postgres: run against the real shared.postgres connection",
    )


def make_mission(**overrides: Any) -> BrandingMission:
    """Build a ``BrandingMission`` with sensible defaults for branding_team tests.

    Preconditions:
        Every key in ``overrides`` must be a valid ``BrandingMission`` field name.
    Postconditions:
        Returns a ``BrandingMission`` whose fields equal the defaults below,
        with any provided ``overrides`` taking precedence.
    """
    defaults: dict[str, Any] = {
        "company_name": "Northstar Labs",
        "company_description": "A studio for product teams",
        "target_audience": "enterprise product leaders",
        "values": ["clarity", "trust", "tech"],
        "differentiators": ["hands-on partnership", "execution speed"],
    }
    defaults.update(overrides)
    return BrandingMission(**defaults)
