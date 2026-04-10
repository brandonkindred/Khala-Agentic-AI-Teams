"""Tests for shared_postgres — all mocked, no live Postgres required."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from shared_postgres import (
    TEAM_POSTGRES_MODULES,
    TeamSchema,
    close_pool,
    ensure_team_schema,
    is_postgres_enabled,
    register_all_team_schemas,
    register_team_schemas,
)
from shared_postgres import client as client_mod
from shared_postgres import registry as registry_mod
from shared_postgres import runner as runner_mod

# ---------------------------------------------------------------------------
# Env-var gating
# ---------------------------------------------------------------------------


def test_is_postgres_enabled_false_when_unset(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    assert is_postgres_enabled() is False


def test_is_postgres_enabled_false_when_empty(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "   ")
    assert is_postgres_enabled() is False


def test_is_postgres_enabled_true_when_set(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    assert is_postgres_enabled() is True


# ---------------------------------------------------------------------------
# TeamSchema dataclass
# ---------------------------------------------------------------------------


def test_team_schema_defaults():
    schema = TeamSchema(team="foo")
    assert schema.team == "foo"
    assert schema.database is None
    assert schema.statements == []


def test_team_schema_frozen():
    schema = TeamSchema(team="foo")
    with pytest.raises(Exception):
        schema.team = "bar"  # type: ignore[misc]


def test_team_schema_database_override():
    schema = TeamSchema(team="job_service", database="khala_jobs", statements=["SELECT 1"])
    assert schema.database == "khala_jobs"
    assert schema.statements == ["SELECT 1"]


# ---------------------------------------------------------------------------
# ensure_team_schema (mocked connection)
# ---------------------------------------------------------------------------


@contextmanager
def _fake_conn_factory(executed: list[str], fail_on: set[int] | None = None):
    """Yield a fake connection whose cursor.execute records statements."""
    call_index = {"n": 0}
    fail_on = fail_on or set()

    cursor = MagicMock()

    def _execute(sql: str) -> None:
        idx = call_index["n"]
        call_index["n"] += 1
        if idx in fail_on:
            raise RuntimeError(f"synthetic DDL failure at {idx}")
        executed.append(sql)

    cursor.execute.side_effect = _execute
    cursor.__enter__ = lambda self: cursor
    cursor.__exit__ = lambda self, *a: None

    conn = MagicMock()
    conn.cursor.return_value = cursor
    yield conn


def test_ensure_team_schema_runs_all_ddl(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")

    executed: list[str] = []

    @contextmanager
    def fake_get_conn(database=None):
        with _fake_conn_factory(executed) as c:
            yield c

    monkeypatch.setattr(runner_mod, "get_conn", fake_get_conn)

    schema = TeamSchema(
        team="demo",
        statements=[
            "CREATE TABLE IF NOT EXISTS demo_a (id TEXT PRIMARY KEY)",
            "CREATE TABLE IF NOT EXISTS demo_b (id TEXT PRIMARY KEY)",
            "CREATE INDEX IF NOT EXISTS idx_demo_a_id ON demo_a(id)",
        ],
    )

    applied = ensure_team_schema(schema)
    assert applied == 3
    assert len(executed) == 3
    assert "demo_a" in executed[0]
    assert "demo_b" in executed[1]
    assert "idx_demo_a_id" in executed[2]


def test_ensure_team_schema_continues_past_failure(monkeypatch, caplog):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")

    state = {"call": 0}

    @contextmanager
    def fake_get_conn(database=None):
        cursor = MagicMock()

        def _execute(sql):
            idx = state["call"]
            state["call"] += 1
            if idx == 1:  # fail on second statement
                raise RuntimeError("boom")

        cursor.execute.side_effect = _execute
        cursor.__enter__ = lambda self: cursor
        cursor.__exit__ = lambda self, *a: None
        conn = MagicMock()
        conn.cursor.return_value = cursor
        yield conn

    monkeypatch.setattr(runner_mod, "get_conn", fake_get_conn)

    schema = TeamSchema(
        team="demo",
        statements=[
            "CREATE TABLE IF NOT EXISTS demo_a (id TEXT)",
            "CREATE TABLE IF NOT EXISTS demo_b (id TEXT)",
            "CREATE TABLE IF NOT EXISTS demo_c (id TEXT)",
        ],
    )

    with caplog.at_level("ERROR"):
        applied = ensure_team_schema(schema)

    assert applied == 2
    assert any("stmt_index=1" in rec.message for rec in caplog.records)


def test_ensure_team_schema_raises_when_disabled(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    schema = TeamSchema(team="demo", statements=["SELECT 1"])
    with pytest.raises(RuntimeError, match="POSTGRES_HOST is not set"):
        ensure_team_schema(schema)


# ---------------------------------------------------------------------------
# register_team_schemas (the no-op-safe wrapper)
# ---------------------------------------------------------------------------


def test_register_team_schemas_noop_when_disabled(monkeypatch, caplog):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    schema = TeamSchema(team="demo", statements=["SELECT 1"])
    with caplog.at_level("INFO"):
        result = register_team_schemas(schema)

    assert result is False
    assert any("postgres disabled" in rec.message for rec in caplog.records)


def test_register_team_schemas_runs_when_enabled(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    called = {"n": 0}

    def fake_ensure(schema):
        called["n"] += 1
        return len(schema.statements)

    monkeypatch.setattr(runner_mod, "ensure_team_schema", fake_ensure)
    result = register_team_schemas(TeamSchema(team="demo", statements=["SELECT 1"]))
    assert result is True
    assert called["n"] == 1


# ---------------------------------------------------------------------------
# register_all_team_schemas (registry iteration)
# ---------------------------------------------------------------------------


def test_register_all_team_schemas_imports_and_calls(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")

    stub_schema = TeamSchema(team="stub_team", statements=["SELECT 1"])

    class _StubModule:
        SCHEMA = stub_schema

    def fake_import_module(name):
        if name == "fake.stub":
            return _StubModule
        raise ImportError(name)

    monkeypatch.setattr(registry_mod, "TEAM_POSTGRES_MODULES", {"stub_team": "fake.stub"})
    monkeypatch.setattr(registry_mod.importlib, "import_module", fake_import_module)

    calls: list[TeamSchema] = []

    def fake_register(schema):
        calls.append(schema)
        return True

    monkeypatch.setattr(registry_mod, "register_team_schemas", fake_register)

    results = register_all_team_schemas()
    assert results == {"stub_team": True}
    assert len(calls) == 1
    assert calls[0] is stub_schema


def test_register_all_team_schemas_only_filter(monkeypatch):
    monkeypatch.setattr(
        registry_mod,
        "TEAM_POSTGRES_MODULES",
        {"a": "fake.a", "b": "fake.b"},
    )

    def fake_import(name):
        class M:
            SCHEMA = TeamSchema(team=name.split(".")[-1], statements=[])

        return M

    monkeypatch.setattr(registry_mod.importlib, "import_module", fake_import)
    monkeypatch.setattr(registry_mod, "register_team_schemas", lambda s: True)

    results = register_all_team_schemas(only=["a"])
    assert list(results.keys()) == ["a"]


def test_register_all_team_schemas_skips_module_missing_schema(monkeypatch, caplog):
    class _NoSchemaModule:
        pass

    monkeypatch.setattr(registry_mod, "TEAM_POSTGRES_MODULES", {"broken": "fake.broken"})
    monkeypatch.setattr(registry_mod.importlib, "import_module", lambda name: _NoSchemaModule)

    with caplog.at_level("WARNING"):
        results = register_all_team_schemas()

    assert results == {"broken": False}
    assert any("does not export a SCHEMA" in rec.message for rec in caplog.records)


def test_register_all_team_schemas_import_failure_is_isolated(monkeypatch, caplog):
    def _boom(_name):
        raise ImportError("synthetic")

    monkeypatch.setattr(
        registry_mod,
        "TEAM_POSTGRES_MODULES",
        {"broken": "fake.broken", "ok": "fake.ok"},
    )
    call_log: list[str] = []

    def fake_import(name):
        call_log.append(name)
        if name == "fake.broken":
            raise ImportError("synthetic")

        class M:
            SCHEMA = TeamSchema(team="ok", statements=[])

        return M

    monkeypatch.setattr(registry_mod.importlib, "import_module", fake_import)
    monkeypatch.setattr(registry_mod, "register_team_schemas", lambda s: True)

    with caplog.at_level("ERROR"):
        results = register_all_team_schemas()

    assert results == {"broken": False, "ok": True}


def test_registry_has_expected_entries():
    # Sanity check on the real registry — every expected team appears.
    for team in (
        "unified_api",
        "job_service",
        "branding",
        "startup_advisor",
        "user_agent_founder",
        "team_assistant",
        "agentic_team_provisioning",
        "blogging",
    ):
        assert team in TEAM_POSTGRES_MODULES, f"{team} missing from TEAM_POSTGRES_MODULES"


# ---------------------------------------------------------------------------
# Connection helpers — guard paths exercised without a live DB
# ---------------------------------------------------------------------------


def test_connect_raises_when_disabled(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    with pytest.raises(RuntimeError, match="POSTGRES_HOST is not set"):
        client_mod._connect()


def test_dsn_defaults(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "h")
    monkeypatch.setenv("POSTGRES_PORT", "1234")
    monkeypatch.setenv("POSTGRES_USER", "u")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p")
    monkeypatch.setenv("POSTGRES_DB", "d")
    dsn = client_mod._dsn()
    assert "host=h" in dsn
    assert "port=1234" in dsn
    assert "dbname=d" in dsn
    assert "user=u" in dsn
    assert "password=p" in dsn


def test_dsn_database_override(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "h")
    monkeypatch.setenv("POSTGRES_DB", "default_db")
    dsn = client_mod._dsn("other_db")
    assert "dbname=other_db" in dsn


def test_pool_sizes_defaults(monkeypatch):
    monkeypatch.delenv("POSTGRES_POOL_MIN_SIZE", raising=False)
    monkeypatch.delenv("POSTGRES_POOL_MAX_SIZE", raising=False)
    min_size, max_size = client_mod._pool_sizes()
    assert (min_size, max_size) == (2, 10)


def test_pool_sizes_from_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_POOL_MIN_SIZE", "5")
    monkeypatch.setenv("POSTGRES_POOL_MAX_SIZE", "25")
    assert client_mod._pool_sizes() == (5, 25)


def test_pool_sizes_clamps_max_below_min(monkeypatch):
    monkeypatch.setenv("POSTGRES_POOL_MIN_SIZE", "8")
    monkeypatch.setenv("POSTGRES_POOL_MAX_SIZE", "4")
    assert client_mod._pool_sizes() == (8, 8)


def test_get_or_create_pool_raises_when_disabled(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    with pytest.raises(RuntimeError, match="POSTGRES_HOST is not set"):
        client_mod._get_or_create_pool()


class _FakePool:
    def __init__(self):
        self.closed = False
        self.conn = MagicMock()
        self._conn_cm = _conn_context(self.conn)

    def connection(self):
        return self._conn_cm

    def close(self):
        self.closed = True


@contextmanager
def _conn_context(conn):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def test_get_conn_uses_pooled_connection(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    fake = _FakePool()

    def _fake_get_or_create_pool(database=None):
        return fake

    monkeypatch.setattr(client_mod, "_get_or_create_pool", _fake_get_or_create_pool)

    with client_mod.get_conn() as c:
        assert c is fake.conn

    fake.conn.commit.assert_called_once()
    fake.conn.rollback.assert_not_called()


def test_get_conn_rolls_back_on_error(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    fake = _FakePool()
    monkeypatch.setattr(client_mod, "_get_or_create_pool", lambda database=None: fake)

    with pytest.raises(RuntimeError), client_mod.get_conn():
        raise RuntimeError("boom")

    fake.conn.rollback.assert_called_once()
    fake.conn.commit.assert_not_called()


def test_close_pool_is_idempotent():
    client_mod._pools.clear()
    close_pool()
    close_pool("some_db")  # no-op when nothing registered


def test_close_pool_closes_registered_pools(monkeypatch):
    client_mod._pools.clear()
    fake_a = _FakePool()
    fake_b = _FakePool()
    client_mod._pools["db_a"] = fake_a
    client_mod._pools["db_b"] = fake_b

    close_pool("db_a")
    assert fake_a.closed is True
    assert fake_b.closed is False
    assert "db_a" not in client_mod._pools
    assert "db_b" in client_mod._pools

    close_pool()  # closes remaining
    assert fake_b.closed is True
    assert client_mod._pools == {}


# ---------------------------------------------------------------------------
# TeamSchema.table_names + truncate_team_tables
# ---------------------------------------------------------------------------


def test_team_schema_table_names_default():
    schema = TeamSchema(team="foo")
    assert schema.table_names == []


def test_truncate_team_tables_noop_on_empty_list(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    from shared_postgres.testing import truncate_team_tables

    schema = TeamSchema(team="demo", statements=[], table_names=[])
    assert truncate_team_tables(schema) == 0


def test_truncate_team_tables_issues_truncate(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    from shared_postgres import testing as testing_mod

    executed: list[str] = []

    @contextmanager
    def fake_get_conn(database=None):
        cursor = MagicMock()
        cursor.__enter__ = lambda self: cursor
        cursor.__exit__ = lambda self, *a: None
        cursor.execute.side_effect = lambda sql: executed.append(sql)
        conn = MagicMock()
        conn.cursor.return_value = cursor
        yield conn

    monkeypatch.setattr(testing_mod, "get_conn", fake_get_conn)

    schema = TeamSchema(
        team="demo",
        table_names=["demo_a", "demo_b"],
    )
    applied = testing_mod.truncate_team_tables(schema)
    assert applied == 2
    assert len(executed) == 1
    assert "TRUNCATE TABLE" in executed[0]
    assert '"demo_a"' in executed[0]
    assert '"demo_b"' in executed[0]
    assert "RESTART IDENTITY CASCADE" in executed[0]


def test_truncate_team_tables_raises_when_disabled(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    from shared_postgres.testing import truncate_team_tables

    schema = TeamSchema(team="demo", table_names=["demo_a"])
    with pytest.raises(RuntimeError, match="POSTGRES_HOST is not set"):
        truncate_team_tables(schema)


def test_truncate_team_tables_rejects_quote_in_name(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    from shared_postgres.testing import truncate_team_tables

    schema = TeamSchema(team="demo", table_names=['bad"name'])
    with pytest.raises(ValueError, match="double-quote"):
        truncate_team_tables(schema)


# ---------------------------------------------------------------------------
# @timed_query decorator
# ---------------------------------------------------------------------------


def test_timed_query_logs_debug_on_fast_call(caplog):
    from shared_postgres.metrics import timed_query

    @timed_query(store="demo")
    def fast(x):
        return x * 2

    with caplog.at_level("DEBUG", logger="shared_postgres.metrics"):
        assert fast(5) == 10

    msgs = [rec.message for rec in caplog.records if rec.name == "shared_postgres.metrics"]
    assert any("store=demo op=fast" in m and "status=ok" in m for m in msgs)


def test_timed_query_logs_info_on_slow_call(monkeypatch, caplog):
    from shared_postgres.metrics import timed_query

    # Force the threshold to 0 so any duration is "slow".
    monkeypatch.setenv("POSTGRES_SLOW_QUERY_MS", "0")

    @timed_query(store="demo", op="slow_op")
    def slow():
        return 42

    with caplog.at_level("INFO", logger="shared_postgres.metrics"):
        assert slow() == 42

    info_msgs = [
        rec.message
        for rec in caplog.records
        if rec.name == "shared_postgres.metrics" and rec.levelname == "INFO"
    ]
    assert any("store=demo op=slow_op" in m and "slow=true" in m for m in info_msgs)


def test_timed_query_re_raises_and_logs_error(caplog):
    from shared_postgres.metrics import timed_query

    @timed_query(store="demo")
    def boom():
        raise ValueError("nope")

    with caplog.at_level("WARNING", logger="shared_postgres.metrics"):
        with pytest.raises(ValueError, match="nope"):
            boom()

    warn_msgs = [
        rec.message
        for rec in caplog.records
        if rec.name == "shared_postgres.metrics" and rec.levelname == "WARNING"
    ]
    assert any("status=error" in m and "ValueError" in m for m in warn_msgs)


# ---------------------------------------------------------------------------
# Lazy Json / dict_row re-exports
# ---------------------------------------------------------------------------


def _psycopg_installed() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _psycopg_installed(), reason="psycopg not installed")
def test_json_reexport_returns_psycopg_adapter():
    import shared_postgres

    Json = shared_postgres.Json
    from psycopg.types.json import Json as PsycopgJson

    assert Json is PsycopgJson


@pytest.mark.skipif(not _psycopg_installed(), reason="psycopg not installed")
def test_dict_row_reexport_returns_psycopg_factory():
    import shared_postgres

    dict_row = shared_postgres.dict_row
    from psycopg.rows import dict_row as psycopg_dict_row

    assert dict_row is psycopg_dict_row


def test_getattr_raises_on_unknown():
    import shared_postgres

    with pytest.raises(AttributeError, match="no attribute"):
        _ = shared_postgres.not_a_real_thing  # type: ignore[attr-defined]
