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
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import cached_property
from typing import Any, ClassVar, Iterable, List, Optional, get_args

from ..spec_dsl import (
    _INDICATOR_PARAM_SPECS,
    EntryRule,
    IndicatorName,
    SignalExitRule,
    Source,
    iter_tree_indicator_refs,
)
from .code_safety import (
    _collect_position_aliases,
    _engine_exits_cover_sides,
    _expr_references_position_qty,
    _extract_side_literal,
    _is_ctx_position_call,
)
from .code_safety_ast import (
    _find_strategy_subclasses,
    _get_call_name,
    _has_universe_constant,
    _has_universe_guard_in_on_bar,
    _iter_method_body_nodes,
    parse_strategy_source,
)
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "code_conformance"

# Hook methods on the Strategy class within which ``ctx.submit_order`` is
# allowed. Helper methods whose names start with "_" are also allowed
# because the existing :class:`CodeSafetyChecker` order-flow gate already
# requires reachable order flow to originate in ``on_bar``; the conformance
# gate's role here is to forbid stray submissions in unrelated methods
# (e.g. ``__init__`` or a public ``run`` wrapper).
_ALLOWED_HOOK_NAMES: frozenset[str] = frozenset({"on_bar", "on_fill", "on_end"})

# DSL → set of acceptable AST call-name(s) for the indicator's named
# implementation. These are the REAL callable helper names a named call must
# resolve to — only names the sandbox ``indicators`` module actually exports.
# Most map 1:1 with the indicator name; the channel/band indicators map to their
# helper (``bollinger`` → ``bollinger_bands``, ``donchian`` → ``donchian_channels``,
# ``keltner`` → ``keltner_channels``). The bare DSL name is intentionally NOT an
# alias here: ``donchian``/``keltner``/``bollinger`` are not exported callables, so a
# bare ``donchian(...)`` call would ``NameError``/``ImportError`` at runtime and must
# not satisfy the gate. The DSL name is credited separately via the
# ``ctx.indicator('<name>', ...)`` accessor in :meth:`_check_indicator_presence`.
_INDICATOR_ALLOWED_CALL_NAMES: dict[str, frozenset[str]] = {
    "sma": frozenset({"sma"}),
    "ema": frozenset({"ema"}),
    "rsi": frozenset({"rsi"}),
    "macd": frozenset({"macd"}),
    "bollinger": frozenset({"bollinger_bands"}),
    "atr": frozenset({"atr"}),
    "adx": frozenset({"adx"}),
    "stochastic": frozenset({"stochastic"}),
    "vwap": frozenset({"vwap"}),
    "donchian": frozenset({"donchian_channels"}),
    "keltner": frozenset({"keltner_channels"}),
    "obv": frozenset({"obv"}),
    "mfi": frozenset({"mfi"}),
    "roc": frozenset({"roc"}),
    "cci": frozenset({"cci"}),
    "williams_r": frozenset({"williams_r"}),
}

# Explicit raise (not a bare ``assert``) so this load-time invariant survives
# ``python -O``: it guards gate correctness — a DSL indicator missing from the
# allow-list would silently let an unsupported call pass the conformance gate.
if set(_INDICATOR_ALLOWED_CALL_NAMES) != set(IndicatorName.__args__):
    raise RuntimeError(
        "indicator allow-list (_INDICATOR_ALLOWED_CALL_NAMES) must cover every DSL "
        f"IndicatorName literal; mismatch: "
        f"{set(IndicatorName.__args__) ^ set(_INDICATOR_ALLOWED_CALL_NAMES)}"
    )

# Names recognised as the position-snapshot receiver in exit branches.
_POSITION_RECEIVER_NAMES: frozenset[str] = frozenset({"position", "pos"})

# Valid ``source`` values for source-aware indicators (mirrors spec_dsl.Source).
_VALID_SOURCES: frozenset[str] = frozenset(get_args(Source))


def _indicators_in_predicate(when: Any) -> set[str]:
    """Return the set of DSL indicator names referenced anywhere in ``when``.

    ``when`` is a rule's predicate position: a single ``Predicate`` or an
    ``all_of`` / ``any_of`` tree. Every leaf predicate's indicator sides are
    collected so a multi-confirmation entry's full indicator set is required of
    the generated code, not just the first leg.
    """
    return {ref.name for ref in iter_tree_indicator_refs(when)}


