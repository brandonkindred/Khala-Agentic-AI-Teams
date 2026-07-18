"""RefinementAgent structured-output happy path and degrade behavior.

``RefinementAgent._invoke_and_parse`` requests provider-enforced
schema-conformant decoding (``REFINEMENT_SCHEMA``) via
``LLMClient.complete_json(schema=...)`` when the active provider supports it
(currently Ollama only), eliminating the ``build_json_correction_prompt``
happy-path resend for this call. These tests lock in: the structured call is
used and skips the legacy ``strands.Agent`` + correction-prompt machinery on
success; the real ``provider_supports_structured_output(resolve_provider())``
wiring degrades to the legacy parse-retry loop for an unsupported provider
(Bedrock); a ``schema_forced`` semantic-exhaustion starvation signal degrades
to the legacy loop the same way; and any OTHER fatal failure from the
structured attempt propagates without degrading (a deliberate scope
boundary, not a hole). Mirrors the fixture/helper shapes in
``test_strategy_lab_refinement_parse_retry.py``, which covers the legacy loop
itself (with the structured seam forced off).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from investment_team.models import RiskLimits, StrategySpec
from investment_team.strategy_lab.agents import refinement as mod
from investment_team.strategy_lab.agents.refinement import RefinementAgent
from llm_service.interface import LLMPermanentError, LLMSemanticExhaustionError


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-structured-output",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        risk_limits=RiskLimits(),
    )


class _ScriptedAgent:
    """Strands ``Agent`` replacement returning a scripted payload per call."""

    def __init__(self, payloads: List[str]) -> None:
        self._payloads = payloads
        self.calls = 0

    def __call__(self, _prompt: str) -> str:
        idx = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
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


_GOOD = '{"strategy_code": "# fixed", "changes_made": "tightened guard"}'


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
    stub_client = _StubClient({"strategy_code": "# fixed", "changes_made": "tightened guard"})
    monkeypatch.setattr(mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(mod, "Agent", _raise_if_agent_built)
    monkeypatch.setattr(mod, "build_json_correction_prompt", _raise_if_correction_prompt_built)

    updates, new_code = RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )

    assert new_code == "# fixed"
    assert updates == {"changes_made": "tightened guard"}
    assert len(stub_client.calls) == 1


def test_structured_call_passes_schema_and_expected_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient({"strategy_code": "# fixed", "changes_made": "x"})
    monkeypatch.setattr(mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(mod, "Agent", _raise_if_agent_built)

    RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )

    assert len(stub_client.calls) == 1
    call = stub_client.calls[0]
    assert call["schema"] is mod.REFINEMENT_SCHEMA
    assert call["system_prompt"] == mod._SYSTEM_PROMPT
    assert "Fix the following trading strategy code" in call["prompt"]
    # The original task prompt was sent, not a correction re-prompt.
    assert "could not be parsed as a single JSON object" not in call["prompt"]


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
        return {"strategy_code": "# fixed", "changes_made": "y"}

    monkeypatch.setattr(mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: _FakeModel(_StubClient({})))
    monkeypatch.setattr(mod, "run_structured_agent", _fake_run_structured_agent)

    RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )

    assert captured["agent_key"] == "strategy_refinement"
    assert captured["phase"] == "refinement_structured"
    assert captured["charge"] is False


# ---------------------------------------------------------------------------
# Degrade: capability unsupported (real provider wiring, not the seam)
# ---------------------------------------------------------------------------


def test_real_bedrock_provider_degrades_to_legacy_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the actual ``provider_supports_structured_output(resolve_provider())``
    wiring — not just the ``_structured_output_available`` seam — routes to
    the legacy loop for a provider without the capability."""
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    agent = _ScriptedAgent([_GOOD])
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", lambda **_k: agent)

    updates, new_code = RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )

    assert new_code == "# fixed"
    assert updates == {"changes_made": "tightened guard"}
    assert agent.calls == 1


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
    agent = _ScriptedAgent([_GOOD])
    monkeypatch.setattr(mod, "Agent", lambda **_k: agent)

    logger_name = "investment_team.strategy_lab.agents.refinement"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        updates, new_code = RefinementAgent().run(
            spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
        )

    assert new_code == "# fixed"
    assert updates == {"changes_made": "tightened guard"}
    assert agent.calls == 1
    starvation_warnings = [r for r in caplog.records if "schema_forced" in r.message]
    assert len(starvation_warnings) == 1
    assert "failure_phase=execution" in starvation_warnings[0].message


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
        RefinementAgent().run(
            spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
        )


def test_non_schema_forced_semantic_exhaustion_propagates_without_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate checks ``.schema_forced`` specifically, not just the exception
    type — an ordinary (non-schema-forced) semantic exhaustion must NOT
    degrade to the legacy loop either."""
    exhausted_client = _FailingClient(
        LLMSemanticExhaustionError(
            "empty, but not schema forced", schema_forced=False, attempts_used=1
        )
    )
    monkeypatch.setattr(mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: _FakeModel(exhausted_client))
    monkeypatch.setattr(mod, "Agent", _raise_if_agent_built)

    with pytest.raises(mod.StrategyLabLLMError):
        RefinementAgent().run(
            spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
        )


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
