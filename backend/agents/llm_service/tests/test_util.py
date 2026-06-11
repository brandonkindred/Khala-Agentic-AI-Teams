"""Tests for llm_service.util helpers: fingerprinting and the shared retry wrapper."""

import pytest

from llm_service.interface import (
    LLMSemanticExhaustionError,
    LLMTemporaryError,
    LLMUnreachableAfterRetriesError,
)
from llm_service.util import call_llm_with_retries, sha256_fingerprint


def test_sha256_fingerprint_is_stable_and_truncated() -> None:
    fp = sha256_fingerprint("hello")
    assert fp == sha256_fingerprint("hello")
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)
    assert len(sha256_fingerprint("hello", length=12)) == 12
    assert sha256_fingerprint("hello", length=64) == sha256_fingerprint("hello", length=64)


def test_call_llm_with_retries_does_not_macro_retry_semantic_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared retry wrapper re-raises the receipt immediately: the client already
    proved the payload yields no content even after its reduced-thinking retry, so a
    macro-retry would re-burn the thinking budget with no proof of change."""
    monkeypatch.setattr("llm_service.util.time.sleep", lambda s: None)
    calls: list[int] = []

    def exhausted() -> str:
        calls.append(1)
        raise LLMSemanticExhaustionError(
            "no content",
            attempts_used=2,
            original_thinking_level="max",
            retry_thinking_level="high",
            content_bytes_seen=False,
            payload_fingerprint="abc123def4567890",
        )

    with pytest.raises(LLMSemanticExhaustionError) as exc_info:
        call_llm_with_retries(exhausted, max_attempts=3)
    assert len(calls) == 1  # no macro-retry of the identical payload
    assert exc_info.value.failure_class == "semantic_exhaustion"  # receipt preserved


def test_call_llm_with_retries_still_retries_plain_temporary_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The semantic-exhaustion carve-out must not affect ordinary transient retries."""
    monkeypatch.setattr("llm_service.util.time.sleep", lambda s: None)
    calls: list[int] = []

    def flaky() -> str:
        calls.append(1)
        raise LLMTemporaryError("5xx")

    with pytest.raises(LLMUnreachableAfterRetriesError):
        call_llm_with_retries(flaky, max_attempts=3)
    assert len(calls) == 3
