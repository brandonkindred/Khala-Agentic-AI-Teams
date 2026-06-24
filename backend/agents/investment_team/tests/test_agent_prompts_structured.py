"""Structured-DSL contract for Strategy Lab LLM agents.

After the structured-DSL migration, the LLM-driven agents that author
specs must:

1. Emit JSON with structured rule objects (every rule carries a ``kind``
   discriminator).
2. Reject prose payloads with :class:`StrategySpecParseError` so the
   orchestrator does not propagate a malformed dict into ``StrategySpec``.

This suite locks in that contract for the three agents that touch rule
shapes: :class:`DesignAgent` (authors specs from scratch — the split-
design replacement for the legacy single-call ideation agent),
:class:`RefinementAgent` (code-only — must drop stray rule keys), and
:class:`ZeroTradeRepairAgent` (may emit ``proposed_spec_updates``
containing rule-shaped values).
"""

from __future__ import annotations

import json
import logging

import pytest

from investment_team.models import (
    BacktestExecutionDiagnostics,
    StrategySpec,
)
from investment_team.strategy_lab.agents._parse_helpers import StrategySpecParseError
from investment_team.strategy_lab.agents.design import DesignAgent
from investment_team.strategy_lab.agents.refinement import RefinementAgent
from investment_team.strategy_lab.agents.zero_trade_repair import ZeroTradeRepairAgent
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    EntryRuleAdapter,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeStrandsAgentReturning:
    """Callable stub that mimics the ``strands.Agent`` instance each agent
    builds inline. Returns the same string payload for every call."""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    def __call__(self, prompt: str) -> str:
        return self._payload


def _structured_entry_rule_dict() -> dict:
    return {
        "kind": "entry",
        "side": "long",
        "when": {
            "lhs": {"name": "rsi", "params": {"period": 14}},
            "op": "<",
            "rhs": 30,
        },
    }


def _structured_signal_exit_rule_dict() -> dict:
    return {
        "kind": "signal_exit",
        "when": {
            "lhs": {"name": "rsi", "params": {"period": 14}},
            "op": ">",
            "rhs": 70,
        },
    }


def _structured_sizing_dict() -> dict:
    return {"kind": "fixed_fraction", "fraction": 0.02}


def _spec() -> StrategySpec:
    """Minimal structured-DSL spec usable as input to refinement / repair."""
    return StrategySpec(
        strategy_id="strat-prompts-test",
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
        speculative=False,
        strategy_code="# original",
    )


def _patch_design(monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design.Agent",
        lambda **kwargs: _FakeStrandsAgentReturning(payload),
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.design.get_strands_model",
        lambda *_a, **_k: object(),
    )


def _patch_refinement(monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.refinement.Agent",
        lambda **kwargs: _FakeStrandsAgentReturning(payload),
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.refinement.get_strands_model",
        lambda *_a, **_k: object(),
    )


def _patch_zero_trade_repair(monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.zero_trade_repair.Agent",
        lambda **kwargs: _FakeStrandsAgentReturning(payload),
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.zero_trade_repair.get_strands_model",
        lambda *_a, **_k: object(),
    )


def _zero_trade_diagnostics() -> BacktestExecutionDiagnostics:
    return BacktestExecutionDiagnostics(
        orders_emitted=0,
        orders_accepted=0,
        orders_rejected=0,
        orders_unfilled=0,
        warmup_orders_dropped=0,
        entries_filled=0,
        exits_emitted=0,
        closed_trades=0,
        open_positions_at_end=[],
        orders_rejection_reasons={},
        last_order_events=[],
        summary="never emitted",
        zero_trade_category="NO_ORDERS_EMITTED",
    )


# ---------------------------------------------------------------------------
# DesignAgent — must accept structured, reject prose / invalid structured
# ---------------------------------------------------------------------------


def _design_payload(
    *,
    entry_rules,
    exit_rules,
    sizing=None,
    extra=None,
    include_strategy_code: bool = False,
) -> str:
    """Build an LLM-style design JSON payload as a string.

    The design agent is contractually spec-only; ``include_strategy_code``
    is the legacy "LLM emitted code anyway" path the agent strips with
    a warning, kept for the defensive-strip test below.
    """
    payload: dict = {
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "target_symbols": [],
        "risk_limits": {"max_position_pct": 5},
        "speculative": False,
        "rationale": "r",
    }
    if include_strategy_code:
        payload["strategy_code"] = "# legacy LLM emitted code"
    if sizing is not None:
        payload["sizing"] = sizing
    if extra:
        payload.update(extra)
    return json.dumps(payload)


