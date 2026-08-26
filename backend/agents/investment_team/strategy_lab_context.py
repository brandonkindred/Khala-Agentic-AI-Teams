"""
Shared helpers for Strategy Lab: prior-results formatting and asset-class mix hints.

Used by strategy ideation and signal intelligence to avoid circular imports.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

from .models import StrategyLabRecord
from .strategy_lab.spec_dsl import AllOf, AnyOf, Predicate, iter_leaf_predicates

if TYPE_CHECKING:  # pragma: no cover - typing-only, avoids a circular import
    from .signal_intelligence_models import SignalIntelligenceBriefV1

logger = logging.getLogger(__name__)

_CANONICAL_ASSET_CLASSES: tuple[str, ...] = (
    "stocks",
    "crypto",
    "forex",
    "options",
    "futures",
    "commodities",
)

# Asset classes the LLM is allowed to choose for new strategies (#535).
# 'options' is canonical (so ``normalize_asset_class`` preserves it for the
# validator gate to reject) but not a valid ideation target, so it's
# excluded from prompt counts and underrepresented-class steering.
_PROMPT_ASSET_CLASSES: tuple[str, ...] = tuple(
    c for c in _CANONICAL_ASSET_CLASSES if c != "options"
)

# Public alias of the ideation-valid asset classes. Callers that need to
# validate user-supplied category selections or compute exclusion complements
# import this rather than the underscore-prefixed internal tuple.
PROMPT_ASSET_CLASSES: tuple[str, ...] = _PROMPT_ASSET_CLASSES

# Explore-mode diversity steering: the recent-window stocks share above which
# equities count as "relatively heavy" and the anti-concentration nudge fires.
_STOCK_CONCENTRATION_THRESHOLD: float = 0.35

# Backtest statuses for cycles that short-circuited *before* running a backtest.
# Their persisted ``strategy.asset_class`` may be a coerced placeholder (an
# unsupported class like ``bonds`` is canonicalized to ``stocks`` for schema
# validity before the redesign route), so they must not feed the asset-class
# diversity steering. Distinct from executed-but-losing statuses (``failed`` /
# ``failed: max_refinement_rounds``), which ran a real backtest with a genuine
# canonical class and SHOULD count — hence an explicit set rather than a blanket
# ``startswith("failed")`` filter.
#
# This must enumerate EVERY status the orchestrator passes to
# ``_build_short_circuit_record`` (the pre-backtest exit path). Keep it in sync
# with the ``short_circuit_status`` values in ``strategy_lab/orchestrator.py``:
# spec_unimplementable, spec_validation, code_synthesis, design_not_ready,
# design_stalled, budget_exhausted. The in-memory ``ConvergenceTracker`` skips
# all of these via ``count_asset_class=False`` at the call site; this set is the
# persisted-record equivalent for ``prior_records`` rebuilt after a restart.
# ``design_stalled`` matters as much as the rest: a stalled cycle persists
# ``compute_metrics([], ...)`` placeholder metrics (0% return/win rate), so
# counting it as executed would drag down both the asset-class steering and the
# performance-attribution buckets below.
_NON_EXECUTED_BACKTEST_STATUSES: frozenset[str] = frozenset(
    {
        "failed: spec_unimplementable",
        "failed: spec_validation",
        "failed: code_synthesis",
        "failed: design_not_ready",
        "failed: design_stalled",
        "failed: budget_exhausted",
    }
)


def _is_executed_record(record: StrategyLabRecord) -> bool:
    """True when a record ran a real backtest (not a pre-backtest short-circuit).

    Single source of truth for the executed/non-executed split shared by
    :func:`asset_class_mix_hint` (diversity steering) and :func:`_executed_records`
    (performance attribution), so the two never disagree on which records count.
    Records persisted before ``BacktestRecord.status`` existed default to
    ``"completed"``, so legacy rows count as executed.

    Preconditions: ``record.backtest`` exposes a ``status`` (or none, treated as
    ``"completed"``).
    Postconditions: returns ``True`` iff the status is not in
    ``_NON_EXECUTED_BACKTEST_STATUSES``.
    """
    return (
        str(getattr(record.backtest, "status", "completed")) not in _NON_EXECUTED_BACKTEST_STATUSES
    )


def normalize_asset_class(ac: object) -> str:
    """Map any asset-class string variant to one of the canonical labels.

    Accepts ``object`` so callers can pass raw LLM output without casting.
    """
    x = str(ac or "stocks").lower().strip()
    if x in ("equities", "equity", "stock", "etf", "etfs"):
        return "stocks"
    if x in ("fx",):
        return "forex"
    if x in ("commodity", "metal", "energy"):
        return "commodities"
    if x in ("cryptocurrency", "cryptocurrencies"):
        return "crypto"
    if x in _CANONICAL_ASSET_CLASSES:
        return x
    return "stocks"


# Asset classes whose venues trade in whole units (shares / contracts); all
# others (forex, crypto) accept fractional quantities. Single source of truth
# shared by the readiness whole-lot gate and the runtime sizing dispatcher so
# the two never disagree on whether a class is fractional.
WHOLE_LOT_ASSET_CLASSES: frozenset[str] = frozenset({"stocks", "futures", "commodities"})


def is_fractional_asset_class(ac: object) -> bool:
    """True when the normalized asset class trades in fractional quantities.

    Preconditions: none — ``ac`` may be any value (``None``/unknown normalize to
    ``"stocks"`` → whole-lot → ``False``).
    Postconditions: returns ``True`` iff ``normalize_asset_class(ac)`` is not in
    ``WHOLE_LOT_ASSET_CLASSES``.
    """
    return normalize_asset_class(ac) not in WHOLE_LOT_ASSET_CLASSES


def normalize_asset_class_strict(ac: object) -> str:
    """Strict variant of :func:`normalize_asset_class`.

    Applies the same alias map (``equity``/``equities``/``stock``/``etf``/``etfs``
    → ``stocks``, ``fx`` → ``forex``, ``commodity``/``metal``/``energy`` →
    ``commodities``, ``cryptocurrency``/``cryptocurrencies`` → ``crypto``) so
    callers see the same canonical class the runtime fetch path does, but raises
    :class:`ValueError` for truly unknown classes instead of silently falling
    back to ``"stocks"``.

    Use this in gates and other fail-closed paths where a typo'd
    ``asset_class`` (``"bonds"``, ``"crpto"``) must surface as an error.
    Runtime paths that need defense-in-depth keep using
    :func:`normalize_asset_class`.
    """
    x = str(ac or "").lower().strip()
    if x in ("equities", "equity", "stock", "etf", "etfs"):
        return "stocks"
    if x in ("fx",):
        return "forex"
    if x in ("commodity", "metal", "energy"):
        return "commodities"
    if x in ("cryptocurrency", "cryptocurrencies"):
        return "crypto"
    if x in _CANONICAL_ASSET_CLASSES:
        return x
    raise ValueError(
        f"unknown asset_class {ac!r}; expected one of {sorted(_CANONICAL_ASSET_CLASSES)} "
        "or a known alias (equity/equities/stock/etf/etfs, fx, "
        "commodity/metal/energy, cryptocurrency/cryptocurrencies)"
    )


def normalize_allowed_asset_classes(raw: Optional[Iterable[str]]) -> Optional[List[str]]:
    """Normalize a user-supplied list of asset categories to canonical, ideation-valid classes.

    The selector on the Strategy Lab UI lets the user constrain which asset
    categories the design agent may generate strategies for. This maps that
    raw selection (canonical names or known aliases such as ``stock`` /
    ``equity`` / ``fx``) to the canonical, ideation-valid labels in
    :data:`PROMPT_ASSET_CLASSES`, deduplicated and returned in canonical order.

    Preconditions:
      - ``raw`` is ``None`` or an iterable of scalar values (each a class name
        or known alias). Items that do not resolve to a known canonical class
        (via :func:`normalize_asset_class_strict`) are silently dropped, as is
        ``options`` — it is canonical but never a valid ideation target, so it
        cannot be a generation category.

    Postconditions:
      - Returns ``None`` when ``raw`` is ``None`` (no constraint — the caller
        treats this as "all categories allowed").
      - Otherwise returns a list that is a subset of :data:`PROMPT_ASSET_CLASSES`
        in canonical order with no duplicates. The list MAY be empty when
        ``raw`` was non-empty but contained nothing valid; the API boundary
        rejects that case rather than running with zero categories.
    """
    if raw is None:
        return None
    seen: set[str] = set()
    for item in raw:
        try:
            canonical = normalize_asset_class_strict(item)
        except ValueError:
            # Unrecognized token (e.g. "bonds", a typo) — drop it rather than
            # coercing to stocks, so the user's intent is never silently widened.
            continue
        if canonical in PROMPT_ASSET_CLASSES:
            seen.add(canonical)
    return [c for c in PROMPT_ASSET_CLASSES if c in seen]


def excluded_for_allowed(allowed: Optional[Iterable[str]]) -> List[str]:
    """Complement of an allowed-category set within the ideation-valid classes.

    The design pipeline constrains generation via an *exclusion* list
    (``exclude_asset_classes``). A positive "allowed categories" selection is
    therefore expressed downstream as everything in :data:`PROMPT_ASSET_CLASSES`
    that the user did NOT allow.

    Preconditions:
      - ``allowed`` is ``None`` or an iterable of canonical class labels
        (typically the output of :func:`normalize_allowed_asset_classes`, which
        may itself return ``None`` for "no constraint").

    Postconditions:
      - Returns ``[]`` when ``allowed`` is ``None`` (no constraint → nothing
        excluded).
      - Otherwise returns the canonical-order list of
        :data:`PROMPT_ASSET_CLASSES` not present in ``allowed``. Empty when
        ``allowed`` covers every class (also no constraint).
    """
    if allowed is None:
        return []
    allowed_set = set(allowed)
    return [c for c in PROMPT_ASSET_CLASSES if c not in allowed_set]


def _canonical_subset(raw: Optional[List[str]]) -> set[str]:
    """Normalize a class-name list to the canonical labels it actually names.

    Shared by :func:`allowed_asset_classes` (for ``exclude_asset_classes``)
    and :func:`select_asset_category` (for its ``avoid`` bias), so an alias
    (``equity`` / ``fx``) is honored rather than silently matching nothing
    against :data:`PROMPT_ASSET_CLASSES`.

    Preconditions:
      - ``raw`` is ``None`` or a list of scalar values.
    Postconditions:
      - Returns the set of canonical labels named by ``raw``; entries that
        resolve to no known class are dropped.
    """
    out: set[str] = set()
    for item in raw or ():
        try:
            out.add(normalize_asset_class_strict(item))
        except ValueError:
            continue
    return out


def select_signal_brief(
    briefs: Optional[Dict[str, "SignalIntelligenceBriefV1"]],
    asset_class: str,
) -> Optional["SignalIntelligenceBriefV1"]:
    """Pick the signal brief synthesized for ``asset_class``.

    The per-batch signal expert produces one brief per allowed category, each
    built from that category's records alone. A design attempt pinned to a
    category takes its own brief and no other — a brief is injected verbatim
    into the design prompt, so handing over another category's would reintroduce
    exactly the cross-category evidence the pin exists to keep out.

    Preconditions:
        * ``briefs`` maps canonical asset-class labels to briefs, or is
          ``None`` / empty when no brief could be produced.
        * ``asset_class`` is the canonical label this attempt is pinned to.

    Postconditions:
        * Returns the brief for ``asset_class``, or ``None`` when there is
          none. ``None`` is a supported outcome the design agent handles by
          omitting the signal section — never a substitute brief from a
          different category.
    """
    if not briefs:
        return None
    return briefs.get(asset_class)


def allowed_asset_classes(exclude_asset_classes: Optional[List[str]]) -> frozenset[str]:
    """Recover the run's user-level allowed-category set from its exclusions.

    The complement of ``exclude_asset_classes`` within
    :data:`PROMPT_ASSET_CLASSES` — the exact inverse of
    :func:`excluded_for_allowed`, so this reconstructs the user's original
    ``allowed_asset_classes`` selection without needing it threaded through
    separately. Entries are normalized through
    :func:`normalize_asset_class_strict`, so an alias (``equity`` / ``fx``)
    excludes the canonical class it names rather than silently matching
    nothing and leaving that class selectable.

    Single source of truth for this computation, shared by
    :func:`select_asset_category` (which further narrows the result to make
    one random pick) and any other caller that needs the same run-level
    complement directly — e.g. constraining symbol-based class inference to
    categories the user actually selected, rather than the narrower
    per-attempt pin.

    Preconditions:
      - ``exclude_asset_classes`` is ``None``/empty (unrestricted run — every
        category allowed) or a list of canonical class labels or known
        aliases naming the categories the user did NOT select. Entries that
        resolve to no known class are ignored.

    Postconditions:
      - Returns the complement within :data:`PROMPT_ASSET_CLASSES`. Never
        empty: an exclusion covering every class degrades to the full menu
        rather than returning nothing, matching :func:`asset_class_mix_hint`'s
        defensive handling of the same internal-caller misuse — the API
        boundary is what actually enforces a non-empty
        ``allowed_asset_classes`` selection.
    """
    excluded_set = _canonical_subset(exclude_asset_classes)
    allowed = frozenset(c for c in PROMPT_ASSET_CLASSES if c not in excluded_set)
    if not allowed:
        logger.warning(
            "allowed_asset_classes: exclude_asset_classes=%s covers every category; "
            "falling back to the full menu instead of failing the cycle.",
            sorted(excluded_set),
        )
        return frozenset(PROMPT_ASSET_CLASSES)
    return allowed


def select_asset_category(
    exclude_asset_classes: Optional[List[str]],
    *,
    avoid: Optional[List[str]] = None,
    rng: Optional[random.Random] = None,
) -> str:
    """Randomly pick exactly one asset category for a design attempt.

    Each design attempt must commit to a single asset category rather than
    leaving the designer free to mix strategies across every category the
    user allowed. The allowed set is :func:`allowed_asset_classes`.

    ``avoid`` (optional) names classes to steer away from when possible —
    typically the convergence tracker's over-represented set
    (``ConvergenceTracker.get_diversity_avoid_classes``), so a pin doesn't
    land on the very class its own diversity directive tells the designer to
    stop using. The bias is soft: when ``allowed - avoid`` is non-empty, the
    choice is made from that narrowed set; otherwise (every allowed class is
    also in ``avoid``, e.g. a single-category restriction) it falls back to
    the full ``allowed`` set — the pin always wins over the bias.

    Both list parameters are typed ``List[str]`` (not ``Iterable[str]``)
    deliberately, matching :func:`asset_class_mix_hint`: ``str`` is itself
    iterable, so an ``Iterable[str]`` annotation would silently accept a bare
    string and iterate its characters — excluding nothing and inverting the
    caller's intent.

    Preconditions:
      - ``exclude_asset_classes`` / ``avoid`` are ``None`` or lists of
        canonical class labels or known aliases. Entries that resolve to no
        known class are ignored.

    Postconditions:
      - Returns one class from :data:`PROMPT_ASSET_CLASSES`, never one named
        (directly or by alias) in ``exclude_asset_classes`` — unless the
        exclusion covers every class, in which case it degrades to the full
        menu rather than raising, matching :func:`asset_class_mix_hint`'s
        defensive handling of the same internal-caller misuse.
    """
    allowed_set = allowed_asset_classes(exclude_asset_classes)
    allowed = [c for c in PROMPT_ASSET_CLASSES if c in allowed_set]
    chooser = rng or random
    avoid_set = _canonical_subset(avoid)
    if avoid_set:
        preferred = [c for c in allowed if c not in avoid_set]
        if preferred:
            return chooser.choice(preferred)
    return chooser.choice(allowed)


def filter_records_by_asset_class(
    records: List[StrategyLabRecord], asset_class: str
) -> List[StrategyLabRecord]:
    """Restrict prior records to genuine, executed evidence for one asset category.

    Used to scope the design agent's "prior strategy results" context, and
    the per-category signal brief, to the category selected for the current
    attempt (see :func:`select_asset_category`), so neither ever reasons
    over hypotheses, rationale, or performance data from unrelated asset
    categories.

    Non-executed short-circuits (see :data:`_NON_EXECUTED_BACKTEST_STATUSES`)
    are excluded outright rather than trusted by their persisted
    ``strategy.asset_class``: a pre-backtest exit for a genuinely unsupported
    class (e.g. ``bonds``) coerces that field to a schema-valid placeholder
    (``stocks``) so the record can be persisted at all, while the hypothesis
    it carries is not actually stocks evidence. Matching on the coerced label
    would launder that placeholder into a later stocks attempt's prior-results
    context or signal brief as if it were real stocks history — the exact
    cross-category contamination category-scoping exists to prevent.

    Preconditions:
      - ``records`` is a list of ``StrategyLabRecord``; ``asset_class`` is a
        canonical label (typically the output of :func:`select_asset_category`).

    Postconditions:
      - Returns the subset of ``records`` that are executed
        (:func:`_is_executed_record`) and whose (normalized) strategy asset
        class equals ``asset_class``, preserving input order.
    """
    return [
        r
        for r in records
        if _is_executed_record(r) and normalize_asset_class(r.strategy.asset_class) == asset_class
    ]


def format_prior_results(records: List[StrategyLabRecord], *, max_records: int = 50) -> str:
    """Render prior lab strategies as the "Prior Strategy Results" block for the design prompt.

    Each record is labeled LOSING / WINNING · PUBLISHABLE / WINNING · NOT PUBLISHABLE
    (with the joined gate codes when available) from ``is_winning`` / ``is_publishable`` /
    ``publishability_skip_reason``, followed by its asset class, hypothesis, backtest
    metrics, ideation rationale, and post-backtest analysis — each field truncated to
    keep the entry prompt-sized.

    Preconditions:
      - ``records`` is a list of ``StrategyLabRecord``; ``max_records >= 0``.
    Postconditions:
      - Returns a non-empty string. Empty ``records`` → a "first strategy" sentinel.
      - When ``len(records) > max_records``, only the ``max_records`` most recently
        created records (by ``created_at``) are rendered, oldest first.
    """
    if not records:
        return "None yet — this is the first strategy."
    ordered = sorted(records, key=lambda x: x.created_at)
    if len(ordered) > max_records:
        ordered = ordered[-max_records:]
    lines = []
    for i, r in enumerate(ordered, start=1):
        if not r.is_winning:
            label = "LOSING"
        elif r.is_publishable:
            label = "WINNING · PUBLISHABLE"
        elif r.publishability_skip_reason:
            label = f"WINNING · NOT PUBLISHABLE ({r.publishability_skip_reason})"
        else:
            label = "WINNING · NOT PUBLISHABLE"
        hyp = r.strategy.hypothesis.replace("\n", " ").strip()
        if len(hyp) > 160:
            hyp = hyp[:157] + "..."
        analysis = (r.analysis_narrative or "").replace("\n", " ").strip()
        if len(analysis) > 420:
            analysis = analysis[:417] + "..."
        rationale = (r.strategy_rationale or "").replace("\n", " ").strip()
        if len(rationale) > 220:
            rationale = rationale[:217] + "..."
        res = r.backtest.result
        lines.append(
            f"{i}. [{label}] {r.strategy.asset_class} | {hyp}\n"
            f"   Metrics: annual {res.annualized_return_pct:.1f}%, Sharpe {res.sharpe_ratio:.2f}, "
            f"max DD {res.max_drawdown_pct:.1f}%, win rate {res.win_rate_pct:.1f}%\n"
            f"   Ideation rationale: {rationale}\n"
            f"   Post-backtest analysis: {analysis}"
        )
    return "\n\n".join(lines)


def _leaf_predicates(when: object) -> list:
    """Flatten a rule's ``when`` to its leaf predicates, defensively.

    A single ``Predicate`` → ``[when]``; an ``all_of`` / ``any_of`` tree → its
    leaf predicates in order; any other / malformed shape → ``[]`` so the caller
    can fall back to a top-level read. Never raises — this feeds prompt-context
    formatting, not a correctness gate.

    Postconditions: returns a list of ``Predicate`` (possibly empty); each
    element exposes ``lhs`` / ``rhs`` / ``op``.
    """
    if isinstance(when, Predicate):
        return [when]
    if isinstance(when, (AllOf, AnyOf)):
        return list(iter_leaf_predicates(when))
    return []


def _entry_archetype(strategy: object) -> str:
    """Classify a strategy's entry rules into a coarse, comparable archetype label.

    The label names the signal family the entry keys on (the indicator(s) named
    in the predicate, or ``"price_level"`` for a pure price/threshold compare),
    suffixed ``_crossover`` when the comparison is a cross. Within one predicate
    the two sides' indicators are ``+``-joined (e.g. ``"ema+sma_crossover"`` for
    an EMA/SMA cross); multiple entry rules are ``,``-joined into the sorted set
    of their distinct archetypes (e.g. ``"macd,rsi"``). Using two separators
    keeps the per-predicate grouping unambiguous when rules are combined — e.g.
    ``"ema+sma_crossover,rsi"`` reads as one EMA/SMA cross plus a separate RSI
    rule, not three loose tokens.

    The signal family can sit on *either* side of the predicate: ``rsi < 30``
    keys on the left-hand side, while the prompt-recommended
    ``bar.close cross_above ema`` keys on the right. Both sides are inspected so
    EMA/SMA/VWAP breakouts written in the latter form keep their indicator
    family instead of all collapsing into ``"price_level"``.

    A multi-confirmation ``when`` (an ``all_of`` / ``any_of`` tree) is flattened
    to its leaf predicates: the indicator families across **all** legs are
    gathered (e.g. ``"rsi+sma"`` for a trend ∧ pullback entry) and the
    ``_crossover`` suffix applies when **any** leg is a cross. Without this, a
    combinator entry would carry no top-level ``lhs``/``op`` and collapse to
    ``"unknown"``, corrupting prior-result attribution for the very
    multi-confirmation strategies this bucketing is meant to compare.

    Preconditions:
      - ``strategy`` exposes an ``entry_rules`` iterable; each rule exposes a
        ``when`` that is a single ``Predicate`` or an ``all_of`` / ``any_of``
        tree of them, with sides that are each an ``IndicatorRef`` (carrying
        ``name``), a price-ref ``str``, or a numeric threshold. Missing/odd
        shapes degrade to ``"unknown"`` rather than raising — this is
        prompt-context formatting, never a correctness gate.
    Postconditions:
      - Returns a non-empty string. No entry rules → ``"none"``.
    """
    rules = list(getattr(strategy, "entry_rules", None) or [])
    if not rules:
        return "none"
    tokens: set[str] = set()
    for rule in rules:
        when = getattr(rule, "when", None)
        leaves = _leaf_predicates(when)
        if leaves:
            sides = [side for leaf in leaves for side in (leaf.lhs, leaf.rhs)]
            crossover = any(
                str(getattr(leaf, "op", "")) in ("cross_above", "cross_below") for leaf in leaves
            )
        else:
            # Unrecognised / malformed ``when`` — degrade exactly as before,
            # reading whatever top-level fields it happens to carry.
            sides = [getattr(when, "lhs", None), getattr(when, "rhs", None)]
            crossover = str(getattr(when, "op", "")) in ("cross_above", "cross_below")
        names = sorted({str(n) for s in sides if (n := getattr(s, "name", None))})
        if names:
            base = "+".join(names)
        elif any(isinstance(s, str) or isinstance(s, (int, float)) for s in sides):
            base = "price_level"
        else:
            base = "unknown"
        if crossover:
            base = f"{base}_crossover"
        tokens.add(base)
    # ``,`` between rules, ``+`` within a predicate (set above) — distinct
    # separators so a combined label stays unambiguous about which indicators
    # share a predicate.
    return ",".join(sorted(tokens))


def _exit_archetypes(strategy: object) -> List[str]:
    """Classify a strategy's exit rules into the distinct exit-type labels present.

    A record contributes to *each* exit bucket it uses, so a spec carrying both a
    trailing stop and a take-profit is counted under both — enabling
    "trailing stops vs fixed take-profits" comparisons in the attribution.

    Label map: ``stop_loss`` with a trailing ``basis`` → ``"trailing_stop"``,
    ``stop_loss`` on ``entry_price`` → ``"fixed_stop"``, ``take_profit`` →
    ``"take_profit"``, ``scaled_take_profit`` → ``"scaled_tp"``, ``signal_exit``
    → ``"signal_exit"``; any other/unknown ``kind`` passes through verbatim.

    Preconditions:
      - ``strategy`` exposes an ``exit_rules`` iterable; each rule exposes a
        ``kind`` (and, for stops, a ``basis``). Odd shapes degrade gracefully.
    Postconditions:
      - Returns a sorted list of distinct labels. No exit rules → ``["none"]``.
    """
    rules = list(getattr(strategy, "exit_rules", None) or [])
    if not rules:
        return ["none"]
    labels: set[str] = set()
    for rule in rules:
        kind = str(getattr(rule, "kind", "") or "unknown")
        if kind == "stop_loss":
            basis = str(getattr(rule, "basis", "") or "")
            labels.add("trailing_stop" if basis.startswith("trailing") else "fixed_stop")
        elif kind == "take_profit":
            labels.add("take_profit")
        elif kind == "scaled_take_profit":
            labels.add("scaled_tp")
        elif kind == "signal_exit":
            labels.add("signal_exit")
        else:
            labels.add(kind)
    return sorted(labels)


def _executed_records(
    records: List[StrategyLabRecord], *, max_records: int, cache: Optional[dict] = None
) -> List[StrategyLabRecord]:
    """The last ``max_records`` executed records, in chronological order.

    Drops pre-backtest short-circuits with :func:`_is_executed_record` and
    *then* tail-trims — the same order as :func:`asset_class_mix_hint` — so a
    window full of recent non-executed rows never crowds out older real
    backtests. (Trimming first, then filtering, would let 50 recent
    short-circuits hide every executed run behind them.)

    ``cache`` (optional) is a caller-owned dict that memoizes the sorted +
    filtered (pre-trim) executed-records list per ``id(records)``, so several
    calls against the *same* ``records`` object — with the same or a
    different ``max_records`` window — within one caller-defined scope (e.g.
    a single ``DesignAgent.run()`` invocation) share one O(N log N) sort +
    O(N) filter pass instead of repeating it. ``None`` (default) disables
    memoization and preserves the historical always-recompute behavior.

    Preconditions: ``max_records >= 0``. When ``cache`` is provided, callers
    must not mutate ``records`` while the cache is in scope.
    Postconditions: returns the last ``max_records`` records satisfying
    :func:`_is_executed_record`, in ``created_at`` order
    (``max_records == 0`` → empty list). Identical result whether or not
    ``cache`` is supplied.
    """
    if cache is None:
        ordered = sorted(records, key=lambda x: x.created_at)
        executed = [r for r in ordered if _is_executed_record(r)]
    else:
        key = id(records)
        executed = cache.get(key)
        if executed is None:
            ordered = sorted(records, key=lambda x: x.created_at)
            executed = [r for r in ordered if _is_executed_record(r)]
            cache[key] = executed
    # ``executed[-0:]`` is ``executed[0:]`` (the whole list), so the zero case
    # must be handled explicitly rather than via the slice.
    return executed[-max_records:] if max_records else []


def _has_parseable_design(strategy: object) -> bool:
    """True when a strategy's structured rules can be meaningfully bucketed.

    Legacy pre-migration specs stored prose entry/exit rules; on load,
    ``_coerce_legacy_strategy_spec_dict`` moves them into ``unparsed_rules`` and
    sets ``requires_redesign=True``, leaving ``entry_rules``/``exit_rules`` empty
    and the sizing at its schema default. Attributing such a record's real
    returns to ``entry:none`` / ``exit:none`` / the default sizing bucket would
    tell the designer that "none" is a winning archetype rather than that the
    design is simply unknown — so these records are excluded from the
    entry/exit/sizing dimensions (their genuine ``asset_class`` still counts).

    Preconditions: ``strategy`` may expose ``requires_redesign`` (bool) and
    ``unparsed_rules`` (list); absent attributes are treated as parseable.
    Postconditions: returns ``False`` iff the spec requires redesign or carries
    any unparsed rules.
    """
    return not getattr(strategy, "requires_redesign", False) and not getattr(
        strategy, "unparsed_rules", None
    )


def aggregate_prior_results(
    records: List[StrategyLabRecord], *, max_records: int = 50, cache: Optional[dict] = None
) -> dict[tuple[str, str], dict]:
    """Aggregate prior lab records into per-dimension performance attribution.

    Buckets the executed records *marginally* along four independent dimensions —
    ``asset_class``, ``entry`` archetype, ``exit`` archetype, and ``sizing`` kind —
    and reports the mean win rate, mean annualized return, and sample size for each
    bucket value. Marginal (rather than composite 4-tuple) bucketing keeps the
    samples large enough to be informative on a diverse history.

    ``cache`` (optional) is forwarded to :func:`_executed_records` to memoize
    the sort/filter pass across calls sharing the same ``records`` object; see
    its docstring for scoping details.

    Preconditions:
      - ``records`` is a list of ``StrategyLabRecord``; ``max_records >= 0``.
    Postconditions:
      - Returns ``{(dimension, value): {"win_rate", "annual_return", "n",
        "publishable_n", "publishable_win_rate", "publishable_annual_return"}}``.
      - Empty / all-non-executed / ``max_records == 0`` input → ``{}``.
      - Every value dict has ``n >= 1`` and means equal to the arithmetic mean of
        the contributing records' ``win_rate_pct`` / ``annualized_return_pct``.
      - ``publishable_n`` counts the contributing records that are
        ``is_publishable``; ``publishable_win_rate``/``publishable_annual_return``
        are the same means restricted to that subset, or ``None`` when
        ``publishable_n == 0`` (never a misleading 0.0).
      - A record with a parseable design contributes to exactly one
        ``asset_class``/``entry``/``sizing`` bucket and to one ``exit`` bucket
        per distinct exit type it uses. A redesign-pending / unparsed-rules
        record contributes to its ``asset_class`` bucket only (see
        :func:`_has_parseable_design`).
    """
    executed = _executed_records(records, max_records=max_records, cache=cache)
    if not executed:
        return {}

    # bucket_key -> [sum_win_rate, sum_annual_return, n, pub_sum_win_rate, pub_sum_annual_return, pub_n]
    acc: dict[tuple[str, str], list[float]] = {}

    def _add(
        dimension: str, value: str, win_rate: float, annual_return: float, is_publishable: bool
    ) -> None:
        slot = acc.setdefault((dimension, value), [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        slot[0] += win_rate
        slot[1] += annual_return
        slot[2] += 1.0
        if is_publishable:
            slot[3] += win_rate
            slot[4] += annual_return
            slot[5] += 1.0

    for r in executed:
        res = r.backtest.result
        win = float(res.win_rate_pct)
        ann = float(res.annualized_return_pct)
        strat = r.strategy
        publishable = bool(r.is_publishable)
        # asset_class is genuine even for legacy redesign-pending rows, so it
        # always counts; the structured design dimensions only count when the
        # spec actually carries parseable rules (see _has_parseable_design).
        _add("asset_class", normalize_asset_class(strat.asset_class), win, ann, publishable)
        if _has_parseable_design(strat):
            _add("entry", _entry_archetype(strat), win, ann, publishable)
            _add(
                "sizing",
                str(getattr(strat.sizing, "kind", "") or "unknown"),
                win,
                ann,
                publishable,
            )
            for exit_label in _exit_archetypes(strat):
                _add("exit", exit_label, win, ann, publishable)

    return {
        key: {
            "win_rate": sw / n,
            "annual_return": sa / n,
            "n": int(n),
            "publishable_n": int(pn),
            "publishable_win_rate": (pw / pn) if pn else None,
            "publishable_annual_return": (pa / pn) if pn else None,
        }
        for key, (sw, sa, n, pw, pa, pn) in acc.items()
    }


_ATTRIBUTION_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("asset_class", "Asset class"),
    ("entry", "Entry archetype"),
    ("exit", "Exit type"),
    ("sizing", "Position sizing"),
)


def format_prior_attribution(
    records: List[StrategyLabRecord],
    *,
    max_records: int = 50,
    thin_n: int = 3,
    cache: Optional[dict] = None,
) -> str:
    """Render per-dimension attribution as a compact "what has worked" digest.

    Wraps :func:`aggregate_prior_results`, grouping buckets by dimension and
    sorting each group by mean annualized return (descending) so the
    highest-scoring regions of the design space lead. Every line shows the sample
    size ``n`` — and a ``(thin sample)`` flag below ``thin_n`` — so the designer
    can exploit strong buckets without over-fitting to a single record.

    ``cache`` (optional) is forwarded to :func:`aggregate_prior_results` to
    memoize the underlying sort/filter pass across calls sharing the same
    ``records`` object.

    Preconditions: ``records`` is a list of ``StrategyLabRecord``; ``thin_n >= 1``.
    Postconditions:
      - Returns a non-empty string. Empty / all-non-executed input → a short
        "not enough history" sentinel.
      - Every rendered bucket line contains its ``n=`` sample size.
    """
    assert thin_n >= 1, f"thin_n must be >= 1, got {thin_n}"
    agg = aggregate_prior_results(records, max_records=max_records, cache=cache)
    if not agg:
        return "Not enough executed history yet to attribute performance."

    # Group buckets by dimension in a single pass over ``agg`` rather than
    # re-scanning it once per dimension.
    by_dim: dict[str, list[tuple[str, dict]]] = {}
    for (dim, value), stats in agg.items():
        by_dim.setdefault(dim, []).append((value, stats))

    sections: List[str] = []
    for dim_key, dim_label in _ATTRIBUTION_DIMENSIONS:
        rows = by_dim.get(dim_key)
        if not rows:
            continue
        rows.sort(key=lambda kv: kv[1]["annual_return"], reverse=True)
        lines = [f"- {dim_label}:"]
        for value, stats in rows:
            thin = "  (thin sample)" if stats["n"] < thin_n else ""
            lines.append(
                f"    - {value}: win {stats['win_rate']:.1f}%, "
                f"annual {stats['annual_return']:.1f}%, n={stats['n']}{thin}"
            )
        sections.append("\n".join(lines))
    return "\n".join(sections)


def _or_join(items: List[str]) -> str:
    """Render a list as an Oxford-style ``a, b, or c`` menu (single item → itself)."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + ", or " + items[-1]


