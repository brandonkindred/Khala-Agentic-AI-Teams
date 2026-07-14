"""Pre-backtest data-driven reachability probe.

The closed-form reachability check in :mod:`spec_readiness`
(``_check_predicate_reachability``) catches only *structural* dead code — a
bounded indicator compared against an out-of-range constant (``rsi > 100``), or
an identical-reference tautology/contradiction (``close < close``). It cannot
see *data-dependent* dead code: an ``all_of`` whose legs never co-occur on the
fetched bars, or ``sma(5) > sma(200)`` that simply never crosses in the window.

The post-backtest :class:`RuleFiringRateGate` catches some of that, but only
*after* a (doomed) backtest has run, only on the compiled path, and without
per-leg diagnostics. This probe runs *before* the backtest and evaluates each
entry rule's authored ``PredicateTree`` against the REAL fetched bars using the
exact same ``evaluate_tree`` the compiled engine uses. So on the compiled path
"zero predicate fires over the post-warmup window" provably means "zero entry
orders" — the strategy cannot generate a single trade as authored — and the
probe reports it, per-rule and per-leg, as an early authoring-time signal.

Path semantics:
  * Compiled path (``requires_custom_code=False``): the engine decides entries
    with this very evaluator, so an unreachable predicate is a **critical** — the
    backtest is guaranteed to be a no-op for that rule.
  * Custom path (``requires_custom_code=True``): the engine runs LLM-authored
    code that may diverge from the spec's DSL, so an unreachable authored
    predicate is a **warning** — the spec's *intent* is untestable on this data,
    but the executed code might still trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, List

from ..executor.predicate_evaluator import PandasHistoryView, evaluate_tree
from ..spec_dsl import EntryRule, iter_leaf_predicates
from .alignment_checks import _bars_to_frame, _format_predicate
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "predicate_reachability_probe"

# Minimum post-warmup bars a rule must be evaluated over before "never fires" is
# read as dead code rather than an artefact of a too-short / all-warmup window.
# Below this the probe abstains (an ``info``) — a short window is a coverage
# problem the warmup / data checks own, not a reachability verdict.
_MIN_EVALUATED_BARS = 20


@dataclass(frozen=True)
class _LegReachability:
    """Per-leaf-predicate firing tally within one entry rule."""

    predicate: str
    evaluated: int
    fires: int


@dataclass(frozen=True)
class _RuleReachability:
    """Whole-rule firing tally (and per-leg breakdown when the rule is dead)."""

    rule_index: int
    side: str
    evaluated: int
    fires: int
    legs: tuple[_LegReachability, ...]

    @property
    def judged(self) -> bool:
        """True when enough post-warmup bars exist to trust the verdict."""
        return self.evaluated >= _MIN_EVALUATED_BARS

    @property
    def dead(self) -> bool:
        """True when the rule was judged and never fired."""
        return self.judged and self.fires == 0


def _sweep(node: Any, views: List[PandasHistoryView]) -> tuple[int, int]:
    """Count ``(evaluated, fires)`` for ``node`` across every bar of every view.

    Pre: ``node`` is a ``PredicateTree`` (whole ``when`` tree or a leaf); ``views``
    are :class:`PandasHistoryView`s over each symbol's bars.
    Post: ``evaluated`` counts non-warmup bars (a warming-up leg yields ``warmup``,
    which is excluded so an all-warmup window never reads as dead code); ``fires``
    counts bars where the tree evaluated to ``satisfied``. Deterministic.
    """
    evaluated = 0
    fires = 0
    for view in views:
        for i in range(view.length()):
            status = evaluate_tree(node, view, i).status
            if status == "warmup":
                continue
            evaluated += 1
            if status == "satisfied":
                fires += 1
    return evaluated, fires


class PredicateReachabilityProbe(GateResultsMixin):
    """Evaluate each entry rule's authored predicate against the real bars.

    Invariants: deterministic; reads no state; reuses the engine's own
    ``evaluate_tree`` and indicator math so its verdict matches the compiled
    engine's entry decisions bar-for-bar.
    """

    GATE: ClassVar[str] = GATE

    def probe(self, spec: Any, market_data: Any) -> List[_RuleReachability]:
        """Reachability tally for every entry rule against ``market_data``.

        Pre: ``spec`` is a ``StrategySpec``; ``market_data`` is
        ``Optional[Dict[str, List[OHLCVBar]]]`` (the fetched bars) or falsy.
        Post: one :class:`_RuleReachability` per ``EntryRule`` (empty when there
        are no entry rules or no usable bars). The per-leg breakdown is computed
        only for a rule that never fired (the diagnostic is only needed then),
        reusing one indicator-cached view per symbol so indicators are computed
        at most once per (symbol, indicator).
        """
        entry_rules = [
            r for r in (getattr(spec, "entry_rules", None) or []) if isinstance(r, EntryRule)
        ]
        if not entry_rules or not market_data:
            return []
        views: List[PandasHistoryView] = []
        for bars in market_data.values():
            if not bars:
                continue
            df = _bars_to_frame(bars)
            if df.empty:
                continue
            views.append(PandasHistoryView(df, {}))
        if not views:
            return []

        out: List[_RuleReachability] = []
        for idx, rule in enumerate(entry_rules):
            evaluated, fires = _sweep(rule.when, views)
            legs: tuple[_LegReachability, ...] = ()
            if fires == 0 and evaluated >= _MIN_EVALUATED_BARS:
                leaves = list(iter_leaf_predicates(rule.when))
                if len(leaves) > 1:
                    legs = tuple(
                        _LegReachability(_format_predicate(leaf), *_sweep(leaf, views))
                        for leaf in leaves
                    )
            out.append(
                _RuleReachability(
                    rule_index=idx, side=rule.side, evaluated=evaluated, fires=fires, legs=legs
                )
            )
        return out

    def all_entries_dead(self, reach: List[_RuleReachability]) -> bool:
        """True iff EVERY entry rule was judged and never fires.

        Pre: ``reach`` is the output of :meth:`probe`.
        Post: True only when there is at least one rule, all rules had enough
        post-warmup bars to judge, and none fired — the condition under which a
        *compiled* strategy is guaranteed to emit zero entries. A rule with too
        few bars to judge makes this ``False`` (we cannot prove zero entries).
        """
        return bool(reach) and all(r.judged for r in reach) and all(r.fires == 0 for r in reach)

    def to_gate_results(
        self, reach: List[_RuleReachability], spec: Any, *, phase: StrategyLabPhase = "synthesis"
    ) -> List[QualityGateResult]:
        """Render a :meth:`probe` tally into phase-tagged gate results.

        Pre: ``reach`` is the output of :meth:`probe` for ``spec``.
        Post: one result per rule — ``critical`` (compiled path) / ``warning``
        (custom path) for an unreachable rule, ``info`` for a reachable rule or
        one with too few bars to judge. Never empty when ``reach`` is non-empty.
        """
        custom = bool(getattr(spec, "requires_custom_code", False))
        with self._using_phase(phase):
            if not reach:
                return [
                    self._info("Predicate reachability probe: no entry rules or bars to probe.")
                ]
            results: List[QualityGateResult] = []
            for r in reach:
                rule_key = f"entry[{r.rule_index}]"
                if not r.judged:
                    results.append(
                        self._info(
                            f"Entry rule {rule_key} (side={r.side}): only {r.evaluated} post-warmup "
                            "bar(s) available — too few to judge reachability; skipped.",
                            rule_id=rule_key,
                        )
                    )
                elif r.fires == 0:
                    detail = (
                        f"Entry rule {rule_key} (side={r.side}) never satisfies its predicate across "
                        f"{r.evaluated} post-warmup bar(s) of the fetched data — it cannot generate "
                        f"entries as authored. {_leg_diagnostic(r)}"
                    )
                    if custom:
                        results.append(
                            self._warning(
                                detail
                                + " (custom-code path: the executed code may differ from the spec, "
                                "but the authored entry logic is unreachable on this data.)",
                                rule_id=rule_key,
                            )
                        )
                    else:
                        results.append(self._critical(detail, rule_id=rule_key))
                else:
                    results.append(
                        self._info(
                            f"Entry rule {rule_key} (side={r.side}) satisfied on {r.fires}/"
                            f"{r.evaluated} post-warmup bar(s).",
                            rule_id=rule_key,
                        )
                    )
            return results

    def check(
        self, spec: Any, market_data: Any, *, phase: StrategyLabPhase = "synthesis"
    ) -> List[QualityGateResult]:
        """Convenience: :meth:`probe` then :meth:`to_gate_results` (used in tests)."""
        return self.to_gate_results(self.probe(spec, market_data), spec, phase=phase)


def _leg_diagnostic(r: _RuleReachability) -> str:
    """Human diagnostic for a dead rule, naming the bottleneck leg(s).

    Pre: ``r`` is a dead :class:`_RuleReachability` (``fires == 0``, judged).
    Post: for a single-condition rule, states the condition never holds; for a
    conjunction, names the conjunct(s) that never hold on their own, or — when
    every conjunct holds individually — reports that they never co-occur (the
    all_of is unsatisfiable on this data). Empty legs → a generic message.
    """
    if not r.legs:
        return "The predicate never holds on any bar."
    never = [leg.predicate for leg in r.legs if leg.fires == 0]
    if never:
        return f"These condition(s) never hold on their own: {never}."
    return (
        "Every condition holds on its own but they never co-occur on the same bar "
        "(the all_of conjunction is unsatisfiable on this data)."
    )
