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
from typing import Any, ClassVar, Dict, List, Sequence

from ..executor.predicate_evaluator import EvalStatus, PandasHistoryView, evaluate_tree
from ..spec_dsl import EntryRule, iter_leaf_predicates
from .alignment_checks import _bars_to_frame, _format_predicate
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "predicate_reachability_probe"

# Minimum post-warmup bars a rule must be evaluated over before "never fires" is
# read as dead code rather than an artefact of a too-short / all-warmup window.
# Below this the probe abstains (an ``info``) — a short window is a coverage
# problem the warmup / data checks own, not a reachability verdict.
_MIN_EVALUATED_BARS = 20


def _entry_rules(spec: Any) -> List[EntryRule]:
    """Entry rules of ``spec`` in authored order — the probe's index space.

    Preconditions: ``spec`` is a ``StrategySpec`` (or any object exposing an
    ``entry_rules`` attribute).
    Postconditions: returns every ``EntryRule`` in ``spec.entry_rules``, in
    order, skipping any non-``EntryRule`` element; ``[]`` when ``spec`` has no
    ``entry_rules`` attribute or it is falsy. Shared by :meth:`probe` and
    :meth:`probe_pairs` so both index into the SAME filtered list.
    """
    return [r for r in (getattr(spec, "entry_rules", None) or []) if isinstance(r, EntryRule)]


def _build_views(market_data: Any) -> List[PandasHistoryView]:
    """Build one PandasHistoryView per symbol with usable bars.

    Preconditions: ``market_data`` is ``Optional[Dict[str, List[OHLCVBar]]]`` or
    falsy.
    Postconditions: one view per symbol with non-empty bars and a non-empty
    frame, in ``market_data``'s iteration order; an empty list when
    ``market_data`` is falsy or every symbol's bars are empty/unusable. Pure;
    no caching across calls.
    """
    views: List[PandasHistoryView] = []
    if not market_data:
        return views
    for bars in market_data.values():
        if not bars:
            continue
        df = _bars_to_frame(bars)
        if df.empty:
            continue
        views.append(PandasHistoryView(df, {}))
    return views


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


@dataclass(frozen=True)
class _PairLegCooccurrence:
    """Per-leaf-predicate co-occurrence tally for one leaf of the LATER rule,
    evaluated against one specific earlier rule (mirrors :class:`_LegReachability`).
    """

    predicate: str
    evaluated: int
    fires: int
    independent_fires: int


@dataclass(frozen=True)
class _PairCooccurrence:
    """Pairwise co-occurrence tally: does the later rule ever fire on a bar
    where the earlier rule doesn't, over the fetched bars.

    Invariants: ``earlier_index < later_index`` (an ordered pair; both index
    into the SAME filtered ``entry_rules`` list :meth:`PredicateReachabilityProbe.probe_pairs`
    builds, matching :meth:`PredicateReachabilityProbe.probe`'s existing indexing
    convention) — enforced in :meth:`__post_init__`.
    """

    earlier_index: int
    later_index: int
    earlier_side: str
    later_side: str
    evaluated: int
    later_fires: int
    later_independent_fires: int
    legs: tuple[_PairLegCooccurrence, ...]

    def __post_init__(self) -> None:
        """Enforce the ``earlier_index < later_index`` invariant at construction time."""
        assert self.earlier_index < self.later_index, (
            "earlier_index must be less than later_index (an ordered pair)"
        )

    @property
    def judged(self) -> bool:
        """True when enough jointly-judged bars exist to trust the verdict."""
        return self.evaluated >= _MIN_EVALUATED_BARS

    @property
    def later_dead(self) -> bool:
        """True when the later rule never fires at all against this pair's
        jointly-judged bars.

        This is the pre-existing "dead" concept (already reported by
        :meth:`PredicateReachabilityProbe.probe`/``to_gate_results``) — NOT
        this analysis's new "never independent" verdict. Kept so callers can
        tell the two apart rather than conflating them.
        """
        return self.judged and self.later_fires == 0

    @property
    def later_never_independent(self) -> bool:
        """True when the later rule fires, but only on bars the earlier rule
        also fires on.

        This pair alone would starve the later rule; the true, union-based
        "structurally starved" verdict (checked against every earlier rule at
        once, not just this one) is a later step's responsibility, not this
        analysis's.
        """
        return self.judged and self.later_fires > 0 and self.later_independent_fires == 0


