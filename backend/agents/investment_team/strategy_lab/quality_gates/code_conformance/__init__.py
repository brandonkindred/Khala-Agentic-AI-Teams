"""Deterministic spec→code conformance gate (issue #541).

Runs immediately after :class:`CodeSafetyChecker` and before the first
sandbox execution. Fails ``severity="critical"`` when generated strategy
code does not implement what the spec declares — e.g. a spec rule
references an indicator that the code never calls, or an entry rule has
no corresponding code branch.

The post-hoc, LLM-driven :class:`TradeAlignmentAgent` can only reason
about rules that actually fired in the test window. This gate catches the
silent failure mode where a rule is never implemented and therefore
never fires at all.

Critical failures route back to synthesis via the orchestrator's
``_refine_or_exhaust(failure_phase="validation")`` path (same routing as
``CodeSafetyChecker``).

Scope choices:

* Check #1 (indicator presence) recognises two forms: the engine-backed
  ``ctx.indicator('<name>', ...)`` accessor (the form the synthesis prompt now
  mandates) and the legacy named call (e.g. ``sma(bars, 50)``, still emitted by
  the deterministic compiler). Inline equivalents (e.g. ``sum(x)/len(x)`` as a
  hand-rolled SMA) are deliberately not recognised and will fail — the prompt
  steers generated code onto the engine's indicator computation instead.
* Check #5 (bar-counting exit rejection) scans generated code for
  bar-counting exit patterns (variables like ``bars_held``,
  ``hold_count``, ``days_held`` and ``if counter >= N: close``
  idioms). These implement the forbidden "time stop" concept and are
  rejected with a critical finding.
* Check #2 (symbol gate) reuses the same AST helpers as
  ``CodeSafetyChecker._check_universe_guard``. The duplication is
  intentional defense-in-depth so this gate remains self-sufficient.

The free-function AST-analysis helpers this gate composes into checks live in
:mod:`ast_helpers`.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import cached_property
from typing import ClassVar, Iterable, List, Optional

from ...spec_dsl import EntryRule, SignalExitRule
from ..code_safety import _collect_position_aliases, _engine_exits_cover_sides
from ..code_safety_ast import (
    _find_strategy_subclasses,
    _has_universe_constant,
    _has_universe_guard_in_on_bar,
    _iter_method_body_nodes,
    parse_strategy_source,
)
from ..models import GateResultsMixin, QualityGateResult, StrategyLabPhase
from .ast_helpers import (
    _ALLOWED_HOOK_NAMES,
    _BOLLINGER_BASE_BANDS,  # noqa: F401 (re-exported for downstream imports)
    _BOLLINGER_DERIVED_BANDS,  # noqa: F401 (re-exported for downstream imports)
    _INDICATOR_ALLOWED_CALL_NAMES,
    _POSITION_SNAPSHOT_ATTRS,  # noqa: F401 (re-exported for downstream imports)
    _collect_called_names_in_methods,
    _collect_ctx_indicator_names,
    _collect_import_aliases,
    _collect_produced_bollinger_bands,
    _collect_required_indicators,
    _divergent_ctx_indicator_reads,
    _find_enclosing_funcdef,
    _find_if_branches_with_submit_order,
    _indicator_falsy_guard_errors,
    _invalid_bollinger_select_calls,
    _invalid_ctx_indicator_reads,
    _invalid_position_attr_errors,
    _is_engine_managed,
    _is_submit_order_call,
    _iter_strategy_methods,
    _methods_reachable_from_hooks,
    _methods_reachable_from_on_bar,
    _node_is_duplicate_close,
    _node_references_ctx_equity,
    _qty_is_constant_int,
    _required_bollinger_derived_bands,
    _submit_order_closes_position,
    _submit_order_has_side_literal,
    _submit_order_is_kwargs_spread,
    _undefined_self_indicator_helper_calls,
)

GATE = "code_conformance"


@dataclass(frozen=True)
class _ConformanceCtx:
    """Per-``check`` derived state, computed lazily and shared by every check.

    Several conformance checks need the same structural views of the Strategy
    class — the set of methods reachable from ``on_bar`` and the list of
    ``if`` branches that submit orders. Exposing them as memoised properties
    means each view is computed **at most once** per ``check`` (removing the
    duplicate ``_methods_reachable_from_on_bar`` / ``_find_if_branches_with_submit_order``
    walks) while still being skipped entirely when no check needs them — e.g.
    an engine-managed spec early-returns from entry/exit coverage before ever
    touching ``submit_branches``, matching the old per-check laziness.

    Invariants: ``reachable`` and ``submit_branches`` are derived purely from
    ``cls`` (read-only); ``submit_branches`` is filtered to ``reachable``
    methods, matching the previous per-check call ``_find_if_branches_with_submit_order(cls,
    reachable_method_names=reachable)``. ``cached_property`` writes into the
    instance ``__dict__`` — frozen-dataclass ``__setattr__`` is bypassed.
    """

    cls: ast.ClassDef
    spec: object

    @cached_property
    def reachable(self) -> frozenset[str]:
        return _methods_reachable_from_on_bar(self.cls)

    @cached_property
    def submit_branches(self) -> List[tuple[ast.If, List[ast.Call]]]:
        return _find_if_branches_with_submit_order(self.cls, reachable_method_names=self.reachable)


class CodeConformanceGate(GateResultsMixin):
    """Deterministic gate that checks generated code implements ``spec``.

    Runs synchronously in the synthesis phase, after
    :class:`CodeSafetyChecker` and before the sandbox executes. Yields one
    or more :class:`QualityGateResult` entries per check; every critical
    failure routes the orchestrator back into refinement.
    """

    GATE: ClassVar[str] = GATE

    def check(
        self,
        code: str,
        spec: object,
        *,
        phase: StrategyLabPhase = "synthesis",
    ) -> List[QualityGateResult]:
        """Run every conformance check.

        Pre: ``code`` is a string; ``phase`` is a valid phase literal.
        Post: returned list is non-empty; every entry carries the
        caller's ``phase`` and ``gate_name == GATE``.
        """
        with self._using_phase(phase):
            try:
                tree = parse_strategy_source(code)
            except SyntaxError as e:
                return [self._critical(f"Code has a syntax error: {e}")]

            strategy_classes = _find_strategy_subclasses(tree)
            if len(strategy_classes) != 1:
                # CodeSafetyChecker owns the "exactly one Strategy class"
                # rule and emits its own critical. Conformance needs a
                # single class to walk; emit info and let the safety gate
                # drive the failure.
                return [
                    self._info(
                        "Skipped: code has 0 or multiple Strategy subclasses "
                        "(handled by code_safety)."
                    )
                ]
            cls = strategy_classes[0]

            # Shared structural views (reachable methods, submit-order branches)
            # are memoised on the context: computed at most once across all
            # checks, and only when a check actually reads them.
            cctx = _ConformanceCtx(cls=cls, spec=spec)

            import_aliases = _collect_import_aliases(tree)

            results: List[QualityGateResult] = []
            results.extend(self._check_indicator_presence(cctx, import_aliases))
            results.extend(self._check_custom_code_faithfulness(cctx))
            results.extend(self._check_symbol_gate(cctx))
            results.extend(self._check_entry_coverage(cctx))
            results.extend(self._note_signal_exit_engine_ownership(cctx))
            results.extend(self._check_no_duplicate_engine_exit(cctx))
            results.extend(self._check_bar_counting_exit(cctx))
            results.extend(self._check_sizing_math(cctx))
            results.extend(self._check_no_extra_side_effects(tree, cctx))

            return results or [self._info("Code conforms to spec across all conformance checks.")]

    # ------------------------------------------------------------------
    # Check 1 — indicator presence
    # ------------------------------------------------------------------
    def _check_indicator_presence(
        self, cctx: _ConformanceCtx, import_aliases: Optional[dict[str, str]] = None
    ) -> Iterable[QualityGateResult]:
        results: List[QualityGateResult] = []
        # A malformed ``ctx.indicator(...)`` read fails at runtime regardless of
        # whether the spec requires that indicator — flag it here (with a precise
        # message) so it is refined, rather than raising in a sandbox where the
        # shadow gate would swallow the exception into a confusing no-trade run.
        target_symbols = frozenset(getattr(cctx.spec, "target_symbols", None) or [])
        for detail in _invalid_ctx_indicator_reads(cctx.cls, cctx.reachable, target_symbols):
            results.append(self._critical(detail))
        # A ``bollinger_bands(..., select=...)`` call likewise always TypeErrors in
        # the sandbox (the scalar helper takes no ``select``), regardless of spec —
        # including alias-called forms, resolved via ``import_aliases``.
        for detail in _invalid_bollinger_select_calls(cctx.cls, cctx.reachable, import_aliases):
            results.append(self._critical(detail))
        # A ``self.<helper>(...)`` call to any indicator helper the class never
        # defines raises ``AttributeError`` on the first bar — the base Strategy
        # provides no such helpers; only the compiler emits them inline. Flag it
        # generically (covers ``self.bollinger_bands``, ``self.macd``, ``self.sma``…).
        for detail in _undefined_self_indicator_helper_calls(cctx.cls, cctx.reachable):
            results.append(self._critical(detail))

        required = _collect_required_indicators(cctx.spec)
        if required:
            # Indicator reads only count when they are actually executed at
            # runtime: walk methods reachable from on_bar (Codex PR #588 P1). Two
            # recognised forms — the engine-backed ``ctx.indicator('<name>', ...)``
            # accessor (preferred) and the legacy named call (e.g. ``sma(...)``,
            # which the deterministic compiler still emits).
            call_names = _collect_called_names_in_methods(cctx.cls, cctx.reachable)
            # Resolve ``from indicators import bollinger_bands as bb`` bindings so a
            # call to the alias credits the real exported helper it names.
            if import_aliases:
                call_names = call_names | {
                    import_aliases[n] for n in call_names if n in import_aliases
                }
            ctx_names = _collect_ctx_indicator_names(cctx.cls, cctx.reachable)
            missing: List[str] = []
            for name in sorted(required):
                # Two independent credit paths, kept separate so a bare DSL-name
                # call (e.g. ``donchian(...)``) is NOT mistaken for a real read:
                #  * ``ctx.indicator('<name>', ...)`` — keyed by the DSL name.
                #  * a legacy named call — must resolve to a REAL exported helper
                #    name in the allow-list (``sma``, ``donchian_channels``, …),
                #    never the bare DSL alias (which isn't an exported callable).
                call_aliases = _INDICATOR_ALLOWED_CALL_NAMES.get(name, frozenset({name}))
                if name not in ctx_names and not (call_names & call_aliases):
                    missing.append(name)
            if missing:
                results.append(
                    self._critical(
                        f"Spec references indicator(s) {missing} but no method "
                        "reachable from on_bar reads them. Use the engine-backed "
                        "accessor ``ctx.indicator('<name>', ...)`` (preferred) or the "
                        "named-call form (e.g. ``sma(bars, 50)``); inline equivalents "
                        "and calls in unreachable helpers are not recognised."
                    )
                )

            # A derived Bollinger band (percent_b/bandwidth) needs the selector:
            # the ``bollinger_bands`` helper returns only upper/middle/lower, so a
            # plain call satisfies the name-level check above yet never computes
            # the requested series. Credit these bands only when a reachable read
            # actually selects them; abstain entirely when a bollinger read uses a
            # dynamic (non-literal) band, which could satisfy any required band.
            required_derived = _required_bollinger_derived_bands(cctx.spec)
            if required_derived:
                produced, dynamic = _collect_produced_bollinger_bands(cctx.cls, cctx.reachable)
                if not dynamic:
                    for band in sorted(required_derived - produced):
                        results.append(
                            self._critical(
                                f"Spec requires the Bollinger '{band}' band but no method "
                                "reachable from on_bar produces it. Read it via "
                                f"``ctx.indicator('bollinger', ..., band='{band}')``; a plain "
                                "bollinger_bands(...) call returns only upper/middle/lower "
                                "(the sandbox helper has no ``select`` param)."
                            )
                        )
        return results

    # ------------------------------------------------------------------
    # Check 1b — custom-code faithfulness
    # ------------------------------------------------------------------
    def _check_custom_code_faithfulness(self, cctx: _ConformanceCtx) -> Iterable[QualityGateResult]:
        """Reject the three ways LLM-authored ``on_bar`` code diverges from the spec.

        Pre: ``cctx`` holds the single Strategy ClassDef and the spec.
        Post: a critical per distinct defect —
          * a ``ctx.indicator(...)`` read on a different source/params than the
            spec's ``IndicatorRef`` declares (the executed trades would not
            implement the specification);
          * a bare falsy-guard on an indicator value that silently disables a
            spec-required condition when the value is ``0.0``/``None``;
          * a read of a position attribute the snapshot does not expose
            (a runtime ``AttributeError`` that aborts the backtest).
        All three fire only on custom (``ctx.``-accessor) code; the deterministic
        compiler emits named calls with spec-matched sources, so compiled
        strategies never trip them.
        """
        results: List[QualityGateResult] = []
        for detail in _divergent_ctx_indicator_reads(cctx.cls, cctx.reachable, cctx.spec):
            results.append(self._critical(detail))
        for detail in _indicator_falsy_guard_errors(cctx.cls, cctx.reachable):
            results.append(self._critical(detail))
        for detail in _invalid_position_attr_errors(cctx.cls, cctx.reachable):
            results.append(self._critical(detail))
        return results

    # ------------------------------------------------------------------
    # Check 2 — symbol/universe gate (defense-in-depth with code_safety)
    # ------------------------------------------------------------------
    def _check_symbol_gate(self, cctx: _ConformanceCtx) -> Iterable[QualityGateResult]:
        spec = cctx.spec
        cls = cctx.cls
        if not getattr(spec, "target_symbols", None):
            return ()
        if not _has_universe_constant(cls):
            return (
                self._critical(
                    "Spec has non-empty target_symbols but the Strategy class "
                    "does not declare a UNIVERSE = frozenset({...}) (or "
                    "set/tuple) class-level constant. Without UNIVERSE plus "
                    "an ``if bar.symbol not in self.UNIVERSE: return`` guard "
                    "in on_bar, bars for non-target symbols will be processed."
                ),
            )
        if not _has_universe_guard_in_on_bar(cls):
            return (
                self._critical(
                    "Strategy defines UNIVERSE but on_bar lacks the runtime "
                    "``if bar.symbol not in self.UNIVERSE: return`` guard "
                    "rejecting bars whose symbol is not in the universe."
                ),
            )
        return ()

    # ------------------------------------------------------------------
    # Check 3 — entry predicate coverage
    # ------------------------------------------------------------------
    def _check_entry_coverage(self, cctx: _ConformanceCtx) -> Iterable[QualityGateResult]:
        spec = cctx.spec
        entry_rules = [
            r for r in (getattr(spec, "entry_rules", []) or []) if isinstance(r, EntryRule)
        ]
        if not entry_rules:
            return ()

        if _is_engine_managed(spec):
            return (
                self._info(
                    f"Spec declares {len(entry_rules)} entry rule(s); entries are "
                    "engine-managed via _EngineEntryDispatcher — no inline "
                    "submit_order entry branches required."
                ),
            )

        entry_branches: List[tuple[ast.If, List[ast.Call]]] = []
        for if_node, calls in cctx.submit_branches:
            if any(
                _submit_order_is_kwargs_spread(c)
                or (_submit_order_has_side_literal(c) and not _submit_order_closes_position(c))
                for c in calls
            ):
                entry_branches.append((if_node, calls))

        if not entry_branches:
            return (
                self._critical(
                    f"Spec declares {len(entry_rules)} entry rule(s) but no "
                    "if/elif branch reachable from on_bar contains a "
                    "ctx.submit_order(..., side=...) entry call. The entry "
                    "path was likely removed during refinement or lives in "
                    "an unreachable helper."
                ),
            )

        if len(entry_branches) < len(entry_rules):
            return (
                self._critical(
                    f"Spec declares {len(entry_rules)} entry rule(s) but only "
                    f"{len(entry_branches)} entry branch(es) were found "
                    "reachable from on_bar. Each EntryRule needs its own "
                    "if/elif branch with a ctx.submit_order(..., side=...) "
                    "call."
                ),
            )
        return ()

    # ------------------------------------------------------------------
    # Check 4 — signal-exit rules are engine-owned
    # ------------------------------------------------------------------
    def _note_signal_exit_engine_ownership(
        self, cctx: _ConformanceCtx
    ) -> Iterable[QualityGateResult]:
        """Signal exits are enforced engine-side for every strategy.

        Pre: ``cctx`` is a built context; ``cctx.spec`` is a
        ``StrategySpec`` or ``None``.
        Post: returns ``()`` when the spec declares no ``SignalExitRule``;
        otherwise a single ``info`` result. The engine evaluates every
        ``SignalExitRule`` via ``_EngineExitDispatcher`` (with streaming
        history views) for both the compiled and the custom-code path, so
        an inline ``submit_order`` exit branch is never required.
        Invariant: emits no critical. The requirement direction is retired
        (the engine owns the exit); the prohibition direction — forbidding
        a manual close that duplicates an engine-owned exit — is owned by
        :meth:`_check_no_duplicate_engine_exit`.
        """
        spec = cctx.spec
        if spec is None:
            return ()
        signal_exits = [
            r for r in (getattr(spec, "exit_rules", []) or []) if isinstance(r, SignalExitRule)
        ]
        if not signal_exits:
            return ()
        return (
            self._info(
                f"Spec declares {len(signal_exits)} signal-exit rule(s); the "
                "engine enforces every spec.exit_rules entry via "
                "_EngineExitDispatcher and stamps engine_exit:<kind> "
                "attribution — no inline submit_order exit branch is required "
                "(the strategy authors entries only)."
            ),
        )

    # ------------------------------------------------------------------
    # Check 4b — no manual close duplicating an engine-owned exit
    # ------------------------------------------------------------------
    def _check_no_duplicate_engine_exit(self, cctx: _ConformanceCtx) -> Iterable[QualityGateResult]:
        """Forbid custom code from closing positions the engine already owns.

        The engine enforces ``spec.exit_rules`` on the custom-code path too
        (the backtest mode passes the spec's exit rules to the engine
        unconditionally). A strategy-emitted close fills ahead of the
        engine's close and overwrites the ``engine_exit:<kind>``
        attribution the trade-alignment gate depends on, so a manual close
        of an engine-owned side is a critical conformance violation.

        Two manual-close shapes are detected, across every ``on_bar``-
        reachable ``submit_order`` (if/elif/else bodies, post-guard top-level
        statements, and reachable helpers):
          * an explicit opposite-side close whose ``qty=`` reads the
            position — ``position.qty``, a renamed position alias, a wrapping
            expression (``abs(position.qty)``), or a direct
            ``ctx.position(...).qty`` — which retires the OPPOSITE of the
            order side; and
          * an entry carrying a non-``None`` ``attached_stop_loss`` /
            ``attached_take_profit`` bracket leg, whose ``bracket_sl`` /
            ``bracket_tp`` children close the order's OWN side.

        Coverage is judged per-side: a close is forbidden only when the
        position side it retires is covered by some ``spec.exit_rules`` entry
        (via :func:`_engine_exits_cover_sides`), intersected with the spec's
        entered sides so a same-side full-size scale-in (``qty=position.qty``
        whose inferred opposite side the spec never enters) is not misread as
        a close. A close (or bracket) whose ``side=`` cannot be resolved
        statically is a hard critical only when the engine covers EVERY
        entered side — under partial coverage it may legitimately retire the
        engine-uncovered side, so it is deferred to the runtime gate rather
        than flagged.

        Pre: ``cctx`` is a built context; ``cctx.spec`` is a
        ``StrategySpec`` or ``None``.
        Post: returns ``()`` when ``spec`` is ``None``, for the compiled path
        (``requires_custom_code`` false — it submits no orders), for a spec
        with no exit rules, or when no reachable close retires an
        engine-owned, entered side. Otherwise returns a single critical
        naming the count of manual closes to remove.
        Invariant: never fires on the compiled path; only forbids closes the
        engine demonstrably owns for the side they retire.

        Static limits (caught by the runtime trade-alignment gate, which
        verifies ``engine_exit:<kind>`` attribution per executed trade and is
        the authoritative backstop): a close whose ``qty`` is a name- or
        attribute-bound position alias (``close_qty = pos.qty``;
        ``self.held = pos.qty``) or a computed opposite-side quantity that
        never references ``position.qty`` is not recognised here — class-wide
        qty-alias collection was tried and produced false positives on
        legitimately-computed entry quantities, so it was removed in favour
        of the runtime backstop. Likewise, in a spec that enters BOTH sides a
        full-size same-side scale-in cannot be told apart from a cross-side
        close without the runtime position side — so it may be flagged (false
        positive) or a cross-side close waved through. The runtime gate
        reconciles all of these.
        """
        spec = cctx.spec
        if spec is None:
            return ()
        if not getattr(spec, "requires_custom_code", False):
            return ()
        exit_rules = getattr(spec, "exit_rules", None) or []
        if not exit_rules:
            return ()
        # Entered sides come from the spec (the authoritative, validated
        # source) rather than the AST: a dynamic ``side=`` expression would
        # leave an AST-derived side set empty and silently disable the check.
        entered_sides = {
            r.side
            for r in (getattr(spec, "entry_rules", []) or [])
            if isinstance(r, EntryRule) and r.side is not None
        }

        # Resolve position aliases once so the close detector catches renamed
        # handles (``current = ctx.position(...)``), wrapped qty expressions
        # (``abs(position.qty)``), and direct ``ctx.position(...).qty`` — not
        # just the literal ``position`` / ``pos`` names.
        position_names = _collect_position_aliases(cctx.cls)

        # Whether the engine covers EVERY entered side. Used for closes whose
        # side cannot be resolved statically: only then is a hard critical
        # safe (any close hits a covered side, with no legitimate uncovered
        # side to retire). Under partial coverage a dynamic-side close may
        # legitimately retire the engine-uncovered side, so it is left to the
        # runtime gate rather than flagged as a false positive.
        all_sides_covered = bool(entered_sides) and _engine_exits_cover_sides(spec, entered_sides)

        # Each flagged entry records the offending close's location
        # (``<method>:L<lineno>``) so the refinement agent / developer can
        # jump straight to the call to remove.
        flagged: list[str] = []
        for method in _iter_strategy_methods(cctx.cls):
            if method.name not in cctx.reachable:
                continue
            for node in _iter_method_body_nodes(method):
                if not _is_submit_order_call(node):
                    continue
                if _node_is_duplicate_close(
                    node,
                    position_names=position_names,
                    entered_sides=entered_sides,
                    all_sides_covered=all_sides_covered,
                    spec=spec,
                ):
                    flagged.append(f"{method.name}:L{getattr(node, 'lineno', 0)}")
        if not flagged:
            return ()
        return (
            self._critical(
                f"Custom-code strategy submits {len(flagged)} manual "
                "position-closing order(s) — an opposite-side close "
                "(ctx.submit_order(..., qty=position.qty)) or an entry with an "
                "attached_stop_loss / attached_take_profit bracket leg — for a "
                f"side the engine already owns (at {', '.join(flagged)}). The "
                "engine enforces every spec.exit_rules entry (stop_loss / "
                "take_profit / signal_exit) and stamps engine_exit:<kind> "
                "attribution; a manual close fills first and overwrites it, "
                "breaking exit alignment. Remove the close(s)/bracket(s) for "
                "engine-owned sides and let the engine own spec.exit_rules — "
                "author entry logic only."
            ),
        )

    # ------------------------------------------------------------------
    # Check 5 — bar-counting exit rejection
    # ------------------------------------------------------------------

    _BAR_COUNTER_NAMES: ClassVar[frozenset] = frozenset(
        {
            "bars_held",
            "hold_count",
            "days_held",
            "bars_in_trade",
            "held_bars",
            "bar_count",
            "hold_period",
            "bars_since_entry",
            "hold_bars",
            "n_bars_held",
            "num_bars_held",
            "time_in_trade",
            "holding_period",
            "exit_countdown",
            "bar_counter",
        }
    )

    def _check_bar_counting_exit(self, cctx: _ConformanceCtx) -> Iterable[QualityGateResult]:
        violations: list[str] = []
        for method in _iter_strategy_methods(cctx.cls):
            for node in ast.walk(method):
                # Only flag self.<counter> instance attributes — these persist
                # across bars and are the pattern LLMs use for holding-period
                # exits.  Bare locals (e.g. a diagnostic `bar_count`) are left
                # alone to avoid false positives on non-exit bookkeeping.
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in self._BAR_COUNTER_NAMES
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"
                ):
                    violations.append(f"self.{node.attr}")
        unique = sorted(set(violations))
        if not unique:
            return ()
        names = ", ".join(f"`{v}`" for v in unique)
        return (
            self._critical(
                f"Bar-counting exit detected ({names}). "
                f"Exits must close on price, P&L, or signal reversal — "
                f"not on an arbitrary bar counter."
            ),
        )

    # ------------------------------------------------------------------
    # Check 6 — sizing math present
    # ------------------------------------------------------------------
    def _check_sizing_math(self, cctx: _ConformanceCtx) -> Iterable[QualityGateResult]:
        cls = cctx.cls
        all_submit_calls = [
            sub
            for method in _iter_strategy_methods(cls)
            for sub in _iter_method_body_nodes(method)
            if _is_submit_order_call(sub)
        ]
        entry_submit_calls = [c for c in all_submit_calls if not _submit_order_closes_position(c)]
        if not entry_submit_calls:
            return ()

        # Hardcoded-int qty on every entry is an immediate fail regardless
        # of whether ctx.equity is referenced elsewhere in the class.
        if all(_qty_is_constant_int(c) for c in entry_submit_calls):
            return (
                self._critical(
                    "Every entry ctx.submit_order call passes a literal "
                    "integer ``qty=``. Sizing must be derived from "
                    "``ctx.equity`` or ``ctx.capital`` so the position "
                    "scales with the account, not a hardcoded number."
                ),
            )

        # Otherwise: any reference to ctx.equity / ctx.capital anywhere in
        # the Strategy class is enough — qty is usually computed into a
        # local variable (``qty = max(1, int(ctx.equity * 0.02 / bar.close))``)
        # which then flows into the submit_order call as ``qty=qty``. We
        # do not attempt to trace the local var across statements; the
        # presence of the account-value reference and the absence of a
        # hardcoded int kwarg is the v1 contract.
        if _node_references_ctx_equity(cls):
            return ()
        return (
            self._critical(
                "Strategy class never references ``ctx.equity`` or "
                "``ctx.capital``. Sizing must derive ``qty=`` from the "
                "account value so the strategy scales correctly."
            ),
        )

    # ------------------------------------------------------------------
    # Check 7 — no submit_order calls outside hook/helper methods
    # ------------------------------------------------------------------
    def _check_no_extra_side_effects(
        self, tree: ast.AST, cctx: _ConformanceCtx
    ) -> Iterable[QualityGateResult]:
        # ``_helper`` methods are only allowed when actually reachable
        # from an allowed hook — a dead ``_helper`` containing
        # ctx.submit_order would otherwise pass silently (Codex PR #588).
        reachable_helpers = _methods_reachable_from_hooks(cctx.cls)
        offenders: List[str] = []
        for node in ast.walk(tree):
            if not _is_submit_order_call(node):
                continue
            enclosing = _find_enclosing_funcdef(tree, node)
            if enclosing is None:
                offenders.append("<module scope>")
                continue
            name = enclosing.name
            if name in _ALLOWED_HOOK_NAMES:
                continue
            # ``_helper`` is the conventional name for hook-reachable
            # closures; allow it only when actually reachable. Dunder
            # methods (``__init__`` / ``__call__`` / ``__enter__`` …) are
            # never the right place for a submit_order — exclude them
            # outright.
            is_helper_name = name.startswith("_") and not (
                name.startswith("__") and name.endswith("__")
            )
            if is_helper_name and name in reachable_helpers:
                continue
            offenders.append(name)
        if not offenders:
            return ()
        return (
            self._critical(
                f"ctx.submit_order called from disallowed scope(s) "
                f"{sorted(set(offenders))}. Order submissions must live in "
                "on_bar / on_fill / on_end (or in a private ``_helper`` "
                "method reachable from one of those hooks)."
            ),
        )