def test_design_accepts_structured_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The structured-DSL shape round-trips through ``DesignAgent.run``."""
    payload = _design_payload(
        entry_rules=[_structured_entry_rule_dict()],
        exit_rules=[_structured_signal_exit_rule_dict(), {"kind": "stop_loss", "pct": 0.03}],
        sizing=_structured_sizing_dict(),
    )
    _patch_design(monkeypatch, payload)

    parsed, rationale = DesignAgent().run(prior_records=[])

    assert rationale == "r"
    # entry_rules round-trips through the DSL adapter.
    rule_model = EntryRuleAdapter.validate_python(parsed["entry_rules"][0])
    assert rule_model.kind == "entry"
    assert rule_model.side == "long"
    assert parsed["sizing"] == _structured_sizing_dict()
    # The design agent NEVER returns strategy_code; even if the LLM
    # emits it, the agent strips it (see strip test below).
    assert "strategy_code" not in parsed


def test_design_round_trips_expectancy_forecast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A structured ``expectancy_forecast`` survives the parse path alongside
    ``rationale`` (it is neither a rule slot nor a popped field)."""
    forecast = {
        "forecast_win_rate": 0.58,
        "reward_risk": 1.8,
        "trades_per_year": 24,
        "projected_annual_return_pct": 11.0,
        "consistency_note": "coherent",
    }
    payload = _design_payload(
        entry_rules=[_structured_entry_rule_dict()],
        exit_rules=[_structured_signal_exit_rule_dict()],
        sizing=_structured_sizing_dict(),
        extra={"expectancy_forecast": forecast},
    )
    _patch_design(monkeypatch, payload)

    parsed, rationale = DesignAgent().run(prior_records=[])

    assert rationale == "r"
    assert parsed["expectancy_forecast"] == forecast


def test_design_strips_stray_strategy_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the LLM emits ``strategy_code`` despite the spec-only contract,
    the agent drops it with a warning so downstream gates never see it."""
    payload = _design_payload(
        entry_rules=[_structured_entry_rule_dict()],
        exit_rules=[_structured_signal_exit_rule_dict()],
        sizing=_structured_sizing_dict(),
        include_strategy_code=True,
    )
    _patch_design(monkeypatch, payload)

    parsed, _rationale = DesignAgent().run(prior_records=[])

    assert "strategy_code" not in parsed


def test_design_rejects_prose_entry_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prose strings in ``entry_rules`` raise ``StrategySpecParseError``."""
    payload = _design_payload(
        entry_rules=["close > sma(20)"],
        exit_rules=[_structured_signal_exit_rule_dict()],
        sizing=_structured_sizing_dict(),
    )
    _patch_design(monkeypatch, payload)

    with pytest.raises(StrategySpecParseError) as exc_info:
        DesignAgent().run(prior_records=[])

    assert exc_info.value.field.startswith("entry_rules")
    assert "close > sma(20)" in str(exc_info.value)


def test_design_rejects_legacy_sizing_rules_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-DSL ``sizing_rules: list[str]`` shape is not silently passed
    through. With no ``sizing`` key the parser is forgiving (the orchestrator
    falls back to the DSL default), but prose rules still raise."""
    payload = _design_payload(
        entry_rules=[_structured_entry_rule_dict()],
        exit_rules=["close when ready"],
        sizing=_structured_sizing_dict(),
    )
    _patch_design(monkeypatch, payload)

    with pytest.raises(StrategySpecParseError) as exc_info:
        DesignAgent().run(prior_records=[])

    assert exc_info.value.field.startswith("exit_rules")


def test_design_rejects_invalid_structured_indicator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structured-but-invalid rules (unknown indicator ``kind``) raise with the
    pydantic error chained as ``__cause__``."""
    bad_entry = {
        "kind": "entry",
        "side": "long",
        "when": {
            "lhs": {"kind": "ema", "period": 1},  # period < 2 trips the bound
            "op": "lt",
            "rhs": {"kind": "const", "value": 30},
        },
    }
    payload = _design_payload(
        entry_rules=[bad_entry],
        exit_rules=[_structured_signal_exit_rule_dict()],
        sizing=_structured_sizing_dict(),
    )
    _patch_design(monkeypatch, payload)

    with pytest.raises(StrategySpecParseError) as exc_info:
        DesignAgent().run(prior_records=[])

    assert exc_info.value.field.startswith("entry_rules")
    assert exc_info.value.__cause__ is not None  # pydantic ValidationError chained


