"""Shared fixtures for branding_team tests.

Installs the dict-backed fake Postgres automatically so every test runs
without requiring a live database. Individual tests that need to inspect
the backing state can still request the ``fake_pg`` fixture explicitly.

Tests marked ``@pytest.mark.real_postgres`` opt out of the fake and run
against the real ``shared.postgres`` connection (used by the live-Postgres
integration job to exercise the actual jsonb / CTE / JOIN SQL).
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from branding_team.models import BrandingMission
from branding_team.tests._fake_postgres import install_fake_postgres


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_postgres: run against the real shared.postgres connection, not the fake",
    )


def make_mission(**overrides: Any) -> BrandingMission:
    """Build a ``BrandingMission`` with sensible defaults for branding_team tests.

    Preconditions:
        - Every key in ``overrides`` must be a valid ``BrandingMission`` field name.
    Postconditions:
        - Returns a ``BrandingMission`` whose fields equal the defaults below,
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


@pytest.fixture(autouse=True)
def fake_pg(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Optional[dict]:
    if request.node.get_closest_marker("real_postgres"):
        # Leave shared.postgres.get_conn untouched so the real DB is used.
        return None
    return install_fake_postgres(monkeypatch)