def _collect_required_indicators(spec: Any) -> set[str]:
    """Indicator names the generated code must read at runtime.

    Pre: ``spec`` is a ``StrategySpec`` or ``None``.
    Post: returns the union of indicator names the code is required to
    compute. Entry-rule indicators are always included — entries are
    authored inline on both the compiled and custom-code paths.
    ``SignalExitRule`` indicators are included only for the compiled path:
    on the custom-code path exits are engine-owned (the engine computes
    their indicators via ``_EngineExitDispatcher`` and the strategy
    authors no exit branch), so requiring the code to read an exit-only
    indicator would contradict the engine-owned-exits contract.
    """
    if spec is None:
        return set()
    refs: set[str] = set()
    for rule in getattr(spec, "entry_rules", []) or []:
        if isinstance(rule, EntryRule):
            refs |= _indicators_in_predicate(rule.when)
    if not getattr(spec, "requires_custom_code", False):
        for rule in getattr(spec, "exit_rules", []) or []:
            if isinstance(rule, SignalExitRule):
                refs |= _indicators_in_predicate(rule.when)
    return refs


# Bollinger bands that the ``bollinger_bands`` helper does NOT return: they are
# derived from the base bands and only materialise when explicitly selected, so
# a plain helper call that yields upper/middle/lower must not credit them.
_BOLLINGER_DERIVED_BANDS: frozenset[str] = frozenset({"percent_b", "bandwidth"})


def _required_bollinger_derived_bands(spec: Any) -> set[str]:
    """Derived Bollinger bands (``percent_b``/``bandwidth``) the code must produce.

    Pre: ``spec`` is a ``StrategySpec`` or ``None``.
    Post: returns the subset of ``{percent_b, bandwidth}`` any required Bollinger
    ref selects. Entry rules count on both paths; ``SignalExitRule`` indicators
    count only for the compiled path (custom-code exits are engine-owned), matching
    :func:`_collect_required_indicators`. Base bands (upper/middle/lower) are not
    tracked here — the ``bollinger_bands`` helper returns them directly, so a plain
    call is a sufficient read; the derived bands need the ``select=``/``band=``
    selector, so they are credited only by a band-matched read.
    """
    if spec is None:
        return set()
    bands: set[str] = set()

    def _scan(when: Any) -> None:
        for ref in iter_tree_indicator_refs(when):
            if ref.name == "bollinger" and ref.param("band") in _BOLLINGER_DERIVED_BANDS:
                bands.add(ref.param("band"))

    for rule in getattr(spec, "entry_rules", []) or []:
        if isinstance(rule, EntryRule):
            _scan(rule.when)
    if not getattr(spec, "requires_custom_code", False):
        for rule in getattr(spec, "exit_rules", []) or []:
            if isinstance(rule, SignalExitRule):
                _scan(rule.when)
    return bands


def _collect_called_names_in_methods(cls: ast.ClassDef, method_names: frozenset[str]) -> set[str]:
    """Return the set of function-call names found inside the listed
    methods of ``cls`` (descent stops at nested defs/classes/lambdas).

    Used by check #1 (indicator presence) so that ``sma(...)`` called
    only from a dead helper does not satisfy the requirement that
    ``on_bar`` actually computes the indicator at runtime.

    ``_get_call_name`` normalises ``indicators.sma(...)`` and bare
    ``sma(...)`` to the same ``"sma"`` key.
    """
    out: set[str] = set()
    for method in _iter_strategy_methods(cls):
        if method.name not in method_names:
            continue
        for node in _iter_method_body_nodes(method):
            if isinstance(node, ast.Call):
                name = _get_call_name(node)
                if name:
                    out.add(name)
    return out


