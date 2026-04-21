"""Tests for the per-question bounded answer schema (issue #260).

``FounderAgent.answer_question`` used to pass the permissive ``FounderAnswer``
schema to ``llm_service.generate_structured``. The LLM could therefore return
any string for ``selected_option_id`` — including option ids that don't exist
on the question — and the hallucinated id would flow through to the SE team.

These tests pin the new behaviour:

* The schema passed to ``generate_structured`` is built per-question via
  :func:`_build_answer_schema`, and its ``selected_option_id`` is a
  ``Literal`` over the question's actual option ids plus ``"other"``.
* Invalid ids, the empty-options edge case, and the ``"other"`` /
  ``other_text`` pairing are all rejected at the schema boundary, where the
  schema-grounded self-correction retry inside
  :func:`llm_service.structured.complete_validated` can recover from them.
"""

from __future__ import annotations

import typing
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ValidationError

from user_agent_founder import agent as agent_module
from user_agent_founder.agent import FounderAnswer, _build_answer_schema

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_without_init() -> object:
    """Construct a FounderAgent without running ``__init__``.

    Mirrors the helper in ``test_agent_generate_spec.py`` — bypasses real
    Strands / llm_service bootstrap. ``answer_question`` doesn't touch
    ``self._agent``, so we can leave it unset.
    """
    founder = agent_module.FounderAgent.__new__(agent_module.FounderAgent)
    return founder


def _allowed_option_ids(schema: type[BaseModel]) -> tuple[str, ...]:
    """Extract the ``Literal[...]`` values from the ``selected_option_id`` field."""
    annotation = schema.model_fields["selected_option_id"].annotation
    return tuple(typing.get_args(annotation))


# ---------------------------------------------------------------------------
# _build_answer_schema — direct schema invariants
# ---------------------------------------------------------------------------


def test_build_answer_schema_encodes_option_ids_plus_other():
    """Schema's selected_option_id must be Literal[<ids>, "other"]."""
    schema = _build_answer_schema(
        [
            {"id": "opt-a", "label": "A"},
            {"id": "opt-b", "label": "B"},
        ],
    )
    assert _allowed_option_ids(schema) == ("opt-a", "opt-b", "other")


def test_build_answer_schema_deduplicates_explicit_other():
    """If an option happens to be called 'other' we don't emit it twice."""
    schema = _build_answer_schema(
        [
            {"id": "opt-a", "label": "A"},
            {"id": "other", "label": "Other (explicit)"},
        ],
    )
    assert _allowed_option_ids(schema) == ("opt-a", "other")


def test_build_answer_schema_empty_options_allows_only_other():
    """With no options the LLM must either say 'other' or be rejected."""
    schema = _build_answer_schema([])
    assert _allowed_option_ids(schema) == ("other",)

    # Valid: 'other' with other_text.
    ok = schema.model_validate(
        {
            "selected_option_id": "other",
            "other_text": "free-form reply",
            "rationale": "no predefined options",
        },
    )
    assert ok.selected_option_id == "other"

    # Any non-'other' id is rejected because Literal forbids it.
    with pytest.raises(ValidationError):
        schema.model_validate(
            {
                "selected_option_id": "opt-a",
                "rationale": "not allowed",
            },
        )


def test_build_answer_schema_rejects_hallucinated_id():
    """Hallucinated ids trip the Literal constraint."""
    schema = _build_answer_schema(
        [{"id": "opt-a", "label": "A"}, {"id": "opt-b", "label": "B"}],
    )
    with pytest.raises(ValidationError) as excinfo:
        schema.model_validate(
            {
                "selected_option_id": "zzz-not-a-real-id",
                "rationale": "the LLM made it up",
            },
        )
    # The error message must identify the offending field so the
    # self-correction retry can embed it in the corrective prompt.
    assert "selected_option_id" in str(excinfo.value)


def test_build_answer_schema_accepts_valid_id():
    """Happy path: a valid id + rationale validates without other_text."""
    schema = _build_answer_schema(
        [{"id": "opt-a", "label": "A"}, {"id": "opt-b", "label": "B"}],
    )
    instance = schema.model_validate(
        {
            "selected_option_id": "opt-a",
            "rationale": "cheapest option",
        },
    )
    assert instance.selected_option_id == "opt-a"
    assert instance.other_text is None
    assert instance.rationale == "cheapest option"


