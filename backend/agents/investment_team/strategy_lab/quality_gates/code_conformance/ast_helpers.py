"""AST-analysis helpers backing the :mod:`code_conformance` gate.

Pure free functions that inspect a strategy's parsed AST (or a
``StrategySpec``) and return structural facts — indicator reads, submit_order
call shapes, method reachability, and the like. No :class:`QualityGateResult`
construction happens here; that stays in :mod:`code_conformance` (the gate),
which composes these helpers into checks.
"""

from __future__ import annotations

import ast
from typing import Any, Iterable, List, Optional, get_args

from ...spec_dsl import (
    _INDICATOR_PARAM_SPECS,
    INDICATOR_HELPER_NAME,
    EntryRule,
    SignalExitRule,
    Source,
    iter_tree_indicator_refs,
)
from ..code_safety import (
    _collect_position_aliases,
    _engine_exits_cover_sides,
    _expr_references_position_qty,
    _extract_side_literal,
    _is_ctx_position_call,
)
from ..code_safety_ast import _get_call_name, _iter_method_body_nodes

# Hook methods on the Strategy class within which ``ctx.submit_order`` is
# allowed. Helper methods whose names start with "_" are also allowed
# because the existing :class:`CodeSafetyChecker` order-flow gate already
# requires reachable order flow to originate in ``on_bar``; the conformance
# gate's role here is to forbid stray submissions in unrelated methods
# (e.g. ``__init__`` or a public ``run`` wrapper).
_ALLOWED_HOOK_NAMES: frozenset[str] = frozenset({"on_bar", "on_fill", "on_end"})

# DSL → set of acceptable AST call-name(s) for the indicator's named
# implementation. These are the REAL callable helper names a named call must
# resolve to — only names the sandbox ``indicators`` module actually exports —
# derived from the single ``spec_dsl.INDICATOR_HELPER_NAME`` source (which carries
# the load-time coverage guard). Most map 1:1 with the indicator name; the
# channel/band indicators map to their helper (``bollinger`` → ``bollinger_bands``,
# ``donchian`` → ``donchian_channels``, ``keltner`` → ``keltner_channels``). The bare
# DSL name is intentionally NOT an alias here: ``donchian``/``keltner``/``bollinger``
# are not exported callables, so a bare ``donchian(...)`` call would
# ``NameError``/``ImportError`` at runtime and must not satisfy the gate. The DSL name
# is credited separately via the ``ctx.indicator('<name>', ...)`` accessor in
# :meth:`_check_indicator_presence`.
_INDICATOR_ALLOWED_CALL_NAMES: dict[str, frozenset[str]] = {
    name: frozenset({helper}) for name, helper in INDICATOR_HELPER_NAME.items()
}

# Every name a strategy might plausibly call as ``self.<name>(...)`` intending an
# indicator: BOTH the exported helper names (``bollinger_bands``,
# ``donchian_channels``, … — what the deterministic compiler emits as inline
# ``self.<helper>`` methods) AND the bare DSL names (``bollinger``, ``donchian``,
# ``keltner`` — what a hand/LLM author sees in the spec and may copy verbatim).
# For 13 of the 16 indicators the two coincide; the union covers the three where
# they differ. The base ``Strategy`` defines none of these, so an undefined
# ``self.<name>(...)`` to any of them raises ``AttributeError`` at runtime.
_KNOWN_INDICATOR_HELPER_NAMES: frozenset[str] = frozenset(_INDICATOR_ALLOWED_CALL_NAMES).union(
    *_INDICATOR_ALLOWED_CALL_NAMES.values()
)

# Names recognised as the position-snapshot receiver in exit branches.
_POSITION_RECEIVER_NAMES: frozenset[str] = frozenset({"position", "pos"})

