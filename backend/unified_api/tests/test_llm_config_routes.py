"""Tests for /api/llm-config GET/PUT — masking, validation, persistence, caches.

Builds a minimal app with just the llm_config router so the whole unified_api
graph (and every team dependency) need not import.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from unified_api.routes import llm_config as route  # noqa: E402


@pytest.fixture
def app_client(monkeypatch):
    """A TestClient over a minimal app, with the secret store + caches stubbed."""
    calls: dict = {"set": [], "cache_clears": 0, "runtime_clears": 0}
    monkeypatch.setattr(route, "is_postgres_enabled", lambda: True)
    # The PUT handler writes every changed key in one set_secrets() transaction.
    # Flatten the batch into (svc, key, val) tuples so existing per-key assertions
    # keep working while still proving the atomic call path is exercised.
    monkeypatch.setattr(
        route,
        "set_secrets",
        lambda svc, values: calls["set"].extend((svc, k, v) for k, v in values.items()),
    )
    monkeypatch.setattr(
        route, "clear_client_cache", lambda: calls.__setitem__("cache_clears", calls["cache_clears"] + 1)
    )
    monkeypatch.setattr(
        route.runtime_config, "clear_cache", lambda: calls.__setitem__("runtime_clears", calls["runtime_clears"] + 1)
    )
    # Make resolvers deterministic regardless of host env.
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    for var in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "ANTHROPIC_API_KEY",
        "LLM_CLAUDE_API_KEY",
        "OLLAMA_API_KEY",
        "LLM_OLLAMA_API_KEY",
        "LLM_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app), calls, monkeypatch


def test_get_returns_defaults_and_options(app_client):
    client, _calls, _mp = app_client
    resp = client.get("/api/llm-config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "ollama"
    assert "ollama" in body["provider_options"] and "claude" in body["provider_options"]
    assert "claude-opus-4-8" in body["claude_model_options"]
    assert body["claude_api_key_configured"] is False


def test_get_masks_keys_but_reports_configured(app_client):
    client, _calls, mp = app_client
    mp.setenv("LLM_PROVIDER", "claude")
    mp.setenv("ANTHROPIC_API_KEY", "sk-super-secret")
    resp = client.get("/api/llm-config")
    body = resp.json()
    assert body["provider"] == "claude"
    assert body["claude_api_key_configured"] is True
    # The key must never appear anywhere in the response.
    assert "sk-super-secret" not in resp.text


def test_put_persists_and_clears_caches(app_client):
    client, calls, _mp = app_client
    resp = client.put(
        "/api/llm-config",
        json={"provider": "claude", "model": "claude-opus-4-8", "claude_api_key": "sk-new"},
    )
    assert resp.status_code == 200
    stored = dict((k, v) for _s, k, v in calls["set"])
    assert stored[route.runtime_config.KEY_PROVIDER] == "claude"
    assert stored[route.runtime_config.KEY_CLAUDE_MODEL] == "claude-opus-4-8"
    assert stored[route.runtime_config.KEY_CLAUDE_API_KEY] == "sk-new"
    assert calls["cache_clears"] == 1
    # Cleared twice: once before the keyless-Claude guard reads the key (fresh read),
    # and once after persisting so subsequent reads in this process see new config.
    assert calls["runtime_clears"] == 2


def test_put_succeeds_even_if_client_cache_clear_raises(app_client):
    """The config is already persisted, so a post-persist cache-clear bug must not 500."""
    client, calls, mp = app_client

    def boom() -> None:
        raise RuntimeError("client cache backend down")

    mp.setattr(route, "clear_client_cache", boom)
    # Local base URL keeps the Ollama-cloud-without-key guard from firing.
    resp = client.put(
        "/api/llm-config",
        json={"provider": "ollama", "model": "llama3.1", "ollama_base_url": "http://localhost:11434"},
    )
    assert resp.status_code == 200
    # The write still happened despite the cache-clear failure.
    stored = dict((k, v) for _s, k, v in calls["set"])
    assert stored[route.runtime_config.KEY_PROVIDER] == "ollama"


def test_put_succeeds_even_if_runtime_cache_clear_raises(app_client):
    """Both runtime-config clears (pre-guard and post-persist) are guarded."""
    client, _calls, mp = app_client

    def boom() -> None:
        raise RuntimeError("runtime cache down")

    mp.setattr(route.runtime_config, "clear_cache", boom)
    # Local base URL keeps the Ollama-cloud-without-key guard from firing.
    resp = client.put(
        "/api/llm-config",
        json={"provider": "ollama", "ollama_base_url": "http://localhost:11434"},
    )
    assert resp.status_code == 200


def test_get_succeeds_even_if_cache_clear_raises(app_client):
    """A runtime-cache-clear failure must not 500 the settings read."""
    client, _calls, mp = app_client

    def boom() -> None:
        raise RuntimeError("runtime cache down")

    mp.setattr(route.runtime_config, "clear_cache", boom)
    resp = client.get("/api/llm-config")
    assert resp.status_code == 200
    assert resp.json()["provider"] == "ollama"


def test_put_skips_empty_fields(app_client):
    client, calls, mp = app_client
    # A Claude key is already configured (env here), so switching to Claude with
    # empty model/key fields is allowed; those empty fields must still be skipped.
    mp.setenv("ANTHROPIC_API_KEY", "sk-existing")
    client.put("/api/llm-config", json={"provider": "claude"})
    keys = [k for _s, k, _v in calls["set"]]
    # provider always written; empty model/keys are NOT written (preserve existing).
    assert route.runtime_config.KEY_PROVIDER in keys
    assert route.runtime_config.KEY_CLAUDE_API_KEY not in keys
    assert route.runtime_config.KEY_CLAUDE_MODEL not in keys
    assert route.runtime_config.KEY_OLLAMA_MODEL not in keys


def test_put_claude_without_key_rejected(app_client):
    # Switching to Claude with no key (request, runtime, or env) is rejected so the
    # factory never builds a keyless ClaudeLLMClient that fails every later call.
    client, calls, _mp = app_client
    resp = client.put("/api/llm-config", json={"provider": "claude", "model": "claude-opus-4-8"})
    assert resp.status_code == 400
    assert "without an API key" in resp.json()["detail"]
    assert calls["set"] == []  # nothing persisted
    # The TTL cache is dropped before the guard resolves the key, so the guard reads
    # committed state (a key just stored by another worker is not missed).
    assert calls["runtime_clears"] == 1


def test_put_claude_allowed_when_key_in_env(app_client):
    # An already-configured key (env here) satisfies the guard even when the request
    # omits claude_api_key.
    client, calls, mp = app_client
    mp.setenv("ANTHROPIC_API_KEY", "sk-existing")
    resp = client.put("/api/llm-config", json={"provider": "claude", "model": "claude-opus-4-8"})
    assert resp.status_code == 200
    stored = dict((k, v) for _s, k, v in calls["set"])
    assert stored[route.runtime_config.KEY_PROVIDER] == "claude"


def test_put_claude_allowed_via_env_key_despite_clear_failure(app_client):
    # The pre-guard runtime-cache clear failing must not break a valid Claude switch:
    # the env key still satisfies the guard, so the request succeeds despite the clear
    # raising (the failure is logged, not propagated).
    client, calls, mp = app_client

    def boom() -> None:
        raise RuntimeError("runtime cache down")

    mp.setenv("ANTHROPIC_API_KEY", "sk-existing")
    mp.setattr(route.runtime_config, "clear_cache", boom)
    resp = client.put("/api/llm-config", json={"provider": "claude", "model": "claude-opus-4-8"})
    assert resp.status_code == 200
    stored = dict((k, v) for _s, k, v in calls["set"])
    assert stored[route.runtime_config.KEY_PROVIDER] == "claude"


def test_put_claude_rejected_without_key_despite_clear_failure(app_client):
    # The pre-guard runtime-cache clear failing must not bypass the guard: with no key
    # anywhere the switch is still rejected (the clear failure is logged, then the
    # guard reads the unchanged — keyless — state and 400s).
    client, calls, mp = app_client

    def boom() -> None:
        raise RuntimeError("runtime cache down")

    mp.setattr(route.runtime_config, "clear_cache", boom)
    resp = client.put("/api/llm-config", json={"provider": "claude", "model": "claude-opus-4-8"})
    assert resp.status_code == 400
    assert "without an API key" in resp.json()["detail"]
    assert calls["set"] == []  # nothing persisted


def test_put_rejects_invalid_provider(app_client):
    client, _calls, _mp = app_client
    resp = client.put("/api/llm-config", json={"provider": "openai"})
    assert resp.status_code == 422  # Literal validation


def test_put_requires_postgres(app_client, monkeypatch):
    client, calls, _mp = app_client
    monkeypatch.setattr(route, "is_postgres_enabled", lambda: False)
    resp = client.put("/api/llm-config", json={"provider": "claude"})
    assert resp.status_code == 503
    assert calls["set"] == []  # nothing persisted


def test_get_reports_resolved_ollama_model(app_client):
    # _build_response routes through resolve_model_for_provider, so the effective
    # Ollama model (here from env) is surfaced — the UI never disagrees with agents.
    client, _calls, mp = app_client
    mp.setenv("LLM_PROVIDER", "ollama")
    mp.setenv("LLM_MODEL", "llama3.2")
    body = client.get("/api/llm-config").json()
    assert body["provider"] == "ollama"
    assert body["model"] == "llama3.2"


def test_get_reports_per_provider_models(app_client):
    # Both providers' effective models are surfaced so the UI can restore the
    # inactive one on a provider switch (lossless toggle).
    client, _calls, mp = app_client
    mp.setenv("LLM_PROVIDER", "ollama")
    mp.setenv("LLM_MODEL", "llama3.2")
    body = client.get("/api/llm-config").json()
    assert body["ollama_model"] == "llama3.2"
    # The non-Claude LLM_MODEL must not leak into the Claude slot; it falls back to
    # the Claude default instead.
    assert body["claude_model"] and body["claude_model"] != "llama3.2"


def test_put_stores_model_under_provider_specific_key(app_client):
    # The model is persisted under the active provider's key (ollama here), so it
    # never collides with a Claude selection in a shared slot.
    client, calls, _mp = app_client
    # Local base URL keeps the Ollama-cloud-without-key guard from firing.
    client.put(
        "/api/llm-config",
        json={"provider": "ollama", "model": "llama3.2", "ollama_base_url": "http://localhost:11434"},
    )
    stored = {k: v for _s, k, v in calls["set"]}
    assert stored[route.runtime_config.KEY_OLLAMA_MODEL] == "llama3.2"
    assert route.runtime_config.KEY_CLAUDE_MODEL not in stored


def test_get_clears_runtime_cache_for_fresh_read(app_client):
    # The settings GET drops the runtime-config TTL cache before resolving, so a
    # read landing on a worker with a stale per-worker cache still reflects the
    # committed store (multi-worker correctness).
    client, calls, _mp = app_client
    resp = client.get("/api/llm-config")
    assert resp.status_code == 200
    assert calls["runtime_clears"] == 1


def test_put_writes_all_keys_in_one_transaction(app_client, monkeypatch):
    # Every changed key must go through a single set_secrets() call so the write is
    # atomic — a half-applied provider/model/key switch can never be committed.
    client, _calls, _mp = app_client
    batches: list[dict] = []
    monkeypatch.setattr(route, "set_secrets", lambda svc, values: batches.append(dict(values)))
    resp = client.put(
        "/api/llm-config",
        json={"provider": "claude", "model": "claude-opus-4-8", "claude_api_key": "sk-new"},
    )
    assert resp.status_code == 200
    assert len(batches) == 1  # one atomic write, not one-per-key
    assert batches[0] == {
        route.runtime_config.KEY_PROVIDER: "claude",
        route.runtime_config.KEY_CLAUDE_MODEL: "claude-opus-4-8",
        route.runtime_config.KEY_CLAUDE_API_KEY: "sk-new",
    }


def test_put_rejects_malformed_ollama_base_url(app_client):
    # A non-empty ollama_base_url must be a well-formed http(s) URL, else it would
    # be persisted and break every Ollama request until manually corrected.
    client, calls, _mp = app_client
    resp = client.put("/api/llm-config", json={"provider": "ollama", "ollama_base_url": "not-a-url"})
    assert resp.status_code == 422  # field validator rejects it before persistence
    assert calls["set"] == []  # nothing persisted


def test_put_rejects_ollama_base_url_with_credentials(app_client):
    # A URL embedding credentials (user:pass@host) must be rejected before persistence
    # so secrets are never written to the runtime store or leaked into request logs.
    client, calls, _mp = app_client
    resp = client.put(
        "/api/llm-config",
        json={
            "provider": "ollama",
            "ollama_base_url": "https://user:pass@host:11434",
            "ollama_api_key": "ok-123",
        },
    )
    assert resp.status_code == 422  # field validator rejects it before persistence
    assert calls["set"] == []  # nothing persisted


def test_put_accepts_valid_ollama_base_url(app_client):
    client, calls, _mp = app_client
    resp = client.put(
        "/api/llm-config",
        json={"provider": "ollama", "ollama_base_url": "http://localhost:11434"},
    )
    assert resp.status_code == 200
    stored = {k: v for _s, k, v in calls["set"]}
    assert stored[route.runtime_config.KEY_OLLAMA_BASE_URL] == "http://localhost:11434"


def test_put_ollama_cloud_without_key_rejected(app_client):
    # Pointing the provider at Ollama Cloud (ollama.com) with no key (request,
    # runtime, or env) is rejected so the factory never builds a keyless cloud client
    # that fails every later call.
    client, calls, _mp = app_client
    resp = client.put(
        "/api/llm-config",
        json={"provider": "ollama", "ollama_base_url": "https://ollama.com"},
    )
    assert resp.status_code == 400
    assert "Ollama Cloud without an API key" in resp.json()["detail"]
    assert calls["set"] == []  # nothing persisted


def test_put_ollama_cloud_allowed_with_key_in_body(app_client):
    # A cloud URL with the key supplied in the request body passes the guard.
    client, calls, _mp = app_client
    resp = client.put(
        "/api/llm-config",
        json={
            "provider": "ollama",
            "ollama_base_url": "https://ollama.com",
            "ollama_api_key": "ok-123",
        },
    )
    assert resp.status_code == 200
    stored = {k: v for _s, k, v in calls["set"]}
    assert stored[route.runtime_config.KEY_OLLAMA_BASE_URL] == "https://ollama.com"
    assert stored[route.runtime_config.KEY_OLLAMA_API_KEY] == "ok-123"


def test_put_ollama_local_without_key_allowed(app_client):
    # A local Ollama URL needs no key, so the cloud guard must not fire.
    client, calls, _mp = app_client
    resp = client.put(
        "/api/llm-config",
        json={"provider": "ollama", "ollama_base_url": "http://localhost:11434"},
    )
    assert resp.status_code == 200
    stored = {k: v for _s, k, v in calls["set"]}
    assert stored[route.runtime_config.KEY_OLLAMA_BASE_URL] == "http://localhost:11434"


def test_put_storage_error_returns_503(app_client, monkeypatch):
    # A failure persisting the config surfaces as a clear 503, not an opaque 500.
    client, _calls, _mp = app_client

    def _boom(_svc, _values):
        raise RuntimeError("db down")

    monkeypatch.setattr(route, "set_secrets", _boom)
    # Use a local base URL so the Ollama-cloud-without-key guard does not short-circuit
    # the request before set_secrets is reached.
    resp = client.put(
        "/api/llm-config",
        json={"provider": "ollama", "model": "llama3.2", "ollama_base_url": "http://localhost:11434"},
    )
    assert resp.status_code == 503
    assert "storage error" in resp.json()["detail"]


def test_put_persists_ollama_base_url_and_api_key(app_client):
    # Non-empty ollama base URL + cloud key are batched into the same atomic write.
    client, calls, _mp = app_client
    resp = client.put(
        "/api/llm-config",
        json={
            "provider": "ollama",
            "ollama_base_url": "https://ollama.com",
            "ollama_api_key": "ok-123",
        },
    )
    assert resp.status_code == 200
    stored = {k: v for _s, k, v in calls["set"]}
    assert stored[route.runtime_config.KEY_OLLAMA_BASE_URL] == "https://ollama.com"
    assert stored[route.runtime_config.KEY_OLLAMA_API_KEY] == "ok-123"


def test_ollama_models_live_listing(app_client):
    # A non-empty live listing from /api/tags is returned verbatim with source=live.
    client, _calls, mp = app_client
    mp.setattr(route, "list_ollama_models", lambda: ["llama3.2", "qwen3-coder:480b-cloud"])
    resp = client.get("/api/llm-config/ollama-models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "live"
    assert body["models"] == ["llama3.2", "qwen3-coder:480b-cloud"]
    # base_url reflects the resolved effective endpoint (cloud default with no env).
    assert body["base_url"] == "https://ollama.com"


def test_ollama_models_falls_back_to_curated(app_client):
    # When the endpoint can't be reached (empty list), the curated suggestions are
    # returned so the dropdown is never empty, flagged source=fallback.
    client, _calls, mp = app_client
    mp.setattr(route, "list_ollama_models", lambda: [])
    resp = client.get("/api/llm-config/ollama-models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "fallback"
    assert body["models"] == list(route._OLLAMA_MODEL_SUGGESTIONS)
    assert body["models"]  # non-empty curated fallback


def test_ollama_models_base_url_reflects_resolved_endpoint(app_client):
    # The reported base_url tracks the resolved Ollama endpoint (env override here).
    client, _calls, mp = app_client
    mp.setenv("LLM_BASE_URL", "http://localhost:11434")
    mp.setattr(route, "list_ollama_models", lambda: ["llama3.1"])
    body = client.get("/api/llm-config/ollama-models").json()
    assert body["base_url"] == "http://localhost:11434"


def test_provider_model_keys_cover_all_provider_options():
    # Every provider the UI offers must have a storage key in PROVIDER_MODEL_KEYS,
    # else PUT would KeyError (500) on save. Asserting equality makes the map an
    # enforced single source: adding a provider to _PROVIDER_OPTIONS without a model
    # key (or vice versa) fails here instead of in production.
    assert set(route.runtime_config.PROVIDER_MODEL_KEYS) == set(route._PROVIDER_OPTIONS)
