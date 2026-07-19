"""DesignAgent structured-output happy path and degrade behavior.

``DesignAgent._invoke_and_parse`` requests provider-enforced
schema-conformant decoding (``DESIGN_SPEC_SCHEMA``) via
``LLMClient.complete_json(schema=...)`` when the active provider supports it
(currently Ollama only), eliminating the unparseable-JSON
``build_json_correction_prompt`` happy-path resend for this call. These
tests lock in: the structured call is used and skips the legacy
``strands.Agent`` + unparseable-JSON correction-prompt machinery on success;
a structurally-valid-but-DSL-invalid payload still falls through to the
legacy loop via ``_build_correction_prompt`` (structured decoding constrains
JSON *shape*, not the DSL semantic rules); the real
``provider_supports_structured_output(resolve_provider())`` wiring degrades
to the legacy parse-retry loop for an unsupported provider (Bedrock); a
``schema_forced`` semantic-exhaustion starvation signal degrades to the
legacy loop the same way; and any OTHER fatal failure from the structured
attempt propagates without degrading (a deliberate scope boundary, not a
hole). Mirrors the fixture/helper shapes in
``test_strategy_lab_refinement_structured_output.py`` and
``test_strategy_lab_design_agent.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import pytest

from investment_team.strategy_lab.agents import design as mod
from investment_team.strategy_lab.agents.design import DesignAgent
from llm_service.interface import LLMPermanentError, LLMSemanticExhaustionError


@pytest.fixture(autouse=True)
def _disable_self_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """This file exercises ``DesignAgent._invoke_and_parse`` in isolation.

    Self-review (``STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED``, default
    ``"true"``) is a separate LLM round trip through the same
    structured-output seam (``DesignAgent._self_review``,
    ``CRITIQUE_SCHEMA``) — disabling it here keeps these tests focused on
    ``_invoke_and_parse``'s ``DESIGN_SPEC_SCHEMA`` wiring and their
    single-call assertions accurate. ``_self_review``'s own
    structured-output behavior is covered separately in
    ``test_strategy_lab_design_review_structured_output.py``.
    """
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "false")


class _CapturingAgent:
    """Strands ``Agent`` replacement that records prompts and returns scripted output."""

    def __init__(self, payload: str | List[str]) -> None:
        self._payloads: List[str] = [payload] if isinstance(payload, str) else list(payload)
        self.calls: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        idx = min(len(self.calls) - 1, len(self._payloads) - 1)
        return self._payloads[idx]


class _StubClient:
    """Backing ``LLMClient`` stand-in that records every ``complete_json`` call."""

    def __init__(self, result: Dict[str, Any]) -> None:
        self._result = result
        self.calls: List[Dict[str, Any]] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append({"prompt": prompt, **kwargs})
        return self._result


class _FailingClient:
    """Backing client stand-in whose ``complete_json`` always raises."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        raise self._exc


class _FakeModel:
    """Minimal ``get_strands_model(...)`` return value: only ``.client`` is used."""

    def __init__(self, client: Any) -> None:
        self.client = client


def _structured_entry_rule() -> Dict[str, Any]:
    return {
        "kind": "entry",
        "side": "long",
        "when": {"lhs": {"name": "rsi", "params": {"period": 14}}, "op": "<", "rhs": 30},
    }


def _structured_signal_exit_rule() -> Dict[str, Any]:
    return {
        "kind": "signal_exit",
        "when": {"lhs": {"name": "rsi", "params": {"period": 14}}, "op": ">", "rhs": 70},
    }


def _structured_sizing() -> Dict[str, Any]:
    return {"kind": "fixed_fraction", "fraction": 0.02}


def _valid_design_dict() -> Dict[str, Any]:
    """A ``DESIGN_SPEC_SCHEMA``-conformant AND DSL-valid payload."""
    return {
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "timeframe": "1d",
        "entry_rules": [_structured_entry_rule()],
        "exit_rules": [_structured_signal_exit_rule()],
        "sizing": _structured_sizing(),
        "target_symbols": [],
        "risk_limits": {"max_position_pct": 5},
        "speculative": False,
        "rationale": "scripted",
    }