def test_build_answer_schema_requires_other_text_when_other_selected():
    """selected_option_id='other' without non-empty other_text must fail."""
    schema = _build_answer_schema([{"id": "opt-a", "label": "A"}])

    with pytest.raises(ValidationError) as excinfo:
        schema.model_validate(
            {
                "selected_option_id": "other",
                "other_text": None,
                "rationale": "custom answer — but I forgot the text",
            },
        )
    assert "other_text" in str(excinfo.value)

    # Empty string / whitespace is also rejected.
    with pytest.raises(ValidationError):
        schema.model_validate(
            {
                "selected_option_id": "other",
                "other_text": "   ",
                "rationale": "only whitespace",
            },
        )

    # Non-empty other_text passes.
    ok = schema.model_validate(
        {
            "selected_option_id": "other",
            "other_text": "a genuinely custom reply",
            "rationale": "none of the options fit",
        },
    )
    assert ok.other_text == "a genuinely custom reply"


# ---------------------------------------------------------------------------
# FounderAnswer back-compat — the return shape is still a plain dict with
# the same keys, so the permissive class stays usable as a reference schema.
# ---------------------------------------------------------------------------


def test_founder_answer_return_shape_unchanged():
    """FounderAnswer still accepts the historical shape so callers don't break."""
    parsed = FounderAnswer(
        selected_option_id="opt-a",
        other_text=None,
        rationale="preserved contract",
    )
    dumped = parsed.model_dump()
    assert set(dumped) == {"selected_option_id", "other_text", "rationale"}


# ---------------------------------------------------------------------------
# answer_question — bounded schema is wired through to generate_structured
# ---------------------------------------------------------------------------


def _make_question(option_ids: list[str]) -> dict[str, Any]:
    return {
        "id": "q-123",
        "question_text": "Which option do you pick?",
        "context": "context",
        "recommendation": "rec",
        "options": [
            {"id": oid, "label": f"Label for {oid}", "is_default": False} for oid in option_ids
        ],
    }


def test_answer_question_passes_bounded_schema_to_generate_structured(monkeypatch):
    """The schema handed to generate_structured must reflect the question's ids."""
    captured: dict[str, Any] = {}

    def fake_generate_structured(prompt, *, schema, system_prompt, agent_key):
        captured["schema"] = schema
        captured["prompt"] = prompt
        captured["system_prompt"] = system_prompt
        captured["agent_key"] = agent_key
        return schema.model_validate(
            {
                "selected_option_id": "opt-a",
                "other_text": None,
                "rationale": "cheapest",
            },
        )

    import llm_service

    monkeypatch.setattr(llm_service, "generate_structured", fake_generate_structured)

    founder = _make_agent_without_init()
    result = founder.answer_question(_make_question(["opt-a", "opt-b"]))

    assert result == {
        "selected_option_id": "opt-a",
        "other_text": None,
        "rationale": "cheapest",
    }
    bounded_schema = captured["schema"]
    assert _allowed_option_ids(bounded_schema) == ("opt-a", "opt-b", "other")
    # System prompt + agent key must still be plumbed through so nothing
    # regresses in the LLM-side routing.
    assert captured["system_prompt"] == agent_module.FOUNDER_SYSTEM_PROMPT
    assert captured["agent_key"] == "user_agent_founder"


def test_answer_question_hallucinated_id_is_rejected_at_boundary(monkeypatch):
    """If we hand the bounded schema a hallucinated id it must raise — which is
    the exact failure that generate_structured's self-correction retry catches
    and corrects before surfacing to the caller.
    """
    bounded_schemas: list[type[BaseModel]] = []

    def fake_generate_structured(prompt, *, schema, system_prompt, agent_key):
        bounded_schemas.append(schema)
        # Simulate what the LLM would return pre-correction — an id that's
        # not in the question's options. complete_validated normally catches
        # this and re-prompts, but here we surface the raw failure to verify
        # the bounded schema actually does the rejecting.
        return schema.model_validate(
            {
                "selected_option_id": "zzz-hallucinated",
                "rationale": "made up",
            },
        )

    import llm_service

    monkeypatch.setattr(llm_service, "generate_structured", fake_generate_structured)

    founder = _make_agent_without_init()
    with pytest.raises(ValidationError):
        founder.answer_question(_make_question(["opt-a", "opt-b"]))

    assert _allowed_option_ids(bounded_schemas[0]) == ("opt-a", "opt-b", "other")