def _edge_exploitation_steer(
    records: List[StrategyLabRecord],
    allowed: List[str],
    menu: str,
    *,
    tail: int,
    cache: Optional[dict] = None,
) -> str:
    """Steer toward the allowed asset class with the best demonstrated *robust* edge.

    The objective-aware counterpart to the diversity nudge. Ranks the marginal
    ``asset_class`` attribution buckets from :func:`aggregate_prior_results`
    (restricted to ``allowed``) in two tiers: any bucket with at least one
    ``is_publishable`` record outranks every bucket with none, so a class whose
    apparent edge is only backed by overfit or unrealistic "wins" never beats a
    class with genuine robust evidence — regardless of raw return. Within each
    tier, buckets are ranked by mean annualized return (publishable-only mean
    for the first tier, raw mean for the second), then mean win rate as the
    dual-objective tie-break, and the leader is named so the designer leans
    into its edge rather than rotating away from it.

    The edge map is bounded to the same ``tail`` window of executed records that
    the caller's recent-counts line uses, so the steering can never name a class
    that has dropped out of the recent window the counts report.

    ``cache`` (optional) is forwarded to :func:`aggregate_prior_results` to
    memoize the underlying sort/filter pass across calls sharing the same
    ``records`` object.

    Preconditions: ``allowed`` is non-empty; ``menu`` is its rendered list;
    ``tail >= 1``.
    Postconditions: returns a non-empty string. When no per-class edge is
    attributable yet (legacy / unparsed history), returns neutral menu text
    rather than fabricating a preference. When no in-bounds bucket has any
    publishable evidence, the message text is identical to the raw-stats-only
    behavior (no publishable-evidence framing is fabricated).
    """
    assert allowed, "allowed must be non-empty"
    assert menu, "menu must be provided"
    assert tail >= 1, f"tail must be >= 1, got {tail}"
    agg = aggregate_prior_results(records, max_records=tail, cache=cache)
    buckets = [
        (value, stats)
        for (dim, value), stats in agg.items()
        if dim == "asset_class" and value in allowed
    ]
    if not buckets:
        return (
            f"No per-class edge attributable yet — choose **asset_class** from {menu}: "
            "pick the class that best fits your strongest multi-signal edge."
        )

    def _rank_key(item: tuple[str, dict]) -> tuple:
        _, stats = item
        if stats["publishable_n"]:
            return (1, stats["publishable_annual_return"], stats["publishable_win_rate"])
        return (0, stats["annual_return"], stats["win_rate"])

    buckets.sort(key=_rank_key, reverse=True)
    top_value, top_stats = buckets[0]
    if top_stats["publishable_n"]:
        return (
            "Objective is return/win-rate — **lean into your demonstrated robust edge**: "
            f"{top_value} scores best among publishable wins (annual "
            f"{top_stats['publishable_annual_return']:.1f}%, win "
            f"{top_stats['publishable_win_rate']:.1f}%, n={top_stats['publishable_n']} "
            f"publishable of {top_stats['n']} total). Prefer the highest-scoring "
            "publishable-backed class when a coherent thesis fits rather than rotating "
            "away from it; pick another class only when its robust edge is clearly stronger."
        )
    return (
        "Objective is return/win-rate — **lean into your demonstrated edge**: "
        f"{top_value} scores best so far (annual {top_stats['annual_return']:.1f}%, "
        f"win {top_stats['win_rate']:.1f}%, n={top_stats['n']}). Prefer the "
        "highest-scoring class when a coherent thesis fits rather than rotating away "
        "from it; pick another class only when your edge there is clearly stronger."
    )