def _source_in_params_dict() -> Dict[str, Any]:
    """Schema-shape-conformant but DSL-invalid: ``source`` nested inside ``params``.

    ``source`` is a TOP-LEVEL field on IndicatorRef, not a member of
    ``params`` — mirrors ``test_strategy_lab_design_agent.py``'s
    ``_source_in_params_payload``. JSON-schema validation of
    ``DESIGN_SPEC_SCHEMA`` does not catch this (it is a pydantic
    ``TypeAdapter``-level rejection performed by
    :func:`validate_structured_rules`, not a JSON-shape constraint), so a
    provider that honors ``schema`` can still emit this payload.
    """
    return {
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "timeframe": "1d",
        "entry_rules": [
            {
                "kind": "entry",
                "side": "long",
                "when": {
                    "lhs": "bar.volume",
                    "op": ">",
                    "rhs": {"name": "sma", "params": {"period": 20, "source": "volume"}},
                },
            }
        ],
        "exit_rules": [_structured_signal_exit_rule()],
        "sizing": _structured_sizing(),
        "target_symbols": [],
        "risk_limits": {"max_position_pct": 5},
        "speculative": False,
        "rationale": "scripted",
    }


_GOOD_JSON = json.dumps(_valid_design_dict())


def _raise_if_agent_built(**_kwargs: Any) -> Any:
    raise AssertionError(
        "strands.Agent must not be constructed on the structured happy/degrade-fatal path"
    )


