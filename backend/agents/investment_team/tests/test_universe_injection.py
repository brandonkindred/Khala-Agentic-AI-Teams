"""Unit tests for ``inject_universe_and_guard``.

The injector deterministically guarantees a generated ``Strategy`` class has
both the ``UNIVERSE`` constant and the ``on_bar`` symbol guard required by
``CodeConformanceGate`` Check #2, so the LLM is never asked to re-emit that
boilerplate. These tests pin the acceptance criteria: the post-transform code
satisfies the gate's own recognizers, the transform is idempotent and never
double-injects, malformed pieces are replaced cleanly, the guard binds the
method's actual third parameter (no ``NameError`` on a renamed ``bar``), and
every no-op / graceful-degradation branch returns sensibly.
"""

from __future__ import annotations

import ast
import textwrap

from investment_team.models import StrategySpec
from investment_team.strategy_lab.quality_gates.code_conformance import (
    CodeConformanceGate,
)
from investment_team.strategy_lab.quality_gates.code_safety_ast import (
    _find_strategy_subclasses,
    _has_universe_constant,
    _has_universe_guard_in_on_bar,
)
from investment_team.strategy_lab.quality_gates.universe_injection import (
    inject_universe_and_guard,
)
from investment_team.strategy_lab.spec_dsl import DEFAULT_SIZING_PAYLOAD

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _spec(*, target_symbols=None, requires_custom_code: bool = False) -> StrategySpec:
    return StrategySpec(
        strategy_id="t-1",
        authored_by="test",
        asset_class="stocks",
        hypothesis="QQQ trend-follow on SMA cross.",
        signal_definition="sma(50) > sma(200)",
        timeframe="1d",
        entry_rules=[],
        exit_rules=[],
        sizing=DEFAULT_SIZING_PAYLOAD,
        target_symbols=list(target_symbols or []),
        requires_custom_code=requires_custom_code,
    )


def _cls(source: str) -> ast.ClassDef:
    """Return the single Strategy subclass parsed from ``source``."""
    classes = _find_strategy_subclasses(ast.parse(source))
    assert len(classes) == 1, "fixture must declare exactly one Strategy class"
    return classes[0]


def _universe_assign_count(source: str) -> int:
    cls = _cls(source)
    count = 0
    for node in cls.body:
        if isinstance(node, ast.Assign):
            count += sum(1 for t in node.targets if isinstance(t, ast.Name) and t.id == "UNIVERSE")
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "UNIVERSE":
                count += 1
    return count


def _symbol_gate_criticals(source: str, spec: StrategySpec) -> list[str]:
    results = CodeConformanceGate().check(source, spec)
    return [
        r.details
        for r in results
        if r.severity == "critical" and not r.passed and "UNIVERSE" in r.details
    ]


# Class body without UNIVERSE and without the on_bar guard.
_GUARDLESS = textwrap.dedent(
    """
    from contract import Strategy

    class S(Strategy):
        def on_bar(self, ctx, bar):
            bars = ctx.history(bar.symbol, 200)
            if len(bars) < 200:
                return
            if sma(bars, 50) > sma(bars, 200):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
    """
)

# Already-conformant class.
_CONFORMANT = textwrap.dedent(
    """
    from contract import Strategy

    class S(Strategy):
        UNIVERSE = frozenset({"QQQ"})

        def on_bar(self, ctx, bar):
            if bar.symbol not in self.UNIVERSE:
                return
            if sma(ctx.history(bar.symbol, 50), 50) > 0:
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
    """
)


# ---------------------------------------------------------------------------
# Core acceptance
# ---------------------------------------------------------------------------


def test_injects_when_neither_present() -> None:
    spec = _spec(target_symbols=["AAPL", "MSFT"])
    out = inject_universe_and_guard(_GUARDLESS, spec)
    cls = _cls(out)
    assert _has_universe_constant(cls)
    assert _has_universe_guard_in_on_bar(cls)
    assert _symbol_gate_criticals(out, spec) == []


def test_roundtrip_passes_recognizers() -> None:
    spec = _spec(target_symbols=["QQQ"])
    cls = _cls(inject_universe_and_guard(_GUARDLESS, spec))
    assert _has_universe_constant(cls) and _has_universe_guard_in_on_bar(cls)


