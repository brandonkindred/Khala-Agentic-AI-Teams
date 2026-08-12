"""Tests for the dynamic-manifest Postgres store.

``_store_active`` gating is hermetic (env only). The CRUD suite is guarded on a
live Postgres — skipped when ``POSTGRES_HOST`` is unset — and uses the shared
schema-registration + truncation helpers, mirroring ``agent_console``'s store
tests.
"""

from __future__ import annotations

import re

import pytest

from agent_platform.registry import dynamic_store as ds
from agent_platform.registry.models import (
    AgentManifest,
    CognitionSpec,
    IOSchema,
    SandboxSpec,
    SourceInfo,
)


def test_table_is_a_bare_sql_identifier() -> None:
    # _TABLE is f-string-interpolated into every raw SQL statement in this
    # module (psycopg %s placeholders can't parameterize identifiers); this
    # locks in the module-load-time invariant that guards that interpolation.
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ds._TABLE)


def _manifest(agent_id: str, team: str = "agent_studio") -> AgentManifest:
    return AgentManifest(
        id=agent_id,
        team=team,
        name="Full",
        summary="s",
        tags=["studio", "x"],
        inputs=IOSchema(inline_schema={"type": "object"}, description="in"),
        outputs=IOSchema(schema_ref="m:Out", description="out"),
        cognition=CognitionSpec(rule_packs=["default_guardrails"], tools=["web.search"]),
        sandbox=SandboxSpec(access_tier="standard"),
        source=SourceInfo(entrypoint="m:f", anatomy_ref="x/ANATOMY.md"),
    )


# --------------------------------------------------------------------------- #
# _store_active gating (hermetic — env only)
# --------------------------------------------------------------------------- #


def test_store_inactive_when_postgres_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.delenv("SANDBOX_AGENT_ID", raising=False)
    assert ds._store_active() is False


def test_store_inactive_inside_a_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inside a sandbox POSTGRES_HOST points at the sandbox's own empty DB — the
    # dynamic store must be OFF there even though Postgres "looks" configured.
    monkeypatch.setenv("POSTGRES_HOST", "sandbox-postgres")
    monkeypatch.setenv("SANDBOX_AGENT_ID", "agent_team_studio.agent_studio.x-1")
    assert ds._store_active() is False


# --------------------------------------------------------------------------- #
# _with_retry (hermetic — no Postgres, plain function retry semantics)
# --------------------------------------------------------------------------- #


def test_with_retry_succeeds_first_try(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "_WRITE_RETRY_DELAY_S", 0.0)
    calls = []
    assert ds._with_retry(lambda: calls.append(1) or "ok") == "ok"
    assert calls == [1]


def test_with_retry_recovers_after_one_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "_WRITE_RETRY_DELAY_S", 0.0)
    attempts = {"n": 0}

    def _flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient blip")
        return "ok"

    assert ds._with_retry(_flaky) == "ok"
    assert attempts["n"] == 2


def test_with_retry_propagates_after_exhausting_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "_WRITE_RETRY_DELAY_S", 0.0)
    attempts = {"n": 0}

    def _always_fails():
        attempts["n"] += 1
        raise RuntimeError("sustained outage")

    with pytest.raises(RuntimeError, match="sustained outage"):
        ds._with_retry(_always_fails)
    assert attempts["n"] == ds._WRITE_RETRY_ATTEMPTS


def test_store_active_when_postgres_on_outside_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "platform-postgres")
    monkeypatch.delenv("SANDBOX_AGENT_ID", raising=False)
    assert ds._store_active() is True


# --------------------------------------------------------------------------- #
# _ensure_schema (hermetic — the table DDL is applied lazily on first write so a
# write from a process that never ran the unified-api lifespan still lands)
# --------------------------------------------------------------------------- #


def test_ensure_schema_registers_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    import shared.postgres

    calls = []
    monkeypatch.setattr(
        shared.postgres, "register_team_schemas", lambda schema: calls.append(schema)
    )
    monkeypatch.setattr(ds, "_schema_ensured", False)
    ds._ensure_schema()
    ds._ensure_schema()  # guarded — no second DDL apply
    assert len(calls) == 1
    assert ds._schema_ensured is True


def test_ensure_schema_failure_leaves_guard_unset_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shared.postgres

    monkeypatch.setattr(ds, "_schema_ensured", False)

    def _boom(_schema):
        raise RuntimeError("postgres not ready")

    monkeypatch.setattr(shared.postgres, "register_team_schemas", _boom)
    with pytest.raises(RuntimeError, match="postgres not ready"):
        ds._ensure_schema()
    # Guard stays False so a later write retries the DDL once Postgres recovers.
    assert ds._schema_ensured is False


