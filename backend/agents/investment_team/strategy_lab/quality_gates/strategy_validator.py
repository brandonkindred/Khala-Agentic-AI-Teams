"""Deterministic validation of StrategySpec fields."""

from __future__ import annotations

import re
from typing import List

from ...models import StrategySpec
from .models import QualityGateResult

GATE = "strategy_spec_validator"

# Keywords that suggest non-computable data sources (no numerical proxy available
# in a pure OHLCV + technical-indicator environment).
_NON_COMPUTABLE_KEYWORDS = re.compile(
    r"\b(sentiment|social media|twitter|reddit|news feed|earnings call|insider)\b",
    re.IGNORECASE,
)

# Keywords that belong to specific asset classes and are misplaced in others.
_ASSET_MISMATCH: dict[str, re.Pattern[str]] = {
    "forex": re.compile(r"\b(earnings|dividend|P/E|EPS|market cap)\b", re.IGNORECASE),
    "crypto": re.compile(r"\b(earnings|dividend|P/E|EPS)\b", re.IGNORECASE),
    "commodities": re.compile(r"\b(earnings|dividend|P/E|EPS|market cap)\b", re.IGNORECASE),
}


class StrategySpecValidator:
    """Run deterministic checks on a StrategySpec before code execution."""

    def validate(self, spec: StrategySpec) -> List[QualityGateResult]:
        results: List[QualityGateResult] = []

        # 1. Entry rules present
        if not spec.entry_rules:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details="No entry rules defined — strategy cannot generate trades.",
                )
            )

        # 2. Exit rules present
        if not spec.exit_rules:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details="No exit rules defined — positions would never close.",
                )
            )

        # 3. Hypothesis present
        if not spec.hypothesis or not spec.hypothesis.strip():
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="warning",
                    details="Hypothesis is empty — strategy rationale is unclear.",
                )
            )

        # 4. Strategy code present
        if not spec.strategy_code or not spec.strategy_code.strip():
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details="strategy_code is missing — nothing to execute.",
                )
            )

        # 5. Risk limits bounds (Phase 3: reads validated RiskLimits attributes).
        risk = spec.risk_limits
        max_pos = risk.max_position_pct
        if max_pos < 1 or max_pos > 25:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details=f"max_position_pct={max_pos}% is outside safe range [1%, 25%].",
                )
            )

        if risk.max_drawdown_pct < 5 or risk.max_drawdown_pct > 50:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="warning",
                    details=(
                        f"max_drawdown_pct={risk.max_drawdown_pct}% is outside typical "
                        "range [5%, 50%]."
                    ),
                )
            )

        # 6. Asset-class keyword mismatch
        all_rules_text = " ".join(spec.entry_rules + spec.exit_rules + spec.sizing_rules)
        ac = spec.asset_class.lower()
        pattern = _ASSET_MISMATCH.get(ac)
        if pattern and pattern.search(all_rules_text):
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="warning",
                    details=f"Rules reference concepts mismatched with asset class '{spec.asset_class}'.",
                )
            )

        # 7. Non-computable data references
        if _NON_COMPUTABLE_KEYWORDS.search(all_rules_text):
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="warning",
                    details="Rules reference non-computable data (sentiment, social media, etc.) without a numerical proxy.",
                )
            )

        # All passed if we got here with no additions
        if not results:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=True,
                    severity="info",
                    details="Strategy spec passed all validation checks.",
                )
            )

        return results
