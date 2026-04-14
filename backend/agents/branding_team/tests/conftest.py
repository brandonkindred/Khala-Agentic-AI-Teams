"""Shared fixtures for branding_team tests.

Installs the dict-backed fake Postgres automatically so every test runs
without requiring a live database. Individual tests that need to inspect
the backing state can still request the ``fake_pg`` fixture explicitly.
"""

from __future__ import annotations

import pytest

from branding_team.tests._fake_postgres import install_fake_postgres


@pytest.fixture(autouse=True)
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)