def test_design_rejects_prose_sizing_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sizing`` as a prose string is rejected — only structured kinds parse."""
    payload = _design_payload(
        entry_rules=[_structured_entry_rule_dict()],
        exit_rules=[_structured_signal_exit_rule_dict()],
        sizing="risk 2% per trade",
    )
    _patch_design(monkeypatch, payload)

    with pytest.raises(StrategySpecParseError) as exc_info:
        DesignAgent().run(prior_records=[])

    assert exc_info.value.field == "sizing"


# ---------------------------------------------------------------------------
# RefinementAgent — stray rule keys must be discarded with a warning
# ---------------------------------------------------------------------------


def test_refinement_discards_stray_rule_keys(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A refinement LLM payload with stray rule keys is narrowed to
    ``{"strategy_code", "changes_made"}`` and the discarded keys land in
    ``spec_mutation_history`` (#543 contract, re-confirmed under the DSL)."""
    payload = json.dumps(
        {
            "strategy_code": "# refined",
            "changes_made": "tightened RSI guard",
            "entry_rules": [_structured_entry_rule_dict()],
            "exit_rules": [_structured_signal_exit_rule_dict()],
            "sizing": _structured_sizing_dict(),
        }
    )
    _patch_refinement(monkeypatch, payload)

    agent = RefinementAgent()
    with caplog.at_level(logging.WARNING, logger="investment_team.strategy_lab.agents.refinement"):
        updates, code = agent.run(
            spec=_spec(),
            code="# original",
            failure_phase="execution",
            failure_details="boom",
        )

    assert code == "# refined"
    assert set(updates) == {"changes_made"}
    assert agent.spec_mutation_history == [
        {
            "failure_phase": "execution",
            "keys": ["entry_rules", "exit_rules", "sizing"],
        }
    ]
    assert any("spec-mutating keys" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# ZeroTradeRepairAgent — proposed_spec_updates must be structured
# ---------------------------------------------------------------------------


def _zero_trade_payload(*, proposed_spec_updates=None) -> str:
    """Build a zero-trade repair LLM payload as a JSON string."""
    payload: dict = {
        "root_cause_category": "NO_ORDERS_EMITTED",
        "evidence": "entries never fired",
        "code_issue": "RSI guard never true on history",
        "strategy_rule_issue": None,
        "proposed_code": "# repaired code",
        "expected_order_count_change": 5,
        "expected_trade_count_change": 3,
        "changes_made": "loosened RSI threshold",
        "proposed_spec_updates": proposed_spec_updates,
    }
    return json.dumps(payload)


def test_zero_trade_repair_accepts_risk_limits_spec_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-#530 only ``risk_limits`` survives the repair agent's whitelist;
    rule-shaped keys (which would mutate the strategy thesis) are silently
    dropped before they reach the orchestrator."""
    spec_updates = {
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        # Rule-shaped keys MUST be dropped — they represent the thesis
        # and zero-trade repair may not weaken them.
        "entry_rules": [_structured_entry_rule_dict()],
        "exit_rules": [_structured_signal_exit_rule_dict()],
    }
    _patch_zero_trade_repair(monkeypatch, _zero_trade_payload(proposed_spec_updates=spec_updates))

    report = ZeroTradeRepairAgent().run(
        spec=_spec(),
        code="# original",
        diagnostics=_zero_trade_diagnostics(),
    )

    assert report.proposed_spec_updates == {
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10}
    }
    assert report.proposed_code == "# repaired code"
    assert report.dropped_spec_update_keys == sorted(["entry_rules", "exit_rules"])


def test_zero_trade_repair_drops_off_list_rule_spec_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule-shaped ``proposed_spec_updates`` keys are off-list post-#530 and
    are silently filtered at the agent before they reach the orchestrator.
    With no whitelisted key remaining, the field collapses to ``None`` but
    the dropped keys are recorded so the orchestrator can still surface a
    warning + quality gate on the production agent-to-orchestrator flow."""
    spec_updates = {
        "entry_rules": ["close > sma(20)"],
        "exit_rules": ["close when ready"],
    }
    _patch_zero_trade_repair(monkeypatch, _zero_trade_payload(proposed_spec_updates=spec_updates))

    report = ZeroTradeRepairAgent().run(
        spec=_spec(),
        code="# original",
        diagnostics=_zero_trade_diagnostics(),
    )

    assert report.proposed_spec_updates is None
    assert report.proposed_code == "# repaired code"
    assert report.dropped_spec_update_keys == sorted(["entry_rules", "exit_rules"])


def test_zero_trade_repair_drops_thesis_spec_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thesis-defining keys (``hypothesis``, ``signal_definition``, ``sizing``)
    are off-list post-#530 and dropped at the agent. The committed report
    surfaces only the whitelisted ``risk_limits`` mutation, and records the
    dropped keys on ``dropped_spec_update_keys`` so the orchestrator can
    persist a warning + quality gate."""
    spec_updates = {
        "hypothesis": "rewritten thesis",
        "signal_definition": "loosened",
        "sizing": {"kind": "fixed_fraction", "fraction": 0.5},
        "risk_limits": {"max_position_pct": 5},
    }
    _patch_zero_trade_repair(monkeypatch, _zero_trade_payload(proposed_spec_updates=spec_updates))

    report = ZeroTradeRepairAgent().run(
        spec=_spec(),
        code="# original",
        diagnostics=_zero_trade_diagnostics(),
    )

    assert report.proposed_spec_updates == {"risk_limits": {"max_position_pct": 5}}
    assert report.dropped_spec_update_keys == sorted(["hypothesis", "signal_definition", "sizing"])
