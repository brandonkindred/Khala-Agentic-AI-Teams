"""Agent catalog metadata for the investment organization."""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, Field


class AgentDefinition(BaseModel):
    name: str
    role: str
    inputs: List[str] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)
    hard_rules: List[str] = Field(default_factory=list)


CORE_AGENTS: List[AgentDefinition] = [
    AgentDefinition(
        name="IPS Generator Agent",
        role="Convert InvestmentProfile into a normalized IPS constitution.",
        inputs=["InvestmentProfile"],
        outputs=["IPSV1"],
        hard_rules=[
            "Resolve profile goal/constraint conflicts with explicit tradeoffs.",
            "Default execution permissions to advisory or paper unless explicitly enabled.",
        ],
    ),
    AgentDefinition(
        name="Asset Universe Builder Agent",
        role="Create tradable universe filtered by IPS and liquidity constraints.",
        inputs=["IPSV1", "market catalog"],
        outputs=["AssetUniverseV1"],
        hard_rules=[
            "Exclude assets that violate min ADV thresholds.",
            "Mark missing-data classes as research_only.",
        ],
    ),
    AgentDefinition(
        name="Portfolio Architect Agent",
        role="Construct core/tactical/speculative sleeves aligned to IPS.",
        inputs=["IPSV1", "AssetUniverseV1", "current holdings"],
        outputs=["PortfolioProposal"],
        hard_rules=[
            "Respect sleeve min/max and asset/position caps.",
            "Provide correlation and factor exposure summaries.",
        ],
    ),
    AgentDefinition(
        name="Global Risk Manager Agent",
        role="Portfolio-level veto gate with authority to reject promotion.",
        inputs=["IPSV1", "portfolio/strategy allocations", "current exposures"],
        outputs=["PromotionDecisionV1"],
        hard_rules=[
            "Enforce drawdown, VaR, concentration, and correlated exposure limits.",
            "Require kill-switch configuration before paper/live progression.",
        ],
    ),
    AgentDefinition(
        name="Explainability and Audit Agent",
        role="Annotate reports and enforce traceability for all material numbers.",
        inputs=["ValidationReportV1", "InvestmentCommitteeMemoV1", "PromotionDecisionV1"],
        outputs=["annotated report"],
        hard_rules=["No orphan numbers: every key figure must cite source refs."],
    ),
]


SPECIALIST_DESKS: Dict[str, List[str]] = {
    "equities": [
        "Equity Research Agent",
        "Equity Strategy Designer Agent",
        "Equity Backtest Engineer Agent",
        "Equity Walk-Forward Validator Agent",
        "Equity Execution Planner Agent",
    ],
    "bonds_treasuries": [
        "Rates Strategist Agent",
        "Credit Analyst Agent",
        "Scenario Stress Tester Agent",
        "Bond Portfolio Builder Agent",
        "Bond Execution Planner Agent",
    ],
    "options": [
        "Options Strategy Designer Agent",
        "Greeks Risk Analyst Agent",
        "Volatility Regime Classifier Agent",
        "Options Backtest and Simulation Agent",
        "Options Execution Planner Agent",
    ],
    "crypto": [
        "Crypto Research Agent",
        "Crypto Strategy Designer Agent",
        "Crypto Backtest Engineer Agent",
        "Exchange and Custody Risk Monitor Agent",
        "Crypto Execution Planner Agent",
    ],
    "fx": [
        "Macro FX Strategist Agent",
        "FX Carry and Momentum Agent",
        "FX Cost and Rollover Agent",
        "FX Risk Agent",
    ],
    "real_estate": [
        "Deal Harvester Agent",
        "Fit Screener Agent",
        "Underwriter Agent",
        "Diligence Request Builder Agent",
        "Financial Diligence Agent",
        "Operational and Commercial Diligence Agents",
        "Legal and Compliance Flagging Agent",
        "IC Memo Writer Agent",
    ],
}
