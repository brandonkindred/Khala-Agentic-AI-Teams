"""Tests for the multi-provider failover path: FailoverLLMClient dispatch,
limit-marking classification, and get_client integration (incl. the Strands
unwrap_client regression guard)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from llm_service import factory
from llm_service import provider_store as ps
from llm_service.clients import ClaudeLLMClient, OllamaLLMClient, RunPodLLMClient
from llm_service.factory import (
    FailoverLLMClient,
    _AttributingClient,
    _build_entry_client,
    _mark_entry_exhausted,
    get_client,
    unwrap_client,
)
from llm_service.interface import (
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    LLMClient,
    LLMNotConfiguredError,
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


def _make_failover(entries, build_map):
    """Build a FailoverLLMClient over `entries`, dispatching to `build_map[id]`.

    `build_map` maps entry id -> stub client. Returns `(client, builds, marks)`:
    `builds` records (entry_id, rl_override) of each build call; `marks` records
    (entry_id, err) of each usage-limit mark.
    """
    builds: list[tuple] = []
    marks: list[tuple] = []

    def load_candidates():
        return list(entries)

    def build(entry, rl_override, model_override=None):
        builds.append((entry.id, rl_override))
        return build_map[entry.id]

    def mark(entry, err):
        marks.append((entry.id, err))

    fc = FailoverLLMClient(load_candidates, build, mark)
    return fc, builds, marks


def test_first_candidate_success_no_mark():
    """The first (most-preferred) candidate succeeding: no failover, no mark."""
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


def test_build_exception_propagates_without_failover():
    """``build()`` raising (e.g. an invalid provider config) is distinct from a client
    METHOD raising: it happens outside the try/except LLMRateLimitError block in
    _dispatch, so it propagates immediately — no failover, no mark, no next candidate."""
    e1, e2 = _entry(1), _entry(2)

    def load_candidates():
        return [e1, e2]

    build_calls: list[int] = []

    def build(entry, rl_override, model_override=None):
        build_calls.append(entry.id)
        if entry.id == 1:
            raise ValueError("invalid provider config")
        return _StubClient(result="should-not-reach")

    def mark(entry, err):
        raise AssertionError("mark must not be called for a build failure")

    fc = FailoverLLMClient(load_candidates, build, mark)
    with pytest.raises(ValueError, match="invalid provider config"):
        fc.complete_json("p")
    assert build_calls == [1]  # no attempt made on the second candidate


def test_empty_candidates_raises_not_configured():
    """An empty candidate list (the provider list emptied at runtime) has no legacy
    fallback — the sole-source contract means dispatch raises LLMNotConfiguredError."""
    fc = FailoverLLMClient(lambda: [], lambda e, r, mo=None: None, lambda e, x: None)
    with pytest.raises(LLMNotConfiguredError):
        fc.chat("p")


def test_getattr_on_empty_candidates_raises_not_configured():
    """Delegated attribute access with no candidates also raises (no legacy fallback)."""
    fc = FailoverLLMClient(lambda: [], lambda e, r, mo=None: None, lambda e, x: None)
    with pytest.raises(LLMNotConfiguredError):
        _ = fc.model


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
    # No retry_after → fixed 24h weekly window (well beyond an hour).
    delta = (captured["reset_at"] - before).total_seconds()
    assert 23 * 3600 < delta < 25 * 3600


def test_mark_weekly_from_limit_kind_ignores_retry_after(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ps,
        "mark_exhausted",
        lambda i, *, limit_type, reset_at: captured.update(
            limit_type=limit_type, reset_at=reset_at
        ),
    )
    err = LLMRateLimitError(
        "weekly usage limit",
        status_code=429,
        retry_after_seconds=60,
        limit_kind="weekly",
    )
    before = datetime.now(timezone.utc)
    _mark_entry_exhausted(_entry(3), err)
    assert captured["limit_type"] == "weekly"
    delta = (captured["reset_at"] - before).total_seconds()
    assert 23 * 3600 < delta < 25 * 3600


def test_mark_session_from_limit_kind_ignores_retry_after(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ps,
        "mark_exhausted",
        lambda i, *, limit_type, reset_at: captured.update(
            limit_type=limit_type, reset_at=reset_at
        ),
    )
    err = LLMRateLimitError(
        "session usage limit",
        status_code=429,
        retry_after_seconds=30,
        limit_kind="session",
    )
    before = datetime.now(timezone.utc)
    _mark_entry_exhausted(_entry(3), err)
    assert captured["limit_type"] == "session"
    delta = (captured["reset_at"] - before).total_seconds()
    assert 60 * 60 < delta < 70 * 60


def test_mark_session_from_body_phrase(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ps,
        "mark_exhausted",
        lambda i, *, limit_type, reset_at: captured.update(limit_type=limit_type),
    )
    err = LLMRateLimitError(
        "you have reached your session usage limit",
        status_code=429,
    )
    _mark_entry_exhausted(_entry(3), err)
    assert captured["limit_type"] == "session"


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


def test_mark_retry_after_zero_resets_immediately(monkeypatch):
    """Retry-After: 0 ("retry now") yields a ~now reset_at, not the full fallback window."""
    captured = {}
    monkeypatch.setattr(
        ps,
        "mark_exhausted",
        lambda i, *, limit_type, reset_at: captured.update(reset_at=reset_at),
    )
    err = LLMRateLimitError("too many requests", status_code=429, retry_after_seconds=0)
    before = datetime.now(timezone.utc)
    _mark_entry_exhausted(_entry(3), err)
    # Honored as a 0s window — reset_at is essentially "now", far short of the
    # configured rate fallback window (so the entry is reconsidered immediately).
    delta = (captured["reset_at"] - before).total_seconds()
    assert 0 <= delta < 5


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
    monkeypatch.setattr(factory, "_build_entry_client", lambda e, ak, orx, rl, mo=None: stubs[e.id])
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


def test_get_client_no_entries_raises_not_configured(monkeypatch):
    """The provider list is the sole source: an empty list (non-dummy provider) has
    no legacy fallback, so get_client raises LLMNotConfiguredError."""
    monkeypatch.setattr(ps, "load_ordered_entries", lambda *a, **k: [])
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "would-be-legacy-model")
    with pytest.raises(LLMNotConfiguredError):
        get_client("backend")


def test_get_client_claude_no_entries_raises_not_configured(monkeypatch):
    """Same for provider=claude: no single-provider Claude fallback remains."""
    monkeypatch.setattr(ps, "load_ordered_entries", lambda *a, **k: [])
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    with pytest.raises(LLMNotConfiguredError):
        get_client("backend")


def test_get_client_dummy_still_pre_empts_empty_list(monkeypatch):
    """dummy is a hard override — it returns a DummyLLMClient even with an empty list."""
    from llm_service.clients import DummyLLMClient

    monkeypatch.setattr(ps, "load_ordered_entries", lambda *a, **k: [])
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    assert isinstance(get_client("backend"), DummyLLMClient)


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


def test_build_entry_client_runpod(monkeypatch):
    e = _full_entry(
        "runpod", model="mixtral", base_url="https://api.runpod.ai/v2/abc123/openai/v1", api_key="sk-rp"
    )
    c = _build_entry_client(e, None, None, 0)
    assert isinstance(c, RunPodLLMClient)
    assert c.model == "mixtral" and c.base_url == "https://api.runpod.ai/v2/abc123/openai/v1"


def test_build_entry_client_runpod_strips_base_url(monkeypatch):
    """A stored RunPod base_url with stray whitespace is trimmed before use, matching
    the Ollama branch — otherwise endpoint resolution would break."""
    e = _full_entry(
        "runpod",
        model="m",
        base_url="  https://api.runpod.ai/v2/abc123/openai/v1  ",
        api_key="sk-rp",
    )
    c = _build_entry_client(e, None, None, 0)
    assert isinstance(c, RunPodLLMClient)
    assert c.base_url == "https://api.runpod.ai/v2/abc123/openai/v1"


def test_build_entry_client_on_reasoning_is_fresh(monkeypatch):
    sink = lambda _t: None  # noqa: E731
    e = _full_entry("ollama", model="m", base_url="http://localhost:11434")
    c = _build_entry_client(e, None, sink, 0)
    assert isinstance(c, OllamaLLMClient) and c.on_reasoning is sink


def test_build_entry_client_runpod_on_reasoning_is_fresh(monkeypatch):
    sink = lambda _t: None  # noqa: E731
    e = _full_entry("runpod", model="m", base_url="https://api.runpod.ai/v2/abc123/openai/v1", api_key="sk-rp")
    c = _build_entry_client(e, None, sink, 0)
    assert isinstance(c, RunPodLLMClient) and c.on_reasoning is sink


def test_build_entry_client_claude_empty_key_no_env_fallback(monkeypatch):
    """An entry with an empty api_key does NOT inherit ANTHROPIC_API_KEY from env —
    entries are self-contained for credentials (the route guard blocks keyless Claude,
    but the factory must not silently pull the env key either)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-should-not-be-used")
    monkeypatch.setenv("LLM_CLAUDE_API_KEY", "sk-env-should-not-be-used")
    e = _full_entry("claude", model="claude-opus-4-8", api_key="")
    c = _build_entry_client(e, None, None, None)
    assert isinstance(c, ClaudeLLMClient) and c.api_key == ""


