"""Deterministic post-synthesis injector for the ``UNIVERSE`` constant and
the ``on_bar`` symbol guard.

``CodeConformanceGate`` Check #2 (:meth:`_check_symbol_gate`) requires, when
``spec.target_symbols`` is non-empty, that the generated ``Strategy`` class
declares both:

1. a class-level ``UNIVERSE = frozenset({...})`` constant, and
2. a runtime guard ``if bar.symbol not in self.UNIVERSE: return`` as a
   statement in ``on_bar``.

Both are *fully determined by* ``spec.target_symbols`` — the symbols come
straight from the spec and the guard shape is identical for every strategy.
LLM-generated code routinely omits one or both, which burns refinement rounds
and — without the guard — lets the historical replay stream feed bars for
every fetched symbol into the signal logic, so trades land on the wrong asset.

Rather than ask the LLM to re-emit the same boilerplate correctly on every
generation, this module rewrites the generated source so the two pieces are
always present and gate-conformant, before the conformance gate runs.

Pure source-to-source: no execution, no I/O, no LLM. Models its AST mechanics
on :mod:`coverage_probe.runtime_instrument` (parse → mutate →
``ast.fix_missing_locations`` → ``ast.unparse``; original source returned
unchanged on a no-op or malformed input).
"""

from __future__ import annotations

import ast

from .code_safety_ast import _find_strategy_subclasses, _is_universe_guard_stmt

__all__ = ["inject_universe_and_guard"]


def inject_universe_and_guard(source: str, spec) -> str:
    """Return ``source`` with a canonical ``UNIVERSE`` constant and ``on_bar``
    symbol guard injected from ``spec.target_symbols``.

    The transform strips any pre-existing (well-formed or malformed)
    ``UNIVERSE`` assignment and universe-guard statement first, then inserts
    canonical versions, so it is idempotent and cannot double-inject.

    Preconditions:
        - ``source`` is a ``str``.
        - ``spec`` exposes ``target_symbols`` (an iterable of symbol strings);
          a missing/empty value is treated as "no universe".

    Postconditions:
        - Returns valid Python source (or the original ``source`` verbatim on
          any no-op branch below).
        - When ``spec.target_symbols`` is non-empty and ``source`` parses to
          exactly one ``Strategy`` subclass, the result satisfies
          ``_has_universe_constant``; additionally, when that class has an
          ``on_bar(self, ctx, bar)`` method with at least three positional
          parameters, the result satisfies ``_has_universe_guard_in_on_bar``
          (the guard references the method's actual third parameter name, so
          it never raises ``NameError`` at runtime).
        - Independent of ``spec.requires_custom_code`` — custom code must not
          process non-target bars either, so injection always applies.

    Invariants:
        - Idempotent: ``inject(inject(s)) == inject(s)``.
        - Never raises: malformed source, a missing/duplicated ``Strategy``
          class, or a malformed ``on_bar`` signature all fall through to a
          best-effort or no-op result rather than an exception. The downstream
          conformance / safety gates own the corresponding criticals.
    """
    assert isinstance(source, str), "source must be a string"

    symbols = list(getattr(spec, "target_symbols", None) or [])
    if not symbols:
        # The gate requires nothing when there is no target universe.
        return source

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # The conformance gate emits its own syntax-error critical; leave the
        # source untouched so that diagnostic is preserved.
        return source

    classes = _find_strategy_subclasses(tree)
    if len(classes) != 1:
        # code_safety owns the "exactly one Strategy class" rule; a 0/>1 case
        # has nowhere unambiguous to inject.
        return source
    cls = classes[0]

    if _has_unsupported_universe_binding(cls):
        # ``UNIVERSE`` is bound at class-creation time in a way the strip can't
        # cleanly rewrite (nested in a compound statement, tuple/list unpacking,
        # etc.). Such a binding would run after a prepended canonical constant
        # and silently override it, while the structural gate only sees the
        # prepended one. Bail — leave the source for the existing gates to
        # evaluate rather than produce code that passes the gate but has a stale
        # runtime universe.
        return source

    expected = sorted(set(symbols))
    on_bars = _find_on_bar_methods(cls)
    if (
        _existing_universe_symbols(cls) == expected
        and len(on_bars) == 1
        and _is_canonical_on_bar(on_bars[0])
    ):
        # Already fully canonical: a single UNIVERSE binding listing exactly the
        # spec's symbols, and a single on_bar whose *first* statement is the
        # guard, bound to ``self.UNIVERSE`` and the method's actual bar
        # parameter (the deterministic compiler path always is). Return the
        # source verbatim rather than round-tripping it through ``ast.unparse``,
        # which would reformat the module and strip comments — a needless
        # mutation that also churns the code hash / drift log. Anything short of
        # canonical — stale symbols, a guard buried after trading logic, a guard
        # bound to the wrong name, a non-``self`` receiver, or multiple UNIVERSE
        # / on_bar definitions — falls through and is rewritten below.
        return source

    # 1. Replace the UNIVERSE constant: drop any existing top-level class-body
    #    binding targeting ``UNIVERSE``. A chained ``UNIVERSE = TARGETS = <stale>``
    #    keeps its sibling targets but has their shared value rewritten to the
    #    spec universe, so an alias used as the trading universe can't silently
    #    retain the stale symbol set. Then prepend a fresh canonical binding.
    cls.body = _strip_universe_assignments(cls.body, expected)
    cls.body.insert(0, _build_universe_assign(expected))

    # 2. Guard the on_bar definition(s). The conformance gate inspects the first
    #    definition while Python runs the last, and the guard is only valid for a
    #    ``self`` receiver (the recognizer requires ``self.UNIVERSE`` and the
    #    engine binds positionally). To avoid a green gate over an unguarded
    #    runtime method, guard ALL definitions or NONE: only when every on_bar is
    #    guardable (sync, three params, ``self`` receiver) do we guard them;
    #    otherwise we inject UNIVERSE only and let the missing-guard conformance
    #    critical drive refinement (fail closed). For each, strip only the
    #    existing *bar-parameter* guard (a guard on a different symbol is user
    #    logic and is preserved) — and only when it is bare (no ``else`` body,
    #    whose removal can't drop trading logic) — then prepend the canonical
    #    guard so it runs before any signal logic.
    if on_bars and all(_is_guardable_on_bar(on_bar) for on_bar in on_bars):
        for on_bar in on_bars:
            bar_param = on_bar.args.args[2].arg
            on_bar.body = _strip_bar_guards(on_bar)
            on_bar.body.insert(0, _build_guard_stmt(bar_param))
    elif on_bars:
        # Fail closed: at least one on_bar can't be guarded, so we guard none.
        # The conformance gate only inspects the *first* definition, so a
        # pre-existing guard there would let it pass while the unguarded
        # runtime-effective (last) definition processes non-target bars. Strip
        # every gate-recognized guard from the first definition (dead code when
        # it's a shadowed duplicate; an unguardable single method is invalid
        # regardless) so the missing-guard critical fires and drives refinement.
        first = on_bars[0]
        first.body = [stmt for stmt in first.body if not _is_universe_guard_stmt(stmt)]

    ast.fix_missing_locations(tree)
    try:
        return ast.unparse(tree)
    except Exception:  # pragma: no cover - ast.unparse is robust on parsed trees
        return source