def test_no_double_injection_on_correct_source() -> None:
    spec = _spec(target_symbols=["QQQ"])
    out = inject_universe_and_guard(_CONFORMANT, spec)
    assert _universe_assign_count(out) == 1
    on_bar = next(n for n in _cls(out).body if isinstance(n, ast.FunctionDef))
    guards = sum(
        1
        for s in on_bar.body
        if isinstance(s, ast.If)
        and isinstance(s.test, ast.Compare)
        and any(isinstance(op, ast.NotIn) for op in s.test.ops)
    )
    assert guards == 1


def test_idempotent() -> None:
    spec = _spec(target_symbols=["AAPL", "MSFT"])
    once = inject_universe_and_guard(_GUARDLESS, spec)
    twice = inject_universe_and_guard(once, spec)
    assert once == twice


def test_malformed_universe_replaced() -> None:
    spec = _spec(target_symbols=["TSLA"])
    src = _GUARDLESS.replace(
        "    def on_bar",
        '    UNIVERSE = "AAPL"\n\n    def on_bar',
    )
    out = inject_universe_and_guard(src, spec)
    cls = _cls(out)
    assert _has_universe_constant(cls)  # str replaced by a real frozenset
    assert "TSLA" in out and '"AAPL"' not in out and "'AAPL'" not in out
    assert _universe_assign_count(out) == 1


def test_stale_universe_symbols_replaced() -> None:
    """A structurally valid but stale UNIVERSE (symbols != spec) is rewritten
    to the spec's symbols, even though a guard is already present."""
    spec = _spec(target_symbols=["QQQ"])
    src = _CONFORMANT.replace('frozenset({"QQQ"})', 'frozenset({"SPY"})')
    out = inject_universe_and_guard(src, spec)
    assert out != src  # not returned verbatim — the stale constant is fixed
    assert "QQQ" in out and "SPY" not in out
    assert _universe_assign_count(out) == 1
    assert _has_universe_constant(_cls(out))
    assert _symbol_gate_criticals(out, spec) == []


def test_non_literal_universe_replaced() -> None:
    """A UNIVERSE built from a non-string-constant member cannot be matched
    against the spec, so it is replaced with the canonical literal."""
    spec = _spec(target_symbols=["QQQ"])
    src = _GUARDLESS.replace(
        "    def on_bar",
        "    _SYM = 'QQQ'\n    UNIVERSE = frozenset({_SYM})\n\n    def on_bar",
    )
    out = inject_universe_and_guard(src, spec)
    assert "frozenset({'QQQ'})" in out
    assert _has_universe_constant(_cls(out))


def test_bare_set_display_universe_matched() -> None:
    """A bare set-display UNIVERSE (``{"QQQ"}``) whose symbols match the spec
    is recognised and returned verbatim."""
    spec = _spec(target_symbols=["QQQ"])
    src = _CONFORMANT.replace('frozenset({"QQQ"})', '{"QQQ"}')
    assert inject_universe_and_guard(src, spec) == src


def test_universe_call_with_non_display_arg_replaced() -> None:
    """``frozenset(<name>)`` (arg is not a literal display) cannot be matched
    against the spec, so the constant is replaced with the canonical literal."""
    spec = _spec(target_symbols=["QQQ"])
    src = _GUARDLESS.replace(
        "    def on_bar",
        "    _SYMS = ('QQQ',)\n    UNIVERSE = frozenset(_SYMS)\n\n    def on_bar",
    )
    out = inject_universe_and_guard(src, spec)
    assert "frozenset({'QQQ'})" in out
    assert _has_universe_constant(_cls(out))


def _on_bar(out: str):
    return next(
        n
        for n in _cls(out).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "on_bar"
    )


def _first_is_guard(out: str) -> bool:
    first = _on_bar(out).body[0]
    return (
        isinstance(first, ast.If)
        and isinstance(first.test, ast.Compare)
        and any(isinstance(op, ast.NotIn) for op in first.test.ops)
    )


