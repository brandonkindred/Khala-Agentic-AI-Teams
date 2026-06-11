"""Tests for lazy first-provision seed-pack installation (`rules/provision.py`).

Split by dependency: the install-path cases hit live Postgres (marked
``_requires_pg`` so they skip when ``POSTGRES_HOST`` is unset, and request the
``pg_schema`` fixture); the best-effort guard branches mock the Postgres gate and
the store boundary, so they run everywhere — including the standard no-Postgres
environment, where they are the only coverage of the failure paths.
"""

from __future__ import annotations

import pytest

from agent_cognition import manifest_scope
from agent_cognition.memory.store import AgentCognitionStorageUnavailable
from agent_cognition.models import RuleSource
from agent_cognition.postgres import SCHEMA
from agent_cognition.rules import provision, store
from shared_postgres import is_postgres_enabled, register_team_schemas
from shared_postgres.testing import truncate_team_tables

# Applied per-test (not module-wide) so the DB-independent guard-branch tests
# below still run when Postgres is absent.
_requires_pg = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres provision tests",
)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Clear the per-process provisioned memo around every test (no DB)."""
    provision._reset_provisioned_cache()
    yield
    provision._reset_provisioned_cache()


@pytest.fixture
def pg_schema() -> None:
    """Register + truncate the cognition tables. Requested only by live-DB tests."""
    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)


# ---------------------------------------------------------------------------
# Happy path + idempotency (live Postgres)
# ---------------------------------------------------------------------------
@_requires_pg
def test_installs_declared_packs(pg_schema, monkeypatch):
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: ["default_guardrails"])
    ids = provision.ensure_seed_packs_installed("a")
    assert len(ids) == 1
    rules = store.list_rules("a")
    assert len(rules) == 1 and rules[0].source == RuleSource.SEED


@_requires_pg
def test_idempotent_via_memo(pg_schema, monkeypatch):
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: ["default_guardrails"])
    assert len(provision.ensure_seed_packs_installed("a")) == 1
    # Second call short-circuits on the per-process memo: nothing new, no duplicate.
    assert provision.ensure_seed_packs_installed("a") == []
    assert len(store.list_rules("a")) == 1


@_requires_pg
def test_idempotent_across_processes(pg_schema, monkeypatch):
    """Even without the memo, the deterministic ids make a re-install a no-op."""
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: ["default_guardrails"])
    assert len(provision.ensure_seed_packs_installed("a")) == 1
    provision._reset_provisioned_cache()  # simulate a fresh process
    assert provision.ensure_seed_packs_installed("a") == []
    assert len(store.list_rules("a")) == 1


@_requires_pg
def test_empty_rule_packs_is_noop_and_not_memoized(pg_schema, monkeypatch):
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: [])
    assert provision.ensure_seed_packs_installed("a") == []
    assert store.list_rules("a") == []
    # An empty result is indistinguishable from a transient lookup failure, so it
    # must not be memoized — a later invoke has to retry.
    assert "a" not in provision._PROVISIONED


@_requires_pg
def test_recovers_after_transient_empty_lookup(pg_schema, monkeypatch):
    """An empty list (e.g. registry not yet loaded) must not block a later install."""
    packs: list[str] = []
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: list(packs))
    assert provision.ensure_seed_packs_installed("a") == []  # registry "unavailable"
    assert store.list_rules("a") == []
    packs.append("default_guardrails")  # registry recovers
    assert len(provision.ensure_seed_packs_installed("a")) == 1
    assert len(store.list_rules("a")) == 1


@_requires_pg
def test_unknown_pack_skipped_others_install(pg_schema, monkeypatch):
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: ["nope", "default_guardrails"])
    ids = provision.ensure_seed_packs_installed("a")
    assert len(ids) == 1  # the unknown pack is skipped, the known one installs
    assert len(store.list_rules("a")) == 1


# ---------------------------------------------------------------------------
# Best-effort guard branches — DB-independent, so they run without Postgres.
# Each patches the Postgres gate and mocks the store, never touching a real DB.
# ---------------------------------------------------------------------------
def test_noop_when_postgres_disabled(monkeypatch):
    monkeypatch.setattr(provision, "is_postgres_enabled", lambda: False)
    # rule_packs must never be consulted once we've decided to no-op.
    monkeypatch.setattr(
        manifest_scope, "rule_packs", lambda _id: (_ for _ in ()).throw(AssertionError)
    )
    assert provision.ensure_seed_packs_installed("a") == []


def test_storage_outage_defers_without_memoizing(monkeypatch):
    # Patch the gate True so we reach the (mocked) store boundary without a DB.
    monkeypatch.setattr(provision, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: ["default_guardrails"])

    def _raise(*_a, **_k):
        raise AgentCognitionStorageUnavailable("down")

    monkeypatch.setattr(store, "install_seed_pack", _raise)
    assert provision.ensure_seed_packs_installed("a") == []
    # Not memoized: a later invoke retries.
    assert "a" not in provision._PROVISIONED


def test_unexpected_error_is_swallowed(monkeypatch):
    monkeypatch.setattr(provision, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: ["default_guardrails"])

    def _boom(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr(store, "install_seed_pack", _boom)
    assert provision.ensure_seed_packs_installed("a") == []
    assert "a" not in provision._PROVISIONED