def _sweep(node: Any, views: List[PandasHistoryView]) -> tuple[int, int]:
    """Count ``(evaluated, fires)`` for ``node`` across every bar of every view.

    Pre: ``node`` is a ``PredicateTree`` (whole ``when`` tree or a leaf); ``views``
    are :class:`PandasHistoryView`s over each symbol's bars.
    Post: ``evaluated`` counts non-warmup bars (a warming-up leg yields ``warmup``,
    which is excluded so an all-warmup window never reads as dead code); ``fires``
    counts bars where the tree evaluated to ``satisfied``. Deterministic.

    Performance: O(symbols × bars × tree-nodes) scalar evaluations, but each
    indicator series is computed once per (symbol, indicator) and cached on the
    shared :class:`PandasHistoryView` (O(1) numpy reads thereafter) — NOT recomputed
    per bar. Callers pass the SAME views to every ``_sweep`` (whole tree and each
    leg), so the cache is warm after the first sweep and the per-bar cost is a
    small fraction of the backtest this probe precedes.

    The loop deliberately does NOT skip a computed warmup prefix: doing so would
    need each indicator's required lookback ahead of time, and that formula is
    already independently duplicated in three places in this codebase (the
    synthesis compiler, the factors compiler, and the executor registry — a
    known, separately-tracked duplication hazard). Reusing or re-deriving it here
    would add a fourth copy that can drift from the others. The bars this loop
    "wastes" evaluating are a cheap early return (``evaluate_predicate`` sees a
    ``None`` indicator value and returns ``warmup`` without doing any comparison
    work), and the probe itself is memoized per round in the orchestrator, so
    this cost is paid at most once per distinct entry-rule set — not once per
    refinement round.
    """
    assert node is not None, "node must be non-None"
    assert isinstance(views, list), "views must be a list of PandasHistoryView"
    statuses = _sweep_statuses(node, views)
    evaluated = sum(1 for s in statuses if s != "warmup")
    fires = sum(1 for s in statuses if s == "satisfied")
    return evaluated, fires


def _sweep_statuses(node: Any, views: List[PandasHistoryView]) -> List[EvalStatus]:
    """Per-bar evaluation status of ``node`` across every bar of every view.

    Preconditions: ``node`` is a ``PredicateTree`` (whole ``when`` tree or a
    leaf); ``views`` are :class:`PandasHistoryView`s over each symbol's bars.
    Postconditions: returns ``evaluate_tree(node, view, i).status`` for every
    ``(view, i)`` pair, in view-major/bar-minor order — the same length and
    order as any other call given the SAME ``views`` list, so two such calls'
    results are positionally alignable per bar. Deterministic; no I/O.
    """
    assert node is not None, "node must be non-None"
    assert isinstance(views, list), "views must be a list of PandasHistoryView"
    return [evaluate_tree(node, view, i).status for view in views for i in range(view.length())]