def _targets_universe(node: ast.stmt) -> bool:
    """True iff ``node`` binds the class-level name ``UNIVERSE`` (any value)."""
    if isinstance(node, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id == "UNIVERSE" for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id == "UNIVERSE"
    return False


def _strip_universe_assignments(body: list[ast.stmt], expected: list[str]) -> list[ast.stmt]:
    """Return ``body`` with every ``UNIVERSE`` binding removed.

    A chained assignment such as ``UNIVERSE = TARGETS = frozenset({...})`` keeps
    its sibling targets (the node survives with ``UNIVERSE`` dropped) so aliases
    referenced elsewhere in the class are preserved — but its shared value is
    rewritten to the spec universe (``expected``), so an alias that mirrored
    ``UNIVERSE`` and is used as the trading universe can't silently retain a
    stale symbol set. Only when ``UNIVERSE`` is the sole target is the whole
    statement removed.
    """
    out: list[ast.stmt] = []
    for node in body:
        if isinstance(node, ast.Assign):
            kept = [t for t in node.targets if not (isinstance(t, ast.Name) and t.id == "UNIVERSE")]
            if not kept:
                continue
            if len(kept) != len(node.targets):
                # The dropped target was ``UNIVERSE``; keep the chained aliases
                # in sync with the canonical universe rather than the stale value.
                node.value = _build_universe_assign(expected).value
            node.targets = kept
            out.append(node)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "UNIVERSE"
        ):
            continue
        else:
            out.append(node)
    return out