def _is_ctx_indicator_call(node: ast.AST) -> bool:
    """True iff ``node`` is a ``ctx.indicator(...)`` call.

    The receiver must be the literal ``ctx`` parameter — ``self.indicator(...)``
    or ``foo.indicator(...)`` is **not** the engine-backed accessor and must not
    be credited as one (it could compute values that drift from the engine).
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "indicator"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ctx"
    )


def _ctx_indicator_arg_name(node: ast.Call) -> Optional[str]:
    """Return the string indicator name of a ``ctx.indicator('<name>', ...)`` call.

    Accepts the first positional argument or the ``name=`` keyword when it is a
    string literal; returns ``None`` for a non-literal name (which cannot be
    statically matched to a spec indicator).
    """
    if node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    for kw in node.keywords:
        if (
            kw.arg == "name"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    return None


def _ctx_indicator_call_errors(node: ast.Call, target_symbols: frozenset[str]) -> list[str]:
    """Static-validation errors for one ``ctx.indicator(...)`` call (``[]`` when
    valid or not statically determinable).

    Catches call shapes that are a guaranteed runtime error, so the conformance
    gate refines them rather than letting them fail in the sandbox:

    * indicator params passed positionally (the accessor is keyword-only past
      ``name``);
    * the name given both positionally and as ``name=`` (a ``TypeError``);
    * an unexpected/typo'd param key — matched by *name*, so a dynamic sibling
      value (e.g. ``period=self.WINDOW``) does not suppress it;
    * an out-of-DSL literal value, a forbidden ``source`` override, or a missing
      required param;
    * a literal ``symbol=`` outside the spec's ``target_symbols`` (it would read
      a symbol that never receives data, so the value is ``None`` forever).

    Validation reuses spec_dsl's ``_INDICATOR_PARAM_SPECS`` as the single source
    of truth. Dynamic values are validated by key name only; the missing-required
    and unexpected-key checks are skipped when ``**kwargs`` unpacking hides keys.
    ``target_symbols`` is the spec universe (empty = open universe → not checked).
    """
    name = _ctx_indicator_arg_name(node)
    label = repr(name) if name else "..."
    if len(node.args) > 1:
        return [
            f"ctx.indicator({label}, ...) passes indicator params positionally; every "
            "argument after the name is keyword-only (use e.g. ctx.indicator('ema', period=20))."
        ]
    if node.args and any(kw.arg == "name" for kw in node.keywords):
        return [
            f"ctx.indicator({label}, ...) gives the indicator name both positionally and as "
            "name=; that is a runtime TypeError."
        ]
    if name is None:
        return []  # dynamic name — the presence check handles absence
    if name not in _INDICATOR_PARAM_SPECS:
        return [
            f"ctx.indicator({name!r}, ...): unknown indicator '{name}'; "
            f"allowed: {sorted(_INDICATOR_PARAM_SPECS)}."
        ]
    spec = _INDICATOR_PARAM_SPECS[name]
    allowed = set(spec["required"]) | set(spec["optional"])
    errors: list[str] = []
    has_kwargs_unpack = any(kw.arg is None for kw in node.keywords)
    seen: set[str] = set()
    # A literal symbol outside the spec universe never receives bars, so the
    # read returns None forever — credited by name but not actually implemented.
    if target_symbols:
        for kw in node.keywords:
            if (
                kw.arg == "symbol"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
                and kw.value.value not in target_symbols
            ):
                errors.append(
                    f"ctx.indicator('{name}', ...): symbol {kw.value.value!r} is not in the "
                    f"spec's target_symbols {sorted(target_symbols)}; it never receives data."
                )
    for kw in node.keywords:
        if kw.arg is None or kw.arg in ("name", "symbol"):
            continue
        if kw.arg == "source":
            if isinstance(kw.value, ast.Constant):
                src = kw.value.value
                if not spec["allow_source"]:
                    if src != "close":
                        errors.append(
                            f"ctx.indicator('{name}', ...): '{name}' does not accept a "
                            "'source' override."
                        )
                elif src not in _VALID_SOURCES:
                    errors.append(
                        f"ctx.indicator('{name}', ...): invalid source {src!r}; "
                        f"allowed: {sorted(_VALID_SOURCES)}."
                    )
            continue
        seen.add(kw.arg)
        if kw.arg not in allowed:
            errors.append(
                f"ctx.indicator('{name}', ...): unexpected param '{kw.arg}'; "
                f"allowed: {sorted(allowed)}."
            )
            continue
        if isinstance(kw.value, ast.Constant):
            checker = spec["required"].get(kw.arg) or spec["optional"][kw.arg][1]
            try:
                checker(kw.value.value)
            except ValueError as exc:
                errors.append(
                    f"ctx.indicator('{name}', ...): invalid {kw.arg}={kw.value.value!r} ({exc})."
                )
    if not has_kwargs_unpack:
        for required_key in spec["required"]:
            if required_key not in seen:
                errors.append(
                    f"ctx.indicator('{name}', ...): missing required param '{required_key}'."
                )
    return errors


def _iter_ctx_indicator_calls(cls: ast.ClassDef, method_names: frozenset[str]):
    """Yield ``ctx.indicator(...)`` calls inside the on_bar-reachable methods."""
    for method in _iter_strategy_methods(cls):
        if method.name not in method_names:
            continue
        for node in _iter_method_body_nodes(method):
            if _is_ctx_indicator_call(node):
                yield node


def _collect_ctx_indicator_names(cls: ast.ClassDef, method_names: frozenset[str]) -> set[str]:
    """Return DSL indicator names read via ``ctx.indicator('<name>', ...)``.

    Recognises the prescriptive single-call accessor the synthesis prompt now
    mandates, scanning the same on_bar-reachable methods as
    :func:`_collect_called_names_in_methods` so a literal name passed to
    ``ctx.indicator`` satisfies check #1 exactly like a named ``sma(...)`` call.
    Only ``ctx``-receiver calls with a literal name are credited.
    """
    out: set[str] = set()
    for node in _iter_ctx_indicator_calls(cls, method_names):
        name = _ctx_indicator_arg_name(node)
        if name:
            out.add(name)
    return out


def _string_kwarg(node: ast.Call, key: str) -> Optional[str]:
    """Return the string-literal value of ``node``'s ``key=`` keyword, else ``None``."""
    for kw in node.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _collect_produced_bollinger_bands(cls: ast.ClassDef, method_names: frozenset[str]) -> set[str]:
    """Return the Bollinger selectors the on_bar-reachable code actually requests.

    ``percent_b``/``bandwidth`` only materialise when explicitly selected — via
    ``ctx.indicator('bollinger', ..., band='<b>')`` (the engine accessor the
    custom-code path uses) or a ``bollinger_bands(..., select='<b>')`` call (the
    form the deterministic compiler emits). A plain ``bollinger_bands(...)`` with
    no ``select=`` yields only the base bands, so it does not credit a derived
    band. Scans the same reachable methods as :func:`_collect_ctx_indicator_names`.
    """
    produced: set[str] = set()
    for method in _iter_strategy_methods(cls):
        if method.name not in method_names:
            continue
        for node in _iter_method_body_nodes(method):
            if not isinstance(node, ast.Call):
                continue
            if _is_ctx_indicator_call(node) and _ctx_indicator_arg_name(node) == "bollinger":
                band = _string_kwarg(node, "band")
                if band:
                    produced.add(band)
            elif _get_call_name(node) == "bollinger_bands":
                select = _string_kwarg(node, "select")
                if select:
                    produced.add(select)
    return produced


