"""DesignReviewAgent structured-output happy path and degrade behavior.

``DesignReviewAgent.run`` requests provider-enforced schema-conformant
decoding (``CRITIQUE_SCHEMA``) via ``LLMClient.complete_json(schema=...)``
when the active provider supports it (currently Ollama only). These tests
lock in: the structured call is used and skips the legacy ``strands.Agent``
machinery on success; the real
``provider_supports_structured_output(resolve_provider())`` wiring degrades
to the legacy single call for an unsupported provider (Bedrock); a
``schema_forced`` semantic-exhaustion starvation signal degrades to the
legacy call the same way; a non-schema_forced fatal failure still reaches
the SAME unchanged ``_fail_closed_critique`` terminal fallback (it is
re-raised out of the structured attempt, not swallowed there). Mirrors
``test_strategy_lab_refinement_structured_output.py`` and
``test_strategy_lab_design_structured_output.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.agents import design_review as design_review_mod
from investment_team.strategy_lab.agents._response_schemas import CRITIQUE_SCHEMA
from investment_team.strategy_lab.agents.design_review import DesignReviewAgent
from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate, SignalExitRule
from llm_service.interface import LLMPermanentError, LLMSemanticExhaustionError

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-review-structured",
        authored_by="test",
        asset_class="stocks",
        hypothesis="RSI mean reversion",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70)
            )
        ],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )


_READY = {"ready": True, "rationale": "spec is implementable", "issues": []}


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


def _raise_if_agent_built(**_kwargs: Any) -> Any:
    raise AssertionError(
        "strands.Agent must not be constructed on the structured happy/degrade-fatal path"
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_structured_path_used_when_available_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient(dict(_READY))
    monkeypatch.setattr(design_review_mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(
        design_review_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client)
    )
    monkeypatch.setattr(design_review_mod, "Agent", _raise_if_agent_built)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is True
    assert critique.rationale == "spec is implementable"
    assert len(stub_client.calls) == 1


def test_structured_call_passes_schema_and_expected_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient(dict(_READY))
    monkeypatch.setattr(design_review_mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(
        design_review_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client)
    )
    monkeypatch.setattr(design_review_mod, "Agent", _raise_if_agent_built)

    DesignReviewAgent().run(_spec(), readiness_results=[])

    assert len(stub_client.calls) == 1
    call = stub_client.calls[0]
    assert call["schema"] is CRITIQUE_SCHEMA
    assert call["system_prompt"] == design_review_mod._SYSTEM_PROMPT
    assert "Review the strategy specification below" in call["prompt"]


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
        return dict(_READY)

    monkeypatch.setattr(design_review_mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(
        design_review_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(_StubClient({}))
    )
    monkeypatch.setattr(design_review_mod, "run_structured_agent", _fake_run_structured_agent)

    DesignReviewAgent().run(_spec(), readiness_results=[])

    assert captured["agent_key"] == "strategy_design_review"
    assert captured["phase"] == "design_review_structured"
    # Charging happens once per round via `charge_active_budget()` in `run()`,
    # not inside `_invoke_structured` — unlike DesignAgent's per-attempt loop.
    assert captured["charge"] is False


# ---------------------------------------------------------------------------
# Degrade: capability unsupported (real provider wiring, not the seam)
# ---------------------------------------------------------------------------


def test_real_bedrock_provider_degrades_to_legacy_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the actual ``provider_supports_structured_output(resolve_provider())``
    wiring — not just the ``_structured_output_available`` seam — routes to
    the legacy call for a provider without the capability."""
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    agent = _ScriptedAgent(['{"ready": true, "rationale": "spec is implementable", "issues": []}'])
    monkeypatch.setattr(design_review_mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(design_review_mod, "Agent", lambda **_k: agent)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is True
    assert agent.calls == 1


# ---------------------------------------------------------------------------
# Degrade: schema_forced starvation
# ---------------------------------------------------------------------------


def test_schema_forced_starvation_degrades_to_legacy_call_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    starved_client = _FailingClient(
        LLMSemanticExhaustionError("starved", schema_forced=True, attempts_used=1)
    )
    monkeypatch.setattr(design_review_mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(
        design_review_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(starved_client)
    )
    agent = _ScriptedAgent(['{"ready": true, "rationale": "spec is implementable", "issues": []}'])
    monkeypatch.setattr(design_review_mod, "Agent", lambda **_k: agent)

    logger_name = "investment_team.strategy_lab.agents.design_review"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is True
    assert agent.calls == 1
    starvation_warnings = [r for r in caplog.records if "design-review decode starved" in r.message]
    assert len(starvation_warnings) == 1


# ---------------------------------------------------------------------------
# No degrade: a non-schema_forced fatal failure still falls closed
# ---------------------------------------------------------------------------


def test_non_schema_forced_permanent_error_falls_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine (non-schema_forced) failure from the structured attempt is
    re-raised out of ``_invoke`` and still lands in the SAME unchanged
    ``_fail_closed_critique`` handler ``run()`` already had — not a hole,
    not a silent degrade."""
    fatal_client = _FailingClient(LLMPermanentError("nope, fatal"))
    monkeypatch.setattr(design_review_mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(
        design_review_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(fatal_client)
    )
    monkeypatch.setattr(design_review_mod, "Agent", _raise_if_agent_built)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False
    assert any("review_parse_error" in issue.description for issue in critique.issues)


def test_non_schema_forced_semantic_exhaustion_falls_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate checks ``.schema_forced`` specifically, not just the exception
    type — an ordinary (non-schema-forced) semantic exhaustion must NOT
    degrade to the legacy call either; it still falls closed."""
    exhausted_client = _FailingClient(
        LLMSemanticExhaustionError(
            "empty, but not schema forced", schema_forced=False, attempts_used=1
        )
    )
    monkeypatch.setattr(design_review_mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(
        design_review_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(exhausted_client)
    )
    monkeypatch.setattr(design_review_mod, "Agent", _raise_if_agent_built)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False
    assert any("review_parse_error" in issue.description for issue in critique.issues)


# ---------------------------------------------------------------------------
# _structured_output_available() — direct unit coverage of the seam
# ---------------------------------------------------------------------------


def test_structured_output_available_true_for_ollama_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert design_review_mod._structured_output_available() is True


def test_structured_output_available_false_for_dummy_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    assert design_review_mod._structured_output_available() is False


def test_structured_output_available_false_for_bedrock_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    assert design_review_mod._structured_output_available() is False
