"""Test helpers for ``shared.postgres``.

Small utilities that are useful only in tests — kept out of the main
package so production imports don't pull them in. Includes live-Postgres
truncate helpers (``truncate_team_tables``, ``truncate_all_teams``,
``drop_team_tables``), the ``real_postgres_schema`` pytest fixture
factory, and re-exports the in-memory fake scaffold from
``shared.postgres.fake`` for store unit tests that must not hit Postgres.
"""

from __future__ import annotations

import logging
from typing import Iterable

import pytest

from shared.postgres.client import get_conn, is_postgres_enabled
from shared.postgres.runner import register_team_schemas
from shared.postgres.schema import TeamSchema

logger = logging.getLogger(__name__)

# pytest's own accepted fixture scopes (session/package/module/class/function) —
# validated against in real_postgres_schema so a typo raises here, not as a more
# obscure error later at collection time.
_PYTEST_FIXTURE_SCOPES = frozenset({"session", "package", "module", "class", "function"})


def _quote_ident(name: str) -> str:
    if '"' in name:
        raise ValueError(f"refusing to quote identifier containing a double-quote: {name!r}")
    return f'"{name}"'


def truncate_team_tables(schema: TeamSchema) -> int:
    """Truncate every table named in ``schema.table_names``.

    All truncates run inside a single transaction with
    ``RESTART IDENTITY CASCADE`` so that sequences reset and any
    foreign-key dependents are wiped together. Returns the number of
    tables truncated.

    Raises ``RuntimeError`` when Postgres is disabled (matches
    ``ensure_team_schema``'s policy of failing loudly on misuse).
    """
    if not is_postgres_enabled():
        raise RuntimeError(f"truncate_team_tables called for team={schema.team} but POSTGRES_HOST is not set.")
    if not schema.table_names:
        return 0

    # Quote identifiers so unusual table names can't break the SQL; we
    # still reject anything with a double quote to be safe.
    quoted = [_quote_ident(name) for name in schema.table_names]
    sql = f"TRUNCATE TABLE {', '.join(quoted)} RESTART IDENTITY CASCADE"

    with get_conn(schema.database) as conn, conn.cursor() as cur:
        cur.execute(sql)

    logger.debug(
        "truncate_team_tables: team=%s truncated=%d tables=%s",
        schema.team,
        len(schema.table_names),
        schema.table_names,
    )
    return len(schema.table_names)


def drop_team_tables(schema: TeamSchema) -> int:
    """Drop every table named in ``schema.table_names`` if it exists.

    Unlike :func:`truncate_team_tables` (which empties rows but leaves the
    table present), this removes the tables themselves — for tests that need
    to simulate a genuinely fresh/empty database (e.g. proving a schema gets
    (re-)created before some other code path reads from it). Uses
    ``DROP TABLE IF EXISTS``, so a missing table is not an error. Returns
    the number of tables named in ``schema.table_names`` (each dropped if it
    exists), not a count of tables that were actually present.

    Raises ``RuntimeError`` when Postgres is disabled (matches
    ``ensure_team_schema``'s policy of failing loudly on misuse).
    """
    if not is_postgres_enabled():
        raise RuntimeError(f"drop_team_tables called for team={schema.team} but POSTGRES_HOST is not set.")
    if not schema.table_names:
        return 0

    # Validate every identifier before opening a connection, matching
    # truncate_team_tables — a bad name must fail fast, not after a pool wait.
    quoted = [_quote_ident(name) for name in schema.table_names]

    with get_conn(schema.database) as conn, conn.cursor() as cur:
        for ident in quoted:
            cur.execute(f"DROP TABLE IF EXISTS {ident} CASCADE")

    logger.debug(
        "drop_team_tables: team=%s dropped=%d tables=%s",
        schema.team,
        len(schema.table_names),
        schema.table_names,
    )
    return len(schema.table_names)


def truncate_all_teams(schemas: Iterable[TeamSchema]) -> int:
    """Truncate every team's tables in a single call.

    Convenience wrapper for top-level test fixtures that want to wipe
    the entire shared Postgres between integration-test runs.
    """
    total = 0
    for schema in schemas:
        if not schema.table_names:
            continue
        total += truncate_team_tables(schema)
    return total


