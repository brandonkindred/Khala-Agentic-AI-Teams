"""Shared fixtures for branding_team tests.

Installs the dict-backed fake Postgres automatically so every test runs
without requiring a live database. Individual tests that need to inspect
the backing state can still request the ``fake_pg`` fixture explicitly.

Tests marked ``@pytest.mark.real_postgres`` opt out of the fake and run
against the real ``shared_postgres`` connection (used by the live-Postgres
integration job to exercise the actual jsonb / CTE / JOIN SQL).
"""

from __future__ import annotations

from typing import Optional

import pytest

from branding_team.tests._fake_postgres import install_fake_postgres


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_postgres: run against the real shared_postgres connection, not the fake",
    )


@pytest.fixture(autouse=True)
def fake_pg(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Optional[dict]:
    if request.node.get_closest_marker("real_postgres"):
        # Leave shared_postgres.get_conn untouched so the real DB is used.
        return None
    return install_fake_postgres(monkeypatch)
