"""Tests for lazy first-provision seed-pack installation (`rules/provision.py`).

The install-path cases run against live Postgres (skipped when ``POSTGRES_HOST``
is unset, like the other store tests); the Postgres-off and error paths
monkeypatch their dependency, so they exercise the guard branches without a DB.
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

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres provision tests",
)


@pytest.fixture(autouse=True)
def _provision_schema() -> None:
    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)
    provision._reset_provisioned_cache()
    yield
    provision._reset_provisioned_cache()


# ---------------------------------------------------------------------------
# Happy path + idempotency
# ---------------------------------------------------------------------------
def test_installs_declared_packs(monkeypatch):
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: ["default_guardrails"])
    ids = provision.ensure_seed_packs_installed("a")
    assert len(ids) == 1
    rules = store.list_rules("a")
    assert len(rules) == 1 and rules[0].source == RuleSource.SEED


def test_idempotent_via_memo(monkeypatch):
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: ["default_guardrails"])
    assert len(provision.ensure_seed_packs_installed("a")) == 1
    # Second call short-circuits on the per-process memo: nothing new, no duplicate.
    assert provision.ensure_seed_packs_installed("a") == []
    assert len(store.list_rules("a")) == 1


def test_idempotent_across_processes(monkeypatch):
    """Even without the memo, the deterministic ids make a re-install a no-op."""
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: ["default_guardrails"])
    assert len(provision.ensure_seed_packs_installed("a")) == 1
    provision._reset_provisioned_cache()  # simulate a fresh process
    assert provision.ensure_seed_packs_installed("a") == []
    assert len(store.list_rules("a")) == 1


def test_empty_rule_packs_is_noop(monkeypatch):
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: [])
    assert provision.ensure_seed_packs_installed("a") == []
    assert store.list_rules("a") == []


def test_unknown_pack_skipped_others_install(monkeypatch):
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: ["nope", "default_guardrails"])
    ids = provision.ensure_seed_packs_installed("a")
    assert len(ids) == 1  # the unknown pack is skipped, the known one installs
    assert len(store.list_rules("a")) == 1


# ---------------------------------------------------------------------------
# Best-effort guard branches (no DB needed beyond the monkeypatch)
# ---------------------------------------------------------------------------
def test_noop_when_postgres_disabled(monkeypatch):
    monkeypatch.setattr(provision, "is_postgres_enabled", lambda: False)
    # rule_packs must never be consulted once we've decided to no-op.
    monkeypatch.setattr(
        manifest_scope, "rule_packs", lambda _id: (_ for _ in ()).throw(AssertionError)
    )
    assert provision.ensure_seed_packs_installed("a") == []


def test_storage_outage_defers_without_memoizing(monkeypatch):
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: ["default_guardrails"])

    def _raise(*_a, **_k):
        raise AgentCognitionStorageUnavailable("down")

    monkeypatch.setattr(store, "install_seed_pack", _raise)
    assert provision.ensure_seed_packs_installed("a") == []
    # Not memoized: a later invoke retries.
    assert "a" not in provision._PROVISIONED


def test_unexpected_error_is_swallowed(monkeypatch):
    monkeypatch.setattr(manifest_scope, "rule_packs", lambda _id: ["default_guardrails"])

    def _boom(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr(store, "install_seed_pack", _boom)
    assert provision.ensure_seed_packs_installed("a") == []
    assert "a" not in provision._PROVISIONED