def _real_postgres_schema_body(schema: TeamSchema, *, worker_id: str = "master"):
    """Generator body shared by every ``real_postgres_schema(schema)`` fixture.

    Split out from :func:`real_postgres_schema` so the skip/register/truncate
    sequence is directly unit-testable (drive the generator with ``next()``)
    without going through pytest's fixture machinery.

    ``worker_id`` is pytest-xdist's per-worker identifier: ``"master"`` only
    when pytest-xdist is inactive (no ``-n`` flag at all — not even ``-n 1``,
    which still runs as worker ``"gw0"``); ``"gw0"``, ``"gw1"``, etc. whenever
    it's active. Module/session-scoped fixtures are instantiated independently
    *per worker process*, and pytest-xdist's default scheduling does not
    guarantee every test in one module lands on the same worker, so under any
    active ``-n`` a sibling worker may still be exercising this schema's
    tables (in an unrelated test — possibly even one from the same module)
    when this worker's fixture instance tears down. An uncoordinated
    ``TRUNCATE ... CASCADE`` at that point would wipe rows the sibling worker
    is mid-test against, so setup/teardown truncation only runs on the
    ``worker_id == "master"`` (xdist-inactive) path; it's skipped under any
    ``-n`` invocation, logged at DEBUG.

    Coordinating a single truncate *after every xdist worker has finished*
    would need a real ``pytest_sessionfinish`` hook registered from a
    ``conftest.py`` — that hook fires once in the xdist controller process,
    after all workers report done — and a bare fixture (which only ever runs
    inside a worker, never the controller) can't reach that on a caller's
    behalf. Until a team actually needs that guarantee, tests sharing a
    schema under ``-n > 1`` should isolate via unique row identifiers instead
    (the existing convention — see
    ``branding_team/tests/test_store_real_postgres.py``'s ``uuid4``-suffixed
    data), not rely on inter-test truncation; expect rows to accumulate
    across repeated ``-n > 1`` runs against a persistent local Postgres (a
    non-issue in CI, where the Postgres container is thrown away per job).
    """
    if not is_postgres_enabled():
        pytest.skip(f"real Postgres tests require POSTGRES_HOST (team={schema.team})")
    register_team_schemas(schema)
    if worker_id == "master":
        # Truncate before yield so the first test (and every function-scoped
        # instance) starts from empty tables even when a prior interrupted
        # or non-xdist run left rows behind. Under xdist both setup and
        # teardown truncate are skipped — see docstring above.
        truncate_team_tables(schema)
    yield
    if worker_id != "master":
        logger.debug(
            "real_postgres_schema: skipping teardown truncate under xdist (worker=%s team=%s) "
            "to avoid racing sibling workers still using this schema",
            worker_id,
            schema.team,
        )
        return
    truncate_team_tables(schema)


def _xdist_worker_id(request: pytest.FixtureRequest) -> str:
    """Return the pytest-xdist worker id, or ``"master"`` when xdist is inactive.

    Preconditions:
        ``request`` is a live pytest ``FixtureRequest`` (has ``.config``).
    Postconditions:
        Returns ``request.config.workerinput["workerid"]`` when running under
        an xdist worker; otherwise ``"master"`` (xdist not installed, plugin
        disabled, or plain pytest with no ``-n``).
    """
    workerinput = getattr(request.config, "workerinput", None)
    if workerinput is None:
        return "master"
    return workerinput["workerid"]


def real_postgres_schema(schema: TeamSchema, *, scope: str = "module"):
    """Build an autouse pytest fixture that provisions ``schema`` against live Postgres.

    Registers ``schema`` once per fixture instance (per ``scope``), truncates
    its tables via :func:`truncate_team_tables` before yielding and again on
    teardown when running without pytest-xdist — so a plain (non-``-n``) run
    always starts each fixture instance from empty tables (including the first
    test after a polluted local DB) and leaves them empty afterward. Skips the
    test (rather than raising) when ``POSTGRES_HOST`` is unset, matching every
    other real-Postgres fixture in this repo.

    Under pytest-xdist (any ``-n``, including ``-n 1``), setup and teardown
    truncation are skipped entirely rather than attempted per-worker — see
    :func:`_real_postgres_schema_body` for why an uncoordinated cross-worker
    truncate is unsafe, and why safely coordinating it after every worker
    finishes isn't something this fixture factory can do without a caller
    also wiring up a ``conftest.py``-level hook.

    The returned fixture is always ``autouse=True``: assigning
    ``_my_schema = real_postgres_schema(SCHEMA)`` is enough for every test in
    the fixture's scope to get register/truncate; tests do not request it by
    name. ``scope`` is passed straight through to ``pytest.fixture(scope=...)`` —
    valid values are ``"session"``, ``"package"``, ``"module"`` (the default),
    ``"class"``, or ``"function"``; a wider scope amortizes the DDL/register
    cost across more tests at the price of more state shared between them.
    Raises ``ValueError`` immediately on any other value, rather than letting
    pytest's own less obvious error surface later at collection time.

    Worker identity is read from ``request.config.workerinput`` (with a
    ``"master"`` fallback) rather than depending on xdist's ``worker_id``
    fixture, so plain pytest runs work even when pytest-xdist is not installed
    or its plugin is disabled.

    Drop-in replacement for the per-team hand-rolled skip/register/truncate
    pattern. ``branding_team`` already wires it in
    ``tests/test_store.py`` (``scope="function"``) and
    ``tests/test_store_real_postgres.py`` (default module scope); other teams
    opt in the same way with ``_my_schema = real_postgres_schema(SCHEMA)``.
    """
    if scope not in _PYTEST_FIXTURE_SCOPES:
        raise ValueError(
            f"real_postgres_schema: invalid scope {scope!r}; must be one of {sorted(_PYTEST_FIXTURE_SCOPES)}"
        )

    def _fixture(request: pytest.FixtureRequest):
        yield from _real_postgres_schema_body(schema, worker_id=_xdist_worker_id(request))

    _fixture.__name__ = f"real_postgres_schema_{schema.team}"
    return pytest.fixture(scope=scope, autouse=True)(_fixture)


# Re-export the in-memory fake scaffold so callers can import from either
# ``shared.postgres.testing`` or ``shared.postgres.fake``.
from shared.postgres.fake import (  # noqa: E402
    FakeConn,
    FakeCursor,
    install_fake_postgres,
    unwrap_json,
)

__all__ = [
    "FakeConn",
    "FakeCursor",
    "drop_team_tables",
    "install_fake_postgres",
    "real_postgres_schema",
    "truncate_all_teams",
    "truncate_team_tables",
    "unwrap_json",
]
