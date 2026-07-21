"""Fresh-database regression test for the auxiliary-schema cold-start race.

Before this fix, ``job_matching_team`` registered ``user_profile.SCHEMA`` from
its FastAPI ``on_startup`` hook — which only runs once uvicorn starts serving,
*after* the team-service wrapper had already started the Temporal worker at
import time. On a fresh database this let a queued ``job_matching_prepare_scan``
activity read ``user_profiles`` before that table existed; the best-effort read
in ``career_store.load_career_profile`` swallowed the "undefined table" error,
logged a WARNING, and silently fell back to the bundled YAML example profile.

Now ``job_matching_team`` declares ``user_profile.SCHEMA`` via
``extra_postgres_schemas``, so it is part of ``app.state.postgres_schemas`` —
the full ordered set the team-service wrapper registers before starting the
worker. This test proves that registering every schema in that set (not just
the team's own primary schema) closes the race, with a negative control proving
the harness actually detects the bug it exists to catch.

DESTRUCTIVE: this test drops and recreates the real ``user_profiles`` /
``user_profile_associations`` tables (structure only is restored — any data
that existed before the drop is gone; there is no undo). Run it ONLY against a
disposable Postgres instance (CI's ephemeral service container, or a local
throwaway) — never against a shared or persistent database whose data matters,
since dropping these tables destroys every team's artifact associations, not
just job_matching's.

Requires POSTGRES_HOST (skipped otherwise). Run against a disposable local
Postgres with:
    POSTGRES_HOST=localhost POSTGRES_USER=postgres POSTGRES_DB=postgres \\
        pytest -m integration test_schema_registration.py -v
(set POSTGRES_PORT / POSTGRES_PASSWORD too if your local instance needs them).
"""

from __future__ import annotations

import logging
import os

import pytest

pytestmark = pytest.mark.integration

# Captured at module-collection time — i.e. before this package's autouse
# `_hermetic_profile_env` fixture (conftest.py) deletes POSTGRES_HOST ahead of
# every test body. Re-applied per test via the `_real_postgres_host` fixture
# below, which (as a fixture the test explicitly requests) runs after that
# autouse deletion and wins.
_REAL_POSTGRES_HOST = os.environ.get("POSTGRES_HOST")

_CAREER_STORE_LOGGER = "job_matching_team.profile.career_store"
_FALLBACK_MESSAGE = "could not read user profile; falling back"


@pytest.fixture
def _real_postgres_host(monkeypatch):
    """Re-set POSTGRES_HOST for this test's body.

    Runs as a fixture the test explicitly requests, so — per pytest's
    autouse-before-explicit ordering within the same (function) scope — it
    executes after `_hermetic_profile_env`'s deletion and reliably wins.
    """
    if not _REAL_POSTGRES_HOST:
        pytest.skip("test_schema_registration requires POSTGRES_HOST to be set")
    monkeypatch.setenv("POSTGRES_HOST", _REAL_POSTGRES_HOST)


@pytest.fixture
def fresh_user_profile_tables(_real_postgres_host):
    """Simulate a brand-new database: drop user_profile's tables, restore after.

    DESTRUCTIVE — see the module docstring. Only structure is restored
    (``CREATE TABLE IF NOT EXISTS``); any data present before the drop is
    gone. The drop itself runs inside the same `try` as the restore so a
    partial failure (e.g. the second table's DROP fails after the first one
    already succeeded) still restores whatever it can, rather than leaving
    the shared instance stuck with a table missing.
    """
    from shared.postgres.testing import drop_team_tables
    from user_profile.postgres import SCHEMA as USER_PROFILE_SCHEMA

    try:
        drop_team_tables(USER_PROFILE_SCHEMA)
        yield
    finally:
        from shared.postgres import ensure_team_schema

        ensure_team_schema(USER_PROFILE_SCHEMA)


def _assert_fallback_logged(caplog, *, expected: bool) -> None:
    """Assert whether career_store's fallback WARNING fired during this test.

    The return value of ``load_career_profile()`` (``None``) can't distinguish
    "table missing" (the bug) from "table exists but has no career section
    yet" (the normal, correct case) — only this WARNING does.
    """
    assert any(_FALLBACK_MESSAGE in r.getMessage() for r in caplog.records) is expected


def test_registering_all_exposed_schemas_closes_the_race(fresh_user_profile_tables, caplog) -> None:
    """Registering every schema in app.state.postgres_schemas — the set the
    team-service wrapper now registers before starting the Temporal worker —
    means user_profiles exists by the time career_store's best-effort read
    runs, so it no longer falls back silently."""
    from job_matching_team.api.main import app as job_matching_app
    from job_matching_team.profile.career_store import load_career_profile
    from shared.postgres import register_team_schemas

    schemas = job_matching_app.state.postgres_schemas
    assert len(schemas) >= 2  # sanity: both JOB_MATCHING_SCHEMA and USER_PROFILE_SCHEMA present
    for schema in schemas:
        assert register_team_schemas(schema) is True  # every schema registered cleanly

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_CAREER_STORE_LOGGER):
        load_career_profile()
    _assert_fallback_logged(caplog, expected=False)


def test_missing_user_profile_schema_reproduces_the_original_bug(
    fresh_user_profile_tables, caplog
) -> None:
    """Negative control: proves this harness actually detects the bug it exists
    to catch. Registers ONLY JOB_MATCHING_SCHEMA — the pre-fix wrapper behavior,
    where the auxiliary user_profile schema was invisible to the pre-worker
    registration step — against freshly-dropped tables, and confirms the
    fallback WARNING fires. Without this, the positive test above could be
    passing vacuously (e.g. a mis-scoped caplog logger)."""
    from job_matching_team.postgres import SCHEMA as JOB_MATCHING_SCHEMA
    from job_matching_team.profile.career_store import load_career_profile
    from shared.postgres import register_team_schemas

    assert register_team_schemas(JOB_MATCHING_SCHEMA) is True  # only the primary schema

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_CAREER_STORE_LOGGER):
        load_career_profile()
    _assert_fallback_logged(caplog, expected=True)
