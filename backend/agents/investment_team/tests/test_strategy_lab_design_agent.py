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
from investment_team.strategy_lab.agents import _structured_output as so_mod
from investment_team.strategy_lab.agents._llm_budget import (
    DesignBudgetExhausted,
    LLMCallBudget,
    use_budget,
)
from investment_team.strategy_lab.agents._parse_helpers import (
    StrategySpecParseError,
    build_json_correction_prompt,
)
from investment_team.strategy_lab.agents._response_schemas import DESIGN_SPEC_SCHEMA
from investment_team.strategy_lab.agents.design import (
    _DESIGN_SPEC_SCHEMA_JSON,
    DesignAgent,
    _build_correction_prompt,
    _get_self_review_system_prompt,
    _resolve_diversity_mode,
)
from investment_team.strategy_lab.agents.design_review import CritiqueIssue, SpecCritique
from investment_team.strategy_lab.market_regime import RegimeEntry, RegimeSummary
from investment_team.strategy_lab.spec_dsl import (
    AllOf,
    EntryRule,
    EntryRuleAdapter,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)


@pytest.fixture(autouse=True)
def _force_legacy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """This file exercises the unconstrained parse-retry loop exclusively.

    Force the structured-output seam off so these tests are deterministic
    regardless of ambient ``LLM_PROVIDER`` (unset defaults to ``"ollama"``,
    whose capability flag is True) — see
    ``so.structured_output_available``. The structured path itself is
    covered by ``test_strategy_lab_design_structured_output.py``.
    """
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: False)


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


def _patch_design(
    monkeypatch: pytest.MonkeyPatch,
    payload: str | List[str],
    *,
    enable_self_review: bool = False,
) -> _CapturingAgent:
    """Replace the in-module ``Agent``/``get_strands_model`` with stubs.

    Returns the capturing agent so the test can inspect the prompt
    sent to the model. Self-review is disabled by default so legacy
    single-call tests don't have to script an extra critique payload;
    self-review tests opt in with ``enable_self_review=True``.
    """
    capture = _CapturingAgent(payload)
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design.Agent",
        lambda **_kwargs: capture,
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design.get_strands_model",
        lambda *_a, **_k: object(),
    )
    # The design-generation retry loop itself now builds its ``Agent`` inside
    # ``_agent_runner.run_json_with_parse_retry`` rather than in ``design.py``
    # directly (see DesignAgent._legacy_parse_retry_loop) — patch that
    # module's names too so the same capturing stub intercepts those calls.
    # ``design.Agent``/``design.get_strands_model`` above remain patched for
    # the still-untouched self-review call site.
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents._agent_runner.Agent",
        lambda **_kwargs: capture,
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents._agent_runner.get_strands_model",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setenv(
        "STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED",
        "true" if enable_self_review else "false",
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


def test_design_prompt_states_dual_objective_and_forecast_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user prompt must state the dual objective (return + win rate, under a
    positive-expectancy constraint) and direct the agent through the FORECAST
    step that emits the structured ``expectancy_forecast``."""
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )
    capture = _patch_design(monkeypatch, payload)

    DesignAgent().run(prior_records=[])

    prompt = capture.calls[0]
    lowered = prompt.lower()
    # Dual objective: both return and win rate are named, under an expectancy constraint.
    assert "annualized return" in lowered
    assert "win rate" in lowered
    assert "expectancy" in lowered
    # The FORECAST step is wired into the decomposed process and asks for the
    # machine-readable forecast object.
    assert "FORECAST" in prompt
    assert "expectancy_forecast" in prompt
    assert "reward:risk" in lowered


def test_run_returns_structured_expectancy_forecast_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A designer-emitted ``expectancy_forecast`` rides through the parse path
    untouched (it is neither a rule slot nor popped like ``rationale``)."""
    forecast = {
        "forecast_win_rate": 0.6,
        "reward_risk": 2.0,
        "trades_per_year": 30,
        "projected_annual_return_pct": 14.0,
        "consistency_note": "60% wins at 2:1 over 30 trades/yr supports ~14%",
    }
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
        extra={"expectancy_forecast": forecast},
    )
    _patch_design(monkeypatch, payload)

    parsed, _ = DesignAgent().run(prior_records=[])

    assert parsed["expectancy_forecast"] == forecast


