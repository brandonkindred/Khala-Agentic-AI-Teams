"""Tests for ``llm_service.complete_validated`` (Phase 2 structured-output guard)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import BaseModel

from llm_service.interface import (
    LLMClient,
    LLMJsonParseError,
    LLMSchemaValidationError,
)
from llm_service.structured import complete_validated


class FounderAnswer(BaseModel):
    selected_option_id: str
    other_text: str | None = None
    rationale: str


class _StubClient(LLMClient):
    """Minimal LLMClient stub — routes ``complete_json`` through a user-supplied callable."""

    def __init__(self, handler):
        self._handler = handler
        self.call_prompts: list[str] = []
        self.call_system_prompts: list[str | None] = []

    def complete_json(self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs):
        self.call_prompts.append(prompt)
        self.call_system_prompts.append(system_prompt)
        return self._handler(prompt, call_index=len(self.call_prompts) - 1)


# ---------------------------------------------------------------------------
# s2-tests-success — corrected parse after one retry
# ---------------------------------------------------------------------------


def test_complete_validated_succeeds_after_parse_error(caplog):
    valid_payload = {
        "selected_option_id": "opt-a",
        "other_text": None,
        "rationale": "because reasons",
    }

    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        if call_index == 0:
            raise LLMJsonParseError(
                "Non-JSON reply",
                response_preview="# Markdown spec — not JSON",
            )
        return valid_payload

    client = _StubClient(handler)

    with caplog.at_level(logging.INFO, logger="llm_service.structured"):
        result = complete_validated(
            client,
            "generate an answer",
            schema=FounderAnswer,
        )

    assert isinstance(result, FounderAnswer)
    assert result.selected_option_id == "opt-a"
    assert len(client.call_prompts) == 2
    # Corrective prompt must embed the error + schema + preview.
    retry_prompt = client.call_prompts[1]
    assert "Non-JSON reply" in retry_prompt
    assert "# Markdown spec — not JSON" in retry_prompt
    assert "selected_option_id" in retry_prompt  # schema embedded
    # One INFO log confirming self-correction.
    success_logs = [
        r for r in caplog.records if "json_self_correction succeeded" in r.getMessage()
    ]
    assert len(success_logs) == 1
    assert "FounderAnswer" in success_logs[0].getMessage()


# ---------------------------------------------------------------------------
# s2-tests-failure — parse errors on every attempt
# ---------------------------------------------------------------------------


def test_complete_validated_terminal_parse_failure_raises_with_attempts(caplog):
    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        raise LLMJsonParseError(
            f"bad json attempt {call_index}",
            response_preview="# still markdown",
        )

    client = _StubClient(handler)

    with caplog.at_level(logging.WARNING, logger="llm_service.structured"):
        with pytest.raises(LLMJsonParseError) as excinfo:
            complete_validated(client, "prompt", schema=FounderAnswer)

    assert excinfo.value.correction_attempts_used == 1
    assert len(client.call_prompts) == 2
    warning_logs = [
        r for r in caplog.records if "json_self_correction failed terminally" in r.getMessage()
    ]
    assert len(warning_logs) == 1
    msg = warning_logs[0].getMessage()
    assert "FounderAnswer" in msg
    assert "attempts_used=1" in msg


# ---------------------------------------------------------------------------
# s2-tests-validation — parse ok on call 1 but missing required field; ok on call 2
# ---------------------------------------------------------------------------


def test_complete_validated_corrects_validation_error():
    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        if call_index == 0:
            # Missing required ``rationale`` field.
            return {"selected_option_id": "opt-a"}
        return {
            "selected_option_id": "opt-a",
            "other_text": None,
            "rationale": "validated on retry",
        }

    client = _StubClient(handler)
    result = complete_validated(client, "prompt", schema=FounderAnswer)

    assert isinstance(result, FounderAnswer)
    assert result.rationale == "validated on retry"
    retry_prompt = client.call_prompts[1]
    # The Pydantic validation error must be present in the corrective prompt.
    assert "rationale" in retry_prompt
    # Previous reply (truncated JSON) must be quoted back to the model.
    assert "opt-a" in retry_prompt


def test_complete_validated_terminal_validation_failure():
    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        return {"selected_option_id": "opt-a"}  # always missing ``rationale``

    client = _StubClient(handler)
    with pytest.raises(LLMSchemaValidationError) as excinfo:
        complete_validated(client, "prompt", schema=FounderAnswer)

    assert excinfo.value.correction_attempts_used == 1
    assert len(client.call_prompts) == 2
    assert "FounderAnswer" in str(excinfo.value)


# ---------------------------------------------------------------------------
# s2-tests-no-extract-call — pin that extract_json_from_response is never called
# ---------------------------------------------------------------------------


def test_complete_validated_never_calls_extract_json_from_response(monkeypatch):
    """The helper must operate on the parsed dict from complete_json — never raw text."""
    import llm_service.util as util_module

    def _sentinel(*args, **kwargs):
        raise AssertionError("extract_json_from_response must not be called by complete_validated")

    monkeypatch.setattr(util_module, "extract_json_from_response", _sentinel)

    # Also patch the symbol on structured in case it imported by name (it does not today,
    # but this makes the contract-pin bulletproof against future edits that add such an import).
    import llm_service.structured as structured_module

    if hasattr(structured_module, "extract_json_from_response"):
        monkeypatch.setattr(
            structured_module, "extract_json_from_response", _sentinel, raising=False
        )

    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        return {
            "selected_option_id": "opt-a",
            "other_text": None,
            "rationale": "ok",
        }

    client = _StubClient(handler)
    result = complete_validated(client, "prompt", schema=FounderAnswer)
    assert isinstance(result, FounderAnswer)


# ---------------------------------------------------------------------------
# Additional invariants — opt-out and retry-budget honoring
# ---------------------------------------------------------------------------


def test_correction_attempts_zero_opts_out():
    """correction_attempts=0 preserves today's single-shot behavior."""

    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        raise LLMJsonParseError("single shot fails", response_preview="")

    client = _StubClient(handler)
    with pytest.raises(LLMJsonParseError) as excinfo:
        complete_validated(client, "prompt", schema=FounderAnswer, correction_attempts=0)

    assert excinfo.value.correction_attempts_used == 0
    assert len(client.call_prompts) == 1


def test_context_is_forwarded_to_model_validate():
    """context= is forwarded to Pydantic validators — proves the sales_team
    citation-verification pattern works end-to-end.
    """
    from pydantic import ValidationInfo, field_validator

    class ContextAwareModel(BaseModel):
        token: str

        @field_validator("token", mode="after")
        @classmethod
        def _allow_listed(cls, value: str, info: ValidationInfo) -> str:
            allowed = (info.context or {}).get("allowed", set())
            if value not in allowed:
                raise ValueError(f"token {value!r} not in allowed set {allowed}")
            return value

    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        return {"token": "green"}

    client = _StubClient(handler)
    result = complete_validated(
        client,
        "prompt",
        schema=ContextAwareModel,
        context={"allowed": {"green", "amber"}},
    )
    assert result.token == "green"

    # Without the context, the validator rejects the same payload.
    client2 = _StubClient(handler)
    with pytest.raises(LLMSchemaValidationError):
        complete_validated(client2, "prompt", schema=ContextAwareModel, correction_attempts=0)
