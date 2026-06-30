"""Tests for the multi-provider failover path: FailoverLLMClient dispatch,
limit-marking classification, and get_client integration (incl. the Strands
unwrap_client regression guard)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from llm_service import factory
from llm_service import provider_store as ps
from llm_service.clients import ClaudeLLMClient, OllamaLLMClient
from llm_service.factory import (
    FailoverLLMClient,
    _AttributingClient,
    _build_entry_client,
    _build_legacy_concrete,
    _mark_entry_exhausted,
    get_client,
    unwrap_client,
)
from llm_service.interface import (
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    LLMClient,
    LLMPermanentError,
    LLMRateLimitError,
)


def _entry(entry_id: int, provider: str = "ollama") -> ps.ProviderEntry:
    return ps.ProviderEntry(
        id=entry_id,
        label=f"e{entry_id}",
        provider=provider,
        model="m",
        base_url="u",
        api_key="k",
        sort_order=entry_id,
        limit_exceeded=False,
        limit_type="",
        reset_at=None,
    )


class _StubClient:
    """A provider client stub whose method either returns or raises."""

    def __init__(self, result=None, exc=None, model="stub-model"):
        self._result = result
        self._exc = exc
        self.model = model
        self.calls = 0

    def _run(self, *a, **k):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result

    complete_json = _run
    complete = _run
    complete_text = _run
    chat = _run


def _make_failover(entries, build_map, *, marks=None):
    """Build a FailoverLLMClient over `entries`, dispatching to `build_map[id]`.

    `build_map` maps entry id -> stub client. Records (entry_id, rl_override) of
    each build into `builds`, and (entry_id, err) into `marks`.
    """
    builds: list[tuple] = []
    marks = marks if marks is not None else []

    def load_candidates():
        return list(entries)

    def build(entry, rl_override):
        builds.append((entry.id, rl_override))
        return build_map[entry.id]

    def mark(entry, err):
        marks.append((entry.id, err))

    def default_build():
        return _StubClient(result="default")

    fc = FailoverLLMClient(load_candidates, build, mark, default_build)
    return fc, builds, marks


def test_first_candidate_success_no_mark():
    e1, e2 = _entry(1), _entry(2)
    c1 = _StubClient(result="ok")
    fc, builds, marks = _make_failover([e1, e2], {1: c1, 2: _StubClient()})
    assert fc.complete_json("p") == "ok"
    assert c1.calls == 1 and marks == []


def test_rate_limit_fails_over_to_next(monkeypatch):
    monkeypatch.setenv("LLM_FAILOVER_FAST_429", "true")
    e1, e2 = _entry(1), _entry(2)
    c1 = _StubClient(exc=LLMRateLimitError("limited", status_code=429))
    c2 = _StubClient(result="second")
    fc, builds, marks = _make_failover([e1, e2], {1: c1, 2: c2})
    assert fc.chat("p") == "second"
    assert [m[0] for m in marks] == [1]  # only the exhausted provider marked
    # Non-last candidate built fast-fail (0); last keeps env schedule (None).
    assert builds == [(1, 0), (2, None)]


def test_all_limited_raises_last_error():
    e1, e2 = _entry(1), _entry(2)
    err1 = LLMRateLimitError("first", status_code=429)
    err2 = LLMRateLimitError("second", status_code=429)
    fc, builds, marks = _make_failover(
        [e1, e2], {1: _StubClient(exc=err1), 2: _StubClient(exc=err2)}
    )
    with pytest.raises(LLMRateLimitError) as ei:
        fc.complete("p")
    assert ei.value is err2  # the LAST 429 is surfaced
    assert [m[0] for m in marks] == [1, 2]


def test_non_rate_limit_error_propagates_without_failover():
    e1, e2 = _entry(1), _entry(2)
    c1 = _StubClient(exc=LLMPermanentError("boom"))
    c2 = _StubClient(result="should-not-reach")
    fc, builds, marks = _make_failover([e1, e2], {1: c1, 2: c2})
    with pytest.raises(LLMPermanentError):
        fc.complete_text("p")
    assert marks == [] and c2.calls == 0


def test_empty_candidates_uses_default_build():
    def load_candidates():
        return []

    built = {"n": 0}

    def default_build():
        built["n"] += 1
        return _StubClient(result="fallback")

    fc = FailoverLLMClient(load_candidates, lambda e, r: None, lambda e, x: None, default_build)
    assert fc.chat("p") == "fallback"
    assert built["n"] == 1


def test_getattr_delegates_to_active_client():
    e1 = _entry(1)
    fc, _b, _m = _make_failover([e1], {1: _StubClient(model="active-model")})
    assert fc.model == "active-model"


def test_fast_429_disabled_uses_env_schedule_for_all(monkeypatch):
    monkeypatch.setenv("LLM_FAILOVER_FAST_429", "false")
    e1, e2 = _entry(1), _entry(2)
    c1 = _StubClient(exc=LLMRateLimitError("x", status_code=429))
    c2 = _StubClient(result="ok")
    fc, builds, marks = _make_failover([e1, e2], {1: c1, 2: c2})
    assert fc.chat("p") == "ok"
    assert builds == [(1, None), (2, None)]  # no fast-fail when disabled


def test_failover_is_virtual_llmclient():
    assert issubclass(FailoverLLMClient, LLMClient)


# --------------------------------------------------------------------------- #
# Limit classification                                                          #
# --------------------------------------------------------------------------- #


def test_mark_weekly_from_message(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ps,
        "mark_exhausted",
        lambda i, *, limit_type, reset_at: captured.update(
            id=i, limit_type=limit_type, reset_at=reset_at
        ),
    )
    err = LLMRateLimitError(OLLAMA_WEEKLY_LIMIT_MESSAGE, status_code=429)
    before = datetime.now(timezone.utc)
    _mark_entry_exhausted(_entry(3), err)
    assert captured["id"] == 3 and captured["limit_type"] == "weekly"
    # No retry_after → long weekly window (well beyond an hour).
    assert (captured["reset_at"] - before).total_seconds() > 3600


def test_mark_rate_uses_retry_after(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ps,
        "mark_exhausted",
        lambda i, *, limit_type, reset_at: captured.update(
            limit_type=limit_type, reset_at=reset_at
        ),
    )
    err = LLMRateLimitError("too many requests", status_code=429, retry_after_seconds=120)
    before = datetime.now(timezone.utc)
    _mark_entry_exhausted(_entry(3), err)
    assert captured["limit_type"] == "rate"
    delta = (captured["reset_at"] - before).total_seconds()
    assert 110 < delta < 180  # ~retry_after, not the default window


# --------------------------------------------------------------------------- #
# Factory integration + Strands unwrap_client regression guard                 #
# --------------------------------------------------------------------------- #


@pytest.fixture
def two_providers(monkeypatch):
    """get_client sees a 2-entry provider list; concrete clients are stubs."""
    entries = [_entry(1), _entry(2)]
    monkeypatch.setattr(ps, "load_ordered_entries", lambda *a, **k: list(entries))
    monkeypatch.setattr(ps, "select_active_entry", lambda es, **k: es[0])
    stubs = {1: _StubClient(model="m1"), 2: _StubClient(model="m2")}
    monkeypatch.setattr(factory, "_build_entry_client", lambda e, ak, orx, rl: stubs[e.id])
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    return stubs


def test_get_client_returns_failover_wrapped_in_attribution(two_providers):
    c = get_client("backend")
    assert isinstance(c, _AttributingClient)
    assert isinstance(c._inner, FailoverLLMClient)


def test_unwrap_client_returns_failover_not_concrete(two_providers):
    """Regression guard: unwrap_client must stop at the failover layer so the
    Strands adapter (unwrap_client(client).chat) still routes through failover."""
    c = get_client("backend")
    inner = unwrap_client(c)
    assert isinstance(inner, FailoverLLMClient)
    # And it duck-types as the active provider client.
    assert inner.model == "m1"


def test_strands_unwrap_path_routes_through_failover(two_providers):
    """unwrap_client(get_client(k)).chat dispatches via failover, not a raw client."""
    c = get_client("backend")
    # The first stub is healthy → its chat returns None (the stub default).
    # Make the first 429 to prove failover reaches the second.
    two_providers[1]._exc = LLMRateLimitError("limited", status_code=429)
    two_providers[2]._result = "from-second"
    assert unwrap_client(c).chat("p") == "from-second"


def test_get_client_no_entries_falls_through_to_legacy(monkeypatch):
    monkeypatch.setattr(ps, "load_ordered_entries", lambda *a, **k: [])
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "legacy-model")
    c = get_client("backend")
    # Legacy path: attribution-wrapped concrete Ollama client, not failover.
    assert isinstance(c, _AttributingClient)
    assert not isinstance(c._inner, FailoverLLMClient)
    assert c.model == "legacy-model"


# --------------------------------------------------------------------------- #
# Concrete client builders (real construction, no network)                     #
# --------------------------------------------------------------------------- #


def _full_entry(provider, model="", base_url="", api_key=""):
    return ps.ProviderEntry(
        id=1,
        label="e",
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        sort_order=0,
        limit_exceeded=False,
        limit_type="",
        reset_at=None,
    )


def test_build_entry_client_ollama(monkeypatch):
    monkeypatch.delenv("LLM_RATE_LIMIT_MAX_RETRIES", raising=False)
    e = _full_entry("ollama", model="qwen", base_url="http://localhost:11434")
    c = _build_entry_client(e, None, None, 0)
    assert isinstance(c, OllamaLLMClient)
    assert c.model == "qwen" and c.base_url == "http://localhost:11434"
    assert c._rate_limit_max_retries_override == 0


def test_build_entry_client_ollama_honors_entry_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LLM_OLLAMA_API_KEY", raising=False)
    e = _full_entry("ollama", model="m", base_url="https://ollama.com", api_key="sk-entry")
    c = _build_entry_client(e, None, None, None)
    assert isinstance(c, OllamaLLMClient)
    assert c._api_key_override == "sk-entry"
    # The per-entry key authenticates the request, not the (absent) global key.
    assert c._ollama_auth_headers() == {"Authorization": "Bearer sk-entry"}


def test_ollama_cache_distinguishes_by_key():
    from llm_service.factory import _ollama_cached

    c1, _ = _ollama_cached("m", "https://ollama.com", 900.0, None, "k1")
    c2, _ = _ollama_cached("m", "https://ollama.com", 900.0, None, "k2")
    c1b, _ = _ollama_cached("m", "https://ollama.com", 900.0, None, "k1")
    assert c1 is c1b  # same key → shared client
    assert c1 is not c2  # different key → distinct client


def test_build_entry_client_claude(monkeypatch):
    e = _full_entry("claude", model="claude-opus-4-8", api_key="sk-ant")
    c = _build_entry_client(e, None, None, None)
    assert isinstance(c, ClaudeLLMClient)
    assert c.model == "claude-opus-4-8" and c.api_key == "sk-ant"


def test_build_entry_client_empty_falls_back_to_resolvers(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.setenv("LLM_BASE_URL", "http://envhost:11434")
    e = _full_entry("ollama")  # no model/base_url
    c = _build_entry_client(e, None, None, None)
    assert c.model == "env-model" and c.base_url == "http://envhost:11434"


def test_build_entry_client_on_reasoning_is_fresh(monkeypatch):
    sink = lambda _t: None  # noqa: E731
    e = _full_entry("ollama", model="m", base_url="http://localhost:11434")
    c = _build_entry_client(e, None, sink, 0)
    assert isinstance(c, OllamaLLMClient) and c.on_reasoning is sink


def test_build_legacy_concrete_ollama(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "legacy")
    c = _build_legacy_concrete(None, None)
    assert isinstance(c, OllamaLLMClient) and c.model == "legacy"


def test_build_legacy_concrete_claude(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("LLM_MODEL", "claude-opus-4-8")
    c = _build_legacy_concrete(None, None)
    assert isinstance(c, ClaudeLLMClient)


def test_build_legacy_concrete_on_reasoning(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "m")
    sink = lambda _t: None  # noqa: E731
    c = _build_legacy_concrete(None, sink)
    assert c.on_reasoning is sink


def test_full_failover_path_unmocked_build(monkeypatch):
    """End-to-end through the real _build_failover_client + _build_entry_client,
    with the provider clients' .chat patched at the class level (no network)."""
    entries = [_full_entry("ollama", model="m1", base_url="http://h1:11434")]
    entries[0] = ps.ProviderEntry(**{**entries[0].__dict__, "id": 1})
    e2 = ps.ProviderEntry(
        id=2,
        label="e2",
        provider="ollama",
        model="m2",
        base_url="http://h2:11434",
        api_key="",
        sort_order=1,
        limit_exceeded=False,
        limit_type="",
        reset_at=None,
    )
    chain = [entries[0], e2]
    monkeypatch.setattr(ps, "load_ordered_entries", lambda *a, **k: list(chain))
    monkeypatch.setattr(ps, "select_active_entry", lambda es, **k: es[0])
    monkeypatch.setattr(ps, "mark_exhausted", lambda *a, **k: None)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    calls = []

    def fake_chat(self, *a, **k):
        calls.append(self.model)
        if self.model == "m1":
            raise LLMRateLimitError("limited", status_code=429)
        return "ok-from-" + self.model

    monkeypatch.setattr(OllamaLLMClient, "chat", fake_chat, raising=True)
    c = get_client("backend")
    assert unwrap_client(c).chat("p") == "ok-from-m2"
    assert calls == ["m1", "m2"]
