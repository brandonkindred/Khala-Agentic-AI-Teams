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
    """Records the prompts the design agent sends and returns scripted output.

    Accepts either a single payload (replayed every call) or a list of
    payloads (consumed in order; the last one repeats if the agent makes
    more calls than supplied).
    """

    def __init__(self, payload: str | List[str]) -> None:
        self._payloads: List[str] = [payload] if isinstance(payload, str) else list(payload)
        self.calls: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        idx = min(len(self.calls) - 1, len(self._payloads) - 1)
        return self._payloads[idx]


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


def _patch_design(monkeypatch: pytest.MonkeyPatch, payload: str | List[str]) -> _CapturingAgent:
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


# ---------------------------------------------------------------------------
# Parse-retry — recover from a single LLM DSL slip without killing the cycle
# ---------------------------------------------------------------------------


def _bar_close_as_indicator_ref_payload() -> str:
    """Real-world failure shape #1: LLM wraps bar.close as an IndicatorRef.

    The schema accepts ``"bar.close"`` as a bare string literal on a
    Predicate side, NOT as ``{"name": "bar.close"}``. Pydantic rejects
    this because ``"bar.close"`` is not in the ``IndicatorName`` literal.
    """
    return _payload(
        entry_rules=[
            {
                "kind": "entry",
                "side": "long",
                "when": {
                    "lhs": {"name": "bar.close"},
                    "op": "cross_above",
                    "rhs": {"name": "ema", "params": {"period": 20}},
                },
            }
        ],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )


def _sma_of_atr_payload() -> str:
    """Real-world failure shape #2: SMA-of-ATR.

    The schema's ``source`` field accepts only price/volume bar fields
    (close/high/low/open/volume/hl2/ohlc4), not indicator names. The DSL
    has no indicator-of-indicator form.
    """
    return _payload(
        entry_rules=[
            {
                "kind": "entry",
                "side": "long",
                "when": {
                    "lhs": {"name": "atr", "params": {"period": 14}},
                    "op": ">",
                    "rhs": {"name": "sma", "params": {"period": 20}, "source": "atr"},
                },
            }
        ],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )


def _good_payload() -> str:
    return _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )


def test_run_retries_parse_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single LLM DSL slip should not kill the cycle: the agent must
    re-prompt with the pydantic error and accept the corrected output."""
    monkeypatch.delenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", raising=False)
    capture = _patch_design(
        monkeypatch,
        [_bar_close_as_indicator_ref_payload(), _good_payload()],
    )

    parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 2
    assert parsed["asset_class"] == "stocks"
    # Second call MUST include corrective context referencing the
    # offending field so the model can self-correct.
    retry_prompt = capture.calls[1]
    assert "entry_rules[0]" in retry_prompt
    assert "bar.close" in retry_prompt


def test_run_retries_sma_of_atr_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second observed failure shape: SMA-of-ATR. Same recovery contract."""
    monkeypatch.delenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", raising=False)
    capture = _patch_design(
        monkeypatch,
        [_sma_of_atr_payload(), _good_payload()],
    )

    parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 2
    assert parsed["asset_class"] == "stocks"
    retry_prompt = capture.calls[1]
    assert "entry_rules[0]" in retry_prompt


def test_run_exhausts_parse_retries_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """If every attempt fails, the agent must still raise StrategySpecParseError
    so the orchestrator's existing error path takes over."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", "2")
    capture = _patch_design(monkeypatch, _bar_close_as_indicator_ref_payload())

    with pytest.raises(StrategySpecParseError):
        DesignAgent().run(prior_records=[])

    # retries=2 means 1 initial + 2 retries = 3 total attempts.
    assert len(capture.calls) == 3


def test_run_parse_retries_zero_means_single_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``STRATEGY_LAB_DESIGN_PARSE_RETRIES=0`` disables retry entirely —
    the agent makes one attempt and raises on failure. Preserves the
    pre-retry contract for callers that explicitly opt out."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", "0")
    capture = _patch_design(monkeypatch, _bar_close_as_indicator_ref_payload())

    with pytest.raises(StrategySpecParseError):
        DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 1


def test_revise_also_retries_on_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``revise()`` shares ``_invoke_and_parse`` and must inherit the
    same retry behaviour — a revision step is a DSL drift point too."""
    monkeypatch.delenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", raising=False)
    capture = _patch_design(
        monkeypatch,
        [_sma_of_atr_payload(), _good_payload()],
    )

    critique = SpecCritique(ready=False, rationale="r", issues=[])
    parsed, _ = DesignAgent().revise(_prior_spec(), critique)

    assert len(capture.calls) == 2
    assert parsed["asset_class"] == "stocks"