def test_build_entry_client_ollama_empty_key_no_env_fallback(monkeypatch):
    """An Ollama entry with an empty api_key does NOT inherit OLLAMA_API_KEY from env."""
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-env-should-not-be-used")
    monkeypatch.setenv("LLM_OLLAMA_API_KEY", "sk-env-should-not-be-used")
    e = _full_entry("ollama", model="m", base_url="https://ollama.com", api_key="")
    c = _build_entry_client(e, None, None, None)
    assert isinstance(c, OllamaLLMClient) and c._api_key_override == ""
    # No Authorization header is sent — the entry carries no key.
    assert "Authorization" not in c._ollama_auth_headers()


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
    marks: list[int] = []
    monkeypatch.setattr(ps, "mark_exhausted", lambda entry_id, **kw: marks.append(entry_id))
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
    # The 429'd provider (entry id 1) must actually be marked exhausted.
    assert marks == [1]


# --------------------------------------------------------------------------- #
# Per-stage model override (e.g. BLOG_PLANNING_MODEL) — keeps failover          #
# --------------------------------------------------------------------------- #


def test_build_entry_client_model_override_ollama_only(monkeypatch):
    """A model override pins an Ollama entry's model but is IGNORED for a Claude
    entry (the override names an Ollama model), so a failover hop across provider
    types still resolves a model valid for that provider."""
    ollama = _build_entry_client(_full_entry("ollama", model="m", base_url="u"), None, None, None, "pinned:7b")
    assert isinstance(ollama, OllamaLLMClient) and ollama.model == "pinned:7b"
    claude = _build_entry_client(_full_entry("claude", model="claude-opus-4-8", api_key="sk"), None, None, None, "pinned:7b")
    assert isinstance(claude, ClaudeLLMClient) and claude.model == "claude-opus-4-8"


