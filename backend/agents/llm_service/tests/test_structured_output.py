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
    LLMTruncatedError,
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

    def complete_json(
        self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
    ):
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
            objective="test",
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
    success_logs = [r for r in caplog.records if "json_self_correction succeeded" in r.getMessage()]
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
            complete_validated(client, "prompt", objective="test", schema=FounderAnswer)

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
    result = complete_validated(client, "prompt", objective="test", schema=FounderAnswer)

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
        complete_validated(client, "prompt", objective="test", schema=FounderAnswer)

    assert excinfo.value.correction_attempts_used == 1
    assert len(client.call_prompts) == 2
    assert "FounderAnswer" in str(excinfo.value)


def test_complete_validated_terminal_validation_preserves_non_dict_preview():
    """Non-dict JSON (e.g. a list) must appear in the terminal error preview."""

    def handler(prompt: str, *, call_index: int) -> list[str]:
        return ["opt-a"]

    client = _StubClient(handler)
    with pytest.raises(LLMSchemaValidationError) as excinfo:
        complete_validated(
            client,
            "prompt",
            objective="test",
            schema=FounderAnswer,
            correction_attempts=0,
        )

    preview = excinfo.value.response_preview
    assert preview != "null"
    assert "opt-a" in preview
    assert excinfo.value.correction_attempts_used == 0


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
    result = complete_validated(client, "prompt", objective="test", schema=FounderAnswer)
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
        complete_validated(
            client, "prompt", objective="test", schema=FounderAnswer, correction_attempts=0
        )

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
        objective="test",
        schema=ContextAwareModel,
        context={"allowed": {"green", "amber"}},
    )
    assert result.token == "green"

    # Without the context, the validator rejects the same payload.
    client2 = _StubClient(handler)
    with pytest.raises(LLMSchemaValidationError):
        complete_validated(
            client2, "prompt", objective="test", schema=ContextAwareModel, correction_attempts=0
        )


def test_on_attempt_called_once_per_call_including_failed_ones():
    """on_attempt sees every attempt (the initial parse failure AND the
    corrective retry that succeeds), not just the final return value —
    otherwise a caller building a durable transcript from it would silently
    drop the first attempt's prompt/response, same as the pre-fix behavior
    this replaces."""
    valid_payload = {
        "selected_option_id": "opt-a",
        "other_text": None,
        "rationale": "because reasons",
    }

    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        if call_index == 0:
            raise LLMJsonParseError("Non-JSON reply", response_preview="# Markdown spec — not JSON")
        return valid_payload

    client = _StubClient(handler)
    attempts: list[tuple[str, str]] = []
    result = complete_validated(
        client,
        "generate an answer",
        objective="test",
        schema=FounderAnswer,
        on_attempt=lambda p, r: attempts.append((p, r)),
    )

    assert isinstance(result, FounderAnswer)
    assert len(attempts) == 2
    # First attempt: the original prompt, and the raw (unparseable) preview.
    assert attempts[0][0] == "generate an answer"
    assert attempts[0][1] == "# Markdown spec — not JSON"
    # Second attempt: the corrective prompt, and the final valid JSON.
    assert "Non-JSON reply" in attempts[1][0]
    assert "opt-a" in attempts[1][1]


def test_on_attempt_called_for_validation_failure_then_success():
    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        if call_index == 0:
            return {"selected_option_id": "opt-a"}  # missing rationale
        return {"selected_option_id": "opt-a", "rationale": "fixed"}

    client = _StubClient(handler)
    attempts: list[tuple[str, str]] = []
    complete_validated(
        client,
        "prompt",
        objective="test",
        schema=FounderAnswer,
        on_attempt=lambda p, r: attempts.append((p, r)),
    )

    assert len(attempts) == 2
    assert attempts[0][0] == "prompt"
    assert "opt-a" in attempts[0][1]  # the invalid payload that triggered the retry
    assert "fixed" in attempts[1][1]


def test_on_attempt_called_on_terminal_failure_too():
    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        raise LLMJsonParseError(f"bad json {call_index}", response_preview=f"raw-{call_index}")

    client = _StubClient(handler)
    attempts: list[tuple[str, str]] = []
    with pytest.raises(LLMJsonParseError):
        complete_validated(
            client,
            "prompt",
            objective="test",
            schema=FounderAnswer,
            on_attempt=lambda p, r: attempts.append((p, r)),
        )

    assert len(attempts) == 2  # initial + the one correction_attempts allows
    assert attempts[0][1] == "raw-0"
    assert attempts[1][1] == "raw-1"


