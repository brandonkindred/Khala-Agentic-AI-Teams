"""Deterministic, semantics-preserving repair of mechanical readiness failures.

The design ↔ design-review loop runs :class:`SpecReadinessGate` at the top of
every round. When the gate emits a *critical* finding the loop otherwise spends
a full LLM ``DesignAgent.revise`` round to fix it — even when the violation is
purely mechanical and fully determined by the spec. This module performs those
fixes deterministically *before* the LLM revise path so the costly loop is
reserved for genuinely substantive defects.

Scope is deliberately minimal — only the two least-debatable, fully-determined
mechanical repairs, exposed via :func:`repair_spec`:

* **Timeframe data availability** (readiness Rule 7): coerce an intraday
  ``timeframe`` to ``"1d"`` for asset classes with no intraday data (anything
  not in :data:`spec_readiness._FULL_TIMEFRAME_ASSET_CLASSES`).
* **Position-cap bound** (readiness Rule 8): clamp
  ``risk_limits.max_position_pct`` down to
  :data:`spec_readiness.MAX_POSITION_PCT_CEILING`.
* **Asset-category pin, symbol half** (readiness Rule 11): when the caller
  passes ``pinned_asset_class`` and the spec *already declares* that class,
  drop any ``target_symbols`` entry that unambiguously belongs to a different
  class — but only when at least one symbol survives the drop. Stripping
  *every* symbol is left unrepaired (Rule 11's critical stays live): an
  empty result would fall back to the pinned class's full default universe,
  silently laundering a hypothesis whose entire named universe contradicts
  its own declared class into a readiness-clean backtest over unrelated
  tickers. The class half of Rule 11 — a spec declaring the *wrong*
  category — is deliberately **not** repaired here either: rewriting
  ``asset_class`` in place would relabel a crypto strategy as a stocks one
  while leaving crypto logic intact, which is precisely the cross-category
  contamination the pin exists to prevent. Both violations stay a readiness
  critical so the design loop's own ``revise`` round rebuilds the strategy
  for the pinned category.

The custom-code decision is a separate, *readiness-gated* concern handled by
:func:`select_code_path`: it trial-compiles a spec and, on :class:`CompilerError`,
reports that ``requires_custom_code`` should be set so the custom-code path is
selected during design rather than discovered later in synthesis (mirroring the
orchestrator's existing synthesis-phase fallback). It is kept out of
:func:`repair_spec` because the compiler assumes structurally valid DSL — a
readiness-defective spec can make ``compile_strategy`` raise a *non*-``CompilerError``
— so callers must only invoke it once the readiness gate reports no criticals.

Each mechanical repair is guarded by exactly the condition its readiness rule
checks, recomputed from the spec via the gate's own shared constants — never
parsed from gate-message text — so the rule and its repair cannot drift apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..models import BacktestConfig, StrategySpec
from ..strategy_lab_context import normalize_asset_class, normalize_asset_class_strict
from ..symbols import COINGECKO_IDS, find_offcategory_symbols
from .quality_gates.spec_readiness import (
    _FULL_TIMEFRAME_ASSET_CLASSES,
    MAX_POSITION_PCT_CEILING,
)
from .synthesis import CompilerError, compile_strategy


@dataclass(frozen=True)
class RepairAction:
    """One deterministic spec edit, recorded for the audit trail.

    Invariants:
      - ``rule`` names the mechanical class; ``field`` names the spec field
        changed; ``before`` / ``after`` are the field values around the edit.
    """

    rule: str
    field: str
    before: Any
    after: Any
    reason: str


@dataclass(frozen=True)
class RepairOutcome:
    """Result of :func:`repair_spec`.

    Invariants:
      - ``actions == []`` ⇔ ``spec`` is the (unchanged) input instance.
      - ``actions`` non-empty ⇒ ``spec`` is a new ``StrategySpec`` carrying
        every recorded edit.
    """

    spec: StrategySpec
    actions: List[RepairAction] = field(default_factory=list)


def _symbol_named_in_thesis(symbol: str, *thesis_text: str) -> bool:
    """True if ``symbol`` (or its bare root, e.g. ``"DOGE"`` for
    ``"DOGE-USDT"``) — or, for a crypto root, its common English name (e.g.
    ``"Bitcoin"`` for ``"BTC"``) — is mentioned by name in ``thesis_text``.

    Preconditions:
      - ``symbol`` is a ticker string; ``thesis_text`` are free-text spec
        fields (``hypothesis``, ``signal_definition``) that may be empty.
    Postconditions:
      - Returns whether the symbol's root (suffix stripped), or a known
        crypto common name derived from :data:`symbols.COINGECKO_IDS` for
        that root, appears as a whole word, case-insensitively, in the
        joined text. A thesis naming an off-category asset by its plain
        name rather than its ticker (e.g. "Bitcoin momentum" targeting
        ``BTC-USD``) would otherwise go undetected and the symbol would be
        silently stripped as an unrelated stray ticker. Still purely a
        lexical check — no semantic understanding of whether the thesis
        actually depends on the symbol, and non-crypto common names (e.g.
        "Gold" for ``GLD``) aren't covered since no equivalent name table
        exists for those asset classes.
    """
    root = re.split(r"[-=]", symbol, maxsplit=1)[0]
    if not root:
        return False
    candidates = {root}
    common_name = COINGECKO_IDS.get(root.upper())
    if common_name:
        # A CoinGecko id can carry a disambiguating suffix (e.g.
        # "avalanche-2") or be a hyphenated compound (e.g. "matic-network");
        # only the leading alpha segment is a plausible English name a
        # hypothesis would actually use.
        alpha_name = re.match(r"[a-zA-Z]+", common_name)
        if alpha_name:
            candidates.add(alpha_name.group(0))
    text = " ".join(thesis_text)
    return any(re.search(r"\b" + re.escape(c) + r"\b", text, re.IGNORECASE) for c in candidates)


def repair_spec(
    spec: StrategySpec,
    *,
    config: Optional[BacktestConfig] = None,
    pinned_asset_class: Optional[str] = None,
) -> RepairOutcome:
    """Apply the in-scope deterministic *mechanical* repairs to ``spec``.

    This function only ever applies fully-determined readiness repairs
    (timeframe data availability, position-cap bound). It deliberately does
    **not** trial-compile: a spec may still carry a readiness-detectable
    structural defect that would make :func:`compile_strategy` raise a
    *non*-``CompilerError``, so the trial compile lives in
    :func:`select_code_path`, which callers invoke only once the spec is
    readiness-clean.

    Preconditions:
      - ``spec`` is a constructed :class:`StrategySpec`.
      - ``config`` is a :class:`BacktestConfig` or ``None`` (accepted for a
        uniform call site; the current repairs do not consult it).
      - ``pinned_asset_class`` is a canonical asset-class label naming the
        single category the caller's design attempt is restricted to, or
        ``None`` when no pin applies.

    Postconditions:
      - Returns a :class:`RepairOutcome`. When no repair applies, the input
        ``spec`` is returned unchanged and ``actions`` is empty.
      - Never raises on the spec's compilability — only structural readiness
        fields are touched, so this is safe to call on a readiness-critical spec.
      - Every applied repair targets exactly the condition its readiness rule
        rejects, so re-running ``repair_spec`` on the result yields
        ``actions == []`` (idempotent) — barring external state changes.
      - The returned spec is never mutated in place; edits are made on a deep
        copy.
      - ``spec.asset_class`` is never rewritten, with or without a pin — see
        the module docstring for why the class half of Rule 11 is left to the
        readiness-critique path.
    """
    assert isinstance(spec, StrategySpec), "spec must be a StrategySpec"
    assert config is None or isinstance(config, BacktestConfig), (
        "config must be a BacktestConfig or None"
    )

    actions: List[RepairAction] = []
    updates: dict[str, Any] = {}

    # --- Rule 7: intraday timeframe on an asset class with no intraday data.
    if spec.timeframe != "1d":
        try:
            canonical = normalize_asset_class_strict(spec.asset_class)
        except ValueError:
            # Unknown asset_class — Rule 5 owns that critical; not a timeframe
            # repair. Leave the timeframe untouched.
            canonical = None
        if canonical is not None and canonical not in _FULL_TIMEFRAME_ASSET_CLASSES:
            actions.append(
                RepairAction(
                    rule="timeframe_data_availability",
                    field="timeframe",
                    before=spec.timeframe,
                    after="1d",
                    reason=(
                        f"asset_class '{spec.asset_class}' has no reliable intraday "
                        f"data for timeframe '{spec.timeframe}'; coerced to '1d'."
                    ),
                )
            )
            updates["timeframe"] = "1d"

    # --- Rule 8: single-position risk budget over the ceiling.
    if spec.risk_limits.max_position_pct > MAX_POSITION_PCT_CEILING:
        actions.append(
            RepairAction(
                rule="max_position_pct_cap",
                field="risk_limits.max_position_pct",
                before=spec.risk_limits.max_position_pct,
                after=MAX_POSITION_PCT_CEILING,
                reason=(
                    f"max_position_pct={spec.risk_limits.max_position_pct:g}% exceeds the "
                    f"{MAX_POSITION_PCT_CEILING:g}% cap; clamped to the ceiling."
                ),
            )
        )
        updates["risk_limits"] = spec.risk_limits.model_copy(
            update={"max_position_pct": MAX_POSITION_PCT_CEILING}
        )

    # --- Rule 11 (symbol half): stray off-category tickers under a pin.
    # Only applies once the spec already declares the pinned class — a spec
    # declaring the wrong class is a readiness critical the design loop's
    # revise round owns, and stripping symbols from it would destroy the very
    # evidence the reviser needs to rebuild the strategy.
    if (
        pinned_asset_class is not None
        and normalize_asset_class(spec.asset_class) == pinned_asset_class
    ):
        # ``find_offcategory_symbols`` (shared with readiness Rule 11's own
        # symbol check, so the two can never disagree) excludes cross-asset
        # ETFs (GLD, QQQ, ...) that legitimately trade in more than one
        # category; only an unambiguous mismatch is stripped.
        offcategory = find_offcategory_symbols(spec.target_symbols, pinned_asset_class)
        # A removed symbol the hypothesis/signal_definition names explicitly
        # (e.g. a "DOGE momentum" thesis targeting ["AAPL", "DOGE-USDT"]) is
        # not a stray ticker to drop — stripping it would leave the pinned
        # class's spec still carrying, and about to backtest, an off-category
        # thesis under a now readiness-clean symbol list. Treat any such
        # symbol as if it were the whole universe: force a rebuild instead of
        # a silent partial strip.
        thesis_bound = {
            sym
            for sym in offcategory
            if _symbol_named_in_thesis(sym, spec.hypothesis, spec.signal_definition)
        }
        kept = [sym for sym in spec.target_symbols if sym not in offcategory or sym in thesis_bound]
        # Only repair a MINORITY mismatch — at least one symbol must survive.
        # Stripping every symbol is not "a stray ticker"; it means the whole
        # explicit universe contradicts the declared class, which is the same
        # mislabeling signal ``build_spec_from_dict``'s symbol-inference guards
        # against on the omitted-``asset_class`` path. An empty result falls
        # back to the pinned class's FULL default universe — silently
        # laundering an (e.g.) crypto-themed hypothesis into a backtest over
        # unrelated stock tickers, recorded as a valid, readiness-clean
        # strategy. Leaving ``target_symbols`` untouched here instead keeps
        # readiness Rule 11's symbol critical alive, so the round routes
        # through a real critique and a full rebuild rather than a silent
        # mechanical erasure.
        if kept and len(kept) != len(spec.target_symbols):
            actions.append(
                RepairAction(
                    rule="asset_category_pin_symbols",
                    field="target_symbols",
                    before=list(spec.target_symbols),
                    after=kept,
                    reason=(
                        f"target_symbols contained tickers outside the pinned asset "
                        f"category '{pinned_asset_class}'; dropped them so the backtest "
                        f"universe matches the declared class."
                    ),
                )
            )
            updates["target_symbols"] = kept

    if not actions:
        return RepairOutcome(spec=spec, actions=[])
    return RepairOutcome(spec=spec.model_copy(update=updates, deep=True), actions=actions)


def select_code_path(spec: StrategySpec) -> Optional[RepairAction]:
    """Trial-compile ``spec`` to decide whether it needs the custom-code path.

    Preconditions:
      - ``spec`` is structurally valid (readiness-clean). The deterministic
        compiler assumes valid DSL; a spec with a readiness-detectable defect
        (e.g. an ``sma`` ref whose required ``period`` was removed) can make
        :func:`compile_strategy` raise a *non*-``CompilerError`` such as
        ``TypeError``. Callers running inside the design loop must therefore only
        invoke this once the readiness gate reports no criticals — a residual
        critical is left to the readiness-critique / revise path.

    Postconditions:
      - Returns a ``compiler_fallback`` :class:`RepairAction` (the caller should
        set ``requires_custom_code=True``) when the spec is outside the
        deterministic-compiler envelope; returns ``None`` when the spec compiles
        or already has ``requires_custom_code`` set.
    """
    assert isinstance(spec, StrategySpec), "spec must be a StrategySpec"
    if spec.requires_custom_code:
        return None
    try:
        compile_strategy(spec)
    except CompilerError as exc:
        return RepairAction(
            rule="compiler_fallback",
            field="requires_custom_code",
            before=False,
            after=True,
            reason=(
                "spec falls outside the deterministic compiler envelope "
                f"({exc}); routing to the custom-code synthesis path."
            ),
        )
    return None


def demote_code_path(spec: StrategySpec) -> Optional[RepairAction]:
    """Trial-compile a ``requires_custom_code`` spec to see whether Path A suffices.

    The inverse of :func:`select_code_path`. The deterministic compiler covers the
    entire indicator catalogue, every operator/source, and ``all_of``/``any_of``
    predicate trees, so a confirmation-stacked entry the LLM flagged as custom code
    is almost always expressible on the faithful compiled path. The compiled path
    decides every entry/exit engine-side straight from the spec, so it cannot drift
    on indicator ``source``, bar indexing, or a falsy volume guard the way
    hand/LLM-authored ``on_bar`` code can. When such a spec compiles cleanly,
    custom code buys nothing and only widens that drift surface — demote it.

    Preconditions:
      - ``spec`` is structurally valid (readiness-clean) — same gating as
        :func:`select_code_path`; a readiness-defective spec can make
        ``compile_strategy`` raise a non-``CompilerError``.

    Postconditions:
      - Returns a ``compiler_demote`` :class:`RepairAction` (the caller should set
        ``requires_custom_code=False``) when ``spec.requires_custom_code`` is set
        AND the spec trial-compiles cleanly; returns ``None`` when the spec is
        already on the compiled path or the compiler raises ``CompilerError`` (the
        authoritative "the DSL cannot express this" signal — a genuinely
        cross-asset / path-dependent strategy stays on custom code).

    Note: the compiled path implements exactly the spec's DSL ``when``/exit rules.
    A caller that wants to preserve a lossy-but-compilable custom spec can gate
    this behind a toggle (the orchestrator does), but the default is to demote:
    an unfaithful compiled implementation of the declared rules is still a truer
    test of the specification than custom code that diverges from it.
    """
    assert isinstance(spec, StrategySpec), "spec must be a StrategySpec"
    if not spec.requires_custom_code:
        return None
    try:
        compile_strategy(spec)
    except CompilerError:
        return None  # genuinely outside the compiler envelope — keep custom code
    return RepairAction(
        rule="compiler_demote",
        field="requires_custom_code",
        before=True,
        after=False,
        reason=(
            "spec compiles cleanly on the deterministic path, so custom code is "
            "unnecessary; demoting to the faithful compiled path to eliminate the "
            "source / bar-indexing / falsy-guard drift surface of custom code."
        ),
    )
