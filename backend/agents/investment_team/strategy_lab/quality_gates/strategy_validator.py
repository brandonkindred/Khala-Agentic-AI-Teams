"""Deterministic validation of StrategySpec fields."""

from __future__ import annotations

import re
from typing import ClassVar, List

from ...models import StrategySpec
from ...strategy_lab_context import normalize_asset_class
from ..spec_dsl import format_rules_for_prompt, format_sizing_rule, iter_tree_indicator_refs
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
# don't accidentally match "ema". This vocabulary is deliberately broader than
# the narrative-fidelity gate's — it also carries strategy-concept words
# (breakout, mean reversion, momentum, volatility, volume) — but it must still
# recognise every DSL indicator name, so the channel/volume/momentum additions
# are mirrored here. ``williams_r`` precedes ``williams`` so the exact DSL token
# is captured rather than the bare prose alias.
_CONCEPT_TERMS = re.compile(
    r"\b(rsi|macd|moving\s+average|ema|sma|bollinger|atr|breakout|"
    r"mean\s+reversion|momentum|volatility|volume|vwap|stochastic|adx|obv|"
    r"on[\s-]balance\s+volume|donchian|keltner|mfi|money\s+flow|roc|"
    r"rate\s+of\s+change|cci|williams_r|williams)\b",
    re.IGNORECASE,
)


def _concept_mentions(text: str) -> list[tuple[str, frozenset[str]]]:
    """Resolve each recognised term in ``text`` to the indicator(s) it denotes.

    Preconditions: ``text`` is a string (empty allowed).
    Postconditions: returns ``(surface_term, candidate_indicator_names)`` pairs,
    one per regex match, whitespace-normalised. A prose alias resolves via the
    shared ``_CONCEPT_TO_INDICATOR_NAMES`` map (so "on-balance volume" and the
    DSL token "obv" both yield ``{"obv"}``); a term with no indicator mapping
    (strategy concepts like "breakout"/"momentum") resolves to a singleton of
    itself so it still compares by surface form. The map is imported lazily to
    keep this validator's module-load surface light and to reuse the one copy
    shared with the narrative-fidelity gate rather than duplicating it here.
    """
    from .spec_readiness import _CONCEPT_TO_INDICATOR_NAMES

    mentions: list[tuple[str, frozenset[str]]] = []
    for m in _CONCEPT_TERMS.finditer(text or ""):
        term = re.sub(r"\s+", " ", m.group(0).lower())
        mentions.append((term, _CONCEPT_TO_INDICATOR_NAMES.get(term, frozenset({term}))))
    return mentions


def _rule_indicator_names(spec: StrategySpec) -> set[str]:
    """DSL indicator names referenced by the structured entry/exit rule predicates.

    Preconditions: ``spec`` is a StrategySpec.
    Postconditions: returns the set of ``IndicatorRef.name`` values across every
    rule's ``when`` tree. Read from the structured refs, NOT the rendered rule
    text, because ``format_rules_for_prompt`` renders band/output selectors as
    underscore-suffixed tokens (``donchian_upper(20)``, ``keltner_lower(...)``)
    that a ``\\b``-anchored concept regex cannot match — regexing the rendered
    text would make every donchian/keltner rule look absent from its own rules.
    """
    names: set[str] = set()
    for rule in list(spec.entry_rules or []) + list(spec.exit_rules or []):
        when = getattr(rule, "when", None)
        if when is not None:
            names.update(ref.name for ref in iter_tree_indicator_refs(when))
    return names


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
                    self._critical(f"max_position_pct={max_pos}% is outside safe range [1%, 25%].")
                )

            # No drawdown check: max drawdown is not a constraint. A Strategy Lab
            # run is an experiment and may lose up to 100% of the account;
            # realised drawdown is reported as a metric, never enforced as a cap.

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
            pattern = _ASSET_MISMATCH.get(normalize_asset_class(spec.asset_class))
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
            #
            # The rules side is built from the STRUCTURED refs (authoritative DSL
            # names) plus a concept scan of the free-text ``unparsed_rules`` — the
            # rendered rule text is deliberately not regexed, because band/output
            # selectors render as underscore-suffixed tokens the concept regex
            # cannot match. Each matched term resolves to the indicator(s) it
            # denotes, so a prose alias in the hypothesis ("on-balance volume")
            # and the rule's ``obv`` ref count as the same concept; a mention is
            # orphaned only when NONE of its candidate indicators appears on the
            # other side. Strategy concepts with no indicator mapping
            # (breakout/momentum/…) resolve to themselves and compare by surface form.
            hyp_mentions = _concept_mentions(spec.hypothesis or "")
            rule_mentions = [(name, frozenset({name})) for name in _rule_indicator_names(spec)]
            rule_mentions += _concept_mentions(" ".join(spec.unparsed_rules))
            hyp_concepts = frozenset().union(*(c for _, c in hyp_mentions))
            rule_concepts = frozenset().union(*(c for _, c in rule_mentions))
            orphan_in_hypothesis = {t for t, c in hyp_mentions if not (c & rule_concepts)}
            orphan_in_rules = {t for t, c in rule_mentions if not (c & hyp_concepts)}
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