def test_upsert_ensures_schema_before_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    # The write path must apply the DDL *before* the INSERT, so the first
    # registration from a process that never ran the lifespan still lands.
    order = []
    monkeypatch.setattr(ds, "_ensure_schema", lambda: order.append("ensure"))
    monkeypatch.setattr(ds, "_with_retry", lambda fn: order.append("write"))
    monkeypatch.setattr(ds, "clear_cache", lambda: order.append("clear"))
    ds.upsert(_manifest("agent_team_studio.agent_studio.x-1"))
    assert order == ["ensure", "write", "clear"]


# --------------------------------------------------------------------------- #
# CRUD (live Postgres — skipped when POSTGRES_HOST is unset)
# --------------------------------------------------------------------------- #

from shared.postgres import is_postgres_enabled  # noqa: E402


@pytest.mark.skipif(
    not is_postgres_enabled(), reason="POSTGRES_HOST not set; skipping live-Postgres store tests"
)
class TestLivePostgres:
    @pytest.fixture(autouse=True)
    def _provision_schema(self):
        from agent_platform.registry.postgres import SCHEMA
        from shared.postgres import register_team_schemas
        from shared.postgres.testing import truncate_team_tables

        register_team_schemas(SCHEMA)
        truncate_team_tables(SCHEMA)
        ds.clear_cache()
        yield
        ds.clear_cache()

    def test_upsert_get_round_trips_full_manifest(self) -> None:
        original = _manifest("agent_team_studio.agent_studio.rt-1")
        ds.upsert(original)
        got = ds.get("agent_team_studio.agent_studio.rt-1")
        assert got is not None
        # Round-trip fidelity across every rich sub-object.
        assert got.model_dump(mode="json") == original.model_dump(mode="json")

    def test_get_unknown_returns_none(self) -> None:
        assert ds.get("agent_team_studio.agent_studio.missing") is None

    def test_upsert_is_idempotent_by_id(self) -> None:
        ds.upsert(_manifest("agent_team_studio.agent_studio.same"))
        m2 = _manifest("agent_team_studio.agent_studio.same")
        m2.name = "Renamed"
        ds.upsert(m2)
        got = ds.get("agent_team_studio.agent_studio.same")
        assert got is not None and got.name == "Renamed"
        assert len(ds.all()) == 1

    def test_delete_removes_row(self) -> None:
        ds.upsert(_manifest("agent_team_studio.agent_studio.del"))
        ds.delete("agent_team_studio.agent_studio.del")
        assert ds.get("agent_team_studio.agent_studio.del") is None

    def test_delete_unknown_is_noop(self) -> None:
        ds.delete("agent_team_studio.agent_studio.nope")  # must not raise

    def test_all_returns_every_row(self) -> None:
        ds.upsert(_manifest("agent_team_studio.agent_studio.a"))
        ds.upsert(_manifest("agent_team_studio.agent_studio.b"))
        assert {m.id for m in ds.all()} == {
            "agent_team_studio.agent_studio.a",
            "agent_team_studio.agent_studio.b",
        }

    def test_manifests_with_prefix_scopes_by_prefix(self) -> None:
        ds.upsert(_manifest("agentic.team-x.gen-1", team="agentic_team_provisioning"))
        ds.upsert(_manifest("agentic.team-y.gen-1", team="agentic_team_provisioning"))
        got = ds.manifests_with_prefix("agentic.team-x.")
        assert {m.id for m in got} == {"agentic.team-x.gen-1"}

    def test_manifests_with_prefix_escapes_like_metachars(self) -> None:
        # An id containing LIKE metachars ('_' / '%') must match literally, not as wildcards.
        ds.upsert(_manifest("agent_team_studio.agent_studio.a_b"))
        ds.upsert(_manifest("agent_team_studio.agent_studio.axb"))
        ds.upsert(_manifest("agent_team_studio.agent_studio.a%b"))
        ds.upsert(_manifest("agent_team_studio.agent_studio.aXb"))
        assert {m.id for m in ds.manifests_with_prefix("agent_team_studio.agent_studio.a_")} == {
            "agent_team_studio.agent_studio.a_b"
        }
        assert {m.id for m in ds.manifests_with_prefix("agent_team_studio.agent_studio.a%")} == {
            "agent_team_studio.agent_studio.a%b"
        }

    def test_replace_manifests_is_atomic(self) -> None:
        keep = _manifest("agent_team_studio.agent_studio.keep")
        drop = _manifest("agent_team_studio.agent_studio.drop")
        ds.upsert(keep)
        ds.upsert(drop)
        new = _manifest("agent_team_studio.agent_studio.new")
        ds.replace_manifests([new], [drop.id])
        assert ds.get(drop.id) is None
        assert ds.get(new.id) is not None
        assert ds.get(keep.id) is not None
        assert {m.id for m in ds.all()} == {
            "agent_team_studio.agent_studio.keep",
            "agent_team_studio.agent_studio.new",
        }