def _invalid_ctx_indicator_reads(
    cls: ast.ClassDef, method_names: frozenset[str], target_symbols: frozenset[str]
) -> list[str]:
    """De-duplicated static-validation messages for every on_bar-reachable
    ``ctx.indicator(...)`` call (see :func:`_ctx_indicator_call_errors`)."""
    errors: list[str] = []
    seen: set[str] = set()
    for node in _iter_ctx_indicator_calls(cls, method_names):
        for msg in _ctx_indicator_call_errors(node, target_symbols):
            if msg not in seen:
                seen.add(msg)
                errors.append(msg)
    return errors


def _is_submit_order_call(node: ast.AST) -> bool:
    """True iff ``node`` is a ``Call`` whose function attribute is ``submit_order``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "submit_order"
    )


def _submit_order_has_side_literal(call: ast.Call) -> bool:
    """True iff the call has a ``side=`` kwarg with any value.

    The CodeSafetyChecker shapes the recognised literal forms more
    strictly (``OrderSide.LONG`` / ``"LONG"``); for conformance coverage
    the presence of a ``side=`` kwarg at all is enough to mark the
    branch as an entry path. Exit paths use ``qty=position.qty`` and are
    detected by ``_submit_order_closes_position`` instead.
    """
    return any(kw.arg == "side" for kw in call.keywords)


def _submit_order_is_kwargs_spread(call: ast.Call) -> bool:
    """True iff the call uses a ``**kwargs`` expansion.

    ``ctx.submit_order(**order_kwargs)`` cannot be statically resolved —
    the spread may carry ``side`` (entry) or ``qty=position.qty`` (exit)
    dynamically. Coverage checks credit a branch with such a call as
    BOTH a plausible entry and a plausible exit so conformant code that
    builds kwargs dynamically does not trip false-positive criticals.
    """
    return any(kw.arg is None for kw in call.keywords)


def _submit_order_closes_position(call: ast.Call) -> bool:
    """True iff ``call`` passes ``qty=<pos|position>.qty``.

    Matches the exit shape from the issue:
    ``ctx.submit_order(..., qty=position.qty)`` or the ``pos`` alias.
    """
    for kw in call.keywords:
        if kw.arg != "qty":
            continue
        v = kw.value
        if (
            isinstance(v, ast.Attribute)
            and v.attr == "qty"
            and isinstance(v.value, ast.Name)
            and v.value.id in _POSITION_RECEIVER_NAMES
        ):
            return True
    return False


def _submit_order_close_side(call: ast.Call) -> Optional[str]:
    """Return the position side a closing ``submit_order`` retires.

    Pre: ``call`` is a ``ctx.submit_order(...)`` ``ast.Call``.
    Post: returns ``"long"`` / ``"short"`` — the position side the order
    closes — when the keyword ``side=`` value is a side literal
    :func:`_extract_side_literal` recognises (an ``OrderSide.LONG`` /
    ``OrderSide.SHORT`` attribute rooted at ``OrderSide``, an
    ``OrderSide(...)`` call, or a bare ``"LONG"`` / ``"SHORT"`` string).
    Returns ``None`` when ``side`` is absent or cannot be resolved to an
    ``OrderSide`` literal (variables, opaque calls, or a non-``OrderSide``
    enum such as a user-defined ``FakeSide.SHORT``).

    The returned side is the OPPOSITE of the order side, because a close
    submits the side opposite the position it retires (an ``OrderSide.SHORT``
    order closes a long). Reusing :func:`_extract_side_literal` keeps side
    inference identical to ``code_safety``'s entry-side detection and avoids
    misreading an unrelated enum member as an order side.

    Only the keyword ``side=`` form is inspected, consistent with the
    position-``qty`` close detector and the synthesis prompt's mandated
    keyword-argument call shape.
    """
    for kw in call.keywords:
        if kw.arg != "side":
            continue
        order_side = _extract_side_literal(kw.value)
        if order_side is None:
            return None
        return "short" if order_side == "long" else "long"
    return None


def _expr_is_position_qty(node: ast.AST, position_names: frozenset[str]) -> bool:
    """True iff ``node`` (or a sub-expression) reads a position's ``.qty``.

    Pre: ``position_names`` is the alias set from
    :func:`_collect_position_aliases`.
    Post: True for ``<alias>.qty`` (alias in ``position_names``) and any
    wrapping expression (``abs(position.qty)``, ``position.qty * 1``) — via
    :func:`_expr_references_position_qty` — and additionally for a direct
    ``ctx.position(...).qty`` receiver. Name-bound and attribute-bound qty
    aliases (``close_qty = pos.qty``; ``self.held = pos.qty``) are NOT
    resolved here: collecting them class-wide produced false positives on
    legitimately-computed entry quantities, so those shapes are left to the
    runtime trade-alignment gate.
    """
    if _expr_references_position_qty(node, position_names):
        return True
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == "qty"
            and _is_ctx_position_call(sub.value)
        ):
            return True
    return False


def _submit_order_qty_references_position(call: ast.Call, position_names: frozenset[str]) -> bool:
    """True iff the call's keyword ``qty=`` reads a position's ``.qty``.

    Pre: ``call`` is a ``ctx.submit_order(...)`` ``ast.Call``;
    ``position_names`` is the alias set from
    :func:`_collect_position_aliases`.
    Post: delegates to :func:`_expr_is_position_qty` — True for
    ``qty=position.qty``, a renamed position alias, a wrapping expression, or
    a direct ``qty=ctx.position(...).qty``. Broader than
    :func:`_submit_order_closes_position` (literal ``position`` / ``pos``
    only) without the false-positive surface of class-wide name aliasing.
    """
    for kw in call.keywords:
        if kw.arg == "qty":
            return _expr_is_position_qty(kw.value, position_names)
    return False


def _attachment_is_active(node: ast.AST) -> bool:
    """True iff a bracket-attachment value is demonstrably not ``None``.

    Pre: ``node`` is the value of an ``attached_stop_loss`` /
    ``attached_take_profit`` keyword.
    Post: True for a truthy constant (non-``None``, non-zero) or a computed
    expression (``BinOp`` / ``Call`` / ``BoolOp`` / ...); False for a falsy
    constant (``None`` / ``0`` / ``0.0`` / ``False`` / ``""``) and for a bare
    ``Name`` / ``Attribute`` whose value cannot be established statically
    (``stop = None; attached_stop_loss=stop``). An engine that treats a
    falsy attachment as "no bracket" creates no bracket child, so a falsy
    or ambiguous attachment is treated as inactive to avoid a false-positive
    hard critical; a genuinely-active bracket built from a variable is caught
    by the runtime trade-alignment gate instead.
    """
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, (ast.Name, ast.Attribute)):
        return False
    return True


def _submit_order_attached_bracket(call: ast.Call) -> tuple[bool, Optional[str]]:
    """Return ``(has_bracket, own_side)`` for an attached-bracket entry.

    Pre: ``call`` is a ``ctx.submit_order(...)`` ``ast.Call``.
    Post: ``has_bracket`` is True iff the call passes a demonstrably
    non-``None`` ``attached_stop_loss`` / ``attached_take_profit``
    (:func:`_attachment_is_active`) — the runtime materialises opposite-side
    ``bracket_sl`` / ``bracket_tp`` children that close the position this
    order opens, ahead of ``_EngineExitDispatcher``. ``own_side`` is the
    resolved ``OrderSide`` literal the bracket closes (the order's OWN side,
    NOT inverted), or ``None`` when the side is dynamic — the caller then
    falls back to every entered side, as it does for explicit closes.
    """
    has_bracket = any(
        kw.arg in ("attached_stop_loss", "attached_take_profit") and _attachment_is_active(kw.value)
        for kw in call.keywords
    )
    if not has_bracket:
        return (False, None)
    for kw in call.keywords:
        if kw.arg == "side":
            return (True, _extract_side_literal(kw.value))
    return (True, None)


def _node_is_duplicate_close(
    node: ast.Call,
    *,
    position_names: frozenset[str],
    entered_sides: set[str],
    all_sides_covered: bool,
    spec: Any,
) -> bool:
    """True iff ``node`` closes (or brackets) a position side the engine owns.

    Pre: ``node`` is an ``on_bar``-reachable ``ctx.submit_order(...)`` call;
    ``position_names`` is the alias set from :func:`_collect_position_aliases`;
    ``entered_sides`` is the spec's entered side set; ``all_sides_covered`` is
    True iff the engine covers every entered side.
    Post: True when the order's resolved closed / bracket side is
    engine-covered (intersected with ``entered_sides``, which also drops a
    same-side full-size scale-in). A side that cannot be resolved statically
    is a duplicate only when ``all_sides_covered`` — otherwise it may
    legitimately retire an engine-uncovered side and is deferred to the
    runtime gate. Extracted from
    :meth:`CodeConformanceGate._check_no_duplicate_engine_exit` (which owns the
    full policy rationale).
    """
    resolved_sides: set[str] = set()
    unresolved = False
    if _submit_order_qty_references_position(node, position_names):
        side = _submit_order_close_side(node)
        if side is not None:
            resolved_sides.add(side)
        else:
            unresolved = True
    has_bracket, bracket_side = _submit_order_attached_bracket(node)
    if has_bracket:
        # Bracket closes the order's OWN side.
        if bracket_side is not None:
            resolved_sides.add(bracket_side)
        else:
            unresolved = True
    # A real close retires a side the spec actually enters; the intersection
    # also drops a same-side scale-in whose inferred opposite side the spec
    # never enters.
    resolved_sides &= entered_sides
    if any(_engine_exits_cover_sides(spec, {s}) for s in resolved_sides):
        return True
    # A dynamic (unresolved) side is a duplicate only when the engine covers
    # every entered side — otherwise it may be the legitimate close of an
    # engine-uncovered side.
    return unresolved and all_sides_covered


def _node_references_ctx_equity(node: ast.AST) -> bool:
    """True iff ``node`` (or any sub-expression) references ``ctx.equity``
    or ``ctx.capital``. Used by the sizing check to confirm the account
    value flows into the qty calculation somewhere in the same scope.
    """
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr in ("equity", "capital")
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "ctx"
        ):
            return True
    return False


def _qty_is_constant_int(call: ast.Call) -> bool:
    """True iff the call's ``qty=`` is a literal int (the anti-pattern).

    Excludes ``bool`` even though ``True`` / ``False`` are ``int``
    subclasses — a boolean qty is a different anti-pattern that the
    sizing check would misreport as "literal integer" otherwise.
    """
    for kw in call.keywords:
        if kw.arg != "qty":
            continue
        return (
            isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, int)
            and not isinstance(kw.value.value, bool)
        )
    return False


def _iter_strategy_methods(
    cls: ast.ClassDef,
) -> Iterable[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Yield every method directly defined on ``cls`` (excludes nested defs)."""
    for node in ast.iter_child_nodes(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _methods_reachable_from(cls: ast.ClassDef, roots: Iterable[str]) -> frozenset[str]:
    """Return method names reachable from ``roots`` via
    ``self.<method>(...)`` calls (transitively).

    Used by every check that needs "is this code actually executed at
    runtime?" — entry/exit coverage walks from ``on_bar`` only, while
    check #9 (side-effects) walks from every allowed hook so a private
    helper reachable from ``on_fill`` is not flagged as a dead helper.
    """
    methods = {m.name: m for m in _iter_strategy_methods(cls)}
    reachable: set[str] = {r for r in roots if r in methods}
    worklist: List[str] = list(reachable)
    while worklist:
        name = worklist.pop()
        method = methods.get(name)
        if method is None:
            continue
        for node in _iter_method_body_nodes(method):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                continue
            callee = node.func.attr
            if callee in methods and callee not in reachable:
                reachable.add(callee)
                worklist.append(callee)
    return frozenset(reachable)


def _methods_reachable_from_on_bar(cls: ast.ClassDef) -> frozenset[str]:
    """BFS from ``on_bar`` — used by entry/exit coverage checks."""
    return _methods_reachable_from(cls, ("on_bar",))


def _methods_reachable_from_hooks(cls: ast.ClassDef) -> frozenset[str]:
    """BFS from every allowed hook (``on_bar`` / ``on_fill`` / ``on_end``)
    — used by check #9 (side-effects) so a private helper reachable
    from ``on_fill`` is not treated as dead code.
    """
    return _methods_reachable_from(cls, _ALLOWED_HOOK_NAMES)


def _find_if_branches_with_submit_order(
    cls: ast.ClassDef,
    *,
    reachable_method_names: Optional[frozenset[str]] = None,
) -> List[tuple[ast.If, List[ast.Call]]]:
    """Walk methods on ``cls`` and yield ``(if_node, submit_calls)``
    pairs where ``submit_calls`` is a non-empty list of ``submit_order``
    calls in that ``If``'s body or its ``orelse`` branches.

    When ``reachable_method_names`` is supplied, methods not in that set
    are skipped — used by entry/exit coverage so dead code in unused
    helpers cannot satisfy the gate.

    Each top-level ``If`` body and each ``elif`` chain branch is treated
    as a separate "branch": ``if A: submit(); elif B: submit()`` yields
    two pairs. Branches nested inside loops or other ``If``s are also
    walked (the engine reaches them too).
    """
    out: List[tuple[ast.If, List[ast.Call]]] = []
    for method in _iter_strategy_methods(cls):
        if reachable_method_names is not None and method.name not in reachable_method_names:
            continue
        for node in _iter_method_body_nodes(method):
            if not isinstance(node, ast.If):
                continue
            for branch in _iter_if_branches(node):
                calls = [
                    sub for sub in _iter_branch_body_nodes(branch) if _is_submit_order_call(sub)
                ]
                if calls:
                    out.append((branch, calls))
    return out


def _iter_if_branches(node: ast.If) -> Iterable[ast.If]:
    """Yield every ``If`` node in an ``if/elif/.../else`` chain rooted at
    ``node``.

    Python represents ``elif`` as a single-element ``orelse`` containing
    another ``If`` node. ``else: ...`` becomes a non-empty ``orelse``
    whose elements are statements (not an ``If``); the gate ignores
    plain ``else`` branches because they have no test referencing
    indicators.
    """
    cur: Optional[ast.If] = node
    while isinstance(cur, ast.If):
        yield cur
        if len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
            cur = cur.orelse[0]
        else:
            cur = None


def _iter_branch_body_nodes(branch: ast.If) -> Iterable[ast.AST]:
    """Yield every node directly in ``branch.body`` without descending
    past nested ``def`` / ``class`` / ``lambda`` / ``If`` boundaries.

    Stopping at nested ``If`` boundaries is what makes branch-coverage
    counts honest: ``if A: if B: submit()`` is one logical entry, not
    two. The inner ``If`` is enumerated separately as its own branch
    by ``_find_if_branches_with_submit_order``, so its ``submit_order``
    is credited once — to the innermost branch — not double-counted
    against both the outer and inner.
    """
    stack: List[ast.AST] = list(branch.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.If)
        ):
            continue
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def _names_referenced(expr: ast.AST) -> set[str]:
    """Return the set of all ``Name.id`` and ``Call`` func-names within ``expr``."""
    out: set[str] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            out.add(node.id)
        if isinstance(node, ast.Call):
            name = _get_call_name(node)
            if name:
                out.add(name)
    return out


