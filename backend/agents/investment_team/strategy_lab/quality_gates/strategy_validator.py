"""Deterministic validation of StrategySpec fields."""

from __future__ import annotations

import re
from typing import List

from ...models import StrategySpec
from ...strategy_lab_context import normalize_asset_class
from ..spec_dsl import format_rules_for_prompt, format_sizing_rule
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

# Recognised indicator/concept vocabulary for the hypothesis-vs-rules
# consistency gate (#547 item 6). Word-boundary anchored so substrings like
# "thematic" don't accidentally match "ema".
_CONCEPT_TERMS = re.compile(
    r"\b(rsi|macd|moving\s+average|ema|sma|bollinger|atr|breakout|"
    r"mean\s+reversion|momentum|volatility|volume|vwap|stochastic|adx|obv)\b",
    re.IGNORECASE,
)


class StrategySpecValidator:
    """Run deterministic checks on a StrategySpec before code execution."""

    def validate(self, spec: StrategySpec) -> List[QualityGateResult]:
        results: List[QualityGateResult] = []

        # 0. Asset class supported (#535). 'options' has no chain data,
        #    Greeks, or contract execution model — reject before
        #    market-data fetch silently treats it as equities.
        if normalize_asset_class(spec.asset_class) == "options":
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details=(
                        "Asset class 'options' is not yet supported — no "
                        "option-chain data, Greeks, or contract execution "
                        "model is available. Choose stocks, crypto, forex, "
                        "futures, or commodities."
                    ),
                )
            )

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

        # 6. Asset-class keyword mismatch. Issue #551/#537: spec rule fields
        #    are structured DSL nodes — render them through the spec_dsl
        #    formatters to recover a human-readable text view suitable for
        #    regex matching. Unparseable prose now lives in
        #    ``spec.unparsed_rules`` (#537 replaced the discriminator
        #    variants) and is folded into the same scan so prose that
        #    escaped the adapter is still caught.
        all_rules_text = " ".join(
            [
                format_rules_for_prompt(spec.entry_rules),
                format_rules_for_prompt(spec.exit_rules),
                format_sizing_rule(spec.sizing),
                " ".join(spec.unparsed_rules),
            ]
        )
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

        # 8. Hypothesis-vs-rules consistency (#547 item 6). If the hypothesis
        #    names indicator concepts that no entry/exit rule references (or
        #    vice versa), the operational spec and the narrative rationale are
        #    out of sync. Warning only — the refinement prompt can react.
        hypothesis_text = spec.hypothesis or ""
        rules_text = " ".join(
            [
                format_rules_for_prompt(spec.entry_rules),
                format_rules_for_prompt(spec.exit_rules),
            ]
        )
        terms_in_hypothesis = {m.group(0).lower() for m in _CONCEPT_TERMS.finditer(hypothesis_text)}
        terms_in_rules = {m.group(0).lower() for m in _CONCEPT_TERMS.finditer(rules_text)}
        orphan_in_hypothesis = terms_in_hypothesis - terms_in_rules
        orphan_in_rules = terms_in_rules - terms_in_hypothesis
        if orphan_in_hypothesis or orphan_in_rules:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="warning",
                    details=(
                        "Hypothesis/rules consistency: hypothesis mentions "
                        f"{sorted(orphan_in_hypothesis) or 'nothing'} that rules don't "
                        f"reference; rules mention {sorted(orphan_in_rules) or 'nothing'} "
                        "that hypothesis doesn't."
                    ),
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