def test_failover_with_model_override_threads_to_build():
    """with_model_override yields a variant that passes the override to every build
    (both a generation dispatch and delegated attribute access); the original is
    unchanged, so failover across the full candidate chain is preserved."""
    seen: list = []

    def build(entry, rl_override, model_override=None):
        seen.append(model_override)
        return _StubClient(result="ok", model=model_override or "default")

    fc = FailoverLLMClient(lambda: [_entry(1), _entry(2)], build, lambda e, x: None)
    fc.complete_json("p")
    assert seen == [None]  # default: no override

    pinned = fc.with_model_override("pinned:7b")
    assert pinned is not fc
    pinned.complete_json("p")
    assert seen[-1] == "pinned:7b"
    # Delegated attribute access (``.model``) also carries the override.
    assert pinned.model == "pinned:7b"
    # The original is untouched — a per-stage override never mutates the base client.
    fc.complete_json("p")
    assert seen[-1] is None


def test_with_model_override_helper_noop_and_variant():
    """The module-level helper: falsy model or a non-failover client returns the
    input unchanged; a FailoverLLMClient returns a pinned variant."""
    from llm_service.clients import DummyLLMClient
    from llm_service.factory import with_model_override

    dummy = DummyLLMClient()
    assert with_model_override(dummy, "x") is dummy  # non-failover unchanged

    fc = FailoverLLMClient(lambda: [_entry(1)], lambda e, r, mo=None: _StubClient(), lambda e, x: None)
    assert with_model_override(fc, "") is fc  # falsy model unchanged
    assert with_model_override(fc, None) is fc
    variant = with_model_override(fc, "pinned:7b")
    assert isinstance(variant, FailoverLLMClient) and variant is not fc


def test_with_model_override_preserves_failover_and_attribution(two_providers):
    """Through get_client: the override wraps the same failover client and keeps the
    agent attribution (attribution outermost, failover within)."""
    from llm_service.factory import with_model_override

    base = get_client("backend")
    assert isinstance(base, _AttributingClient)
    pinned = with_model_override(base, "pinned:7b")
    assert isinstance(pinned, _AttributingClient)
    assert pinned._agent_key == base._agent_key
    assert isinstance(unwrap_client(pinned), FailoverLLMClient)