def _raise_if_correction_prompt_built(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError(
        "build_json_correction_prompt must not be called on the structured happy path"
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_structured_path_used_when_available_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient(_valid_design_dict())
    monkeypatch.setattr(mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(mod, "Agent", _raise_if_agent_built)
    monkeypatch.setattr(mod, "build_json_correction_prompt", _raise_if_correction_prompt_built)

    parsed, rationale = DesignAgent().run(prior_records=[])

    assert parsed["asset_class"] == "stocks"
    assert "strategy_code" not in parsed
    assert "rationale" not in parsed
    assert rationale == "scripted"
    assert len(stub_client.calls) == 1


def test_structured_call_passes_schema_and_expected_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient(_valid_design_dict())
    monkeypatch.setattr(mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(mod, "Agent", _raise_if_agent_built)

    DesignAgent().run(prior_records=[])

    assert len(stub_client.calls) == 1
    call = stub_client.calls[0]
    assert call["schema"] is mod.DESIGN_SPEC_SCHEMA
    assert call["system_prompt"] == mod._SYSTEM_PROMPT
    assert "Design ONE novel swing-style strategy" in call["prompt"]
    # The original task prompt was sent, not a correction re-prompt.
    assert "could not be parsed as a single JSON object" not in call["prompt"]
    assert "Offending field" not in call["prompt"]


def test_structured_agent_key_and_phase_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    def _fake_run_structured_agent(
        _agent_callable: Any,
        _prompt: str,
        *,
        agent_key: str,
        phase: str,
        parse: Any,
        coerce: Any = None,
        charge: bool = True,
        logger: Any = None,
        **_invoke_kwargs: Any,
    ) -> Dict[str, Any]:
        captured["agent_key"] = agent_key
        captured["phase"] = phase
        captured["charge"] = charge
        return _valid_design_dict()

    monkeypatch.setattr(mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: _FakeModel(_StubClient({})))
    monkeypatch.setattr(mod, "run_structured_agent", _fake_run_structured_agent)

    DesignAgent().run(prior_records=[])

    assert captured["agent_key"] == "strategy_design"
    assert captured["phase"] == "design_generate_structured"
    # Unlike RefinementAgent (charge=False), DesignAgent charges its structured
    # attempt — matches the legacy loop's charge=True (each round is a real
    # billable LLM call in the design-phase budget).
    assert captured["charge"] is True


# ---------------------------------------------------------------------------
# DSL-shape rejection on a structurally-valid structured payload
# ---------------------------------------------------------------------------


def test_structured_payload_fails_dsl_validation_falls_through_to_legacy_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structured decoding constrains JSON *shape*, not the DSL semantic
    rules. A schema-conformant-but-DSL-invalid payload must still trigger
    the ``_build_correction_prompt`` DSL-rejection retry path — never the
    unparseable-JSON ``build_json_correction_prompt`` path — and the legacy
    loop must pick up seeded with that correction, still within its own
    (unconsumed-by-the-structured-attempt) retry budget."""
    monkeypatch.delenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", raising=False)
    stub_client = _StubClient(_source_in_params_dict())
    monkeypatch.setattr(mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(mod, "build_json_correction_prompt", _raise_if_correction_prompt_built)
    capture = _CapturingAgent(_GOOD_JSON)
    monkeypatch.setattr(mod, "Agent", lambda **_k: capture)

    parsed, _ = DesignAgent().run(prior_records=[])

    assert parsed["asset_class"] == "stocks"
    assert len(stub_client.calls) == 1
    # The structured attempt is the sole "attempt 0"; the legacy loop still
    # needed only its first (retries-unconsumed) call, seeded with the DSL
    # correction prompt.
    assert len(capture.calls) == 1
    retry_prompt = capture.calls[0]
    assert "entry_rules[0]" in retry_prompt
    assert "TOP-LEVEL" in retry_prompt
    assert "source" in retry_prompt


# ---------------------------------------------------------------------------
# Degrade: capability unsupported (real provider wiring, not the seam)
# ---------------------------------------------------------------------------


def test_real_bedrock_provider_degrades_to_legacy_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the actual ``provider_supports_structured_output(resolve_provider())``
    wiring — not just the ``_structured_output_available`` seam — routes to
    the legacy loop for a provider without the capability."""
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    capture = _CapturingAgent(_GOOD_JSON)
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", lambda **_k: capture)

    parsed, _ = DesignAgent().run(prior_records=[])

    assert parsed["asset_class"] == "stocks"
    assert len(capture.calls) == 1


# ---------------------------------------------------------------------------
# Degrade: schema_forced starvation
# ---------------------------------------------------------------------------


def test_schema_forced_starvation_degrades_to_legacy_loop_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    starved_client = _FailingClient(
        LLMSemanticExhaustionError("starved", schema_forced=True, attempts_used=1)
    )
    monkeypatch.setattr(mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: _FakeModel(starved_client))
    capture = _CapturingAgent(_GOOD_JSON)
    monkeypatch.setattr(mod, "Agent", lambda **_k: capture)

    logger_name = "investment_team.strategy_lab.agents.design"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        parsed, _ = DesignAgent().run(prior_records=[])

    assert parsed["asset_class"] == "stocks"
    assert len(capture.calls) == 1
    # Falls through with the ORIGINAL prompt, not a DSL-correction seed.
    assert "Offending field" not in capture.calls[0]
    starvation_warnings = [r for r in caplog.records if "schema_forced" in r.message]
    assert len(starvation_warnings) == 1


# ---------------------------------------------------------------------------
# No degrade: a non-schema_forced fatal failure propagates unchanged
# ---------------------------------------------------------------------------


def test_non_schema_forced_permanent_error_propagates_without_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fatal_client = _FailingClient(LLMPermanentError("nope, fatal"))
    monkeypatch.setattr(mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: _FakeModel(fatal_client))
    monkeypatch.setattr(mod, "Agent", _raise_if_agent_built)

    with pytest.raises(mod.StrategyLabLLMError):
        DesignAgent().run(prior_records=[])


def test_non_schema_forced_semantic_exhaustion_propagates_without_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate checks ``.schema_forced`` specifically, not just the
    exception type — an ordinary (non-schema-forced) semantic exhaustion
    must NOT degrade to the legacy loop either."""
    exhausted_client = _FailingClient(
        LLMSemanticExhaustionError(
            "empty, but not schema forced", schema_forced=False, attempts_used=1
        )
    )
    monkeypatch.setattr(mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: _FakeModel(exhausted_client))
    monkeypatch.setattr(mod, "Agent", _raise_if_agent_built)

    with pytest.raises(mod.StrategyLabLLMError):
        DesignAgent().run(prior_records=[])


# ---------------------------------------------------------------------------
# _structured_output_available() — direct unit coverage of the seam
# ---------------------------------------------------------------------------


def test_structured_output_available_true_for_ollama_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert mod._structured_output_available() is True


def test_structured_output_available_false_for_dummy_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    assert mod._structured_output_available() is False


def test_structured_output_available_false_for_bedrock_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    assert mod._structured_output_available() is False