# Attributes a position snapshot actually exposes. Mirrors the fields of
# ``trading_service.strategy.contract._PositionSnapshot`` plus the ``quantity``
# read-only alias for ``qty``. Any OTHER attribute read on a position snapshot
# (``pos.size``, ``pos.shares``, …) raises ``AttributeError`` at runtime and
# aborts the backtest, so the gate rejects it pre-execution. A unit test asserts
# this stays in sync with the model so the two can never silently drift apart.
_POSITION_SNAPSHOT_ATTRS: frozenset[str] = frozenset(
    {"symbol", "side", "qty", "quantity", "entry_price", "entry_timestamp"}
)

# Valid ``source`` values for source-aware indicators (mirrors spec_dsl.Source).
_VALID_SOURCES: frozenset[str] = frozenset(get_args(Source))


def _iter_required_indicator_refs(spec: Any):
    """Yield every ``IndicatorRef`` the generated code is required to read.

    Pre: ``spec`` is a ``StrategySpec`` or ``None``.
    Post: yields entry-rule refs on both paths; ``SignalExitRule`` refs only on
    the compiled path — on the custom-code path exits are engine-owned (the
    engine computes their indicators via ``_EngineExitDispatcher`` and the
    strategy authors no exit branch), so requiring the code to read an exit-only
    indicator would contradict the engine-owned-exits contract. This is the
    single encoding of the "which rules count as required" policy; both
    :func:`_collect_required_indicators` and :func:`_required_bollinger_derived_bands`
    project it so they can never disagree on scope.
    """
    if spec is None:
        return
    for rule in getattr(spec, "entry_rules", []) or []:
        if isinstance(rule, EntryRule):
            yield from iter_tree_indicator_refs(rule.when)
    if not getattr(spec, "requires_custom_code", False):
        for rule in getattr(spec, "exit_rules", []) or []:
            if isinstance(rule, SignalExitRule):
                yield from iter_tree_indicator_refs(rule.when)


def _collect_required_indicators(spec: Any) -> set[str]:
    """Indicator names the generated code must read at runtime.

    Pre: ``spec`` is a ``StrategySpec`` or ``None``.
    Post: returns the union of indicator names required per the policy in
    :func:`_iter_required_indicator_refs`.
    """
    return {ref.name for ref in _iter_required_indicator_refs(spec)}


# Bollinger bands the ``bollinger_bands`` helper returns directly, so a plain
# call reads them and no selector is required.
_BOLLINGER_BASE_BANDS: frozenset[str] = frozenset({"upper", "middle", "lower"})
# Derived bands are every other accepted ``bollinger`` band — currently
# ``percent_b``/``bandwidth`` — which the helper does NOT return; they only
# materialise when explicitly selected. Derived from the DSL band validator so a
# future derived band added to spec_dsl is picked up automatically (the
# ``.allowed`` attribute is set by ``spec_dsl._one_of``); the empty-set guard
# fails loudly at import if that contract ever changes.
_BOLLINGER_DERIVED_BANDS: frozenset[str] = (
    frozenset(_INDICATOR_PARAM_SPECS["bollinger"]["optional"]["band"][1].allowed)
    - _BOLLINGER_BASE_BANDS
)
if not _BOLLINGER_DERIVED_BANDS:
    raise RuntimeError(
        "Bollinger band validator exposes no derived bands beyond "
        f"{sorted(_BOLLINGER_BASE_BANDS)}; spec_dsl._one_of.allowed contract changed."
    )


def _required_bollinger_derived_bands(spec: Any) -> set[str]:
    """Derived Bollinger bands (e.g. ``percent_b``/``bandwidth``) the code must produce.

    Pre: ``spec`` is a ``StrategySpec`` or ``None``.
    Post: returns the derived bands any required Bollinger ref selects, over the
    same rule scope as :func:`_collect_required_indicators` (both project
    :func:`_iter_required_indicator_refs`). Base bands (upper/middle/lower) are
    not tracked — the ``bollinger_bands`` helper returns them directly, so a
    plain call suffices; the derived bands need the selector, so they are
    credited only by a band-matched read.
    """
    return {
        ref.param("band")
        for ref in _iter_required_indicator_refs(spec)
        if ref.name == "bollinger" and ref.param("band") in _BOLLINGER_DERIVED_BANDS
    }


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


