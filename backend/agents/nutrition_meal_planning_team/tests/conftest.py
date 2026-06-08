"""Pytest config for nutrition_meal_planning_team.

Ensures the agents/backend dirs are on sys.path, and — for tests that
touch Postgres-backed stores — registers the team schema once per
session and truncates tables between tests.
"""

import os
import sys
from pathlib import Path

import pytest

_agents_dir = Path(__file__).resolve().parent.parent.parent
_backend_dir = _agents_dir.parent
for _d in (_backend_dir, _agents_dir):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

# Disable LLM retries so tests that hit an unavailable LLM fail fast and fall
# through to structural fallback paths rather than waiting minutes.
os.environ.setdefault("LLM_MAX_RETRIES", "0")
# Disable the slow 429 rate-limit backoff so no test can ever sleep the 300s+
# schedule (this team overrides pytest's rootdir, hiding backend/conftest.py).
os.environ.setdefault("LLM_RATE_LIMIT_MAX_RETRIES", "0")


@pytest.fixture(scope="session", autouse=True)
def _register_nutrition_schema():
    """Create the nutrition tables once per test session (when Postgres is enabled).

    No-op when ``POSTGRES_HOST`` is unset — postgres-dependent tests in
    this suite are marked to skip under that condition.
    """
    from shared_postgres import is_postgres_enabled, register_team_schemas

    if not is_postgres_enabled():
        yield
        return

    from nutrition_meal_planning_team.postgres import SCHEMA

    register_team_schemas(SCHEMA)
    yield


@pytest.fixture(autouse=True)
def _clean_nutrition_tables():
    """Truncate all nutrition tables before each test that uses Postgres.

    Skipped when Postgres isn't enabled so pure-unit tests (mocked LLM,
    Pydantic models) can still run in that environment.
    """
    from shared_postgres import is_postgres_enabled

    if not is_postgres_enabled():
        yield
        return

    from nutrition_meal_planning_team.postgres import SCHEMA
    from shared_postgres.testing import truncate_team_tables

    truncate_team_tables(SCHEMA)
    yield
