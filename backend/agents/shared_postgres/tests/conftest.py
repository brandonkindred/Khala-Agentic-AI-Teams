"""Test harness for ``shared_postgres``.

Adds the ``backend/agents`` directory to ``sys.path`` so tests can
import ``shared_postgres`` without relying on a project install, and
provides a session-scoped fixture that applies every team's schema
exactly once against the configured Postgres (used by tests marked
with ``@pytest.mark.integration``).

The session fixture assumes ``POSTGRES_HOST`` is already set in the
environment — either by a CI service container or by running
``docker compose -f docker/docker-compose.yml up -d postgres`` locally.
Mocked unit tests don't need the fixture.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def live_postgres():
    """Session-scoped fixture that runs every team's DDL once.

    Skips the test when ``POSTGRES_HOST`` is unset so local ``pytest``
    runs without Docker stay green. Closes every pool at teardown.
    """
    if not os.environ.get("POSTGRES_HOST", "").strip():
        pytest.skip("POSTGRES_HOST not set; skipping live Postgres fixture")

    from shared_postgres import close_pool, register_all_team_schemas

    results = register_all_team_schemas()
    failed = [team for team, ok in results.items() if not ok]
    if failed:
        pytest.fail(f"register_all_team_schemas failed for: {failed}")

    yield results

    close_pool()
