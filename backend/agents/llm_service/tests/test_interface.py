"""Tests for llm_service.interface exception contracts."""

import pickle
from typing import Any, Dict, Optional

import llm_service
from llm_service.interface import LLMClient, LLMSemanticExhaustionError


def test_semantic_exhaustion_error_exported_from_package() -> None:
    """The receipt exception is part of the public llm_service namespace like its siblings."""
    assert llm_service.LLMSemanticExhaustionError is LLMSemanticExhaustionError
    assert "LLMSemanticExhaustionError" in llm_service.__all__


def test_semantic_exhaustion_error_pickle_roundtrip() -> None:
    """The receipt must survive exception-serialization boundaries (pickle rebuilds
    via cls(*args) then restores __dict__) — required kwargs without defaults would
    raise TypeError on reconstruction."""
    err = LLMSemanticExhaustionError(
        "no content",
        attempts_used=2,
        original_thinking_level="max",
        retry_thinking_level="high",
        content_bytes_seen=False,
        payload_fingerprint="abc123def4567890",
        finish_reason="stop",
    )
    restored = pickle.loads(pickle.dumps(err))
    assert isinstance(restored, LLMSemanticExhaustionError)
    assert restored.failure_class == "semantic_exhaustion"
    assert restored.attempts_used == 2
    assert restored.original_thinking_level == "max"
    assert restored.retry_thinking_level == "high"
    assert restored.content_bytes_seen is False
    assert restored.payload_fingerprint == "abc123def4567890"
    assert restored.finish_reason == "stop"


def test_semantic_exhaustion_error_schema_forced_defaults_false() -> None:
    """Constructing without schema_forced keeps existing-caller behavior unchanged."""
    err = LLMSemanticExhaustionError("no content")
    assert err.schema_forced is False


def test_semantic_exhaustion_error_pickle_roundtrip_with_schema_forced() -> None:
    """schema_forced survives the same pickle-rebuild path as the other receipt fields."""
    err = LLMSemanticExhaustionError(
        "schema-forced starvation",
        attempts_used=1,
        original_thinking_level=True,
        retry_thinking_level=None,
        content_bytes_seen=False,
        payload_fingerprint="deadbeef",
        finish_reason="stop",
        schema_forced=True,
    )
    restored = pickle.loads(pickle.dumps(err))
    assert restored.schema_forced is True


class _StubLLMClient(LLMClient):
    """Minimal concrete LLMClient for exercising the ABC's default methods."""

    def complete_json(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        schema: Any = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return {"ok": 1}


def test_llm_client_default_supports_structured_output_is_false() -> None:
    """A client that doesn't override the capability flag reports no support, by design."""
    assert _StubLLMClient().supports_structured_output() is False


class _RecordingLLMClient(LLMClient):
    """Concrete LLMClient that records the kwargs each complete_json() call received.

    Used to verify complete()'s default implementation forwards its own
    keyword arguments (e.g. max_tokens) through to complete_json() rather
    than silently dropping them.
    """

    def __init__(self) -> None:
        self.complete_json_calls: list[Dict[str, Any]] = []

    def complete_json(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        schema: Any = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.complete_json_calls.append({"max_tokens": max_tokens, **kwargs})
        return {"text": "stub response"}


def test_complete_forwards_max_tokens_to_complete_json() -> None:
    """complete()'s max_tokens argument must reach the delegated complete_json() call."""
    client = _RecordingLLMClient()
    client.complete(prompt="hi", objective="test", max_tokens=123)
    assert client.complete_json_calls[-1]["max_tokens"] == 123


def test_complete_forwards_max_tokens_none_by_default() -> None:
    """Omitting max_tokens on complete() keeps the no-cap default behavior unchanged."""
    client = _RecordingLLMClient()
    client.complete(prompt="hi", objective="test")
    assert client.complete_json_calls[-1]["max_tokens"] is None
