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
    schema = TeamSchema(team="job_service", database="strands_jobs", statements=["SELECT 1"])
    assert schema.database == "strands_jobs"
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


def test_close_pool_is_idempotent(monkeypatch):
    # No connections tracked — close_pool must not raise.
    client_mod._active_conns.clear()
    close_pool()
    close_pool("some_db")


def test_close_pool_warns_on_leaked_conns(monkeypatch, caplog):
    client_mod._active_conns.clear()
    client_mod._active_conns["mydb"] = 2
    with caplog.at_level("WARNING"):
        close_pool()
    assert any("active connection" in rec.message for rec in caplog.records)
    assert client_mod._active_conns == {}


def test_get_conn_commits_on_success(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")

    conn = MagicMock()
    monkeypatch.setattr(client_mod, "_connect", lambda database=None: conn)
    client_mod._active_conns.clear()

    with client_mod.get_conn() as c:
        assert c is conn

    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()
    conn.close.assert_called_once()
    assert client_mod._active_conns.get("postgres", 0) == 0


def test_get_conn_rolls_back_on_error(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")

    conn = MagicMock()
    monkeypatch.setattr(client_mod, "_connect", lambda database=None: conn)
    client_mod._active_conns.clear()

    with pytest.raises(RuntimeError), client_mod.get_conn():
        raise RuntimeError("boom")

    conn.rollback.assert_called_once()
    conn.close.assert_called_once()
    assert client_mod._active_conns.get("postgres", 0) == 0
