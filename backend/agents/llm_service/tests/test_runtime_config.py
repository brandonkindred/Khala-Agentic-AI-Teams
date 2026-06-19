"""Tests for llm_service.runtime_config (TTL cache + Postgres gating)."""

from __future__ import annotations

import pytest

import shared_postgres.secrets as secrets_mod
from llm_service import runtime_config as rc


@pytest.fixture(autouse=True)
def _reset_cache():
    rc.clear_cache()
    yield
    rc.clear_cache()


def _fake_store(monkeypatch, store: dict, *, enabled=True):
    monkeypatch.setattr(rc, "_postgres_enabled", lambda: enabled)

    def _get_secrets(service, keys):
        return {k: store[k] for k in keys if k in store}

    monkeypatch.setattr(secrets_mod, "get_secrets", _get_secrets)


def test_returns_empty_when_postgres_disabled(monkeypatch):
    _fake_store(monkeypatch, {rc.KEY_PROVIDER: "claude"}, enabled=False)
    assert rc.get_runtime(rc.KEY_PROVIDER) == ""


def test_reads_value_from_store(monkeypatch):
    _fake_store(monkeypatch, {rc.KEY_PROVIDER: "claude", rc.KEY_MODEL: "claude-opus-4-8"})
    assert rc.get_runtime(rc.KEY_PROVIDER) == "claude"
    assert rc.get_runtime(rc.KEY_MODEL) == "claude-opus-4-8"
    assert rc.get_runtime(rc.KEY_CLAUDE_API_KEY) == ""


def test_ttl_cache_holds_value_until_cleared(monkeypatch):
    store = {rc.KEY_PROVIDER: "ollama"}
    _fake_store(monkeypatch, store)
    assert rc.get_runtime(rc.KEY_PROVIDER) == "ollama"
    # Mutate the underlying store; cache (default 30s TTL) still serves old value.
    store[rc.KEY_PROVIDER] = "claude"
    assert rc.get_runtime(rc.KEY_PROVIDER) == "ollama"
    # Explicit invalidation reloads.
    rc.clear_cache()
    assert rc.get_runtime(rc.KEY_PROVIDER) == "claude"


def test_zero_ttl_reads_through(monkeypatch):
    monkeypatch.setenv(rc.ENV_RUNTIME_TTL, "0")
    store = {rc.KEY_PROVIDER: "ollama"}
    _fake_store(monkeypatch, store)
    assert rc.get_runtime(rc.KEY_PROVIDER) == "ollama"
    store[rc.KEY_PROVIDER] = "claude"
    assert rc.get_runtime(rc.KEY_PROVIDER) == "claude"


def test_snapshot_bypasses_cache(monkeypatch):
    store = {rc.KEY_PROVIDER: "ollama"}
    _fake_store(monkeypatch, store)
    rc.get_runtime(rc.KEY_PROVIDER)  # prime cache with "ollama"
    store[rc.KEY_PROVIDER] = "claude"
    snap = rc.snapshot()
    assert snap[rc.KEY_PROVIDER] == "claude"  # fresh read, not cached


def test_unknown_key_asserts():
    with pytest.raises(AssertionError):
        rc.get_runtime("not_a_key")


def test_load_returns_empty_when_batch_read_raises(monkeypatch):
    def _boom(service, keys):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(rc, "_postgres_enabled", lambda: True)
    monkeypatch.setattr(secrets_mod, "get_secrets", _boom)
    # A failed batch read resolves every key to "" (env fallback), never raises.
    assert rc.get_runtime(rc.KEY_MODEL) == ""
    assert rc.get_runtime(rc.KEY_PROVIDER) == ""


def test_postgres_enabled_swallows_import_error(monkeypatch):
    import shared_postgres

    monkeypatch.setattr(
        shared_postgres, "is_postgres_enabled", lambda: (_ for _ in ()).throw(RuntimeError("x"))
    )
    # _postgres_enabled must never raise.
    assert rc._postgres_enabled() is False


def test_ttl_defensive_parse(monkeypatch):
    monkeypatch.setenv(rc.ENV_RUNTIME_TTL, "garbage")
    assert rc._ttl_seconds() == rc._DEFAULT_TTL_S
    monkeypatch.setenv(rc.ENV_RUNTIME_TTL, "-5")
    assert rc._ttl_seconds() == 0.0