def asset_class_mix_hint(
    records: List[StrategyLabRecord],
    *,
    tail: int = 24,
    exclude: Optional[List[str]] = None,
    mode: str = "explore",
    cache: Optional[dict] = None,
) -> str:
    """Steer the LLM's asset-class choice, objective-aware.

    ``mode`` selects how the hint steers once executed history exists:

    - ``"exploit"`` — the run's objective is to maximize return/win rate, so
      steer **toward** the asset class with the best demonstrated edge (highest
      mean annualized return, win rate as the dual-objective tie-break) drawn
      from :func:`aggregate_prior_results`, rather than rotating away from a
      class the agent is winning in.
    - ``"explore"`` (default) — portfolio-diversity steering: nudge **away**
      from an over-represented class (the anti-equities concentration nudge) and
      toward the least-used / underrepresented classes. The default preserves
      the historical hint for callers that have not opted into objective-aware
      steering; the design agent passes ``exploit`` explicitly via its own
      ``STRATEGY_LAB_DIVERSITY_MODE`` resolution.

    ``exclude`` (optional) names asset classes the design agent is forbidden to
    pick this run — the complement of a user's allowed-category selection. It is
    typed ``List[str]`` (not ``Iterable[str]``) deliberately: ``str`` is itself
    iterable, so an ``Iterable[str]`` annotation would silently accept a bare
    string and iterate its characters. When provided, the menu, recent-class
    counts, and all steering (both modes) are restricted to the still-allowed
    classes so the hint never nudges the model toward a class the run is not
    permitted to use. When ``exclude`` is ``None`` / empty the output spans the
    full ideation-valid set.

    ``cache`` (optional) is forwarded to :func:`_executed_records` and
    :func:`_edge_exploitation_steer` to memoize the underlying sort/filter
    pass across calls sharing the same ``records`` object.

    Preconditions:
      - ``mode in {"exploit", "explore"}`` (callers resolve unknown values to a
        default before calling).
    Postconditions:
      - Returns a non-empty string. With no records / no executed backtests the
        output is the neutral menu text, identical across modes (no edge or
        concentration to steer on yet).
      - ``explore`` output is identical to the historical diversity hint.
      - ``exploit`` output never emits the anti-equities rotation nudge; it
        names the highest-scoring allowed class when an attributable per-class
        edge exists, else stays neutral.
    """
    assert mode in ("exploit", "explore"), f"mode must be 'exploit' or 'explore', got {mode!r}"
    allowed = [c for c in PROMPT_ASSET_CLASSES if c not in set(exclude or ())]
    if not allowed:
        # Defensive: an exclusion covering every class would leave nothing to
        # steer toward. The API boundary rejects an empty allowed set, so this
        # only guards against a misuse from internal callers.
        allowed = list(PROMPT_ASSET_CLASSES)
    menu = _or_join(allowed)
    # The anti-stocks-bias nudge only makes sense when stocks is still a valid
    # choice; drop it when stocks has been excluded so the hint never references
    # a class the run cannot use. Unconstrained runs keep stocks, so their
    # output is unchanged.
    stocks_nudge = "do **not** default to stocks; " if "stocks" in allowed else ""
    if not records:
        return (
            "No prior lab strategies. Choose **asset_class** from "
            f"{menu} with similar frequency over time — "
            f"{stocks_nudge}pick the class that best fits your multi-signal story."
        )

    # Steer on the most recent executed backtests only. ``_executed_records``
    # drops cycles that short-circuited *before* running a backtest (their
    # ``strategy.asset_class`` may be a coerced placeholder like ``bonds`` →
    # ``stocks`` that would pollute the diversity picture) and tail-trims to the
    # window afterwards. Executed-but-losing cycles (status ``failed`` /
    # ``failed: max_refinement_rounds``) DID run a real backtest with a genuine
    # canonical class and keep counting; legacy rows without a status default to
    # ``"completed"`` and are unaffected.
    sample = _executed_records(records, max_records=tail, cache=cache)
    if not sample:
        return (
            "No executed lab backtests yet. Choose **asset_class** from "
            f"{menu} with similar frequency over time — "
            f"{stocks_nudge}pick the class that best fits your multi-signal story."
        )
    # #535: count only asset classes the LLM may still target. 'options' is
    # rejected by StrategySpecValidator, so leaving it in the count dict
    # would push it into ``underrep`` whenever no options strategies have
    # run and steer the LLM toward a guaranteed-failure choice. Excluded
    # classes are likewise dropped so the counts/steering only span the
    # categories this run is allowed to generate.
    counts = {c: 0 for c in allowed}
    for r in sample:
        k = normalize_asset_class(r.strategy.asset_class)
        if k in counts:
            counts[k] += 1
        elif k not in PROMPT_ASSET_CLASSES and "stocks" in counts:
            # Only a class outside the ideation-valid set (``options``, or an
            # unknown coerced by ``normalize_asset_class``) folds into stocks,
            # and only when stocks is still an allowed target. A class that is a
            # valid ideation target but *excluded* this run is deliberately
            # skipped — it is outside the steering window, so counting it (as
            # stocks or anything else) would fabricate the diversity picture.
            counts["stocks"] += 1

    n_sample = len(sample)
    parts: List[str] = [
        "Recent asset-class counts (last "
        f"{n_sample} strategies): " + ", ".join(f"{k}={v}" for k, v in counts.items()) + "."
    ]
    if mode == "explore":
        # Portfolio-diversity steering: push away from an over-represented class
        # and toward the least-used ones.
        stock_share = counts.get("stocks", 0) / n_sample if n_sample else 0.0
        min_n = min(counts.values())
        underrep = [c for c, n in counts.items() if n == min_n]
        if "stocks" in counts and stock_share > _STOCK_CONCENTRATION_THRESHOLD and n_sample >= 2:
            non_stock = [c for c in allowed if c != "stocks"]
            if non_stock:
                parts.append(
                    "Equities are relatively heavy in this window — **strongly prefer** "
                    f"{_or_join(non_stock)} for this run if you can state coherent rules."
                )
        parts.append(
            "Underrepresented line(s) to favor when ties: "
            f"{', '.join(underrep)} — use one of these **unless** your thesis clearly requires a different class."
        )
        return " ".join(parts)

    # mode == "exploit": objective is return/win-rate, so steer toward the
    # class with the best demonstrated edge instead of forcing rotation. Bound
    # the edge map to the same ``tail`` window as the recent-counts line above
    # so the steering can never name a class that has dropped out of it.
    parts.append(_edge_exploitation_steer(records, allowed, menu, tail=tail, cache=cache))
    return " ".join(parts)
