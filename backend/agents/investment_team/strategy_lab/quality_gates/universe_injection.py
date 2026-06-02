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

    expected = sorted(set(symbols))
    on_bar = _find_on_bar_method(cls)
    if _existing_universe_symbols(cls) == expected and _first_stmt_is_correct_guard(on_bar):
        # Already fully canonical: a single UNIVERSE binding listing exactly the
        # spec's symbols, and the *first* statement of on_bar is the guard
        # referencing the method's actual third parameter (the deterministic
        # compiler path always is). Return the source verbatim rather than
        # round-tripping it through ``ast.unparse``, which would reformat the
        # module and strip comments — a needless mutation that also churns the
        # code hash / drift log. Anything short of canonical — stale symbols, a
        # guard buried after trading logic, a guard bound to the wrong name, or
        # multiple UNIVERSE bindings — falls through and is rewritten below.
        return source

    # 1. Replace the UNIVERSE constant: drop any existing class-level binding
    #    targeting ``UNIVERSE`` (preserving sibling aliases in a chained
    #    assignment), then prepend a fresh one populated from the spec's symbols
    #    (canonical, sorted, de-duplicated).
    cls.body = _strip_universe_assignments(cls.body)
    cls.body.insert(0, _build_universe_assign(expected))

    # 2. Replace the on_bar guard when the signature supports it. Strip only
    #    bare guards (no ``else`` body, whose removal can't drop trading logic),
    #    then prepend the canonical guard so it runs before any signal logic.
    if on_bar is not None:
        bar_param = _bar_parameter_name(on_bar)
        if bar_param is not None:
            on_bar.body = [stmt for stmt in on_bar.body if not _is_strippable_guard(stmt)]
            on_bar.body.insert(0, _build_guard_stmt(bar_param))

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


def _strip_universe_assignments(body: list[ast.stmt]) -> list[ast.stmt]:
    """Return ``body`` with every ``UNIVERSE`` binding removed.

    A chained assignment such as ``UNIVERSE = TARGETS = frozenset({...})`` keeps
    its sibling targets (the node survives with ``UNIVERSE`` dropped) so aliases
    referenced elsewhere in the class are preserved; only when ``UNIVERSE`` is
    the sole target is the whole statement removed.
    """
    out: list[ast.stmt] = []
    for node in body:
        if isinstance(node, ast.Assign):
            kept = [t for t in node.targets if not (isinstance(t, ast.Name) and t.id == "UNIVERSE")]
            if not kept:
                continue
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
    """Build ``if <bar_param>.symbol not in self.UNIVERSE: return``."""
    return ast.parse(f"if {bar_param}.symbol not in self.UNIVERSE:\n    return").body[0]


def _find_on_bar_method(cls: ast.ClassDef):
    """Return the ``on_bar`` method node (sync or async) on ``cls``, or None."""
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "on_bar":
            return node
    return None


def _bar_parameter_name(on_bar) -> str | None:
    """Return the third positional parameter name of ``on_bar(self, ctx, bar)``.

    Returns None when the method is async or has fewer than three positional
    parameters — in both cases code_safety independently flags the malformed
    signature, and we must not emit a guard referencing a nonexistent name.
    """
    if isinstance(on_bar, ast.AsyncFunctionDef):
        return None
    args = on_bar.args.args
    if len(args) < 3:
        return None
    return args[2].arg


def _is_strippable_guard(stmt: ast.stmt) -> bool:
    """True iff ``stmt`` is a universe guard whose removal cannot drop logic.

    Only bare guards (no ``else`` body) are strippable; a guard that nests
    trading logic under ``else`` is left in place — the canonical guard is
    prepended ahead of it, which still rejects non-target bars first while
    preserving the original branch's body.
    """
    return _is_universe_guard_stmt(stmt) and not getattr(stmt, "orelse", None)


def _first_stmt_is_correct_guard(on_bar) -> bool:
    """True iff ``on_bar``'s first statement is the canonical universe guard.

    Requires the method to have a usable third parameter and its very first
    statement to be a bare guard (no ``else``) whose receiver is exactly that
    parameter — so non-target bars are rejected before any signal logic and the
    guard can never raise ``NameError`` on a renamed parameter. Anything weaker
    (guard buried later, bound to the wrong name, or carrying an ``else``) is
    not canonical and must be rewritten.
    """
    if on_bar is None:
        return False
    bar_param = _bar_parameter_name(on_bar)
    if bar_param is None or not on_bar.body:
        return False
    first = on_bar.body[0]
    if not _is_strippable_guard(first):
        return False
    # ``_is_universe_guard_stmt`` guarantees ``first.test.left`` is
    # ``<Name>.symbol``; require that ``<Name>`` is the actual bar parameter.
    return first.test.left.value.id == bar_param