def _cooccurrence_counts(
    later_statuses: Sequence[EvalStatus], earlier_statuses: Sequence[EvalStatus]
) -> tuple[int, int, int]:
    """Pairwise co-occurrence tally between two same-length status sequences.

    Preconditions: ``later_statuses`` and ``earlier_statuses`` have equal
    length and are positionally aligned — both produced by ``_sweep_statuses``
    over the SAME ``views`` list, so index ``k`` names the same bar in both.
    Postconditions: pure (no I/O, no bar-walking); returns ``(evaluated,
    later_fires, later_independent_fires)``. ``evaluated`` counts bars where
    BOTH sequences are non-``"warmup"`` — the only bars where "did the earlier
    rule also fire here" is a judged fact rather than an unknowable warmup
    gap. ``later_fires`` counts the subset of those bars where
    ``later_statuses[k] == "satisfied"``. ``later_independent_fires`` counts
    the further subset where ``earlier_statuses[k] != "satisfied"`` — i.e. the
    later rule fired on a bar the earlier rule did not.
    """
    assert len(later_statuses) == len(earlier_statuses), "status sequences must be aligned"
    evaluated = 0
    later_fires = 0
    later_independent_fires = 0
    for later_status, earlier_status in zip(later_statuses, earlier_statuses):
        if later_status == "warmup" or earlier_status == "warmup":
            continue
        evaluated += 1
        if later_status == "satisfied":
            later_fires += 1
            if earlier_status != "satisfied":
                later_independent_fires += 1
    return evaluated, later_fires, later_independent_fires


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
        assert spec is not None, "spec must be a StrategySpec"
        entry_rules = _entry_rules(spec)
        if not entry_rules or not market_data:
            return []
        views = _build_views(market_data)
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

    def probe_pairs(self, spec: Any, market_data: Any) -> List[_PairCooccurrence]:
        """Pairwise co-occurrence tally for every ordered (earlier, later)
        entry-rule pair against ``market_data``.

        Preconditions: ``spec`` is a ``StrategySpec``; ``market_data`` is
        ``Optional[Dict[str, List[OHLCVBar]]]`` (the fetched bars) or falsy.
        Postconditions: one :class:`_PairCooccurrence` per ordered pair
        ``(i, j)`` with ``i < j`` over ``spec.entry_rules`` (same ``EntryRule``
        filtering, and hence the same index space, as :meth:`probe`) — empty
        when there are fewer than 2 entry rules or no usable bars. Every rule
        pairs with every earlier rule regardless of ``side``, matching
        ``evaluate_entry_rules``'s default ``side_filter=None`` (the current
        sole caller doesn't pass it, so priority applies across long/short
        alike). Per-leg diagnostics are computed only for a pair where the
        later rule fires but never independently of that specific earlier
        rule (the diagnostic is only needed then), decomposing the LATER
        rule's own leaves — never the earlier rule's, mirroring
        ``_leg_diagnostic``'s single-rule decomposition pattern. This is a
        pure computation over already-evaluated predicate results: no
        severity, no ``QualityGateResult`` — finding emission is a separate,
        later step.
        """
        assert spec is not None, "spec must be a StrategySpec"
        entry_rules = _entry_rules(spec)
        if len(entry_rules) < 2 or not market_data:
            return []
        views = _build_views(market_data)
        if not views:
            return []

        statuses = [_sweep_statuses(rule.when, views) for rule in entry_rules]
        leaves = [list(iter_leaf_predicates(rule.when)) for rule in entry_rules]
        leaf_status_cache: Dict[int, tuple[List[EvalStatus], ...]] = {}

        out: List[_PairCooccurrence] = []
        for j in range(1, len(entry_rules)):
            for i in range(j):
                evaluated, later_fires, later_independent = _cooccurrence_counts(
                    statuses[j], statuses[i]
                )
                legs: tuple[_PairLegCooccurrence, ...] = ()
                if (
                    evaluated >= _MIN_EVALUATED_BARS
                    and later_fires > 0
                    and later_independent == 0
                    and len(leaves[j]) > 1
                ):
                    if j not in leaf_status_cache:
                        leaf_status_cache[j] = tuple(
                            _sweep_statuses(leaf, views) for leaf in leaves[j]
                        )
                    legs = tuple(
                        _PairLegCooccurrence(
                            _format_predicate(leaf),
                            *_cooccurrence_counts(leaf_statuses, statuses[i]),
                        )
                        for leaf, leaf_statuses in zip(leaves[j], leaf_status_cache[j])
                    )
                out.append(
                    _PairCooccurrence(
                        earlier_index=i,
                        later_index=j,
                        earlier_side=entry_rules[i].side,
                        later_side=entry_rules[j].side,
                        evaluated=evaluated,
                        later_fires=later_fires,
                        later_independent_fires=later_independent,
                        legs=legs,
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
