"""Contract tests for :class:`DesignAgent` — the spec-only authoring agent.

The design agent replaces the legacy single-call ideation step. These
tests pin three properties:

* ``run`` returns ``(strategy_dict, rationale)`` — no ``strategy_code``.
* The agent strips any stray ``strategy_code`` the LLM emits.
* ``revise`` honours the supplied critique (the agent receives it in
  the prompt; the test captures the prompt to prove it).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.agents._parse_helpers import StrategySpecParseError
from investment_team.strategy_lab.agents.design import DesignAgent
from investment_team.strategy_lab.agents.design_review import CritiqueIssue, SpecCritique
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _CapturingAgent:
    """Records the prompts the design agent sends and returns scripted output."""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self._payload


def _payload(
    *,
    entry_rules: List[Dict[str, Any]],
    exit_rules: List[Dict[str, Any]],
    sizing: Dict[str, Any],
    extra: Dict[str, Any] | None = None,
) -> str:
    """Build a complete design-agent payload, no strategy_code."""
    body: Dict[str, Any] = {
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "timeframe": "1d",
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "sizing": sizing,
        "target_symbols": [],
        "risk_limits": {"max_position_pct": 5},
        "speculative": False,
        "rationale": "scripted",
    }
    if extra:
        body.update(extra)
    return json.dumps(body)


def _structured_entry_rule() -> Dict[str, Any]:
    return {
        "kind": "entry",
        "side": "long",
        "when": {
            "lhs": {"name": "rsi", "params": {"period": 14}},
            "op": "<",
            "rhs": 30,
        },
    }


def _structured_signal_exit_rule() -> Dict[str, Any]:
    return {
        "kind": "signal_exit",
        "when": {
            "lhs": {"name": "rsi", "params": {"period": 14}},
            "op": ">",
            "rhs": 70,
        },
    }


def _structured_sizing() -> Dict[str, Any]:
    return {"kind": "fixed_fraction", "fraction": 0.02}


def _patch_design(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> _CapturingAgent:
    """Replace the in-module ``Agent``/``get_strands_model`` with stubs.

    Returns the capturing agent so the test can inspect the prompt
    sent to the model.
    """
    capture = _CapturingAgent(payload)
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design.Agent",
        lambda **_kwargs: capture,
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design.get_strands_model",
        lambda role: object(),
    )
    return capture


# ---------------------------------------------------------------------------
# run() — happy path + defensive strip + parse / validation errors
# ---------------------------------------------------------------------------


def test_run_returns_spec_without_code(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )
    _patch_design(monkeypatch, payload)

    parsed, rationale = DesignAgent().run(prior_records=[])

    assert "strategy_code" not in parsed
    assert rationale == "scripted"
    assert parsed["asset_class"] == "stocks"


def test_run_strips_stray_strategy_code_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Defensive: if the LLM emits ``strategy_code`` despite the contract,
    the agent drops it and logs a warning so the prompt drift is observable."""
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
        extra={"strategy_code": "# the model leaked code\n"},
    )
    _patch_design(monkeypatch, payload)

    with caplog.at_level(logging.WARNING, logger="investment_team.strategy_lab.agents.design"):
        parsed, _ = DesignAgent().run(prior_records=[])

    assert "strategy_code" not in parsed
    assert any("strategy_code" in rec.message for rec in caplog.records)


def test_run_raises_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_design(monkeypatch, "no JSON here at all")
    with pytest.raises(ValueError):
        DesignAgent().run(prior_records=[])


def test_run_raises_on_prose_entry_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prose rules must trip ``StrategySpecParseError`` (locked-in DSL contract)."""
    payload = _payload(
        entry_rules=["close > sma(20)"],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )
    _patch_design(monkeypatch, payload)

    with pytest.raises(StrategySpecParseError):
        DesignAgent().run(prior_records=[])


def test_run_includes_signal_brief_and_directives_in_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )
    capture = _patch_design(monkeypatch, payload)

    # The simplest signal brief we can construct so the prompt block is
    # rendered. The brief content itself isn't asserted on; only the
    # fact that the agent dropped it into the prompt is.
    from investment_team.signal_intelligence_models import SignalIntelligenceBriefV1

    brief = SignalIntelligenceBriefV1(
        macro_themes=["risk-on"],
        micro_themes=["semis breakout"],
        confidence="medium",
    )

    DesignAgent().run(
        prior_records=[],
        signal_brief=brief,
        convergence_directives=["TIGHTEN risk", "EXPLORE crypto"],
    )

    assert len(capture.calls) == 1
    prompt = capture.calls[0]
    assert "Signal Intelligence Brief" in prompt
    assert "TIGHTEN risk" in prompt
    assert "EXPLORE crypto" in prompt


def test_run_includes_exclude_directives_in_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )
    capture = _patch_design(monkeypatch, payload)

    DesignAgent().run(prior_records=[], exclude_asset_classes=["forex"])

    assert any("MANDATORY EXCLUSION" in p and "forex" in p for p in capture.calls)


# ---------------------------------------------------------------------------
# revise() — must serialize the critique into the prompt
# ---------------------------------------------------------------------------


def _prior_spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-design-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="RSI mean reversion",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="<",
                    rhs=30,
                ),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70,
                )
            )
        ],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
        strategy_code="# legacy code that should NOT leak into the prompt",
    )


def test_revise_renders_critique_into_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )
    capture = _patch_design(monkeypatch, payload)

    critique = SpecCritique(
        ready=False,
        rationale="Sizing is too aggressive for the asset class.",
        issues=[
            CritiqueIssue(
                field="sizing",
                severity="warning",
                description="2% per trade is high for a 200d-trend strategy.",
                suggested_fix="Reduce fixed_fraction to 0.01.",
            )
        ],
    )

    parsed, _ = DesignAgent().revise(_prior_spec(), critique)

    assert "strategy_code" not in parsed
    assert len(capture.calls) == 1
    prompt = capture.calls[0]
    # The critique payload must reach the LLM.
    assert "Reduce fixed_fraction to 0.01." in prompt
    assert "sizing" in prompt
    assert "2% per trade is high" in prompt
    # The prior spec serialised into the prompt MUST NOT carry the
    # legacy ``strategy_code`` value (the prompt template's instructions
    # may mention the field name, so we look for the actual code string).
    assert "# legacy code that should NOT leak into the prompt" not in prompt


def test_revise_includes_prior_critiques_in_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )
    capture = _patch_design(monkeypatch, payload)

    critique_now = SpecCritique(
        ready=False,
        rationale="Latest concern",
        issues=[
            CritiqueIssue(field="hypothesis", description="hand-wavy", suggested_fix="tighten")
        ],
    )
    prior = [
        SpecCritique(ready=False, rationale="Round-1 concern", round=0),
    ]
    DesignAgent().revise(_prior_spec(), critique_now, prior_critiques=prior)

    assert len(capture.calls) == 1
    prompt = capture.calls[0]
    assert "Round 0" in prompt
    assert "Round-1 concern" in prompt


def test_revise_strips_stray_strategy_code(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
        extra={"strategy_code": "# leaked from revise too"},
    )
    _patch_design(monkeypatch, payload)

    critique = SpecCritique(ready=False, rationale="r", issues=[])
    parsed, _ = DesignAgent().revise(_prior_spec(), critique)
    assert "strategy_code" not in parsed
