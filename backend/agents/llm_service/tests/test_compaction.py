"""Tests for LLM text compaction and its memoization cache (compaction.py)."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from llm_service import DummyLLMClient
from llm_service.compaction import (  # noqa: PLC2701 - internals are under test
    _COMPACTION_CACHE_NAMESPACE,
    DEFAULT_COMPACTION_CACHE_SIZE,
    _compaction_cache_key,
    _compaction_cache_size,
    _model_fingerprint,
    clear_compaction_cache,
    compact_text,
    supports_compaction,
)


class _CountingClient(DummyLLMClient):
    """A dummy client that counts ``complete`` calls and returns canned output.

    Lets tests assert exactly how many LLM compaction calls fire, which is the
    whole point of the memoization change.
    """

    def __init__(
        self,
        *,
        result: str = "COMPACTED",
        model_id: Optional[str] = None,
        raise_exc: Optional[Exception] = None,
        ctx: int = 16384,
        fail_on: Optional[str] = None,
        empty_on: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.calls = 0
        self._result = result
        self._raise = raise_exc
        self._ctx = ctx
        self._fail_on = fail_on
        self._empty_on = empty_on
        if model_id is not None:
            self.model_id = model_id

    def get_max_context_tokens(self) -> int:
        return self._ctx

    def complete(self, prompt: str, **kwargs: Any) -> str:  # type: ignore[override]
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        if self._fail_on is not None and self._fail_on in prompt:
            raise RuntimeError("chunk boom")
        if self._empty_on is not None and self._empty_on in prompt:
            return ""
        return self._result


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    """Start and end each test with an empty process-global compaction cache."""
    clear_compaction_cache()
    yield
    clear_compaction_cache()


# ---------------------------------------------------------------------------
# Fits-as-is / trivial paths
# ---------------------------------------------------------------------------


def test_text_within_budget_returned_unchanged_without_llm() -> None:
    client = _CountingClient()
    text = "short enough"
    assert compact_text(text, 100, client, "spec") == text
    assert client.calls == 0


def test_empty_text_returns_empty_without_llm() -> None:
    client = _CountingClient()
    assert compact_text("", 100, client, "spec") == ""
    assert compact_text(None, 100, client, "spec") == ""  # type: ignore[arg-type]
    assert client.calls == 0


# ---------------------------------------------------------------------------
# Memoization
# ---------------------------------------------------------------------------


def test_repeated_identical_call_reuses_cached_result() -> None:
    client = _CountingClient(result="COMPACTED SPEC")
    text = "x" * 200
    first = compact_text(text, 50, client, "spec")
    second = compact_text(text, 50, client, "spec")
    assert first == second == "COMPACTED SPEC"
    assert client.calls == 1  # second call served from cache


def test_cache_hit_survives_across_client_instances_of_same_model() -> None:
    text = "y" * 200
    a = _CountingClient()
    b = _CountingClient()  # distinct instance, same fingerprint (type name)
    compact_text(text, 50, a, "spec")
    compact_text(text, 50, b, "spec")
    assert a.calls == 1
    assert b.calls == 0  # reused a's cached compaction


def test_different_budget_is_a_cache_miss() -> None:
    client = _CountingClient()
    text = "z" * 200
    compact_text(text, 50, client, "spec")
    compact_text(text, 60, client, "spec")
    assert client.calls == 2


def test_different_text_is_a_cache_miss() -> None:
    client = _CountingClient()
    compact_text("a" * 200, 50, client, "spec")
    compact_text("b" * 200, 50, client, "spec")
    assert client.calls == 2


def test_different_content_description_is_a_cache_miss() -> None:
    # content_description feeds the compaction prompt, so the same text under a
    # different label must recompute rather than reuse the earlier summary.
    client = _CountingClient()
    text = "L" * 200
    compact_text(text, 50, client, "specification")
    compact_text(text, 50, client, "existing codebase")
    assert client.calls == 2


def test_different_model_is_a_cache_miss() -> None:
    text = "m" * 200
    m1 = _CountingClient(model_id="model-1")
    m1_again = _CountingClient(model_id="model-1")
    m2 = _CountingClient(model_id="model-2")
    compact_text(text, 50, m1, "spec")
    compact_text(text, 50, m1_again, "spec")  # same model → hit
    compact_text(text, 50, m2, "spec")  # different model → miss
    assert m1.calls == 1
    assert m1_again.calls == 0
    assert m2.calls == 1


# ---------------------------------------------------------------------------
# Fallbacks are never cached
# ---------------------------------------------------------------------------


def test_llm_failure_returns_raw_and_is_not_cached() -> None:
    text = "q" * 200
    raiser = _CountingClient(raise_exc=RuntimeError("boom"))
    # Failure falls back to raw text sliced to budget by the coordinator; here
    # compact_text itself returns the raw text unchanged.
    assert compact_text(text, 50, raiser, "spec") == text
    assert raiser.calls == 1

    # A subsequent healthy call for the same key must actually compact — the
    # failed fallback was not frozen in the cache.
    good = _CountingClient(result="OK")
    assert compact_text(text, 50, good, "spec") == "OK"
    assert good.calls == 1


def test_empty_compaction_returns_raw_and_is_not_cached() -> None:
    text = "e" * 200
    empty = _CountingClient(result="")
    assert compact_text(text, 50, empty, "spec") == text
    good = _CountingClient(result="OK")
    assert compact_text(text, 50, good, "spec") == "OK"
    assert good.calls == 1


# ---------------------------------------------------------------------------
# Chunked path (text larger than one compaction call)
# ---------------------------------------------------------------------------


def test_chunked_success_is_cached() -> None:
    # ctx forces chunk_chars down to the 4000 floor, so a 9000-char input splits.
    client = _CountingClient(result="PART", ctx=1000)
    text = "c" * 9000
    first = compact_text(text, 2000, client, "existing codebase")
    calls_after_first = client.calls
    assert calls_after_first >= 2  # multiple chunks compacted
    second = compact_text(text, 2000, client, "existing codebase")
    assert second == first
    assert client.calls == calls_after_first  # fully served from cache


def test_chunked_partial_failure_returns_original_and_is_not_cached() -> None:
    text = "d" * 4000 + "FAILME" + "d" * 4000
    client = _CountingClient(result="PART", ctx=1000, fail_on="FAILME")
    # Any degraded chunk must return the full original — never a truncated join.
    assert compact_text(text, 2000, client, "existing codebase") == text
    calls_after_first = client.calls
    # A degraded chunked result must be retried, not frozen.
    assert compact_text(text, 2000, client, "existing codebase") == text
    assert client.calls > calls_after_first


def test_chunked_empty_chunk_returns_original_and_is_not_cached() -> None:
    text = "d" * 4000 + "EMPTYME" + "d" * 4000
    client = _CountingClient(result="PART", ctx=1000, empty_on="EMPTYME")
    assert compact_text(text, 2000, client, "existing codebase") == text
    calls_after_first = client.calls
    # An empty compaction for any chunk marks the aggregate un-cacheable.
    assert compact_text(text, 2000, client, "existing codebase") == text
    assert client.calls > calls_after_first


def test_chunked_complete_only_failure_preserves_full_text() -> None:
    """Default ctx sizing (no get_max_context_tokens) must not truncate on failure."""

    class FailCompleteOnly:
        def complete(self, prompt: str, **kwargs: Any) -> str:
            raise RuntimeError("boom")

    text = "x" * 23000
    assert compact_text(text, 8000, FailCompleteOnly(), "spec") == text


# ---------------------------------------------------------------------------
# Disabled cache + LRU eviction
# ---------------------------------------------------------------------------


def test_disabled_cache_recomputes_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_COMPACTION_CACHE_SIZE", "0")
    client = _CountingClient()
    text = "w" * 200
    compact_text(text, 50, client, "spec")
    compact_text(text, 50, client, "spec")
    assert client.calls == 2


def test_lru_evicts_oldest_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_COMPACTION_CACHE_SIZE", "1")
    client = _CountingClient()
    text_a = "a" * 200
    text_b = "b" * 200
    compact_text(text_a, 50, client, "spec")  # cache: {A}
    compact_text(text_b, 50, client, "spec")  # cache: {B}, A evicted
    compact_text(text_a, 50, client, "spec")  # A is a miss again
    assert client.calls == 3


def test_clear_compaction_cache_forces_cold_recompute() -> None:
    client = _CountingClient()
    text = "r" * 200
    compact_text(text, 50, client, "spec")
    clear_compaction_cache()
    compact_text(text, 50, client, "spec")
    assert client.calls == 2


def test_corrupt_compaction_cache_entry_is_evicted_and_recomputed() -> None:
    """Non-UTF-8 durable bytes are deleted; compaction recomputes and re-caches."""
    from shared.cache import get_shared_cache

    client = _CountingClient(result="REBUILT")
    text = "c" * 200
    key = _compaction_cache_key(text, 50, "spec", client)
    cache = get_shared_cache(_COMPACTION_CACHE_NAMESPACE)
    cache.set(key, b"\xff\xfe not-utf8", max_entries=8)

    first = compact_text(text, 50, client, "spec")
    assert first == "REBUILT"
    assert client.calls == 1
    raw = cache.get(key)
    assert raw == b"REBUILT"

    second = compact_text(text, 50, client, "spec")
    assert second == "REBUILT"
    assert client.calls == 1  # hit after re-store


# ---------------------------------------------------------------------------
# supports_compaction capability check
# ---------------------------------------------------------------------------


def test_supports_compaction_true_with_callable_complete_only() -> None:
    class CompleteOnly:
        def complete(self, prompt: str, **kwargs: Any) -> str:
            return prompt

    assert supports_compaction(CompleteOnly()) is True


def test_supports_compaction_true_with_complete_and_context() -> None:
    class Full:
        def complete(self, prompt: str, **kwargs: Any) -> str:
            return prompt

        def get_max_context_tokens(self) -> int:
            return 16384

    assert supports_compaction(Full()) is True


def test_supports_compaction_false_when_complete_missing() -> None:
    class CtxOnly:
        def get_max_context_tokens(self) -> int:
            return 16384

    assert supports_compaction(CtxOnly()) is False


def test_supports_compaction_false_when_complete_not_callable() -> None:
    class Bad:
        complete = "not-a-function"

    assert supports_compaction(Bad()) is False


def test_supports_compaction_false_for_none() -> None:
    assert supports_compaction(None) is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_cache_size_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_COMPACTION_CACHE_SIZE", raising=False)
    assert _compaction_cache_size() == DEFAULT_COMPACTION_CACHE_SIZE
    monkeypatch.setenv("LLM_COMPACTION_CACHE_SIZE", "abc")
    assert _compaction_cache_size() == DEFAULT_COMPACTION_CACHE_SIZE
    monkeypatch.setenv("LLM_COMPACTION_CACHE_SIZE", "-5")
    assert _compaction_cache_size() == 0  # floored, disables cache
    monkeypatch.setenv("LLM_COMPACTION_CACHE_SIZE", "10")
    assert _compaction_cache_size() == 10


def test_model_fingerprint_prefers_model_attr_then_type_name() -> None:
    assert _model_fingerprint(_CountingClient(model_id="the-model")) == "the-model"
    # No model attributes → falls back to the client's type name.
    assert _model_fingerprint(DummyLLMClient()) == "DummyLLMClient"


def test_model_fingerprint_survives_attribute_that_raises() -> None:
    class _Angry(DummyLLMClient):
        @property
        def model_id(self) -> str:
            raise RuntimeError("no touching")

    # The raising attribute is swallowed; fingerprint falls back to the type name.
    assert _model_fingerprint(_Angry()) == "_Angry"


def test_cache_key_is_stable_and_input_sensitive() -> None:
    client = _CountingClient(model_id="m")
    k1 = _compaction_cache_key("text", 100, "spec", client)
    k2 = _compaction_cache_key("text", 100, "spec", client)
    assert k1 == k2
    assert k1 != _compaction_cache_key("other", 100, "spec", client)
    assert k1 != _compaction_cache_key("text", 200, "spec", client)
    assert k1 != _compaction_cache_key("text", 100, "architecture", client)


def test_cache_key_resists_null_byte_collision() -> None:
    """Embedded NULs in description/text must not create colliding keys."""
    client = _CountingClient(model_id="m")
    a = _compaction_cache_key("baz", 100, "foo\x00bar", client)
    b = _compaction_cache_key("bar\x00baz", 100, "foo", client)
    assert a != b