def _find_enclosing_funcdef(
    tree: ast.AST, target: ast.AST
) -> Optional[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return the innermost ``FunctionDef`` whose body contains ``target``,
    or ``None`` when ``target`` is at module scope.

    Walks every function definition and uses ``_iter_method_body_nodes``
    (which stops at nested ``def`` / ``class`` / ``lambda`` boundaries)
    so the innermost direct enclosing scope is the one that matches.
    """
    enclosing: Optional[ast.FunctionDef | ast.AsyncFunctionDef] = None
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in _iter_method_body_nodes(func):
            if sub is target:
                enclosing = func
                break
    return enclosing


def _is_engine_managed(spec: Any) -> bool:
    """True when the engine dispatchers own entry and exit decisions for this spec.

    Pre:  ``spec`` is a ``StrategySpec`` or ``None``.
    Post: True when entry rules exist AND ``requires_custom_code`` is False —
    the compiled output is a thin indicator shim with zero ``submit_order``
    calls, and all orders come from ``_EngineEntryDispatcher`` /
    ``_EngineExitDispatcher``.
    """
    if spec is None:
        return False
    if getattr(spec, "requires_custom_code", False):
        return False
    entry_rules = getattr(spec, "entry_rules", None)
    return bool(entry_rules)


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
    spec: Any

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
        spec: Any,
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

            results: List[QualityGateResult] = []
            results.extend(self._check_indicator_presence(cctx))
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
    def _check_indicator_presence(self, cctx: _ConformanceCtx) -> Iterable[QualityGateResult]:
        results: List[QualityGateResult] = []
        # A malformed ``ctx.indicator(...)`` read fails at runtime regardless of
        # whether the spec requires that indicator — flag it here (with a precise
        # message) so it is refined, rather than raising in a sandbox where the
        # shadow gate would swallow the exception into a confusing no-trade run.
        target_symbols = frozenset(getattr(cctx.spec, "target_symbols", None) or [])
        for detail in _invalid_ctx_indicator_reads(cctx.cls, cctx.reachable, target_symbols):
            results.append(self._critical(detail))

        required = _collect_required_indicators(cctx.spec)
        if required:
            # Indicator reads only count when they are actually executed at
            # runtime: walk methods reachable from on_bar (Codex PR #588 P1). Two
            # recognised forms — the engine-backed ``ctx.indicator('<name>', ...)``
            # accessor (preferred) and the legacy named call (e.g. ``sma(...)``,
            # which the deterministic compiler still emits).
            call_names = _collect_called_names_in_methods(cctx.cls, cctx.reachable)
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
            # actually selects them.
            required_derived = _required_bollinger_derived_bands(cctx.spec)
            if required_derived:
                produced = _collect_produced_bollinger_bands(cctx.cls, cctx.reachable)
                for band in sorted(required_derived - produced):
                    results.append(
                        self._critical(
                            f"Spec requires the Bollinger '{band}' band but no method "
                            "reachable from on_bar produces it. Read it via "
                            f"``ctx.indicator('bollinger', ..., band='{band}')`` (preferred) "
                            f"or ``bollinger_bands(..., select='{band}')``; a plain "
                            "bollinger_bands(...) call returns only upper/middle/lower."
                        )
                    )
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