def _existing_universe_symbols(cls: ast.ClassDef) -> list[str] | None:
    """Return the sorted, de-duplicated string symbols of the class-level
    ``UNIVERSE`` literal, or ``None`` when it cannot be safely matched against
    the spec.

    ``None`` (rather than an empty list) forces the caller to fall through to
    canonical injection — for a missing binding, a malformed value
    (``UNIVERSE = "QQQ"`` — a string, not a collection), a computed expression,
    or **multiple** ``UNIVERSE`` bindings (Python keeps the last, so matching
    only the first would leave a stale runtime universe). An empty but
    well-formed literal (``frozenset()``) returns ``[]``.
    """
    values = [node.value for node in cls.body if _targets_universe(node)]
    if len(values) != 1:
        return None
    value = values[0]
    if value is None:  # bare ``UNIVERSE: frozenset`` annotation with no value
        return None

    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        elts = value.elts
    elif (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in {"frozenset", "set", "tuple", "list"}
    ):
        if not value.args:
            elts = []
        elif len(value.args) == 1 and isinstance(value.args[0], (ast.Set, ast.List, ast.Tuple)):
            elts = value.args[0].elts
        else:
            return None
    else:
        return None

    symbols: set[str] = set()
    for elt in elts:
        if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
            return None
        symbols.add(elt.value)
    return sorted(symbols)


def _build_universe_assign(symbols: list[str]) -> ast.stmt:
    """Build ``UNIVERSE = frozenset({<sorted-deduped symbols>})``.

    Mirrors the deterministic compiler's literal form
    (``frozenset({repr(s), ...})``) so injected and compiled code are
    structurally identical.
    """
    ordered = sorted(set(symbols))
    literal = "frozenset({" + ", ".join(repr(s) for s in ordered) + "})"
    return ast.parse(f"UNIVERSE = {literal}").body[0]


def _build_guard_stmt(bar_param: str) -> ast.stmt:
    """Build ``if <bar_param>.symbol not in self.UNIVERSE: return``.

    The receiver is always ``self`` because the injector normalizes on_bar's
    instance parameter to ``self`` first (see ``_normalize_receiver_to_self``).
    ``self.UNIVERSE`` is both runtime-correct and what the conformance gate's
    ``_has_universe_guard_in_on_bar`` recognizer accepts.
    """
    return ast.parse(f"if {bar_param}.symbol not in self.UNIVERSE:\n    return").body[0]


def _find_on_bar_methods(cls: ast.ClassDef) -> list:
    """Return every ``on_bar`` method (sync or async) defined on ``cls``.

    A class may erroneously define ``on_bar`` more than once: the conformance
    gate inspects the first, Python runs the last. The injector guards all of
    them so the gate-checked and runtime-effective methods stay consistent.
    """
    return [
        node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "on_bar"
    ]


def _bar_parameter_name(on_bar) -> str | None:
    """Return the bar parameter name of ``on_bar(self, ctx, bar)``.

    Returns None when the method is async or does not have *exactly* three
    positional parameters — the harness requires the precise
    ``on_bar(self, ctx, bar)`` arity (code_safety enforces ``== 3``), so a
    4+-parameter overload is invalid and must not be guarded (fail closed; the
    safety/conformance gates own that critical).
    """
    if isinstance(on_bar, ast.AsyncFunctionDef):
        return None
    args = on_bar.args.args
    if len(args) != 3:
        return None
    return args[2].arg


def _is_guardable_on_bar(on_bar) -> bool:
    """True iff a canonical ``self.UNIVERSE`` guard can be injected into ``on_bar``.

    Requires a sync, plain instance method with exactly three positional
    parameters whose instance parameter is ``self`` — the only shape for which
    ``self.UNIVERSE`` is both runtime-correct and accepted by the conformance
    recognizer. A ``@staticmethod`` / ``@classmethod`` ``on_bar`` is rejected:
    the harness calls ``instance.on_bar(ctx, bar)`` expecting a bound ``self``,
    so a static/class method receives the wrong arguments and fails at runtime —
    that must stay a validation failure, not be normalized into passing code.
    """
    return (
        _bar_parameter_name(on_bar) is not None
        and on_bar.args.args[0].arg == "self"
        and not _has_static_or_class_decorator(on_bar)
    )


def _has_static_or_class_decorator(func) -> bool:
    """True iff ``func`` is decorated ``@staticmethod`` / ``@classmethod`` (the
    builtins are always applied bare, as plain ``Name`` decorators)."""
    return any(
        isinstance(dec, ast.Name) and dec.id in {"staticmethod", "classmethod"}
        for dec in func.decorator_list
    )


