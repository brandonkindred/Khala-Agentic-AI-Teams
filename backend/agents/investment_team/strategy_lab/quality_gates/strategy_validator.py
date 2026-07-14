"""Deterministic validation of StrategySpec fields."""

from __future__ import annotations

import re
from typing import ClassVar, List

from ...models import StrategySpec
from ...strategy_lab_context import normalize_asset_class
from ..spec_dsl import (
    IndicatorRef,
    format_rules_for_prompt,
    format_sizing_rule,
    iter_leaf_predicates,
    iter_tree_indicator_refs,
)
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase
from .spec_readiness import _CONCEPT_TERMS_BROAD as _CONCEPT_TERMS
from .spec_readiness import MAX_POSITION_PCT_CEILING

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

def _concept_mentions(text: str) -> list[tuple[str, frozenset[str]]]:
    """Resolve each recognised term in ``text`` to the indicator(s) it denotes.

    Preconditions: ``text`` is a string (empty allowed).
    Postconditions: returns ``(surface_term, candidate_indicator_names)`` pairs,
    one per regex match, whitespace-normalised. A prose alias resolves via the
    shared ``_CONCEPT_TO_INDICATOR_NAMES`` map (so "on-balance volume" and the
    DSL token "obv" both yield ``{"obv"}``); a term with no indicator mapping
    (strategy concepts like "breakout"/"momentum") resolves to a singleton of
    itself so it still compares by surface form. Both ``_CONCEPT_TERMS`` and
    ``_CONCEPT_TO_INDICATOR_NAMES`` are imported from ``spec_readiness`` so
    this validator and the narrative-fidelity gate share one copy and cannot
    diverge.
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


def _rule_derived_concepts(spec: StrategySpec) -> set[str]:
    """Concepts a rule references via a bar-field or an indicator ``source``.

    Preconditions: ``spec`` is a StrategySpec.
    Postconditions: returns vocabulary concepts implied by *how* a rule reads data
    rather than by an ``IndicatorRef.name`` — a ``bar.volume`` price-ref or an
    indicator computed on the ``volume`` source both mean the rule is "about
    volume", even when its only indicator name is ``sma``. Without this a volume
    filter (``bar.volume > sma(20, source='volume')``) false-orphans a hypothesis
    that mentions "volume". Price sources (close/high/low/open) have no concept
    term in the gate vocabulary, so they contribute nothing.
    """
    concepts: set[str] = set()
    for rule in list(spec.entry_rules or []) + list(spec.exit_rules or []):
        when = getattr(rule, "when", None)
        if when is None:
            continue
        for pred in iter_leaf_predicates(when):
            for side in (pred.lhs, pred.rhs):
                if side == "bar.volume":
                    concepts.add("volume")
                elif isinstance(side, IndicatorRef) and side.source == "volume":
                    concepts.add("volume")
    return concepts


def _hypothesis_rules_orphans(spec: StrategySpec) -> tuple[set[str], set[str]]:
    """Concept terms mentioned on one side (hypothesis / rules) but not the other.

    Preconditions: ``spec`` is a StrategySpec.
    Postconditions: returns ``(orphan_in_hypothesis, orphan_in_rules)``. The rules
    side is built from the STRUCTURED refs (indicator names) PLUS the derived
    concepts a rule reads via bar-fields / indicator ``source``
    (:func:`_rule_derived_concepts`) PLUS a concept scan of free-text
    ``unparsed_rules``. A mention is orphaned only when NONE of its candidate
    indicators appears on the other side; strategy concepts with no indicator
    mapping (breakout/momentum/…) resolve to themselves and compare by surface
    form. Single source of the mismatch computation, shared by the pre-synthesis
    :meth:`StrategySpecValidator.validate` and the design-phase
    :meth:`StrategySpecValidator.check_hypothesis_rules`.
    """
    hyp_mentions = _concept_mentions(spec.hypothesis or "")
    rule_mentions = [(name, frozenset({name})) for name in _rule_indicator_names(spec)]
    rule_mentions += [(c, frozenset({c})) for c in _rule_derived_concepts(spec)]
    rule_mentions += _concept_mentions(" ".join(spec.unparsed_rules))
    hyp_concepts = frozenset().union(*(c for _, c in hyp_mentions)) if hyp_mentions else frozenset()
    rule_concepts = (
        frozenset().union(*(c for _, c in rule_mentions)) if rule_mentions else frozenset()
    )
    orphan_in_hypothesis = {t for t, c in hyp_mentions if not (c & rule_concepts)}
    orphan_in_rules = {t for t, c in rule_mentions if not (c & hyp_concepts)}
    return orphan_in_hypothesis, orphan_in_rules


def _hypothesis_rules_message(orphan_in_hypothesis: set[str], orphan_in_rules: set[str]) -> str:
    """Render the human-readable hypothesis/rules-consistency finding text."""
    return (
        "Hypothesis/rules consistency: hypothesis mentions "
        f"{sorted(orphan_in_hypothesis) or 'nothing'} that rules don't reference; rules mention "
        f"{sorted(orphan_in_rules) or 'nothing'} that hypothesis doesn't."
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
            if max_pos < 1 or max_pos > MAX_POSITION_PCT_CEILING:
                results.append(
                    self._critical(
                        f"max_position_pct={max_pos}% is outside safe range [1%, {MAX_POSITION_PCT_CEILING:g}%]."
                    )
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
            # operational spec and the narrative rationale are out of sync. The
            # mismatch computation lives in :func:`_hypothesis_rules_orphans`
            # (shared with :meth:`check_hypothesis_rules`); it now also credits
            # concepts a rule reads via a bar-field / indicator ``source`` so a
            # volume filter no longer false-orphans a "volume" hypothesis.
            orphan_in_hypothesis, orphan_in_rules = _hypothesis_rules_orphans(spec)
            if orphan_in_hypothesis or orphan_in_rules:
                results.append(
                    self._warning(_hypothesis_rules_message(orphan_in_hypothesis, orphan_in_rules))
                )

            return results or [self._info("Strategy spec passed all validation checks.")]

    def check_hypothesis_rules(
        self, spec: StrategySpec, *, phase: StrategyLabPhase = "design"
    ) -> List[QualityGateResult]:
        """Run ONLY the hypothesis-vs-rules consistency check.

        Pre: ``spec`` is a StrategySpec; ``phase`` is a valid phase literal.
        Post: returns ``[warning]`` when the narrative and the structured DSL are
        out of sync, else ``[]``. Extracted so the design ↔ review loop can surface
        the mismatch to the reviewer *before* synthesis (so ``DesignAgent.revise``
        can reconcile the two), rather than only recording it as a pre-synthesis
        warning after the design loop has already converged.
        """
        with self._using_phase(phase):
            orphan_in_hypothesis, orphan_in_rules = _hypothesis_rules_orphans(spec)
            if orphan_in_hypothesis or orphan_in_rules:
                return [
                    self._warning(_hypothesis_rules_message(orphan_in_hypothesis, orphan_in_rules))
                ]
            return []
