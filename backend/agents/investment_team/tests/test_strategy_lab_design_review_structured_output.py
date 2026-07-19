"""``CRITIQUE_SCHEMA`` structured-output happy path and degrade behavior.

``DesignReviewAgent.run`` (the external reviewer) and
``DesignAgent._self_review`` (the internal self-audit) both emit
``CRITIQUE_SCHEMA``-shaped output and both route through the shared
``design_review._invoke_structured_critique`` helper when the active
provider supports provider-enforced schema-conformant decoding. These tests
lock in: the structured call is used and skips ``strands.Agent`` on success;
a ``schema_forced`` starvation signal degrades to the existing legacy
single-shot call for both callers; and each caller's pre-existing failure
contract is preserved — ``DesignReviewAgent.run`` never raises (a
non-``schema_forced`` failure still resolves through its fail-closed
``_fail_closed_critique`` path), while ``DesignAgent._self_review`` still
propagates (its caller, ``_with_self_review``, is the one that falls back
best-effort). Mirrors the fixture shapes in
``test_strategy_lab_design_structured_output.py`` and
``test_strategy_lab_refinement_structured_output.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.agents import design as design_mod
from investment_team.strategy_lab.agents import design_review as review_mod
from investment_team.strategy_lab.agents.design import DesignAgent
from investment_team.strategy_lab.agents.design_review import DesignReviewAgent
from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate, SignalExitRule
from llm_service.interface import LLMPermanentError, LLMSemanticExhaustionError


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


class _CapturingAgent:
    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self._payload


def _raise_if_agent_built(**_kwargs: Any) -> Any:
    raise AssertionError(
        "strands.Agent must not be constructed on the structured happy/degrade-fatal path"
    )


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-review-structured-output",
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


def _strategy_dict() -> Dict[str, Any]:
    """A parsed, DSL-valid spec dict — the shape ``_self_review`` receives."""
    return {
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "timeframe": "1d",
        "entry_rules": [
            {
                "kind": "entry",
                "side": "long",
                "when": {"lhs": {"name": "rsi", "params": {"period": 14}}, "op": "<", "rhs": 30},
            }
        ],
        "exit_rules": [
            {
                "kind": "signal_exit",
                "when": {"lhs": {"name": "rsi", "params": {"period": 14}}, "op": ">", "rhs": 70},
            }
        ],
        "sizing": {"kind": "fixed_fraction", "fraction": 0.02},
        "target_symbols": [],
        "risk_limits": {"max_position_pct": 5},
        "speculative": False,
    }


_READY_CRITIQUE = {"ready": True, "rationale": "spec is implementable", "issues": []}
_READY_JSON = json.dumps(_READY_CRITIQUE)


# ---------------------------------------------------------------------------
# DesignReviewAgent.run
# ---------------------------------------------------------------------------


def test_review_structured_path_used_when_available_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_client = _StubClient(dict(_READY_CRITIQUE))
    monkeypatch.setattr(review_mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(review_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(review_mod, "Agent", _raise_if_agent_built)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is True
    assert critique.rationale == "spec is implementable"
    assert len(stub_client.calls) == 1
    call = stub_client.calls[0]
    assert call["schema"] is review_mod.CRITIQUE_SCHEMA
    assert call["system_prompt"] == review_mod._SYSTEM_PROMPT


def test_review_schema_forced_starvation_degrades_to_legacy_single_shot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    starved_client = _FailingClient(
        LLMSemanticExhaustionError("starved", schema_forced=True, attempts_used=1)
    )
    monkeypatch.setattr(review_mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(
        review_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(starved_client)
    )
    agent = _CapturingAgent(_READY_JSON)
    monkeypatch.setattr(review_mod, "Agent", lambda **_k: agent)

    logger_name = "investment_team.strategy_lab.agents.design_review"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is True
    assert len(agent.calls) == 1
    starvation_warnings = [r for r in caplog.records if "schema_forced" in r.message]
    assert len(starvation_warnings) == 1


def test_review_non_schema_forced_failure_still_falls_closed_not_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DesignReviewAgent.run's contract is "never raises" — unlike
    RefinementAgent/DesignAgent, a non-schema_forced structured failure must
    still resolve through the existing fail-closed critique, not propagate."""
    fatal_client = _FailingClient(LLMPermanentError("nope, fatal"))
    monkeypatch.setattr(review_mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(review_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(fatal_client))
    monkeypatch.setattr(review_mod, "Agent", _raise_if_agent_built)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False
    assert any("review_parse_error" in issue.description for issue in critique.issues)


# ---------------------------------------------------------------------------
# DesignAgent._self_review
# ---------------------------------------------------------------------------


def test_self_review_structured_path_used_when_available_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``_invoke_structured_critique`` lives in ``design_review.py`` (design.py
    # imports it by name), so its ``get_strands_model(...).client`` call
    # resolves through *that* module's binding, not design.py's — only the
    # legacy fallback's ``Agent``/``get_strands_model`` live in design.py.
    stub_client = _StubClient(dict(_READY_CRITIQUE))
    monkeypatch.setattr(design_mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(review_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(design_mod, "Agent", _raise_if_agent_built)

    critique = DesignAgent()._self_review(_strategy_dict())

    assert critique.ready is True
    assert len(stub_client.calls) == 1
    call = stub_client.calls[0]
    assert call["schema"] is review_mod.CRITIQUE_SCHEMA
    assert call["system_prompt"] == design_mod._SELF_REVIEW_SYSTEM_PROMPT


def test_self_review_schema_forced_starvation_degrades_to_legacy_single_shot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    starved_client = _FailingClient(
        LLMSemanticExhaustionError("starved", schema_forced=True, attempts_used=1)
    )
    monkeypatch.setattr(design_mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(
        review_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(starved_client)
    )
    agent = _CapturingAgent(_READY_JSON)
    monkeypatch.setattr(design_mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(design_mod, "Agent", lambda **_k: agent)

    logger_name = "investment_team.strategy_lab.agents.design"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        critique = DesignAgent()._self_review(_strategy_dict())

    assert critique.ready is True
    assert len(agent.calls) == 1
    starvation_warnings = [r for r in caplog.records if "schema_forced" in r.message]
    assert len(starvation_warnings) == 1


def test_self_review_non_schema_forced_failure_propagates_without_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike DesignReviewAgent.run, _self_review's documented contract is
    to raise on failure — its caller (_with_self_review) is the best-effort
    boundary that catches it and falls back to the current spec."""
    fatal_client = _FailingClient(LLMPermanentError("nope, fatal"))
    monkeypatch.setattr(design_mod, "_structured_output_available", lambda: True)
    monkeypatch.setattr(review_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(fatal_client))
    monkeypatch.setattr(design_mod, "Agent", _raise_if_agent_built)

    with pytest.raises(design_mod.StrategyLabLLMError):
        DesignAgent()._self_review(_strategy_dict())
