"""Tests for llm_service.util helpers: fingerprinting and the shared retry wrapper."""

import pytest

from llm_service.interface import (
    LLMSemanticExhaustionError,
    LLMTemporaryError,
    LLMUnreachableAfterRetriesError,
)
from llm_service.util import call_llm_with_retries, extract_json_from_response, sha256_fingerprint


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


# ---------------------------------------------------------------------------
# extract_json_from_response: recovery capabilities absorbed from
# shared.llm_recovery.agent_call_json (see test_extract_json_from_response.py
# for the full shared corpus). One focused test per newly-added capability.
# ---------------------------------------------------------------------------


def test_extract_json_from_response_repairs_truncated_object() -> None:
    """A max-tokens-truncated object gets its closing bracket/brace fabricated."""
    text = 'Here is the plan: {"tasks": [{"id": "t1"}, {"id": "t2"'
    result = extract_json_from_response(text)
    assert result["tasks"][0]["id"] == "t1"
    assert result["tasks"][1]["id"] == "t2"


def test_extract_json_from_response_strips_think_block() -> None:
    """A brace inside a <think> block must not corrupt extraction of the real payload."""
    text = '<think>the schema might look like {"maybe": true} but let us see</think>\n{"ok": true}'
    assert extract_json_from_response(text) == {"ok": True}


def test_extract_json_from_response_descends_into_envelope() -> None:
    """A payload nested one level under an envelope key is recovered when
    expected_keys anchors on a key only the nested object carries."""
    text = (
        'Note: format looks like {"format": {"a": 1}} but the real answer is '
        '{"result": {"tasks": [{"id": "t1"}]}}'
    )
    result = extract_json_from_response(text, expected_keys=frozenset({"tasks"}))
    assert result == {"tasks": [{"id": "t1"}]}


def test_extract_json_from_response_prefers_real_payload_over_format_echo() -> None:
    """An echoed format example followed by the real payload resolves to the
    real (later) object, not an invalid splice of both."""
    text = (
        'Format: {"approved": true, "issues": []}\n'
        'Verdict: {"approved": false, "issues": ["missing tests"]}'
    )
    result = extract_json_from_response(text)
    assert result == {"approved": False, "issues": ["missing tests"]}


def test_extract_json_from_response_prefers_real_payload_over_fenced_format_echo() -> None:
    """Same disambiguation as the prose case above, but with BOTH the echoed
    format example and the real answer in their own fenced ```json blocks --
    the fast first-fenced-block path must defer to last-match-wins instead of
    returning the echo."""
    text = (
        "Format example:\n```json\n"
        '{"approved": true, "issues": []}\n'
        "```\n"
        "Actual verdict:\n```json\n"
        '{"approved": false, "issues": ["missing tests"]}\n'
        "```"
    )
    result = extract_json_from_response(text)
    assert result == {"approved": False, "issues": ["missing tests"]}


def test_extract_json_from_response_clean_object_unaffected() -> None:
    """A case the existing strategies already handle must not be touched by
    the new fallback path (it returns before the fallback is ever reached)."""
    assert extract_json_from_response('{"tasks": [{"id": "t1"}]}') == {"tasks": [{"id": "t1"}]}