def _is_strippable_guard(stmt: ast.stmt) -> bool:
    """True iff ``stmt`` is a universe guard whose removal cannot drop logic.

    Only bare guards (no ``else`` body) are strippable; a guard that nests
    trading logic under ``else`` is left in place — the canonical guard is
    prepended ahead of it, which still rejects non-target bars first while
    preserving the original branch's body.
    """
    return _is_universe_guard_stmt(stmt) and not getattr(stmt, "orelse", None)


def _strip_bar_guards(on_bar) -> list[ast.stmt]:
    """Return ``on_bar.body`` with the existing bar-parameter guard(s) removed,
    so the canonical guard can be prepended without duplication.

    A bare universe guard is removed only when its receiver is the bar
    parameter, or a method-local name that is **not yet bound** at the guard's
    position (a misnamed bar guard that would ``UnboundLocalError`` — only names
    bound *before* the guard make a different receiver safe). A guard whose
    receiver is bound before it (an auxiliary symbol filtered by user logic) or
    is not a method-local at all (a module-level/global/closure name) is
    preserved — the injector adds the required bar guard without dropping user
    filtering it can't prove is dead.
    """
    bar_param = on_bar.args.args[2].arg
    assigned = _assigned_names(on_bar)
    bound_before = _param_names(on_bar)
    kept: list[ast.stmt] = []
    for stmt in on_bar.body:
        if _is_strippable_guard(stmt):
            receiver = stmt.test.left.value.id
            if receiver == bar_param or (receiver in assigned and receiver not in bound_before):
                continue
        kept.append(stmt)
        bound_before |= _store_names(stmt)
    return kept


def _param_names(on_bar) -> set[str]:
    """Return on_bar's parameter names (bound from entry)."""
    args = on_bar.args
    names = {a.arg for a in (*getattr(args, "posonlyargs", []), *args.args, *args.kwonlyargs)}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _store_names(node: ast.AST) -> set[str]:
    """Return names bound by ``node`` in its own scope (``Store``-context names),
    not descending into nested function/lambda/class scopes."""
    names: set[str] = set()

    def visit(n: ast.AST) -> None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(node)
    return names


def _assigned_names(on_bar) -> set[str]:
    """Return every method-local name assigned anywhere in ``on_bar`` (used to
    tell a function-local receiver apart from a module-level/global one)."""
    names: set[str] = set()
    for stmt in on_bar.body:
        names |= _store_names(stmt)
    return names


