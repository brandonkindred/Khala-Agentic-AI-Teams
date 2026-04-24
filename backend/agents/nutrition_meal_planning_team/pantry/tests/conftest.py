"""Conftest for the pantry test tree.

Tests under ``pantry/tests/`` do *not* inherit the autouse fixtures from
``nutrition_meal_planning_team/tests/conftest.py`` — pytest discovers
conftests along the ancestor chain of each test file, and ``tests`` is a
sibling of ``pantry`` rather than an ancestor. So the schema
registration and truncation fixtures are duplicated here.

Kept in sync with ``tests/conftest.py`` at the team root.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _register_nutrition_schema():
    """Create nutrition tables once per session when Postgres is enabled."""
    from shared_postgres import is_postgres_enabled, register_team_schemas

    if not is_postgres_enabled():
        yield
        return

    from nutrition_meal_planning_team.postgres import SCHEMA

    register_team_schemas(SCHEMA)
    yield


@pytest.fixture(autouse=True)
def _clean_nutrition_tables():
    """Truncate nutrition tables between pantry tests that use Postgres."""
    from shared_postgres import is_postgres_enabled

    if not is_postgres_enabled():
        yield
        return

    from nutrition_meal_planning_team.postgres import SCHEMA
    from shared_postgres.testing import truncate_team_tables

    truncate_team_tables(SCHEMA)
    yield
