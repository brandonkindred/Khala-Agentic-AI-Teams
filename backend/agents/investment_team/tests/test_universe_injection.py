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


def test_existing_guard_wrong_param_name_rewritten() -> None:
    """A guard bound to the wrong receiver (``bar`` while the param is ``b``) is
    not trusted — it is rewritten to the actual parameter so no NameError."""
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
    assert "b.symbol not in self.UNIVERSE" in out
    assert "bar.symbol not in self.UNIVERSE" not in out
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


def test_chained_assignment_alias_preserved() -> None:
    """Rewriting a stale ``UNIVERSE = TARGETS = ...`` must keep the alias."""
    spec = _spec(target_symbols=["QQQ"])
    src = _GUARDLESS.replace(
        "    def on_bar",
        "    UNIVERSE = TARGETS = frozenset({'SPY'})\n\n    def on_bar",
    )
    out = inject_universe_and_guard(src, spec)
    assert "TARGETS" in out  # sibling alias survives
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


def test_non_self_receiver_normalized_to_self() -> None:
    """A non-``self`` instance parameter is renamed to ``self`` (with its body
    references rewritten) so the guard reads ``self.UNIVERSE`` — both
    runtime-correct and accepted by the conformance gate's recognizer."""
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
    assert "self.UNIVERSE" in out
    assert "self.position(bar.symbol)" in out  # receiver reference rewritten
    assert "strategy" not in out
    assert _has_universe_guard_in_on_bar(_cls(out))  # gate recognizer accepts it
    compile(out, "<injected>", "exec")


def test_non_self_receiver_with_self_collision_left_for_gate() -> None:
    """If the body already binds a distinct ``self``, renaming would shadow it;
    the injector leaves the receiver alone (the gate then flags it)."""
    spec = _spec(target_symbols=["QQQ"])
    src = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(strategy, ctx, bar):
                self = 1
                return self
        """
    )
    out = inject_universe_and_guard(src, spec)
    # Receiver not renamed; guard still inserted against the original receiver.
    assert "def on_bar(strategy, ctx, bar):" in out
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
