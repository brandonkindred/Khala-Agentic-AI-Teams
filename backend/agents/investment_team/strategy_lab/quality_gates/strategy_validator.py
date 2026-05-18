"""Deterministic validation of StrategySpec fields."""

from __future__ import annotations

import re
from typing import ClassVar, List

from ...models import StrategySpec
from ...strategy_lab_context import normalize_asset_class
from ..spec_dsl import format_rules_for_prompt, format_sizing_rule
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase

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
# consistency gate. Word-boundary anchored so substrings like "thematic"
# don't accidentally match "ema".
_CONCEPT_TERMS = re.compile(
    r"\b(rsi|macd|moving\s+average|ema|sma|bollinger|atr|breakout|"
    r"mean\s+reversion|momentum|volatility|volume|vwap|stochastic|adx|obv)\b",
    re.IGNORECASE,
)


class StrategySpecValidator(GateResultsMixin):
    """Run deterministic checks on a StrategySpec before code execution."""

    GATE: ClassVar[str] = GATE

    def validate(
        self, spec: StrategySpec, *, phase: StrategyLabPhase = "design"
    ) -> List[QualityGateResult]:
        """Run the validator and tag every result with ``phase``.

        Pre: ``spec`` is a StrategySpec; ``phase`` is a valid phase literal.
        Post: every returned result carries the caller's ``phase`` and
        ``gate_name == GATE``. The default matches the primary pre-synthesis
        call; callers that re-run the validator outside the design phase
        (e.g. the zero-trade repair path inside the refinement loop) must
        pass an explicit ``phase`` so the persisted gate history reflects
        where the spec was actually re-checked.
        """
        with self._using_phase(phase):
            return self._run_checks(spec)

    def _run_checks(self, spec: StrategySpec) -> List[QualityGateResult]:
        results: List[QualityGateResult] = []

        if normalize_asset_class(spec.asset_class) == "options":
            results.append(
                self._critical(
                    "Asset class 'options' is not yet supported — no "
                    "option-chain data, Greeks, or contract execution "
                    "model is available. Choose stocks, crypto, forex, "
                    "futures, or commodities."
                )
            )

        if not spec.entry_rules:
            results.append(
                self._critical("No entry rules defined — strategy cannot generate trades.")
            )

        if not spec.exit_rules:
            results.append(
                self._critical("No exit rules defined — positions would never close.")
            )

        if not spec.hypothesis or not spec.hypothesis.strip():
            results.append(
                self._warning("Hypothesis is empty — strategy rationale is unclear.")
            )

        if not spec.strategy_code or not spec.strategy_code.strip():
            results.append(self._critical("strategy_code is missing — nothing to execute."))

        risk = spec.risk_limits
        max_pos = risk.max_position_pct
        if max_pos < 1 or max_pos > 25:
            results.append(
                self._critical(
                    f"max_position_pct={max_pos}% is outside safe range [1%, 25%]."
                )
            )

        if risk.max_drawdown_pct < 5 or risk.max_drawdown_pct > 50:
            results.append(
                self._warning(
                    f"max_drawdown_pct={risk.max_drawdown_pct}% is outside typical range [5%, 50%]."
                )
            )

        # Spec rule fields are structured DSL nodes — render through the
        # spec_dsl formatters to recover a human-readable text view for the
        # asset-class and non-computable scans. Unparseable prose lives in
        # ``spec.unparsed_rules`` and is folded into the same scan.
        all_rules_text = " ".join(
            [
                format_rules_for_prompt(spec.entry_rules),
                format_rules_for_prompt(spec.exit_rules),
                format_sizing_rule(spec.sizing),
                " ".join(spec.unparsed_rules),
            ]
        )
        pattern = _ASSET_MISMATCH.get(spec.asset_class.lower())
        if pattern and pattern.search(all_rules_text):
            results.append(
                self._warning(
                    f"Rules reference concepts mismatched with asset class '{spec.asset_class}'."
                )
            )

        if _NON_COMPUTABLE_KEYWORDS.search(all_rules_text):
            results.append(
                self._warning(
                    "Rules reference non-computable data (sentiment, social media, etc.) "
                    "without a numerical proxy."
                )
            )

        # Hypothesis-vs-rules consistency. If the hypothesis names indicator
        # concepts that no entry/exit rule references (or vice versa), the
        # operational spec and the narrative rationale are out of sync.
        rules_text = " ".join(
            [
                format_rules_for_prompt(spec.entry_rules),
                format_rules_for_prompt(spec.exit_rules),
                " ".join(spec.unparsed_rules),
            ]
        )
        terms_in_hypothesis = {
            m.group(0).lower() for m in _CONCEPT_TERMS.finditer(spec.hypothesis or "")
        }
        terms_in_rules = {m.group(0).lower() for m in _CONCEPT_TERMS.finditer(rules_text)}
        orphan_in_hypothesis = terms_in_hypothesis - terms_in_rules
        orphan_in_rules = terms_in_rules - terms_in_hypothesis
        if orphan_in_hypothesis or orphan_in_rules:
            results.append(
                self._warning(
                    "Hypothesis/rules consistency: hypothesis mentions "
                    f"{sorted(orphan_in_hypothesis) or 'nothing'} that rules don't "
                    f"reference; rules mention {sorted(orphan_in_rules) or 'nothing'} "
                    "that hypothesis doesn't."
                )
            )

        return results or [self._info("Strategy spec passed all validation checks.")]