def test_run_without_expectancy_forecast_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forecast is advisory and never gated: a spec emitted without one
    still parses cleanly."""
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )
    _patch_design(monkeypatch, payload)

    parsed, _ = DesignAgent().run(prior_records=[])

    assert "expectancy_forecast" not in parsed


def test_design_system_prompt_states_dual_objective_and_forecast() -> None:
    """The design system prompt must frame the dual objective + expectancy
    constraint, flag the win-rate-alone trap, and wire in the FORECAST step."""
    from investment_team.strategy_lab.agents.design import _get_design_system_prompt

    text = _get_design_system_prompt()
    lowered = text.lower()
    assert "dual objective" in lowered
    assert "win rate" in lowered
    assert "expectancy after costs" in lowered
    assert "trap" in lowered  # the win-rate-alone caveat
    assert "FORECAST" in text  # the new decomposed-process step
    assert "expectancy_forecast" in text  # the machine-readable forecast field


def test_design_system_prompt_documents_multi_confirmation_combinator() -> None:
    """The design system prompt must document the ``all_of`` / ``any_of``
    combinator as the win-rate lever for multi-confirmation entries — selective,
    compilable, and NOT a ``requires_custom_code`` case."""
    # The system prompt is loaded lazily; the test reads it via the helper to
    # assert prompt-content guidance (mirrors the existing
    # ``test_design_system_prompt_states_dual_objective_and_forecast``).
    from investment_team.strategy_lab.agents.design import _get_design_system_prompt

    text = _get_design_system_prompt()
    lowered = text.lower()
    # Entry selection is framed around win-rate selectivity.
    assert "selectivity" in lowered
    # The combinator is documented, with a worked all_of example.
    assert "all_of" in lowered
    assert "any_of" in lowered
    assert "multi-confirmation" in lowered
    assert '"kind": "all_of"' in text
    # Faking an AND-thesis via OR'd entry rules is still called out as looser.
    assert "loosens" in lowered


def test_design_system_prompt_keeps_custom_code_off_the_multi_confirmation_path() -> None:
    """``requires_custom_code`` must NOT be steered toward for multi-confirmation
    (the Codex P1): the combinator handles it, so the flag reverts to genuinely
    inexpressible cases (cross-asset / path-dependent state)."""
    from investment_team.strategy_lab.agents.design import _get_design_system_prompt

    text = _get_design_system_prompt()
    lowered = text.lower()
    assert "requires_custom_code" in text
    # The default framing is restored: setting it true is rare.
    assert "setting it `true` is rare" in lowered
    # The genuinely-inexpressible cases remain the only triggers.
    assert "cross-asset" in lowered
    assert "path-dependent" in lowered


def test_design_system_prompt_states_win_rate_tuned_exit_guidance() -> None:
    """The design system prompt must carry the win-rate-tuned exit guidance:
    bank partials early via a scaled take-profit, with a trailing-stop runner
    on the remainder to preserve return."""
    from investment_team.strategy_lab.agents.design import _get_design_system_prompt

    lowered = _get_design_system_prompt().lower()
    assert "scaled_take_profit" in lowered
    assert "partials early" in lowered
    assert "trailing" in lowered and "runner" in lowered
    # Tied back to the existing reward:risk / break-even win-rate self-check.
    assert "reward_risk" in lowered or "break-even win rate" in lowered


def test_run_accepts_all_of_multi_confirmation_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-confirmation entry authored as one ``all_of`` rule parses cleanly
    through ``DesignAgent().run()`` and stays ``requires_custom_code`` false —
    the combinator path the prompt now steers toward is representable end-to-end."""
    all_of_entry = {
        "kind": "entry",
        "side": "long",
        "when": {
            "kind": "all_of",
            "of": [
                {"lhs": "bar.close", "op": ">", "rhs": {"name": "sma", "params": {"period": 200}}},
                {"lhs": {"name": "rsi", "params": {"period": 14}}, "op": "<", "rhs": 40},
            ],
        },
    }
    payload = _payload(
        entry_rules=[all_of_entry],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
        extra={"hypothesis": "Long only when the 200-SMA trend AND an RSI pullback both confirm."},
    )
    _patch_design(monkeypatch, payload)

    parsed, _ = DesignAgent().run(prior_records=[])

    # The all_of tree round-trips through validation; the flag stays false.
    rule = EntryRuleAdapter.validate_python(parsed["entry_rules"][0])
    assert isinstance(rule.when, AllOf)
    assert len(rule.when.of) == 2
    assert parsed.get("requires_custom_code", False) is False


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


def test_run_includes_positive_menu_in_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Excluding all-but-one class must surface a positive menu naming the single
    remaining category — supplied by the (menu-restricted) mix hint, not a second
    allow-list line in the design agent (which would duplicate the menu)."""
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )
    capture = _patch_design(monkeypatch, payload)

    DesignAgent().run(
        prior_records=[],
        exclude_asset_classes=["stocks", "crypto", "futures", "commodities"],
    )

    # The positive menu comes from asset_class_mix_hint ("Choose **asset_class**
    # from forex ..."); the design agent no longer restates the allowed list.
    assert any(
        "Choose **asset_class** from forex" in p and "MANDATORY EXCLUSION" in p
        for p in capture.calls
    )
    # The redundant second positive allow-list line must be gone.
    assert all("Choose **asset_class** ONLY from:" not in p for p in capture.calls)


def test_run_prompt_embeds_response_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generation prompt carries the JSON Schema so the wire model, the
    hand-written skeleton, and the downstream coercer cannot drift apart."""
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )
    capture = _patch_design(monkeypatch, payload)

    DesignAgent().run(prior_records=[])

    prompt = capture.calls[0]
    assert "MUST conform to this JSON Schema" in prompt
    assert _DESIGN_SPEC_SCHEMA_JSON in prompt
    assert "expectancy_forecast" in prompt


def test_embedded_schema_matches_format_constraint() -> None:
    """The schema embedded in the prompt is the same object exported from
    ``_response_schemas`` — the prompt-level contract cannot silently drift
    from whatever is validated elsewhere."""
    assert json.loads(_DESIGN_SPEC_SCHEMA_JSON) == DESIGN_SPEC_SCHEMA
    assert "expectancy_forecast" in DESIGN_SPEC_SCHEMA["properties"]


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