def _keyword_node(node: ast.Call, key: str) -> Optional[ast.keyword]:
    """Return ``node``'s ``key=`` keyword AST node, or ``None`` when absent."""
    for kw in node.keywords:
        if kw.arg == key:
            return kw
    return None


def _literal_str(node: Optional[ast.AST]) -> Optional[str]:
    """Return the value of a string-constant AST node, else ``None`` (dynamic)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_self_call(node: ast.Call, attr: str) -> bool:
    """True iff ``node`` is a ``self.<attr>(...)`` method call."""
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == attr
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    )


def _class_defines_method(cls: ast.ClassDef, name: str) -> bool:
    """True iff ``cls`` directly defines a method named ``name``.

    Pre: ``cls`` is a Strategy ClassDef. Post: True only when :func:`_iter_strategy_methods`
    (the module's single definition of "method directly defined on ``cls``") yields
    a ``def name`` — used to confirm that a ``self.<name>(...)`` call resolves to a
    real method rather than an ``AttributeError`` (the base ``Strategy`` provides
    no indicator helpers; only the compiler's emitted module defines them inline).
    """
    return any(m.name == name for m in _iter_strategy_methods(cls))


def _collect_produced_bollinger_bands(
    cls: ast.ClassDef, method_names: frozenset[str]
) -> tuple[set[str], bool]:
    """Bollinger selectors the on_bar-reachable code produces, and whether any is dynamic.

    Pre: ``cls`` is the Strategy ClassDef; ``method_names`` are the on_bar-reachable
    methods.
    Post: returns ``(produced, dynamic)``. ``produced`` holds the string-literal
    bands materialised by a valid selector-aware read:

      * ``ctx.indicator('bollinger', ..., band='<b>')`` — the custom-code accessor;
      * ``self.bollinger_bands(..., select='<b>')`` — ONLY when the class actually
        defines a ``bollinger_bands`` method (the compiler's emitted inline helper).
        The base ``Strategy`` provides no such method, so a custom strategy writing
        ``self.bollinger_bands`` without defining it would ``AttributeError`` at
        runtime; it is not credited here and is flagged by
        :func:`_invalid_bollinger_select_calls`.

    ``dynamic`` is True when such a read carries a NON-literal band/select
    (``band=self.BAND``): the value is runtime-valid but unresolvable statically,
    so the caller abstains rather than demanding a band it cannot confirm — matching
    the gate's abstain-on-dynamic policy elsewhere. A plain ``bollinger_bands(...)``
    with no ``select=`` yields only base bands and produces nothing here.
    """
    defines_helper = _class_defines_method(cls, "bollinger_bands")
    produced: set[str] = set()
    dynamic = False
    for method in _iter_strategy_methods(cls):
        if method.name not in method_names:
            continue
        for node in _iter_method_body_nodes(method):
            if not isinstance(node, ast.Call):
                continue
            if _is_ctx_indicator_call(node) and _ctx_indicator_arg_name(node) == "bollinger":
                kw = _keyword_node(node, "band")
            elif _is_self_call(node, "bollinger_bands") and defines_helper:
                kw = _keyword_node(node, "select")
            else:
                continue
            if kw is None:
                continue
            lit = _literal_str(kw.value)
            if lit is not None:
                produced.add(lit)
            else:
                dynamic = True
    return produced, dynamic


def _iter_reachable_calls(cls: ast.ClassDef, method_names: frozenset[str]) -> Iterable[ast.Call]:
    """Yield every ``ast.Call`` in the bodies of ``cls`` methods reachable from on_bar.

    Pre: ``cls`` is the Strategy ClassDef; ``method_names`` are the on_bar-reachable
    method names. Post: each call node executed at runtime, once, in source order —
    the shared traversal behind the per-call conformance checks so they don't each
    re-walk the class body.
    """
    for method in _iter_strategy_methods(cls):
        if method.name not in method_names:
            continue
        for node in _iter_method_body_nodes(method):
            if isinstance(node, ast.Call):
                yield node


def _undefined_self_indicator_helper_calls(
    cls: ast.ClassDef, method_names: frozenset[str]
) -> list[str]:
    """Reachable ``self.<helper>(...)`` calls to an indicator helper the class never defines.

    Pre: ``cls`` is the Strategy ClassDef; ``method_names`` are on_bar-reachable.
    Post: one message per distinct indicator-helper name (``sma``, ``macd``,
    ``bollinger_bands``, ``donchian_channels``, …) that is called as ``self.<name>``
    but not defined on the class. The compiler emits these helpers as inline
    ``self.<name>`` methods and DOES define them, so compiled strategies are clean;
    a custom (hand/LLM-authored) strategy that copies the ``self.<helper>`` calling
    convention without emitting the helper body raises ``AttributeError`` on the
    first bar. Flag it (like invalid ctx reads) so the refinement loop fixes the
    call — read indicators via ``ctx.indicator('<name>', ...)`` or the imported
    named helper (e.g. ``sma(bars, 50)``) instead.
    """
    defined = {m.name for m in _iter_strategy_methods(cls)}
    flagged: set[str] = set()
    out: list[str] = []
    for node in _iter_reachable_calls(cls, method_names):
        if not isinstance(node.func, ast.Attribute):
            continue
        recv = node.func.value
        helper = node.func.attr
        if (
            isinstance(recv, ast.Name)
            and recv.id == "self"
            and helper in _KNOWN_INDICATOR_HELPER_NAMES
            and helper not in defined
            and helper not in flagged
        ):
            flagged.add(helper)
            out.append(
                f"self.{helper}(...) is called but the strategy defines no '{helper}' "
                "method; the base Strategy provides no indicator helpers, so this raises "
                "AttributeError at runtime. Read indicators via "
                "``ctx.indicator('<name>', ...)`` (preferred) or the imported named helper "
                f"(e.g. ``sma(bars, 50)``), not ``self.{helper}(...)``."
            )
    return out


def _invalid_bollinger_select_calls(
    cls: ast.ClassDef, method_names: frozenset[str], import_aliases: Optional[dict[str, str]] = None
) -> list[str]:
    """Reachable NON-self ``bollinger_bands(..., select=...)`` calls (a runtime TypeError).

    Pre: ``cls`` is the Strategy ClassDef; ``method_names`` are on_bar-reachable;
    ``import_aliases`` maps ``from indicators import bollinger_bands as bb`` bindings.
    Post: one message per bare / ``indicators.``-qualified / aliased
    ``bollinger_bands(..., select=...)`` call. The sandbox scalar helper
    ``bollinger_bands(data, period=20, num_std=2.0)`` has no ``select`` param, so
    such a call raises ``TypeError``. ``self.bollinger_bands`` is excluded here: the
    compiler's inline helper accepts ``select`` (valid), and an UNDEFINED
    ``self.bollinger_bands`` is caught generically by
    :func:`_undefined_self_indicator_helper_calls` (an ``AttributeError``, not this
    ``TypeError``).
    """
    aliases = import_aliases or {}
    out: list[str] = []
    for node in _iter_reachable_calls(cls, method_names):
        if _keyword_node(node, "select") is None:
            continue
        call_name = _get_call_name(node)
        if aliases.get(call_name, call_name) != "bollinger_bands":
            continue
        if _is_self_call(node, "bollinger_bands"):
            continue  # compiler inline helper (valid) or undefined-self (caught elsewhere)
        out.append(
            "bollinger_bands(..., select=...) is invalid: the sandbox "
            "``indicators.bollinger_bands(data, period, num_std)`` helper has no "
            "``select`` param and raises TypeError at runtime. Read a derived "
            "Bollinger band via ``ctx.indicator('bollinger', ..., band='percent_b')``."
        )
    return out


def _collect_import_aliases(tree: ast.AST) -> dict[str, str]:
    """Map ``from indicators import <real> as <alias>`` bindings: alias -> real name.

    Pre: ``tree`` is the parsed module AST of the strategy source.
    Post: returns ``{alias: real_name}`` for every aliased import from the sandbox
    ``indicators`` module, so a call to the alias can be credited as the real
    exported helper it binds. Non-aliased imports (already matched by their own
    name) and imports from other modules are ignored.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "indicators":
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
    return aliases


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


# ---------------------------------------------------------------------------
# Custom-code faithfulness: the executed strategy must read indicators on the
# same source/params the spec authored, must not disable a spec condition with a
# falsy guard, and must not read a non-existent position attribute. These are
# the three faithful-execution defects that let LLM-authored ``on_bar`` code
# diverge from the specification while still passing the older presence checks.
# ---------------------------------------------------------------------------


def _literal_value(node: Optional[ast.AST]) -> tuple[bool, Any]:
    """Return ``(is_literal, value)`` for an AST node.

    Pre: ``node`` is an AST node or ``None``.
    Post: ``is_literal`` is True only for an ``ast.Constant`` holding a
    ``str``/``int``/``float`` (the value types indicator params and sources take);
    otherwise ``(False, None)`` so a caller comparing against a spec value
    abstains on an unresolved (``Name``/``Attribute``/computed) operand rather
    than guessing.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)):
        return True, node.value
    return False, None


def _authorized_indicator_signatures(spec: Any) -> dict[str, list[dict[str, Any]]]:
    """Map each spec-required indicator name → the signatures the spec authorizes.

    Pre: ``spec`` is a ``StrategySpec`` or ``None``.
    Post: for every ``IndicatorRef`` in :func:`_iter_required_indicator_refs`,
    records one signature ``{"source": ref.source, **ref.params}`` (spec_dsl has
    already default-filled the ref's optional params). This is the faithful
    contract a ``ctx.indicator('<name>', ...)`` read must satisfy — same
    ``source`` and same params. An indicator name absent from the result is not
    required by the spec, so a read of it is left un-checked (an extra/helper
    read is another gate's concern, not a faithfulness defect here).
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for ref in _iter_required_indicator_refs(spec):
        sig: dict[str, Any] = {"source": ref.source}
        sig.update(dict(ref.params))
        out.setdefault(ref.name, []).append(sig)
    return out


def _ctx_indicator_pinned_fields(node: ast.Call) -> dict[str, Any]:
    """The source/param fields a ``ctx.indicator(...)`` call pins to a literal.

    Pre: ``node`` is a ``ctx.indicator(...)`` call.
    Post: returns ``{field: literal_value}`` for every keyword pinned to a
    literal, EXCLUDING ``name``/``symbol`` and any ``**kwargs`` spread. ``source``
    is always present: an omitted ``source`` reads the runtime default ``'close'``
    (a firm pin), an explicit literal source pins that value, and a dynamic
    ``source=`` is treated as unpinned (dropped) so it is not falsely compared.
    Dynamic params (``period=self.WINDOW``) are likewise dropped.
    """
    pinned: dict[str, Any] = {}
    source_seen = False
    for kw in node.keywords:
        if kw.arg is None or kw.arg in ("name", "symbol"):
            continue
        is_lit, val = _literal_value(kw.value)
        if kw.arg == "source":
            source_seen = True
            if is_lit:
                pinned["source"] = val
            continue
        if is_lit:
            pinned[kw.arg] = val
    if not source_seen:
        pinned["source"] = "close"
    return pinned


def _ctx_indicator_spec_divergence(
    node: ast.Call, authorized: dict[str, list[dict[str, Any]]]
) -> Optional[str]:
    """Message when a ``ctx.indicator(...)`` read diverges from the spec, else ``None``.

    Pre: ``node`` is a ``ctx.indicator(...)`` call; ``authorized`` is
    :func:`_authorized_indicator_signatures` for the spec.
    Post: returns ``None`` when the name is dynamic, the indicator is not
    spec-required, or the call matches at least one authorized signature on every
    field it pins to a literal. Otherwise returns a critical message naming the
    divergent field(s) — the faithful-execution defect where code reads an
    indicator on a different ``source``/params than the spec declared (e.g.
    ``source='low'`` against a ``source='close'`` spec). Matching is lenient on
    unpinned/dynamic fields, so only a demonstrable literal mismatch is flagged.
    """
    name = _ctx_indicator_arg_name(node)
    if name is None or name not in authorized:
        return None
    sigs = authorized[name]
    # Compare only fields the spec's signatures actually carry (source + the
    # indicator's real params). An unknown/typo'd key (``perod=20``) is left to
    # the malformed-call checker, not treated as a source/params divergence.
    valid_fields: set[str] = set().union(*(sig.keys() for sig in sigs))
    pinned = {
        field: value
        for field, value in _ctx_indicator_pinned_fields(node).items()
        if field in valid_fields
    }
    for sig in sigs:
        if all(sig.get(field) == value for field, value in pinned.items()):
            return None  # conforms to at least one authorized IndicatorRef
    # Divergent. Prefer a precise per-field message; fall back to the whole
    # combination when each field is individually authorized but the pairing is not.
    per_field: list[str] = []
    for field, value in pinned.items():
        allowed = sorted((sig.get(field) for sig in sigs if sig.get(field) is not None), key=str)
        if value not in allowed:
            per_field.append(f"{field}={value!r} but the spec authorizes {field} in {allowed}")
    if per_field:
        return (
            f"ctx.indicator('{name}', ...) diverges from the spec: {'; '.join(per_field)}. "
            "Read the indicator on the same source/params the spec's IndicatorRef declares "
            "so the executed trades faithfully implement the specification."
        )
    return (
        f"ctx.indicator('{name}', ...) uses the field combination {pinned!r}, which matches "
        f"no IndicatorRef the spec authored for '{name}' (authorized: {sigs}). Read the "
        "indicator on a source/params combination the spec declares."
    )


def _divergent_ctx_indicator_reads(
    cls: ast.ClassDef, method_names: frozenset[str], spec: Any
) -> list[str]:
    """De-duplicated spec-divergence messages for reachable ``ctx.indicator(...)`` reads.

    Pre: ``cls`` is the Strategy ClassDef; ``method_names`` are on_bar-reachable.
    Post: one message per distinct divergence (see :func:`_ctx_indicator_spec_divergence`);
    empty when the spec requires no indicators (nothing to be faithful to).
    """
    authorized = _authorized_indicator_signatures(spec)
    if not authorized:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for node in _iter_ctx_indicator_calls(cls, method_names):
        msg = _ctx_indicator_spec_divergence(node, authorized)
        if msg and msg not in seen:
            seen.add(msg)
            out.append(msg)
    return out


def _ctx_indicator_bound_names(cls: ast.ClassDef, method_names: frozenset[str]) -> frozenset[str]:
    """Local names bound directly from a ``ctx.indicator(...)`` call in reachable code.

    Pre: ``cls`` is the Strategy ClassDef; ``method_names`` are on_bar-reachable.
    Post: every ``<name>`` in a single-target ``<name> = ctx.indicator(...)`` — the
    values that are ``None`` during warm-up and may legitimately be ``0.0`` (e.g. a
    volume SMA on a flat window), so a falsy guard on them silently drops a check.
    """
    names: set[str] = set()
    for method in _iter_strategy_methods(cls):
        if method.name not in method_names:
            continue
        for node in _iter_method_body_nodes(method):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and _is_ctx_indicator_call(node.value)
            ):
                names.add(node.targets[0].id)
    return frozenset(names)


def _is_bare_indicator_truthiness(node: ast.AST, bound_names: frozenset[str]) -> bool:
    """True iff ``node`` is a bare truthiness read of an indicator value.

    Matches a bare ``ast.Name`` bound from ``ctx.indicator(...)`` or a bare
    ``ctx.indicator(...)`` call used directly where its truth value is taken. A
    comparison (``x is None``, ``x > y``) wraps the value in an explicit test and
    is therefore NOT bare — only the unwrapped forms are matched.
    """
    if isinstance(node, ast.Name):
        return node.id in bound_names
    return _is_ctx_indicator_call(node)


def _indicator_falsy_guard_errors(cls: ast.ClassDef, method_names: frozenset[str]) -> list[str]:
    """Reachable falsy-guards on an indicator value (``if vol_sma and ...``).

    Pre: ``cls`` is the Strategy ClassDef; ``method_names`` are on_bar-reachable.
    Post: one de-duplicated message per distinct bare-truthiness read of a
    ctx.indicator value. ``ctx.indicator(...)`` returns ``None`` during warm-up and
    can be a legitimate ``0.0`` (a flat-window volume SMA); the idiom
    ``if vol_sma and bar.volume > vol_sma:`` treats BOTH as "skip the filter",
    silently dropping a spec-required condition. The faithful form guards with an
    explicit ``is None`` check so a ``0.0`` value still gates the order.
    """
    bound = _ctx_indicator_bound_names(cls, method_names)
    out: list[str] = []
    seen: set[str] = set()

    def _flag(operand: ast.AST) -> None:
        if not _is_bare_indicator_truthiness(operand, bound):
            return
        label = (
            operand.id
            if isinstance(operand, ast.Name)
            else f"ctx.indicator('{_ctx_indicator_arg_name(operand)}', ...)"
        )
        msg = (
            f"Indicator value '{label}' is used as a bare truthiness test (e.g. "
            "``if <ind> and ...`` / ``if not <ind>``). ctx.indicator(...) returns None "
            "during warm-up and can be a legitimate 0.0 (a flat-window volume SMA), so a "
            "falsy guard silently skips the spec-required condition. Guard it explicitly "
            "with ``is None`` / ``is not None`` instead."
        )
        if msg not in seen:
            seen.add(msg)
            out.append(msg)

    for method in _iter_strategy_methods(cls):
        if method.name not in method_names:
            continue
        for node in _iter_method_body_nodes(method):
            if isinstance(node, ast.BoolOp):
                for operand in node.values:
                    _flag(operand)
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                _flag(node.operand)
            elif isinstance(node, (ast.If, ast.While, ast.IfExp)):
                _flag(node.test)
    return out


def _invalid_position_attr_errors(cls: ast.ClassDef, method_names: frozenset[str]) -> list[str]:
    """Reachable ``<position>.<attr>`` reads of an attribute the snapshot lacks.

    Pre: ``cls`` is the Strategy ClassDef; ``method_names`` are on_bar-reachable.
    Post: one de-duplicated message per distinct ``<name>.<attr>`` value read where
    ``<name>`` is a position alias (``position``/``pos`` or a var bound from
    ``ctx.position(...)``) and ``<attr>`` is not in :data:`_POSITION_SNAPSHOT_ATTRS`.
    Such a read raises ``AttributeError`` at runtime and aborts the backtest (the
    reported ``'_PositionSnapshot' object has no attribute 'quantity'`` — now an
    alias, but a typo like ``.size``/``.shares`` still crashes). Method-call
    receivers (``pos.model_dump()``) are excluded — the snapshot inherits pydantic
    methods this gate does not enumerate.
    """
    position_names = _collect_position_aliases(cls)
    out: list[str] = []
    seen: set[str] = set()
    for method in _iter_strategy_methods(cls):
        if method.name not in method_names:
            continue
        nodes = list(_iter_method_body_nodes(method))
        call_func_ids = {
            id(n.func)
            for n in nodes
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        for node in nodes:
            if id(node) in call_func_ids:
                continue  # ``pos.something(...)`` — a method call, not a field read
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in position_names
                and node.attr not in _POSITION_SNAPSHOT_ATTRS
                and not node.attr.startswith("__")
            ):
                msg = (
                    f"Position snapshot read '{node.value.id}.{node.attr}' accesses an "
                    f"attribute _PositionSnapshot does not expose (valid: "
                    f"{sorted(_POSITION_SNAPSHOT_ATTRS)}); it raises AttributeError at "
                    "runtime. Use '.qty' for the position size."
                )
                if msg not in seen:
                    seen.add(msg)
                    out.append(msg)
    return out


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
