"""Tests for the two-call reasoning/formatting split helpers.

``complete_json_via_reasoning`` and ``complete_validated_via_reasoning`` each
sequence a think=True prose reasoning call (``client.complete``) followed by a
think=False JSON transcription call (``client.complete_json`` /
``complete_validated``). These tests lock in the contract the six migrated
call sites depend on: which parameter feeds which pass (notably the
``reasoning_temperature`` / ``temperature`` split, whose misbinding is easy to
reintroduce), that the reasoning prose actually reaches the formatting prompt,
that ``**kwargs`` (e.g. provider-enforced ``schema=``) route to the formatting
call only, and that a step-1 failure propagates without invoking step 2.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from pydantic import BaseModel

from llm_service.interface import LLMClient, LLMPermanentError
from llm_service.structured import (
    complete_json_via_reasoning,
    complete_validated_via_reasoning,
)


class _Verdict(BaseModel):
    status: str
    score: float


class _RecordingClient(LLMClient):
    """Records every ``complete`` / ``complete_json`` call in invocation order.

    ``complete`` returns ``prose`` (or raises ``complete_error``); the
    ``LLMClient`` base's default ``complete`` would route through
    ``complete_json`` and consume the canned formatting response, so it is
    overridden explicitly here — the same trap the migrated call sites' own
    test doubles hit.
    """

    def __init__(
        self,
        json_response: Optional[dict[str, Any]] = None,
        *,
        prose: str = "ANALYSIS PROSE",
        complete_error: Optional[Exception] = None,
    ) -> None:
        self._json_response = json_response if json_response is not None else {}
        self._prose = prose
        self._complete_error = complete_error
        self.reasoning_calls: list[dict[str, Any]] = []
        self.format_calls: list[dict[str, Any]] = []
        self.order: list[str] = []

    def complete(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
    ) -> str:
        self.order.append("complete")
        self.reasoning_calls.append(
            {
                "prompt": prompt,
                "objective": objective,
                "temperature": temperature,
                "system_prompt": system_prompt,
                "think": think,
            }
        )
        if self._complete_error is not None:
            raise self._complete_error
        return self._prose

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.order.append("complete_json")
        self.format_calls.append(
            {
                "prompt": prompt,
                "temperature": temperature,
                "system_prompt": system_prompt,
                "think": think,
                **kwargs,
            }
        )
        return self._json_response


# ---------------------------------------------------------------------------
# complete_json_via_reasoning
# ---------------------------------------------------------------------------


def test_json_via_reasoning_sequences_reason_then_format() -> None:
    client = _RecordingClient({"ok": True})

    out = complete_json_via_reasoning(
        client,
        reasoning_prompt="think about X",
        reasoning_system_prompt="you are a reasoner",
        formatting_instructions='emit {"ok": bool}',
        objective="do the thing",
    )

    assert out == {"ok": True}
    # Exactly two provider calls, reasoning first.
    assert client.order == ["complete", "complete_json"]


def test_json_via_reasoning_sets_think_flags_per_pass() -> None:
    client = _RecordingClient({"ok": True})

    complete_json_via_reasoning(
        client,
        reasoning_prompt="p",
        reasoning_system_prompt=None,
        formatting_instructions="f",
        objective="obj",
    )

    assert client.reasoning_calls[0]["think"] is True
    assert client.format_calls[0]["think"] is False


def test_json_via_reasoning_splits_temperature_between_passes() -> None:
    """``reasoning_temperature`` drives the analysis; ``temperature`` the
    transcription. A caller passing only ``temperature=`` must NOT change the
    reasoning pass — the misbinding this split originally shipped with."""
    client = _RecordingClient({"ok": True})

    complete_json_via_reasoning(
        client,
        reasoning_prompt="p",
        reasoning_system_prompt=None,
        formatting_instructions="f",
        objective="obj",
        reasoning_temperature=0.1,
        temperature=0.0,
    )

    assert client.reasoning_calls[0]["temperature"] == 0.1
    assert client.format_calls[0]["temperature"] == 0.0


def test_json_via_reasoning_temperature_defaults() -> None:
    client = _RecordingClient({"ok": True})

    complete_json_via_reasoning(
        client,
        reasoning_prompt="p",
        reasoning_system_prompt=None,
        formatting_instructions="f",
        objective="obj",
    )

    # Documented defaults: latitude for reasoning, deterministic transcription.
    assert client.reasoning_calls[0]["temperature"] == 0.3
    assert client.format_calls[0]["temperature"] == 0.0


def test_json_via_reasoning_embeds_prose_and_instructions_in_format_prompt() -> None:
    client = _RecordingClient({"ok": True}, prose="THE REASONED ANALYSIS")

    complete_json_via_reasoning(
        client,
        reasoning_prompt="the original question",
        reasoning_system_prompt=None,
        formatting_instructions="SHAPE INSTRUCTIONS",
        objective="obj",
    )

    format_prompt = client.format_calls[0]["prompt"]
    assert "THE REASONED ANALYSIS" in format_prompt
    assert "SHAPE INSTRUCTIONS" in format_prompt


def test_json_via_reasoning_routes_system_prompts_per_pass() -> None:
    client = _RecordingClient({"ok": True})

    complete_json_via_reasoning(
        client,
        reasoning_prompt="p",
        reasoning_system_prompt="REASONING SYSTEM",
        formatting_instructions="f",
        formatting_system_prompt="FORMATTING SYSTEM",
        objective="obj",
    )

    assert client.reasoning_calls[0]["system_prompt"] == "REASONING SYSTEM"
    assert client.format_calls[0]["system_prompt"] == "FORMATTING SYSTEM"


def test_json_via_reasoning_tags_objectives_per_pass() -> None:
    client = _RecordingClient({"ok": True})

    complete_json_via_reasoning(
        client,
        reasoning_prompt="p",
        reasoning_system_prompt=None,
        formatting_instructions="f",
        objective="rank job candidates",
    )

    assert client.reasoning_calls[0]["objective"] == "rank job candidates (reasoning)"
    assert client.format_calls[0]["objective"] == "rank job candidates (format)"


def test_json_via_reasoning_forwards_kwargs_to_formatting_call_only() -> None:
    """``schema=`` (provider-enforced decoding) must reach ``complete_json``;
    the unconstrained reasoning call takes no schema."""
    client = _RecordingClient({"ok": True})
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

    complete_json_via_reasoning(
        client,
        reasoning_prompt="p",
        reasoning_system_prompt=None,
        formatting_instructions="f",
        objective="obj",
        schema=schema,
    )

    assert client.format_calls[0]["schema"] == schema
    assert "schema" not in client.reasoning_calls[0]


def test_json_via_reasoning_step_one_failure_skips_formatting_call() -> None:
    """Postcondition: a reasoning-pass exception propagates immediately and
    step 2 is never invoked — matching the single-call form it replaced."""
    boom = LLMPermanentError("reasoning pass is down")
    client = _RecordingClient({"ok": True}, complete_error=boom)

    with pytest.raises(LLMPermanentError, match="reasoning pass is down"):
        complete_json_via_reasoning(
            client,
            reasoning_prompt="p",
            reasoning_system_prompt=None,
            formatting_instructions="f",
            objective="obj",
        )

    assert client.order == ["complete"]
    assert client.format_calls == []


# ---------------------------------------------------------------------------
# complete_validated_via_reasoning
# ---------------------------------------------------------------------------


def test_validated_via_reasoning_returns_validated_model() -> None:
    client = _RecordingClient({"status": "PASS", "score": 0.9})

    out = complete_validated_via_reasoning(
        client,
        schema=_Verdict,
        reasoning_prompt="p",
        reasoning_system_prompt="rs",
        objective="obj",
    )

    assert isinstance(out, _Verdict)
    assert out.status == "PASS"
    assert out.score == 0.9
    assert client.order == ["complete", "complete_json"]


def test_validated_via_reasoning_splits_temperature_between_passes() -> None:
    client = _RecordingClient({"status": "PASS", "score": 1.0})

    complete_validated_via_reasoning(
        client,
        schema=_Verdict,
        reasoning_prompt="p",
        reasoning_system_prompt=None,
        objective="obj",
        reasoning_temperature=0.0,
        temperature=0.0,
    )

    assert client.reasoning_calls[0]["temperature"] == 0.0
    assert client.format_calls[0]["temperature"] == 0.0


def test_validated_via_reasoning_reasoning_temperature_default_is_independent() -> None:
    """A caller passing only ``temperature=0.0`` (the pre-split idiom for "be
    deterministic") leaves the reasoning pass at the helper's own default —
    the regression the six migrated call sites had to fix explicitly."""
    client = _RecordingClient({"status": "PASS", "score": 1.0})

    complete_validated_via_reasoning(
        client,
        schema=_Verdict,
        reasoning_prompt="p",
        reasoning_system_prompt=None,
        objective="obj",
        temperature=0.0,
    )

    assert client.format_calls[0]["temperature"] == 0.0
    assert client.reasoning_calls[0]["temperature"] == 0.3


def test_validated_via_reasoning_sets_think_flags_per_pass() -> None:
    client = _RecordingClient({"status": "PASS", "score": 1.0})

    complete_validated_via_reasoning(
        client,
        schema=_Verdict,
        reasoning_prompt="p",
        reasoning_system_prompt=None,
        objective="obj",
    )

    assert client.reasoning_calls[0]["think"] is True
    assert client.format_calls[0]["think"] is False


def test_validated_via_reasoning_embeds_prose_in_format_prompt() -> None:
    client = _RecordingClient({"status": "FAIL", "score": 0.1}, prose="MY CRITIQUE")

    complete_validated_via_reasoning(
        client,
        schema=_Verdict,
        reasoning_prompt="p",
        reasoning_system_prompt=None,
        objective="obj",
    )

    assert "MY CRITIQUE" in client.format_calls[0]["prompt"]


def test_validated_via_reasoning_step_one_failure_skips_formatting_call() -> None:
    boom = LLMPermanentError("reasoning down")
    client = _RecordingClient({"status": "PASS", "score": 1.0}, complete_error=boom)

    with pytest.raises(LLMPermanentError, match="reasoning down"):
        complete_validated_via_reasoning(
            client,
            schema=_Verdict,
            reasoning_prompt="p",
            reasoning_system_prompt=None,
            objective="obj",
        )

    assert client.order == ["complete"]
    assert client.format_calls == []


def test_validated_via_reasoning_runs_one_reasoning_pass_across_corrections() -> None:
    """A schema miss re-prompts only the formatting call; the (expensive)
    reasoning pass is not repeated."""

    class _CorrectingClient(_RecordingClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
            super().complete_json(prompt, **kwargs)
            if len(self.format_calls) == 1:
                return {"status": "PASS"}  # missing `score` -> validation error
            return {"status": "PASS", "score": 0.5}

    client = _CorrectingClient({})

    out = complete_validated_via_reasoning(
        client,
        schema=_Verdict,
        reasoning_prompt="p",
        reasoning_system_prompt=None,
        objective="obj",
        correction_attempts=1,
    )

    assert out.score == 0.5
    assert len(client.reasoning_calls) == 1
    assert len(client.format_calls) == 2