def _has_unsupported_universe_binding(cls: ast.ClassDef) -> bool:
    """True iff ``UNIVERSE`` is bound — at class-creation time or at runtime — in
    a way the strip logic can't cleanly rewrite, so the injector must bail.

    The strip handles only a top-level ``UNIVERSE = ...`` / ``UNIVERSE: T = ...``
    (possibly chained ``UNIVERSE = X = ...``) — a direct ``Name`` target of a
    class-body ``Assign``/``AnnAssign``. Anything else would leave the runtime
    ``self.UNIVERSE`` different from the prepended canonical constant while the
    structural gate only sees the prepended one. Flagged (→ bail):

    - a class-creation-time ``UNIVERSE`` store/delete that isn't a clean
      top-level ``Name`` target — nested in a compound statement, tuple/list
      unpacking, augmented assignment, walrus, or ``del UNIVERSE``;
    - a class-body ``def UNIVERSE`` / ``async def UNIVERSE`` / ``class UNIVERSE``,
      which binds the attribute to a function/class object after the prepend;
    - a ``__slots__`` naming ``UNIVERSE`` (a slot conflicts with the prepended
      class variable, raising ``ValueError`` at class definition);
    - an instance-level shadow in any method — ``self.UNIVERSE = ...``,
      ``self.__dict__["UNIVERSE"] = ...``, or ``setattr(self, "UNIVERSE", ...)`` —
      which rebinds the attribute on the instance at runtime.

    Method and nested-class bodies are not class-creation-time, so the
    class-creation walk does not descend into them — the instance-shadowing case
    is handled by a separate per-method scan.
    """
    clean: set[int] = set()
    for node in cls.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "UNIVERSE":
                    clean.add(id(target))
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "UNIVERSE":
                clean.add(id(node.target))

    def walk(node: ast.AST) -> bool:
        # A ``def``/``class`` named UNIVERSE binds the class attribute (to the
        # function/class object) at class-creation time — an override of the
        # prepended constant. Otherwise nested scopes are not class-creation-time.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node.name == "UNIVERSE"
        if isinstance(node, ast.Lambda):
            return False
        if (
            isinstance(node, ast.Name)
            and node.id == "UNIVERSE"
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and id(node) not in clean
        ):
            return True
        return any(walk(child) for child in ast.iter_child_nodes(node))

    if any(walk(stmt) for stmt in cls.body):
        return True

    # A ``__slots__`` naming UNIVERSE would conflict with the prepended class
    # variable (``ValueError`` at class definition) — bail rather than move that
    # failure to runtime.
    if _has_universe_slot(cls):
        return True

    # Instance-level shadowing: a method rebinding ``UNIVERSE`` on the instance
    # (directly or indirectly) makes the guard read that stale value rather than
    # the injected class constant.
    return any(
        _method_shadows_universe(node)
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _has_universe_slot(cls: ast.ClassDef) -> bool:
    """True iff the class declares ``__slots__`` containing ``"UNIVERSE"``.

    A slot named UNIVERSE conflicts with a class-level ``UNIVERSE = ...`` (Python
    raises ``ValueError: 'UNIVERSE' in __slots__ conflicts with class variable``),
    so the prepended constant can't coexist with it.
    """
    for node in cls.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__slots__" for t in targets):
            continue
        if node.value is not None and any(
            isinstance(sub, ast.Constant) and sub.value == "UNIVERSE"
            for sub in ast.walk(node.value)
        ):
            return True
    return False


def _method_shadows_universe(method) -> bool:
    """True iff ``method`` rebinds ``UNIVERSE`` on its instance parameter — in
    its own scope — so the guard would read that stale value rather than the
    injected class constant.

    Catches the direct ``self.UNIVERSE = ...`` attribute store and the common
    indirect forms ``self.__dict__["UNIVERSE"] = ...`` and
    ``setattr(self, "UNIVERSE", ...)``. (Truly arbitrary metaprogramming —
    ``object.__setattr__``, ``vars(self)[...]`` — is out of scope; the realistic
    generated-code forms fail closed.)
    """
    params = method.args.args
    if not params:
        return False
    receiver = params[0].arg
    found = False

    def _is_receiver(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == receiver

    def visit(node: ast.AST) -> None:
        nonlocal found
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        # self.UNIVERSE = ...
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "UNIVERSE"
            and isinstance(node.ctx, ast.Store)
            and _is_receiver(node.value)
        ):
            found = True
        # self.__dict__["UNIVERSE"] = ...
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "__dict__"
            and _is_receiver(node.value.value)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "UNIVERSE"
        ):
            found = True
        # setattr(self, "UNIVERSE", ...)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) >= 2
            and _is_receiver(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "UNIVERSE"
        ):
            found = True
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in method.body:
        visit(stmt)
    return found


def _is_canonical_on_bar(on_bar) -> bool:
    """True iff ``on_bar`` is already in canonical guarded form.

    Requires a ``self`` instance parameter, a usable third (bar) parameter, and
    a first statement that is a bare guard (no ``else``) reading
    ``<bar_param>.symbol not in self.UNIVERSE`` — so non-target bars are
    rejected before any signal logic, the guard never raises ``NameError``, and
    the shape matches the conformance gate's recognizer. Anything weaker (guard
    buried later, wrong bar name, non-``self`` receiver, or an ``else``) is not
    canonical and must be rewritten.
    """
    bar_param = _bar_parameter_name(on_bar)
    if bar_param is None or on_bar.args.args[0].arg != "self" or not on_bar.body:
        return False
    first = on_bar.body[0]
    if not _is_strippable_guard(first):
        return False
    # ``_is_universe_guard_stmt`` guarantees ``first.test.left`` is
    # ``<Name>.symbol`` and the right side is ``self.UNIVERSE`` or bare
    # ``UNIVERSE``. Require the left ``<Name>`` to be the actual bar parameter
    # AND the right side to be ``self.UNIVERSE`` specifically — a bare
    # ``UNIVERSE`` is unresolvable inside a method and would ``NameError`` at
    # runtime, so it must be rewritten rather than treated as canonical.
    if first.test.left.value.id != bar_param:
        return False
    right = first.test.comparators[0]
    return (
        isinstance(right, ast.Attribute)
        and right.attr == "UNIVERSE"
        and isinstance(right.value, ast.Name)
        and right.value.id == "self"
    )
