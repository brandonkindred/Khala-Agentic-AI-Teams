"""Tests for Signal Intelligence Expert, market snapshot, and Strategy Lab wiring."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agents.investment_team.market_lab_data.models import MarketLabContext, StrategyLabDataRequest
from agents.investment_team.signal_intelligence_agent import (
    SignalIntelligenceExpert,
    brief_to_prompt_block,
    sanitize_brief_for_injection,
)
from agents.investment_team.signal_intelligence_models import SignalIntelligenceBriefV1
from agents.investment_team.strategy_ideation_agent import StrategyIdeationAgent
from agents.investment_team.strategy_lab_context import format_prior_results

from llm_service.interface import LLMClient


class _FakeLLM(LLMClient):
    """Returns valid JSON for signal brief vs ideation prompts."""

    def __init__(self) -> None:
        self.prompts: List[str] = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.prompts.append(prompt)
        if "brief_version" in prompt and "high_value_signal_hypotheses" in prompt:
            return {
                "brief_version": 1,
                "macro_themes": ["rates", "liquidity"],
                "micro_themes": ["breadth"],
                "high_value_signal_hypotheses": ["mean reversion when vol spikes"],
                "trade_structures_benefiting": ["pairs"],
                "pairing_guidance": "combine macro gate with vol filter",
                "evidence_from_priors": "none / first run",
                "evidence_from_market_data": "FX snapshot",
                "confidence": "medium",
                "unsupported_claims": [],
            }
        return {
            "asset_class": "forex",
            "hypothesis": "Test hypothesis",
            "signal_definition": "ensemble",
            "signal_sources": ["price_action", "macro_rates"],
            "entry_rules": ["rule1"],
            "exit_rules": ["rule2"],
            "sizing_rules": ["size1"],
            "risk_limits": {"max_position_pct": 5, "stop_loss_pct": 3},
            "speculative": False,
            "rationale": "test rationale",
        }


def test_signal_intelligence_brief_v1_roundtrip() -> None:
    raw = {
        "brief_version": 1,
        "macro_themes": ["a"],
        "micro_themes": ["b"],
        "high_value_signal_hypotheses": ["h"],
        "trade_structures_benefiting": ["t"],
        "pairing_guidance": "p",
        "evidence_from_priors": "none",
        "evidence_from_market_data": "none",
        "confidence": "low",
        "unsupported_claims": [],
    }
    m = SignalIntelligenceBriefV1.model_validate(raw)
    dumped = m.model_dump(mode="json")
    assert dumped["brief_version"] == 1


def test_sanitize_brief_strips_nul() -> None:
    s = "hello\x00world"
    assert "\x00" not in sanitize_brief_for_injection(s)


def test_format_prior_results_empty() -> None:
    assert "first strategy" in format_prior_results([]).lower()


def test_expert_produces_brief() -> None:
    llm = _FakeLLM()
    expert = SignalIntelligenceExpert(llm)
    ctx = MarketLabContext(
        fetched_at="2020-01-01T00:00:00+00:00",
        degraded=False,
        sources_used=["test"],
        fx_rates={"EUR": 0.92},
    )
    brief = expert.produce_signal_brief([], ctx)
    assert brief.brief_version == 1
    assert len(llm.prompts) == 1
    assert "EUR" in llm.prompts[0] or "0.92" in llm.prompts[0]


def test_ideation_injects_signal_block() -> None:
    llm = _FakeLLM()
    agent = StrategyIdeationAgent(llm_client=llm)
    brief = SignalIntelligenceBriefV1(
        brief_version=1,
        macro_themes=["m"],
        micro_themes=["u"],
        high_value_signal_hypotheses=["h"],
        trade_structures_benefiting=["t"],
        pairing_guidance="p",
        evidence_from_priors="none",
        evidence_from_market_data="none",
        confidence="high",
    )
    _, _rationale = agent.ideate_strategy([], precomputed_signal_brief=brief)
    assert len(llm.prompts) == 1
    assert "<signal_intelligence_brief>" in llm.prompts[0]
    assert brief_to_prompt_block(brief) in llm.prompts[0]


def test_market_lab_context_prompt_text() -> None:
    ctx = MarketLabContext(
        fetched_at="2020-01-01T00:00:00+00:00",
        degraded=True,
        degraded_reason="timeout",
        sources_used=["frankfurter"],
        fx_rates={"EUR": 0.9},
    )
    t = ctx.as_prompt_text()
    assert "degraded" in t.lower()
    assert "EUR" in t


def test_strategy_lab_data_request_defaults() -> None:
    r = StrategyLabDataRequest()
    assert r.benchmark_symbol == "SPY"