def test_on_attempt_receives_full_raw_response_on_parse_failure():
    """Parse-failure observers get the untruncated reply, not the log-safe preview.

    ``response_preview`` stays truncated for corrective prompts; ``raw_response``
    is what transcript recorders need.
    """
    full = "malformed reply " + ("y" * 800)

    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        raise LLMJsonParseError(
            "bad json",
            response_preview=full[:500],
            raw_response=full,
        )

    client = _StubClient(handler)
    attempts: list[tuple[str, str]] = []
    with pytest.raises(LLMJsonParseError):
        complete_validated(
            client,
            "prompt",
            objective="test",
            schema=FounderAnswer,
            correction_attempts=0,
            on_attempt=lambda p, r: attempts.append((p, r)),
        )

    assert len(attempts) == 1
    assert attempts[0][1] == full
    assert len(attempts[0][1]) > 500


def test_on_attempt_exception_is_swallowed(caplog):
    """A broken observer must never break the underlying structured-output call."""

    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        return {"selected_option_id": "opt-a", "rationale": "ok"}

    client = _StubClient(handler)

    def _boom(_p, _r):
        raise RuntimeError("observer bug")

    with caplog.at_level(logging.WARNING, logger="llm_service.structured"):
        result = complete_validated(
            client, "prompt", objective="test", schema=FounderAnswer, on_attempt=_boom
        )

    assert isinstance(result, FounderAnswer)
    assert any("on_attempt callback failed" in r.message for r in caplog.records)


def test_on_attempt_called_for_truncated_complete_json():
    """A token-limit truncation is a completed LLM call; on_attempt must see
    the partial content even though complete_validated does not JSON-retry it."""

    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        raise LLMTruncatedError("hit max_tokens", partial_content='{"selected_option_id":')

    client = _StubClient(handler)
    attempts: list[tuple[str, str]] = []
    with pytest.raises(LLMTruncatedError):
        complete_validated(
            client,
            "prompt",
            objective="test",
            schema=FounderAnswer,
            on_attempt=lambda p, r: attempts.append((p, r)),
        )
    assert attempts == [("prompt", '{"selected_option_id":')]


def test_on_attempt_prefers_last_complete_json_raw_over_reserialized_dict():
    """Successful complete_json may unwrap fenced JSON; the observer must
    see the model text, not json.dumps of the parsed dict."""

    class _FencedClient(LLMClient):
        def complete_json(self, prompt, **kwargs):
            self.last_complete_json_raw = (
                '```json\n{"selected_option_id": "opt-a", "rationale": "x"}\n```'
            )
            return {"selected_option_id": "opt-a", "rationale": "x"}

    attempts: list[tuple[str, str]] = []
    complete_validated(
        _FencedClient(),
        "prompt",
        objective="test",
        schema=FounderAnswer,
        on_attempt=lambda p, r: attempts.append((p, r)),
    )
    assert len(attempts) == 1
    assert attempts[0][1].startswith("```json")
    assert '"selected_option_id": "opt-a"' in attempts[0][1]


def test_context_is_isolated_across_retry_attempts():
    """Each retry attempt must see a pristine copy of the original context.

    Regression: ``complete_validated`` used to pass the same mutable dict into
    every ``schema.model_validate(data, context=...)`` call, so a validator
    that mutated the context on a failed attempt (e.g. the sales outreach
    flow setting ``context["citations_stripped"] = True``) would leak that
    flag into the next retry and silently corrupt a clean payload.
    """
    from pydantic import ValidationInfo, field_validator, model_validator

    class MutatingRetryModel(BaseModel):
        value: str

        @field_validator("value", mode="after")
        @classmethod
        def _record_marker(cls, v: str, info: ValidationInfo) -> str:
            # Every call sets a flag on the context — simulating the sales
            # outreach ``citations_stripped`` side-channel. If retries share
            # state, attempt 2 sees ``was_marked=True`` and ``failed_first=True``
            # set by attempt 1.
            if info.context is not None:
                info.context["was_marked"] = True
            return v

        @model_validator(mode="after")
        def _fail_once_if_previously_marked(self, info: ValidationInfo):
            if info.context is not None and info.context.get("failed_first"):
                raise ValueError("context from a previous attempt leaked into this retry")
            if info.context is not None:
                info.context["failed_first"] = True
            if self.value == "fail":
                raise ValueError("initial attempt fails to trigger a retry")
            return self

    payloads = [{"value": "fail"}, {"value": "clean"}]

    def handler(prompt: str, *, call_index: int) -> dict[str, Any]:
        return payloads[call_index]

    client = _StubClient(handler)
    caller_context: dict[str, Any] = {"was_marked": False, "failed_first": False}

    # With context isolation, the retry sees ``failed_first=False`` and
    # ``_fail_once_if_previously_marked`` does not raise.
    result = complete_validated(
        client,
        "prompt",
        objective="test",
        schema=MutatingRetryModel,
        context=caller_context,
        correction_attempts=1,
    )
    assert result.value == "clean"
    # And the caller's context dict is never mutated.
    assert caller_context == {"was_marked": False, "failed_first": False}
