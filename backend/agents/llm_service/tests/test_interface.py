"""Tests for llm_service.interface exception contracts."""

import pickle

import llm_service
from llm_service.interface import LLMSemanticExhaustionError


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