def test_revise_prompt_embeds_response_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """The revision prompt asks the LLM to return the same wire shape as
    initial generation, so it must carry the same schema."""
    payload = _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )
    capture = _patch_design(monkeypatch, payload)

    critique = SpecCritique(ready=False, rationale="r", issues=[])
    DesignAgent().revise(_prior_spec(), critique)

    prompt = capture.calls[0]
    assert "MUST conform to this JSON Schema" in prompt
    assert _DESIGN_SPEC_SCHEMA_JSON in prompt


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


def _source_in_params_payload() -> str:
    """Real-world failure shape #3: ``source`` nested inside ``params``.

    ``source`` is a TOP-LEVEL field on IndicatorRef, not a member of
    ``params``. The per-indicator param registry rejects unexpected keys,
    so ``params: {"period": 20, "source": "volume"}`` trips with
    "unexpected param 'source'; allowed: ['period']" for SMA/EMA.
    """
    return _payload(
        entry_rules=[
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


def test_run_retries_source_in_params_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Third observed failure shape: ``source`` nested inside ``params``.

    The correction prompt must explicitly call out that ``source`` is a
    top-level field on IndicatorRef — without that nudge the model often
    re-emits the same misplaced key on retry.
    """
    monkeypatch.delenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", raising=False)
    capture = _patch_design(
        monkeypatch,
        [_source_in_params_payload(), _good_payload()],
    )

    parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 2
    assert parsed["asset_class"] == "stocks"
    retry_prompt = capture.calls[1]
    assert "entry_rules[0]" in retry_prompt
    # The corrective preamble must steer the LLM toward the top-level
    # `source` shape so the retry is more than just a re-roll.
    assert "TOP-LEVEL" in retry_prompt
    assert "source" in retry_prompt


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


def test_run_validation_error_of_unexpected_type_raises_typeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_on_validation_error`` must fail loudly (``TypeError``) instead of
    silently passing through a non-``StrategySpecParseError`` exception —
    the ``assert isinstance(...)`` it replaced would have vanished entirely
    under ``python -O``, letting the wrong type reach the correction-prompt
    builder unnoticed."""
    capture = _patch_design(monkeypatch, _bar_close_as_indicator_ref_payload())

    def _boom(_parsed: Dict[str, Any]) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("investment_team.strategy_lab.agents.design._finalize_parsed", _boom)

    with pytest.raises(TypeError, match="expected StrategySpecParseError") as excinfo:
        DesignAgent().run(prior_records=[])

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    # The TypeError propagates immediately from on_validation_error itself —
    # it is not caught/retried by run_json_with_parse_retry, so only the
    # first attempt's call is made.
    assert len(capture.calls) == 1


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


def test_parse_retry_builds_fresh_agent_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each parse-retry attempt must use a freshly constructed, history-free
    agent — never one instance reused across attempts.

    ``strands.Agent`` accumulates conversation history in ``self.messages``;
    reusing one instance would feed the model its own rejected JSON back as
    context on the correction re-prompt, biasing it toward defending the
    malformed shape. We assert (a) the factory is invoked once per LLM call
    and (b) every constructed agent saw exactly one prompt (no cross-attempt
    carryover).
    """
    monkeypatch.delenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", raising=False)

    payloads = [_bar_close_as_indicator_ref_payload(), _good_payload()]
    built: List[_CapturingAgent] = []

    def _factory(**_kwargs: Any) -> _CapturingAgent:
        # Hand each freshly constructed agent the single payload it is
        # expected to emit, by construction order, so attempt 1 slips the
        # DSL and attempt 2 recovers.
        idx = min(len(built), len(payloads) - 1)
        agent = _CapturingAgent(payloads[idx])
        built.append(agent)
        return agent

    monkeypatch.setattr(
        "investment_team.strategy_lab.agents._agent_runner.Agent",
        _factory,
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents._agent_runner.get_strands_model",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "false")

    parsed, _ = DesignAgent().run(prior_records=[])

    assert parsed["asset_class"] == "stocks"
    # One construction per attempt (here: initial slip + one recovery).
    assert len(built) == 2
    # No conversation carryover: each agent fielded exactly one prompt.
    assert all(len(agent.calls) == 1 for agent in built)
    # The recovery agent — not the slipping one — saw the correction context.
    assert "bar.close" in built[1].calls[0]
    assert "entry_rules[0]" in built[1].calls[0]


# ---------------------------------------------------------------------------
# Self-review pass — catch prose↔predicate + risk-math contradictions
# inside DesignAgent before they reach the external review loop.
# ---------------------------------------------------------------------------


def _ready_critique_payload() -> str:
    """Self-review verdict declaring the candidate spec internally coherent."""
    return json.dumps(
        {
            "ready": True,
            "rationale": "Internally coherent; predicates implement every prose claim.",
            "issues": [],
        }
    )


def _failing_critique_payload() -> str:
    """Self-review verdict flagging a prose↔predicate completeness gap."""
    return json.dumps(
        {
            "ready": False,
            "rationale": (
                "Hypothesis names an ADX > 25 trend filter but no entry rule references ADX."
            ),
            "issues": [
                {
                    "field": "entry_rules",
                    "severity": "critical",
                    "description": "Hypothesis mentions ADX > 25 but no predicate uses adx.",
                    "suggested_fix": "Add an entry predicate {lhs: adx(14), op: >, rhs: 25}.",
                }
            ],
        }
    )


def _ready_with_warning_critique_payload() -> str:
    """Self-review verdict that is ready=true but carries an advisory warning.

    This is the common LLM shape the over-demotion bug fired on: the model
    is satisfied with the spec yet flags a minor non-blocking note.
    """
    return json.dumps(
        {
            "ready": True,
            "rationale": "Internally coherent; one minor advisory note.",
            "issues": [
                {
                    "field": "sizing",
                    "severity": "warning",
                    "description": "note: fraction param 26 is unusual but valid.",
                }
            ],
        }
    )


def _ready_with_critical_critique_payload() -> str:
    """Self-review verdict that is ready=true but contradicts itself with a critical."""
    return json.dumps(
        {
            "ready": True,
            "rationale": "Looks ready.",
            "issues": [
                {
                    "field": "entry_rules",
                    "severity": "critical",
                    "description": "Hypothesis mentions ADX > 25 but no predicate uses adx.",
                    "suggested_fix": "Add an entry predicate {lhs: adx(14), op: >, rhs: 25}.",
                }
            ],
        }
    )


def test_run_self_review_passes_no_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    """When self-review marks the candidate ready, no self-revision fires.

    Two LLM calls total: initial generation + self-review verdict. The
    returned spec is the original draft, unmodified.
    """
    capture = _patch_design(
        monkeypatch,
        [_good_payload(), _ready_critique_payload()],
        enable_self_review=True,
    )

    parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 2
    assert parsed["asset_class"] == "stocks"


def test_run_self_review_flags_then_self_revises(monkeypatch: pytest.MonkeyPatch) -> None:
    """When self-review flags issues, the designer self-revises then re-audits.

    Four LLM calls total: initial generation + self-review verdict +
    self-revision + re-audit of the revised spec. The retry prompt for the
    third call must carry the self-critique's field+description so the LLM has
    something concrete to act on (otherwise the revision is a blind re-roll);
    the fourth call re-audits the revised spec and readies it.
    """
    capture = _patch_design(
        monkeypatch,
        [
            _good_payload(),
            _failing_critique_payload(),
            _good_payload(),
            _ready_critique_payload(),
        ],
        enable_self_review=True,
    )

    parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 4
    assert parsed["asset_class"] == "stocks"
    # The third call (self-revision) must include the self-critique payload.
    revision_prompt = capture.calls[2]
    assert "entry_rules" in revision_prompt
    assert "ADX" in revision_prompt or "adx" in revision_prompt
    # The internal self-revision path reuses ``_REVISION_USER_TEMPLATE`` and
    # must carry the same schema as the external run()/revise() paths.
    assert "MUST conform to this JSON Schema" in revision_prompt
    assert _DESIGN_SPEC_SCHEMA_JSON in revision_prompt


def test_run_self_review_ready_with_warning_no_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A self-review verdict of ready=true + advisory warning is accepted.

    Two LLM calls total: generation + self-review. The warning must NOT be
    demoted to ready=false (which would fire a wasted self-revision on
    content-free meta-commentary).
    """
    capture = _patch_design(
        monkeypatch,
        [_good_payload(), _ready_with_warning_critique_payload()],
        enable_self_review=True,
    )

    parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 2
    assert parsed["asset_class"] == "stocks"


def test_run_self_review_ready_with_critical_self_revises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A self-review verdict of ready=true + critical is a real contradiction.

    The critical still demotes to ready=false, so exactly one self-revision
    fires, followed by a re-audit of the revised spec (four LLM calls total).
    """
    capture = _patch_design(
        monkeypatch,
        [
            _good_payload(),
            _ready_with_critical_critique_payload(),
            _good_payload(),
            _ready_critique_payload(),
        ],
        enable_self_review=True,
    )

    parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 4
    assert parsed["asset_class"] == "stocks"


def test_revise_self_review_ready_with_warning_no_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``revise()`` likewise accepts a ready=true + warning self-review verdict
    without firing a self-revision (two LLM calls total)."""
    capture = _patch_design(
        monkeypatch,
        [_good_payload(), _ready_with_warning_critique_payload()],
        enable_self_review=True,
    )

    critique = SpecCritique(ready=False, rationale="external r", issues=[])
    parsed, _ = DesignAgent().revise(_prior_spec(), critique)

    assert len(capture.calls) == 2
    assert parsed["asset_class"] == "stocks"


def test_revise_self_review_passes_no_extra_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """``revise()`` also runs through self-review. When the revision is
    self-coherent, exactly two LLM calls fire: the revision + the
    self-review verdict. No self-revision."""
    capture = _patch_design(
        monkeypatch,
        [_good_payload(), _ready_critique_payload()],
        enable_self_review=True,
    )

    critique = SpecCritique(ready=False, rationale="external r", issues=[])
    parsed, _ = DesignAgent().revise(_prior_spec(), critique)

    assert len(capture.calls) == 2
    assert parsed["asset_class"] == "stocks"


def test_revise_self_review_flags_then_self_revises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revision that fails self-review triggers one self-revision + re-audit.

    The external-loop revision was the failure mode named in the user's
    example feedback ("defects persist after 9 prior rounds") — this test
    pins that revise() inherits the same self-review + re-audit contract as
    run() (four LLM calls total).
    """
    capture = _patch_design(
        monkeypatch,
        [
            _good_payload(),
            _failing_critique_payload(),
            _good_payload(),
            _ready_critique_payload(),
        ],
        enable_self_review=True,
    )

    critique = SpecCritique(ready=False, rationale="external r", issues=[])
    parsed, _ = DesignAgent().revise(_prior_spec(), critique)

    assert len(capture.calls) == 4
    assert parsed["asset_class"] == "stocks"


def test_self_review_disabled_via_env_single_call_on_both_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED=false`` restores the
    pre-change single-call behaviour for both run() and revise().

    ``_patch_design`` defaults to disabled, so this test mirrors that
    default explicitly — same expectation, no extra calls.
    """
    capture = _patch_design(monkeypatch, _good_payload())

    DesignAgent().run(prior_records=[])
    DesignAgent().revise(_prior_spec(), SpecCritique(ready=False, rationale="r", issues=[]))

    assert len(capture.calls) == 2  # one per public method, no self-review


def test_self_review_garbage_response_falls_back_to_original(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Self-review is best-effort: a malformed verdict must not block the
    cycle. Return the original spec unchanged and log a warning so the
    drift is observable."""
    capture = _patch_design(
        monkeypatch,
        [_good_payload(), "not a json verdict at all"],
        enable_self_review=True,
    )

    with caplog.at_level(logging.WARNING, logger="investment_team.strategy_lab.agents.design"):
        parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 2  # generation + self-review; no revision attempted
    assert parsed["asset_class"] == "stocks"
    assert any("self-review" in rec.message.lower() for rec in caplog.records)


def test_self_revision_garbage_response_falls_back_to_original(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Self-revision is best-effort: when self-review flags issues but the
    follow-up revision call returns malformed JSON (``ValueError`` from
    ``extract_json_object``, not :class:`StrategySpecParseError`), the
    designer must fall back to the original valid spec and log a warning.
    Regression guard for a bug where only ``StrategySpecParseError`` was
    caught, leaking ``ValueError`` and aborting the whole cycle."""
    capture = _patch_design(
        monkeypatch,
        [
            _good_payload(),
            _failing_critique_payload(),
            # Self-revision LLM returns no JSON object — ``extract_json_object``
            # will raise ``ValueError``.
            "the model rambled but never produced JSON",
        ],
        enable_self_review=True,
    )
    # Pin parse-retries to 0 so the malformed self-revision is attempted
    # exactly once: this test pins the *fallback*, not the retry budget
    # (malformed-JSON retries are covered separately). Without this the
    # revision call would be re-prompted up to the default retry budget
    # before falling back.
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_PARSE_RETRIES", "0")

    with caplog.at_level(logging.WARNING, logger="investment_team.strategy_lab.agents.design"):
        parsed, _ = DesignAgent().run(prior_records=[])

    # All three calls fired (generation + verdict + revision attempt),
    # but the malformed revision was discarded and the original spec is
    # returned unchanged.
    assert len(capture.calls) == 3
    assert parsed["asset_class"] == "stocks"
    assert any("self-revision failed" in rec.message.lower() for rec in caplog.records)


def test_self_revision_result_is_reaudited(monkeypatch: pytest.MonkeyPatch) -> None:
    """The self-revised spec is re-audited through self-review.

    Four LLM calls: generation + audit (flags) + self-revision + RE-AUDIT.
    The fourth call is a fresh self-review *audit* of the revised spec — it
    carries the self-review user-prompt marker, distinguishing it from a
    revision prompt. This closes the gap where a self-revision could introduce
    a new contradiction that reached the external reviewer unchecked.
    """
    capture = _patch_design(
        monkeypatch,
        [
            _good_payload(),
            _failing_critique_payload(),
            _good_payload(),
            _ready_critique_payload(),
        ],
        enable_self_review=True,
    )

    parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 4
    # The 4th call audits the revised spec (self-review user prompt), not a
    # revision (which would carry the "Revise the following ..." template).
    assert "Audit the following candidate StrategySpec" in capture.calls[3]
    assert "Revise the following strategy specification" not in capture.calls[3]
    assert parsed["asset_class"] == "stocks"


def test_self_review_depth_bound_stops_second_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the default round cap of 1, a re-audit that still flags issues does
    NOT trigger a second self-revision — the loop is bounded and defers to the
    authoritative external review loop.

    Four LLM calls: generation + audit + self-revision + re-audit (still not
    ready). No fifth call. ``_CapturingAgent`` repeats its last payload, so an
    unbounded loop would over-count — the exact ``== 4`` is the bound guard.
    """
    capture = _patch_design(
        monkeypatch,
        [
            _good_payload(),
            _failing_critique_payload(),
            _good_payload(),
            _failing_critique_payload(),
        ],
        enable_self_review=True,
    )

    parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 4
    # The once-revised spec is returned despite the re-audit still flagging.
    assert parsed["asset_class"] == "stocks"


def test_revise_threads_external_lineage_into_self_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``revise()`` threads the external critique lineage into the internal
    self-revision prompt.

    The orchestrator passes ``prior_critiques`` already including the current
    critique, so the self-revision prompt must render the full lineage — not
    ``"None yet."`` / ``"(0 so far)"`` as the pre-change code did. This is what
    stops a self-revision from regressing a fix an earlier external round
    extracted.
    """
    capture = _patch_design(
        monkeypatch,
        [
            _good_payload(),
            _failing_critique_payload(),
            _good_payload(),
            _ready_critique_payload(),
        ],
        enable_self_review=True,
    )

    c0 = SpecCritique(ready=False, rationale="ROUND0-SIZING-FIX", round=0, issues=[])
    c1 = SpecCritique(
        ready=False,
        rationale="ROUND1-EXIT-FIX",
        round=1,
        issues=[
            CritiqueIssue(
                field="sizing",
                description="position too large",
                suggested_fix="cap fixed_fraction at 0.01",
            )
        ],
    )

    DesignAgent().revise(_prior_spec(), c1, prior_critiques=[c0, c1])

    # The third call is the self-revision; it must carry the full external
    # lineage (both prior rounds), not the empty "(0 so far)" / "None yet.".
    self_revision_prompt = capture.calls[2]
    assert "(2 so far)" in self_revision_prompt
    assert "ROUND0-SIZING-FIX" in self_revision_prompt
    assert "ROUND1-EXIT-FIX" in self_revision_prompt
    # Per-issue detail (not just the truncated rationale) must reach the
    # self-revision so it can see *what* the earlier round fixed and avoid
    # silently regressing it.
    assert "position too large" in self_revision_prompt
    assert "cap fixed_fraction at 0.01" in self_revision_prompt


def test_revise_threads_regression_notice_into_self_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``revise()`` threads the external ``regression_notice`` into the internal
    self-revision prompt.

    When the external loop escalates a regression ("do not reintroduce X") and
    the revised spec then fails self-review, the self-revision must still see
    that warning — otherwise the internal fix can undo the very prior-round
    defect the regression machinery is keeping fixed.
    """
    capture = _patch_design(
        monkeypatch,
        [
            _good_payload(),
            _failing_critique_payload(),
            _good_payload(),
            _ready_critique_payload(),
        ],
        enable_self_review=True,
    )

    critique = SpecCritique(ready=False, rationale="external r", issues=[])
    DesignAgent().revise(
        _prior_spec(),
        critique,
        regression_notice="DO-NOT-REINTRODUCE: exit_rules take-profit removed",
    )

    # The self-revision (third call) must carry the regression notice, not the
    # hardcoded "None." the pre-fix code rendered.
    self_revision_prompt = capture.calls[2]
    assert "DO-NOT-REINTRODUCE: exit_rules take-profit removed" in self_revision_prompt


def test_run_self_revision_has_no_external_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contrast to the ``revise()`` lineage test: ``run()`` is initial
    generation with no external rounds, so its self-revision prompt shows no
    lineage (``"None yet."`` / ``"(0 so far)"``)."""
    capture = _patch_design(
        monkeypatch,
        [
            _good_payload(),
            _failing_critique_payload(),
            _good_payload(),
            _ready_critique_payload(),
        ],
        enable_self_review=True,
    )

    DesignAgent().run(prior_records=[])

    self_revision_prompt = capture.calls[2]
    assert "(0 so far)" in self_revision_prompt
    assert "None yet." in self_revision_prompt


def test_self_revision_rounds_zero_disables_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS=0`` makes self-review
    audit-only: a not-ready verdict yields NO self-revision (two LLM calls,
    the original spec returned unchanged)."""
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS", "0")
    capture = _patch_design(
        monkeypatch,
        [_good_payload(), _failing_critique_payload()],
        enable_self_review=True,
    )

    parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 2  # generation + audit only; no self-revision
    assert parsed["asset_class"] == "stocks"


def test_design_self_revision_rounds_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The round-cap resolver: default 1, floor 0, garbage falls back to 1."""
    from investment_team.strategy_lab.agents.design import _design_self_revision_rounds

    monkeypatch.delenv("STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS", raising=False)
    assert _design_self_revision_rounds() == 1
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS", "0")
    assert _design_self_revision_rounds() == 0
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS", "3")
    assert _design_self_revision_rounds() == 3
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS", "-5")
    assert _design_self_revision_rounds() == 0
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS", "garbage")
    assert _design_self_revision_rounds() == 1


# ---------------------------------------------------------------------------
# LLM-call budget — charging stays wired to real model invocations and the
# budget signal is NOT swallowed by the best-effort self-review guards.
# ---------------------------------------------------------------------------


def test_run_budget_charged_per_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """With self-review enabled, ``run()`` makes two real LLM calls and the
    active budget is charged exactly twice — counting stays tied to actual
    model invocations, not method calls."""
    capture = _patch_design(
        monkeypatch,
        [_good_payload(), _ready_critique_payload()],
        enable_self_review=True,
    )
    budget = LLMCallBudget(100)

    with use_budget(budget):
        DesignAgent().run(prior_records=[])

    assert budget.calls_made == len(capture.calls) == 2


def test_revise_budget_charged_across_parse_retries_and_self_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every underlying call counts: a generation + self-review verdict +
    self-revision + re-audit verdict all charge the active budget."""
    capture = _patch_design(
        monkeypatch,
        [
            _good_payload(),
            _failing_critique_payload(),
            _good_payload(),
            _ready_critique_payload(),
        ],
        enable_self_review=True,
    )
    budget = LLMCallBudget(100)

    critique = SpecCritique(ready=False, rationale="external r", issues=[])
    with use_budget(budget):
        DesignAgent().revise(_prior_spec(), critique)

    assert budget.calls_made == len(capture.calls) == 4


def test_budget_exhaustion_propagates_through_self_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget trip during the self-review call must raise
    ``DesignBudgetExhausted`` — the best-effort ``except Exception`` guard
    in ``_with_self_review`` must not swallow it."""
    _patch_design(
        monkeypatch,
        [_good_payload(), _ready_critique_payload()],
        enable_self_review=True,
    )
    # limit 1: generation charges once (ok), the self-review charge trips.
    with use_budget(LLMCallBudget(1)):
        with pytest.raises(DesignBudgetExhausted):
            DesignAgent().run(prior_records=[])


def test_budget_exhaustion_propagates_through_self_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget trip during the self-revision call (not just the audit) must
    raise ``DesignBudgetExhausted`` — the best-effort ``except Exception`` guard
    around the self-revision must not swallow it."""
    _patch_design(
        monkeypatch,
        [_good_payload(), _failing_critique_payload(), _good_payload()],
        enable_self_review=True,
    )
    # limit 2: generation (ok) + self-review audit (ok); the self-revision
    # charge is the third and trips.
    with use_budget(LLMCallBudget(2)):
        with pytest.raises(DesignBudgetExhausted):
            DesignAgent().run(prior_records=[])


def test_budget_not_charged_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No active budget (no enclosing ``use_budget``) leaves the agents
    unchanged — ``charge_active_budget`` is a no-op, backward-compatible
    with every caller outside a design cycle."""
    capture = _patch_design(
        monkeypatch,
        [_good_payload(), _ready_critique_payload()],
        enable_self_review=True,
    )

    # No budget bound; must behave exactly as before.
    parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 2
    assert parsed["asset_class"] == "stocks"


def test_json_correction_prompt_carries_full_error() -> None:
    """The malformed-JSON re-prompt embeds the full exception text — the
    error string is no longer cut off at 400 chars."""
    long_error = "boom " * 200  # ~1000 chars, well past the old 400 cap
    prompt = build_json_correction_prompt("ORIGINAL", ValueError(long_error))
    assert long_error in prompt
    assert "…" not in prompt


def test_correction_prompt_carries_full_payload_and_cause() -> None:
    """The DSL-validation re-prompt embeds the full payload and pydantic
    error — neither is truncated (old caps were 400 / 1200 chars)."""
    long_payload = "P" * 5000
    long_cause = "C" * 5000
    exc = StrategySpecParseError("sizing", long_payload, ValueError(long_cause))
    prompt = _build_correction_prompt("ORIGINAL", exc)
    assert long_payload in prompt
    assert long_cause in prompt
    assert "…" not in prompt


# ---------------------------------------------------------------------------
# Self-review: objective / expectancy sanity check
# ---------------------------------------------------------------------------


def _expectancy_incoherent_critique_payload() -> str:
    """Self-review verdict flagging an expectancy/objective incoherence.

    Models the tight-take-profit / wide-stop trap: a high claimed win rate
    that the reward:risk geometry cannot support (negative expectancy). The
    new third self-review check must surface this as ``critical`` on the
    ``expectancy_forecast`` field before the external loop ever runs.
    """
    return json.dumps(
        {
            "ready": False,
            "rationale": (
                "forecast_win_rate 0.60 is below the ~0.83 break-even the 1%/5% "
                "take-profit:stop geometry demands — negative expectancy."
            ),
            "issues": [
                {
                    "field": "expectancy_forecast",
                    "severity": "critical",
                    "description": (
                        "reward_risk 0.2 needs >83% wins to break even; forecast is 60%."
                    ),
                    "suggested_fix": "Widen the take-profit or tighten the stop so reward:risk supports the win rate.",
                }
            ],
        }
    )


def test_self_review_system_prompt_includes_expectancy_check() -> None:
    """The self-review system prompt must carry the objective/expectancy audit
    and allow flagging the ``expectancy_forecast`` field."""
    text = _get_self_review_system_prompt()
    assert "Expectancy / objective sanity" in text
    assert "break-even" in text
    assert "expectancy_forecast" in text


def test_run_self_review_flags_expectancy_incoherence_then_self_revises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incoherent expectancy forecast is a critical self-review finding, so
    exactly one self-revision fires and the revised spec is re-audited (four
    LLM calls). The revision prompt must carry the expectancy critique so the
    model has something concrete to fix."""
    capture = _patch_design(
        monkeypatch,
        [
            _good_payload(),
            _expectancy_incoherent_critique_payload(),
            _good_payload(),
            _ready_critique_payload(),
        ],
        enable_self_review=True,
    )

    parsed, _ = DesignAgent().run(prior_records=[])

    assert len(capture.calls) == 4
    assert parsed["asset_class"] == "stocks"
    revision_prompt = capture.calls[2]
    assert "expectancy_forecast" in revision_prompt
    assert "break even" in revision_prompt or "break-even" in revision_prompt


# ---------------------------------------------------------------------------
# Diversity-steering mode resolution + wiring
# ---------------------------------------------------------------------------


def test_diversity_mode_defaults_to_exploit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRATEGY_LAB_DIVERSITY_MODE", raising=False)
    assert _resolve_diversity_mode() == "exploit"


def test_diversity_mode_parses_explore_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRATEGY_LAB_DIVERSITY_MODE", "  EXPLORE ")
    assert _resolve_diversity_mode() == "explore"


def test_diversity_mode_unknown_falls_back_to_exploit_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A set-but-unrecognized value resolves to exploit AND logs a warning so a
    misconfiguration is visible rather than silently masked."""
    monkeypatch.setenv("STRATEGY_LAB_DIVERSITY_MODE", "rotate-everything")
    with caplog.at_level(logging.WARNING, logger="investment_team.strategy_lab.agents.design"):
        assert _resolve_diversity_mode() == "exploit"
    assert any("rotate-everything" in rec.message for rec in caplog.records)


def test_diversity_mode_empty_value_is_silent_exploit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty value is treated as unset — exploit, with no warning noise."""
    monkeypatch.setenv("STRATEGY_LAB_DIVERSITY_MODE", "   ")
    with caplog.at_level(logging.WARNING, logger="investment_team.strategy_lab.agents.design"):
        assert _resolve_diversity_mode() == "exploit"
    assert not caplog.records


def test_run_threads_resolved_mode_into_mix_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run`` must pass the env-resolved diversity mode into
    ``asset_class_mix_hint``. Spy on the hint and stub the other prior-results
    formatters so the test needs no real lab records — only a non-empty list
    (the hint is skipped entirely when there are no priors)."""
    _patch_design(monkeypatch, _good_payload())
    seen: Dict[str, Any] = {}

    def _spy_hint(records: Any, **kwargs: Any) -> str:
        seen["mode"] = kwargs.get("mode")
        return "MIX-HINT"

    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design.asset_class_mix_hint", _spy_hint
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design.format_prior_results", lambda _r: "PR"
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design.format_prior_attribution",
        lambda _r, **_kwargs: "PA",
    )
    monkeypatch.setenv("STRATEGY_LAB_DIVERSITY_MODE", "explore")

    DesignAgent().run(prior_records=[object()])

    assert seen["mode"] == "explore"


def test_run_shares_one_prior_results_cache_across_both_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run`` independently calls ``format_prior_attribution`` and
    ``asset_class_mix_hint`` over the same ``prior_records`` list; both
    internally windowed-aggregate via ``strategy_lab_context._executed_records``.
    A single cache dict must be created per ``run()`` call and passed to both,
    so they share its memoized sort/filter pass instead of each recomputing
    it."""
    _patch_design(monkeypatch, _good_payload())
    seen: Dict[str, Any] = {}

    def _spy_attribution(records: Any, **kwargs: Any) -> str:
        seen["attribution_cache"] = kwargs.get("cache")
        return "PA"

    def _spy_hint(records: Any, **kwargs: Any) -> str:
        seen["hint_cache"] = kwargs.get("cache")
        return "MIX-HINT"

    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design.format_prior_attribution", _spy_attribution
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design.asset_class_mix_hint", _spy_hint
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design.format_prior_results", lambda _r: "PR"
    )

    DesignAgent().run(prior_records=[object()])

    assert seen["attribution_cache"] is not None
    assert isinstance(seen["attribution_cache"], dict)
    assert seen["attribution_cache"] is seen["hint_cache"]


# ---------------------------------------------------------------------------
# run() — market-regime section injection
# ---------------------------------------------------------------------------


def _regime_summary() -> RegimeSummary:
    return RegimeSummary(
        computed_at="2026-01-01T00:00:00+00:00",
        entries=[
            RegimeEntry(
                asset_class="stocks",
                benchmark_symbol="SPY",
                trend_direction="up",
                trend_strength="strong",
                volatility_regime="low",
                close=500.0,
                sma50=490.0,
                sma200=470.0,
                adx=32.0,
                atr_pct=0.008,
                atr_pct_percentile=0.15,
            )
        ],
    )


def test_run_injects_regime_section_when_summary_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty regime summary is rendered into a ``## Market Regime``
    section of the design prompt so the designer can condition on it."""
    capture = _patch_design(monkeypatch, _good_payload())

    DesignAgent().run(prior_records=[], regime_summary=_regime_summary())

    prompt = capture.calls[0]
    assert "## Market Regime" in prompt
    assert "stocks (SPY)" in prompt
    assert "trend=up (strong)" in prompt
    assert "volatility=low" in prompt


def test_run_omits_regime_section_when_summary_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward compatible: no summary → no regime section in the prompt."""
    capture = _patch_design(monkeypatch, _good_payload())

    DesignAgent().run(prior_records=[])

    assert "## Market Regime" not in capture.calls[0]


def test_run_omits_regime_section_for_degraded_empty_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded summary with no classified entries must not inject a
    placeholder as if it were signal."""
    capture = _patch_design(monkeypatch, _good_payload())
    empty = RegimeSummary(computed_at="2026-01-01T00:00:00+00:00", degraded=True, entries=[])

    DesignAgent().run(prior_records=[], regime_summary=empty)

    assert "## Market Regime" not in capture.calls[0]