def test_answer_question_terminal_validation_failure_propagates(monkeypatch):
    """If the schema-grounded retry inside generate_structured also fails, the
    ``LLMSchemaValidationError`` must propagate out of ``answer_question``
    unchanged so the orchestrator's narrowed ``except`` can skip the question.
    """
    from llm_service import LLMSchemaValidationError

    def fake_generate_structured(prompt, *, schema, system_prompt, agent_key):
        raise LLMSchemaValidationError(
            f"terminal failure for schema {schema.__name__}",
            correction_attempts_used=1,
        )

    import llm_service

    monkeypatch.setattr(llm_service, "generate_structured", fake_generate_structured)

    founder = _make_agent_without_init()
    with pytest.raises(LLMSchemaValidationError):
        founder.answer_question(_make_question(["opt-a", "opt-b"]))


def test_answer_question_handles_empty_options(monkeypatch):
    """Empty options => bounded schema only allows 'other'; answer_question
    returns a dict with the 'other' id and the free-text reply.
    """

    def fake_generate_structured(prompt, *, schema, system_prompt, agent_key):
        assert _allowed_option_ids(schema) == ("other",)
        return schema.model_validate(
            {
                "selected_option_id": "other",
                "other_text": "free-text reply",
                "rationale": "no predefined options",
            },
        )

    import llm_service

    monkeypatch.setattr(llm_service, "generate_structured", fake_generate_structured)

    founder = _make_agent_without_init()
    question = {
        "id": "q-empty",
        "question_text": "Anything to add?",
        "context": "",
        "recommendation": "",
        "options": [],
    }
    result = founder.answer_question(question)
    assert result["selected_option_id"] == "other"
    assert result["other_text"] == "free-text reply"


# ---------------------------------------------------------------------------
# Self-correction flow — simulate the real complete_validated behaviour by
# letting the first call surface a ValidationError and the second succeed.
# ---------------------------------------------------------------------------


def test_answer_question_recovers_from_hallucination_on_retry(monkeypatch):
    """End-to-end: first LLM reply hallucinates, retry lands a valid id.

    We fake generate_structured's internal retry by driving two calls through
    ``_build_answer_schema`` manually. This guards the contract that the
    per-question schema is stable across retries — the same ids are allowed
    on attempt 2 as on attempt 1.
    """
    call_payloads = [
        {"selected_option_id": "zzz", "rationale": "hallucinated"},
        {"selected_option_id": "opt-a", "rationale": "corrected"},
    ]

    def fake_generate_structured(prompt, *, schema, system_prompt, agent_key):
        # Try each payload in order, mirroring complete_validated: surface a
        # ValidationError on the first hallucinated payload, then return the
        # corrected instance.
        first = call_payloads[0]
        try:
            return schema.model_validate(first)
        except ValidationError:
            pass
        return schema.model_validate(call_payloads[1])

    import llm_service

    monkeypatch.setattr(llm_service, "generate_structured", fake_generate_structured)

    founder = _make_agent_without_init()
    result = founder.answer_question(_make_question(["opt-a", "opt-b"]))
    assert result["selected_option_id"] == "opt-a"
    assert result["rationale"] == "corrected"


# ---------------------------------------------------------------------------
# Sanity — the bounded schema must not be the permissive FounderAnswer
# ---------------------------------------------------------------------------


def test_bounded_schema_is_not_founder_answer():
    """Guard against a regression where answer_question reverts to the
    permissive FounderAnswer schema (which would silently accept hallucinated
    ids because ``selected_option_id`` is typed as ``str``)."""
    schema = _build_answer_schema([{"id": "opt-a", "label": "A"}])
    assert schema is not FounderAnswer
    # FounderAnswer's annotation is the plain builtin ``str``.
    assert FounderAnswer.model_fields["selected_option_id"].annotation is str
    # The bounded schema's annotation is a typing.Literal origin.
    bounded_annotation = schema.model_fields["selected_option_id"].annotation
    assert typing.get_origin(bounded_annotation) is Literal