def test_misplaced_guard_moved_to_first_statement() -> None:
    """A matching UNIVERSE plus a guard that sits *after* signal logic is not
    canonical — the guard is rewritten to run before any trading."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
                if bar.symbol not in self.UNIVERSE:
                    return
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert out != src
    assert _first_is_guard(out)  # guard now precedes submit_order
    assert _has_universe_guard_in_on_bar(_cls(out))


def test_wrong_param_guard_gets_canonical_added() -> None:
    """A guard whose receiver is not the bar parameter and not a method-local
    (e.g. ``bar`` while the param is ``b``) could resolve through globals, so it
    is preserved rather than stripped; the canonical ``b.symbol`` guard is
    prepended ahead of it so the bar guard is present and runs first."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, b):
                if bar.symbol not in self.UNIVERSE:
                    return
                ctx.submit_order(symbol=b.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _first_is_guard(out)  # canonical bar guard prepended first
    on_bar = next(n for n in _cls(out).body if isinstance(n, ast.FunctionDef))
    assert on_bar.body[0].test.left.value.id == "b"  # uses the actual bar param
    compile(out, "<injected>", "exec")


def test_multiple_universe_bindings_collapsed() -> None:
    """With two UNIVERSE bindings, Python keeps the last; matching only the
    first must not short-circuit — both are collapsed to one canonical binding."""
    spec = _spec(target_symbols=["QQQ"])
    src = _CONFORMANT.replace(
        '    UNIVERSE = frozenset({"QQQ"})',
        '    UNIVERSE = frozenset({"QQQ"})\n    UNIVERSE = frozenset({"SPY"})',
    )
    out = inject_universe_and_guard(src, spec)
    assert _universe_assign_count(out) == 1
    assert "QQQ" in out and "SPY" not in out


def test_guard_with_else_body_preserved() -> None:
    """A guard nesting trading logic under ``else`` is not stripped (which would
    delete that logic); the canonical guard is prepended ahead of it."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                else:
                    ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert "submit_order" in out  # else-body logic survives
    assert _first_is_guard(out)
    compile(out, "<injected>", "exec")


def test_chained_assignment_alias_preserved_and_synced() -> None:
    """Rewriting a stale ``UNIVERSE = TARGETS = frozenset({'SPY'})`` must keep
    the ``TARGETS`` alias AND bring its value in sync with the spec universe, so
    a strategy that trades off ``self.TARGETS`` can't silently use the stale
    symbol set."""
    spec = _spec(target_symbols=["QQQ"])
    src = _GUARDLESS.replace(
        "    def on_bar",
        "    UNIVERSE = TARGETS = frozenset({'SPY'})\n\n    def on_bar",
    )
    out = inject_universe_and_guard(src, spec)
    assert "TARGETS" in out  # sibling alias survives
    assert "SPY" not in out  # but no longer bound to the stale universe
    assert "frozenset({'QQQ'})" in out
    assert _universe_assign_count(out) == 1
    compile(out, "<injected>", "exec")


def test_matching_universe_but_no_on_bar_injects() -> None:
    """A class whose UNIVERSE matches the spec but has no on_bar is not
    canonical (no guard); injection still runs without crashing."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def helper(self):
                return 1
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _has_universe_constant(_cls(out))
    assert _universe_assign_count(out) == 1


def test_matching_universe_async_on_bar_not_short_circuited() -> None:
    """A matching UNIVERSE with an async on_bar (no usable bar param) is not
    canonical; injection runs and does not add a guard to the async method."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            async def on_bar(self, ctx, bar):
                pass
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _has_universe_constant(_cls(out))
    assert "not in self.UNIVERSE" not in out


def test_bare_annassign_universe_replaced() -> None:
    """A value-less ``UNIVERSE: frozenset`` annotation cannot be matched and is
    replaced with the canonical literal."""
    spec = _spec(target_symbols=["QQQ"])
    src = _GUARDLESS.replace(
        "    def on_bar",
        "    UNIVERSE: frozenset\n\n    def on_bar",
    )
    out = inject_universe_and_guard(src, spec)
    assert "frozenset({'QQQ'})" in out
    assert _universe_assign_count(out) == 1


def test_non_self_receiver_injects_universe_only() -> None:
    """A non-``self`` instance parameter is left untouched and the guard is
    skipped (fail closed): the gate recognizer needs ``self.UNIVERSE`` and the
    engine binds positionally, so emitting a guard here would be wrong. UNIVERSE
    is still injected; the missing guard drives a conformance critical."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(strategy, ctx, bar):
                pos = strategy.position(bar.symbol)
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _has_universe_constant(_cls(out))
    assert not _has_universe_guard_in_on_bar(_cls(out))  # no guard for non-self
    assert "def on_bar(strategy, ctx, bar):" in out  # signature untouched
    assert "self.position" not in out  # body references not rewritten
    compile(out, "<injected>", "exec")


def test_non_self_receiver_with_self_binding_no_unbound_local() -> None:
    """A non-``self`` receiver whose body binds ``self`` must not get a
    ``self.UNIVERSE`` guard prepended — that would ``UnboundLocalError`` before
    the local ``self`` is assigned. The guard is skipped entirely."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(strategy, ctx, bar):
                self = strategy
                return self
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert "not in self.UNIVERSE" not in out  # no prepended guard
    compile(out, "<injected>", "exec")


def test_nested_helper_receiver_not_corrupted() -> None:
    """A non-``self`` receiver with a nested helper that reuses the name must not
    have the helper's references rewritten (no rename happens at all)."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(strategy, ctx, bar):
                def helper(strategy):
                    return strategy + 1
                return helper(1)
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert "def helper(strategy):" in out
    assert "return strategy + 1" in out
    compile(out, "<injected>", "exec")


def test_bare_universe_guard_rewritten_to_self() -> None:
    """A first-statement guard using bare ``UNIVERSE`` (unresolvable inside a
    method) is not treated as canonical — it is rewritten to ``self.UNIVERSE``."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in UNIVERSE:
                    return
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert out != src  # not short-circuited as canonical
    assert "not in self.UNIVERSE" in out
    on_bar = next(n for n in _cls(out).body if isinstance(n, ast.FunctionDef))
    # Exactly one guard, and it reads self.UNIVERSE.
    guards = [s for s in on_bar.body if isinstance(s, ast.If)]
    assert len(guards) == 1
    assert _has_universe_guard_in_on_bar(_cls(out))
    compile(out, "<injected>", "exec")


def test_duplicate_on_bar_guards_runtime_effective_method() -> None:
    """Python keeps the last on_bar; the guard must land in that one, not an
    earlier shadowed copy that never runs."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                pass

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    on_bars = [
        n
        for n in _cls(out).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "on_bar"
    ]
    # Every on_bar is guarded — the gate inspects the first, Python runs the
    # last, so both must carry the guard for the gate to pass AND the runtime
    # method to be protected.
    for ob in on_bars:
        first = ob.body[0]
        assert isinstance(first, ast.If) and any(isinstance(op, ast.NotIn) for op in first.test.ops)
    assert _has_universe_guard_in_on_bar(_cls(out))  # gate (inspects first) passes
    assert "submit_order" in out  # the runtime method's logic is preserved


def test_auxiliary_symbol_guard_preserved() -> None:
    """A guard on a different *defined* variable (filtering an auxiliary symbol)
    is user logic and must not be stripped — only the bar guard is replaced."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                ref = ctx.reference(bar.symbol)
                if ref.symbol not in self.UNIVERSE:
                    return
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert "ref.symbol not in self.UNIVERSE" in out  # user guard preserved
    assert "bar.symbol not in self.UNIVERSE" in out  # canonical bar guard added
    assert _first_is_guard(out)  # bar guard runs first
    compile(out, "<injected>", "exec")


def test_global_receiver_guard_preserved() -> None:
    """A guard whose receiver is a module-level/global name (not a method-local)
    can resolve through globals, so it is preserved as user logic."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        REF = object()

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if REF.symbol not in self.UNIVERSE:
                    return
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert "REF.symbol not in self.UNIVERSE" in out  # global-receiver guard kept
    assert _first_is_guard(out)  # canonical bar guard prepended first
    compile(out, "<injected>", "exec")


def test_later_bound_local_guard_stripped() -> None:
    """A guard whose receiver is a method-local bound only *after* the guard
    would UnboundLocalError; it is stripped (only names bound before the guard
    make a different receiver safe to keep)."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if ref.symbol not in self.UNIVERSE:
                    return
                ref = ctx.reference(bar.symbol)
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert "ref.symbol not in self.UNIVERSE" not in out  # broken guard stripped
    assert "ref = ctx.reference" in out  # the assignment itself is preserved
    assert _first_is_guard(out)
    compile(out, "<injected>", "exec")


def test_duplicate_guarded_first_unguardable_last_strips_to_fail_closed() -> None:
    """When duplicate on_bars can't all be guarded, a pre-existing guard on the
    gate-inspected first definition is stripped so the gate fails closed instead
    of passing while the unguarded runtime-effective last definition runs."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")

            def on_bar(strategy, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _has_universe_constant(_cls(out))
    assert not _has_universe_guard_in_on_bar(_cls(out))  # first guard stripped -> fail closed
    compile(out, "<injected>", "exec")


def test_mixed_receiver_duplicate_on_bar_fails_closed() -> None:
    """Duplicate on_bar where the runtime-effective last one has a non-self
    receiver: guard NONE (so the gate fails on the unguarded first) rather than
    guard only the first and let the gate pass over an unguarded runtime method."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")

            def on_bar(strategy, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _has_universe_constant(_cls(out))
    assert not _has_universe_guard_in_on_bar(_cls(out))  # no guard -> gate fails closed
    compile(out, "<injected>", "exec")


def test_on_bar_with_varargs_and_nested_helper() -> None:
    """A guardable on_bar with ``*args``/``**kwargs`` and a nested helper is
    guarded; the helper (a nested scope) is not scanned for bound names."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar, *args, **kwargs):
                def helper(x):
                    return x + 1
                ctx.submit_order(symbol=bar.symbol, qty=helper(1), side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _has_universe_guard_in_on_bar(_cls(out))
    assert "def helper(x):" in out
    compile(out, "<injected>", "exec")


def test_nested_universe_in_try_handler_bails() -> None:
    """A UNIVERSE assignment nested in an except handler at class-creation time
    is detected and the injector bails."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            try:
                pass
            except Exception:
                UNIVERSE = frozenset({"SPY"})

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_unpacking_universe_assignment_bails() -> None:
    """A destructured ``UNIVERSE, OTHER = (...)`` binding can't be cleanly
    stripped (it would change the unpack), so the injector bails rather than
    let the unpacking override a prepended constant."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE, OTHER = (frozenset({"SPY"}), 1)

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_extra_param_on_bar_overload_fails_closed() -> None:
    """A duplicate on_bar whose runtime-effective definition has 4 parameters is
    invalid (the harness calls it with 3); guard none and fail closed rather
    than guard a method that can't run."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")

            def on_bar(self, ctx, bar, extra):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _has_universe_constant(_cls(out))
    assert not _has_universe_guard_in_on_bar(_cls(out))  # no guard -> fail closed
    compile(out, "<injected>", "exec")


def test_single_four_param_on_bar_not_guarded() -> None:
    """A lone 4-parameter on_bar is not the harness arity; UNIVERSE is injected
    but the guard is skipped (the safety/conformance gates own the arity error)."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar, extra):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _has_universe_constant(_cls(out))
    assert not _has_universe_guard_in_on_bar(_cls(out))


def test_class_lambda_and_noarg_method_inject_normally() -> None:
    """Odd-but-harmless members — a class-level lambda attribute and a
    parameter-less method — don't trip the unsupported-binding bail; the class
    is guarded normally."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            HANDLER = lambda x: x

            def helper():
                return 1

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _has_universe_constant(_cls(out))
    assert _has_universe_guard_in_on_bar(_cls(out))
    assert "HANDLER" in out and "def helper" in out
    compile(out, "<injected>", "exec")


def test_required_kwonly_on_bar_not_guarded() -> None:
    """A required keyword-only param makes ``instance.on_bar(ctx, bar)`` raise
    TypeError, so the method is not guardable -> fail closed."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar, *, threshold):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _has_universe_constant(_cls(out))
    assert not _has_universe_guard_in_on_bar(_cls(out))  # not guarded -> fail closed
    compile(out, "<injected>", "exec")


def test_kwonly_with_default_on_bar_guarded() -> None:
    """A keyword-only param *with* a default is harness-callable, so the method
    is still guarded normally."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar, *, threshold=1.0):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _has_universe_constant(_cls(out))
    assert _has_universe_guard_in_on_bar(_cls(out))
    compile(out, "<injected>", "exec")


def test_qualified_staticmethod_on_bar_not_guarded() -> None:
    """A qualified ``@builtins.staticmethod`` on_bar is rejected like the bare
    ``@staticmethod`` form."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            @builtins.staticmethod
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert not _has_universe_guard_in_on_bar(_cls(out))  # not guarded -> fail closed
    compile(out, "<injected>", "exec")


def test_staticmethod_on_bar_not_guarded() -> None:
    """A ``@staticmethod`` on_bar has no bound ``self`` at runtime, so it isn't
    guardable; UNIVERSE is injected but the guard is skipped (fail closed)."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            @staticmethod
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert _has_universe_constant(_cls(out))
    assert not _has_universe_guard_in_on_bar(_cls(out))  # not guarded -> fail closed
    compile(out, "<injected>", "exec")


def test_classmethod_on_bar_not_guarded() -> None:
    """A ``@classmethod`` on_bar binds the class, not an instance — also rejected."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            @classmethod
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert not _has_universe_guard_in_on_bar(_cls(out))
    compile(out, "<injected>", "exec")


def test_def_universe_override_bails() -> None:
    """A class-body ``def UNIVERSE`` binds the attribute to a function after the
    prepend, so the injector bails rather than emit a false-green gate."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def UNIVERSE(self):
                return frozenset({"SPY"})

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_class_universe_override_bails() -> None:
    """A nested ``class UNIVERSE`` likewise overrides the attribute -> bail."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            class UNIVERSE:
                pass

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_match_capture_universe_bails() -> None:
    """A ``match``/``case UNIVERSE`` capture binds UNIVERSE at class-creation
    time with no ``Name``-Store node — the symbol-table check catches it where a
    plain AST store-scan would miss it, so the injector bails."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            match 1:
                case UNIVERSE:
                    pass

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_global_universe_declaration_bails() -> None:
    """A class-body ``global UNIVERSE`` would make a prepended ``UNIVERSE =``
    a compile-time SyntaxError, so the injector bails (returns valid source)."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            global UNIVERSE

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    out = inject_universe_and_guard(src, spec)
    assert out == src
    compile(out, "<injected>", "exec")  # still valid Python


def test_locals_subscript_universe_mutation_bails() -> None:
    """A dynamic ``locals()["UNIVERSE"] = ...`` class-namespace mutation rebinds
    the attribute after the prepend -> bail."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            locals()["UNIVERSE"] = frozenset({"SPY"})

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_vars_update_universe_mutation_bails() -> None:
    """A dynamic ``vars().update(UNIVERSE=...)`` class-namespace mutation -> bail."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            vars().update(UNIVERSE=frozenset({"SPY"}))

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_vars_update_dict_universe_mutation_bails() -> None:
    """The ``vars().update({"UNIVERSE": ...})`` dict-literal form also bails."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            vars().update({"UNIVERSE": frozenset({"SPY"})})

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_import_alias_universe_bails() -> None:
    """A class-body ``import x as UNIVERSE`` rebinds the attribute after the
    prepend, so the injector bails."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            from math import inf as UNIVERSE

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_import_name_universe_bails() -> None:
    """A class-body ``from m import UNIVERSE`` (bound name UNIVERSE, no alias)
    likewise rebinds the attribute -> bail."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            from contract import UNIVERSE

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_universe_in_slots_bails() -> None:
    """A ``__slots__`` naming UNIVERSE would conflict with the prepended class
    variable at class-definition time, so the injector bails."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            __slots__ = ("UNIVERSE", "_state")

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_dict_subscript_universe_shadowing_bails() -> None:
    """An indirect ``self.__dict__["UNIVERSE"] = ...`` shadow is detected -> bail."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def __init__(self):
                self.__dict__["UNIVERSE"] = frozenset({"SPY"})

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_setattr_universe_shadowing_bails() -> None:
    """An indirect ``setattr(self, "UNIVERSE", ...)`` shadow is detected -> bail."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def __init__(self):
                setattr(self, "UNIVERSE", frozenset({"SPY"}))

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_instance_universe_shadowing_bails() -> None:
    """A ``self.UNIVERSE = ...`` assignment in a method shadows the class
    constant at runtime, so the injector bails."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def __init__(self):
                self.UNIVERSE = frozenset({"SPY"})

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_del_universe_bails() -> None:
    """A class-body ``del UNIVERSE`` removes the prepended constant at runtime,
    so the injector bails rather than pass the structural gate with no constant."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"SPY"})
            del UNIVERSE

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_nested_conditional_universe_assignment_bails() -> None:
    """A class-creation-time UNIVERSE assignment nested in a conditional would
    override a prepended constant; the injector bails (returns source unchanged)
    rather than produce code that passes the gate with a stale runtime universe."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            if True:
                UNIVERSE = frozenset({"SPY"})

            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_annassign_universe_stripped() -> None:
    spec = _spec(target_symbols=["TSLA"])
    src = _GUARDLESS.replace(
        "    def on_bar",
        "    UNIVERSE: frozenset = frozenset()\n\n    def on_bar",
    )
    out = inject_universe_and_guard(src, spec)
    assert _universe_assign_count(out) == 1
    assert "TSLA" in out


def test_custom_param_name() -> None:
    spec = _spec(target_symbols=["QQQ"])
    src = _GUARDLESS.replace("def on_bar(self, ctx, bar):", "def on_bar(self, ctx, b):").replace(
        "bar.symbol", "b.symbol"
    )
    out = inject_universe_and_guard(src, spec)
    assert "b.symbol not in self.UNIVERSE" in out
    # Compiles without a NameError-inducing reference to a missing ``bar``.
    compile(out, "<injected>", "exec")
    assert _has_universe_guard_in_on_bar(_cls(out))


def test_symbols_sorted_and_deduped() -> None:
    spec = _spec(target_symbols=["MSFT", "AAPL", "AAPL"])
    out = inject_universe_and_guard(_GUARDLESS, spec)
    assert "frozenset({'AAPL', 'MSFT'})" in out


def test_requires_custom_code_still_injects() -> None:
    spec = _spec(target_symbols=["QQQ"], requires_custom_code=True)
    out = inject_universe_and_guard(_GUARDLESS, spec)
    cls = _cls(out)
    assert _has_universe_constant(cls) and _has_universe_guard_in_on_bar(cls)


# ---------------------------------------------------------------------------
# No-op / graceful-degradation branches
# ---------------------------------------------------------------------------


def test_empty_target_symbols_unchanged() -> None:
    spec = _spec(target_symbols=[])
    assert inject_universe_and_guard(_GUARDLESS, spec) == _GUARDLESS


def test_unparseable_source_unchanged() -> None:
    spec = _spec(target_symbols=["QQQ"])
    broken = "class S(Strategy):\n    def on_bar(self  # broken"
    assert inject_universe_and_guard(broken, spec) == broken


def test_zero_strategy_subclasses_unchanged() -> None:
    spec = _spec(target_symbols=["QQQ"])
    src = "class NotAStrategy:\n    pass\n"
    assert inject_universe_and_guard(src, spec) == src


def test_two_strategy_subclasses_unchanged() -> None:
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class A(Strategy):
            def on_bar(self, ctx, bar):
                pass

        class B(Strategy):
            def on_bar(self, ctx, bar):
                pass
        """
    )
    assert inject_universe_and_guard(src, spec) == src


def test_missing_on_bar_injects_universe_only() -> None:
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def helper(self):
                return 1
        """
    )
    out = inject_universe_and_guard(src, spec)
    cls = _cls(out)
    assert _has_universe_constant(cls)
    assert not _has_universe_guard_in_on_bar(cls)


def test_fewer_than_three_params_universe_only() -> None:
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx):
                pass
        """
    )
    out = inject_universe_and_guard(src, spec)
    cls = _cls(out)
    assert _has_universe_constant(cls)
    assert not _has_universe_guard_in_on_bar(cls)


def test_async_on_bar_injects_universe_only() -> None:
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            async def on_bar(self, ctx, bar):
                pass
        """
    )
    out = inject_universe_and_guard(src, spec)
    cls = _cls(out)
    assert _has_universe_constant(cls)
    # No guard injected into an async on_bar (code_safety rejects it anyway).
    assert "not in self.UNIVERSE" not in out
