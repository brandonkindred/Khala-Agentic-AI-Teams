"""Unit tests for the Strategy-Lab code-safety gate (PR 3 contract).

The gate runs before generated strategy code hits the subprocess harness.
It's the last chance to catch contract-shape problems before a runtime
``StrategyRuntimeError`` makes them the orchestrator's problem instead.

Covers the three classes of check that matter after the PR 3 cutover:

1. Shape: exactly one ``contract.Strategy`` subclass with a correctly-typed
   ``on_bar`` override.
2. Allowlist: the event-driven contract has no pandas/numpy; those are
   now rejected even though the legacy contract used them.
3. Look-ahead tripwires: syntactic hints that the code is trying to read
   non-existent forward data.
"""

from __future__ import annotations

import textwrap

from investment_team.strategy_lab.quality_gates.code_safety import CodeSafetyChecker


def _severities(results):
    return {(r.gate_name, r.severity, r.passed) for r in results}


def _critical_details(results):
    return [r.details for r in results if r.severity == "critical" and not r.passed]


# ---------------------------------------------------------------------------
# Shape: Strategy subclass
# ---------------------------------------------------------------------------


def test_valid_minimal_strategy_passes() -> None:
    code = textwrap.dedent("""
        from contract import Strategy

        class MyStrat(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='FLAT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert criticals == [], criticals


def test_missing_strategy_subclass_is_critical() -> None:
    """Legacy ``def run_strategy(data, config)`` is no longer acceptable —
    it would crash at the subprocess harness as ``runtime_error`` otherwise.
    """
    code = textwrap.dedent("""
        def run_strategy(data, config):
            return []
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("exactly one subclass of contract.Strategy" in c for c in criticals)


def test_multiple_strategy_subclasses_is_critical() -> None:
    code = textwrap.dedent("""
        from contract import Strategy

        class One(Strategy):
            def on_bar(self, ctx, bar):
                pass

        class Two(Strategy):
            def on_bar(self, ctx, bar):
                pass
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("multiple Strategy subclasses" in c for c in criticals)


def test_contract_attribute_base_also_recognised() -> None:
    """Users may write ``class X(contract.Strategy):`` — also valid."""
    code = textwrap.dedent("""
        import contract

        class X(contract.Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='FLAT')
    """)
    results = CodeSafetyChecker().check(code)
    assert _critical_details(results) == []


def test_on_bar_wrong_signature_is_critical() -> None:
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, bar):   # missing ctx
                pass
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("on_bar must accept exactly 3 parameters" in c for c in criticals)


def test_on_bar_async_is_critical() -> None:
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            async def on_bar(self, ctx, bar):
                pass
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("non-async" in c for c in criticals)


def test_missing_on_bar_override_is_critical() -> None:
    """A strategy that doesn't override on_bar emits zero orders — flag it
    so the orchestrator can refine rather than silently waste a cycle."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            pass
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("does not override on_bar" in c for c in criticals)


# ---------------------------------------------------------------------------
# Allowlist: pandas/numpy no longer allowed under the event-driven contract
# ---------------------------------------------------------------------------


def test_pandas_import_is_flagged() -> None:
    """The new contract feeds one Bar at a time — pandas is unused."""
    code = textwrap.dedent("""
        import pandas as pd
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                pass
    """)
    results = CodeSafetyChecker().check(code)
    # Non-allowlisted imports are a warning (not critical), but should be
    # present in the results so the refinement prompt can act.
    warning_details = [r.details for r in results if r.severity == "warning"]
    assert any("pandas" in d for d in warning_details)


def test_indicators_import_still_allowed() -> None:
    code = textwrap.dedent("""
        from contract import Strategy
        from indicators import sma, rsi

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='FLAT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert criticals == [], criticals
    # Neither `contract` nor `indicators` should trigger the non-allowlisted warning.
    warn_details = [r.details for r in results if r.severity == "warning"]
    assert not any("indicators" in d for d in warn_details)
    assert not any("contract" in d for d in warn_details)


def test_os_import_is_critical() -> None:
    code = textwrap.dedent("""
        import os
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                os.system('bad')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("Banned import" in c and "os" in c for c in criticals)


# ---------------------------------------------------------------------------
# Look-ahead tripwires
# ---------------------------------------------------------------------------


def test_bar_next_close_is_critical() -> None:
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.next_close > bar.close:
                    pass
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("bar.next_" in c or "next_" in c for c in criticals)


def test_ctx_future_accessor_is_critical() -> None:
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                _ = ctx.future_bar(1)
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("ctx.future_" in c for c in criticals)


def test_ctx_peek_is_critical() -> None:
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                _ = ctx.peek()
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("ctx.peek" in c for c in criticals)


def test_comment_mentioning_future_close_is_not_flagged() -> None:
    """Tripwire patterns inside comments / strings should not false-flag."""
    code = textwrap.dedent('''
        from contract import Strategy

        class S(Strategy):
            """Don't read bar.next_close."""
            def on_bar(self, ctx, bar):
                # historical note: bar.next_close would be forbidden
                pass
    ''')
    results = CodeSafetyChecker().check(code)
    # Only the missing-on_bar check could fire — let's be precise and
    # assert no *lookahead* critical fired.
    criticals = _critical_details(results)
    assert not any("bar.next_" in c or "ctx.future_" in c or "ctx.peek" in c for c in criticals)


# ---------------------------------------------------------------------------
# Order-flow shape (#547 item 4): entry path + exit path required
# ---------------------------------------------------------------------------


def test_no_submit_order_is_critical_no_entry() -> None:
    """A Strategy that never calls ``ctx.submit_order`` has no entry path."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                pass  # no submit_order — strategy emits zero trades
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no entry path" in c for c in criticals), criticals


def test_single_submit_order_is_critical_no_exit() -> None:
    """A Strategy with only one ``ctx.submit_order`` call has no exit path."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no exit path" in c for c in criticals), criticals


def test_two_submit_orders_passes_order_flow_gate() -> None:
    """Two ``ctx.submit_order`` calls (entry + exit) satisfies the gate."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.close > 100:
                    ctx.submit_order(symbol='X', qty=1, side='LONG')
                else:
                    ctx.submit_order(symbol='X', qty=1, side='FLAT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c for c in criticals), criticals


def test_single_submit_order_with_bracket_exit_passes_order_flow_gate() -> None:
    """A single ``ctx.submit_order(..., attached_stop_loss=...)`` is a complete
    entry+bracket-exit pair (issue #389) and must not be flagged."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(
                    symbol='X',
                    qty=1,
                    side='LONG',
                    attached_stop_loss={'stop_price': 95.0},
                    attached_take_profit={'limit_price': 110.0},
                )
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c for c in criticals), criticals


def test_self_submit_order_does_not_satisfy_order_flow_gate() -> None:
    """Helper methods named ``self.submit_order`` never reach the runtime
    engine, so they must NOT count toward the entry/exit requirement."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def submit_order(self, **kw):
                pass  # internal helper, never calls ctx
            def on_bar(self, ctx, bar):
                self.submit_order(symbol='X')
                self.submit_order(symbol='Y')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no entry path" in c for c in criticals), criticals


def test_explicit_none_bracket_kwarg_does_not_satisfy_exit() -> None:
    """``attached_stop_loss=None`` is not a real bracket attachment.

    At the AST layer ``kw.value`` is always an ``ast.AST`` node — never
    Python ``None`` — so a naive ``kw.value is not None`` check would
    incorrectly accept the literal ``=None``. The gate must reject this
    as a one-sided strategy.
    """
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(
                    symbol='X',
                    qty=1,
                    side='LONG',
                    attached_stop_loss=None,
                    attached_take_profit=None,
                )
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no exit path" in c for c in criticals), criticals


def test_on_bar_with_context_param_name_is_accepted() -> None:
    """``on_bar(self, context, bar)`` is valid — the runtime calls hooks
    positionally, so the parameter name is the strategy's choice. The
    gate must accept ``context.submit_order(...)`` against a hook whose
    second positional parameter is named ``context``."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, context, bar):
                context.submit_order(symbol='X', qty=1, side='LONG')
                context.submit_order(symbol='X', qty=1, side='FLAT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c for c in criticals), criticals


def test_submit_order_in_unused_helper_does_not_satisfy_order_flow_gate() -> None:
    """``ctx.submit_order(...)`` inside a helper method that ``on_bar``
    never calls cannot reach the runtime engine. Walking the whole class
    body would falsely accept it; the gate must scan only engine hooks
    (``on_bar`` / ``on_start`` / ``on_fill`` / ``on_end``)."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def _unused_helper(self, ctx, bar):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='FLAT')

            def on_bar(self, ctx, bar):
                pass  # engine calls this, but it never emits orders
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no entry path" in c for c in criticals), criticals


def test_submit_order_in_on_start_does_not_satisfy_gate() -> None:
    """``send_start`` is invoked without processing its ``HarnessResponse``
    in the current trading service, so any ``submit_order`` made from
    ``on_start`` is dropped before backtesting. The gate must not count
    those calls — the strategy needs at least one entry call in ``on_bar``.
    """
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_start(self, ctx):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
            def on_bar(self, ctx, bar):
                pass
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no entry path" in c for c in criticals), criticals


def test_submit_order_only_in_on_fill_is_critical_no_entry() -> None:
    """``send_fill`` responses are dropped by the trading service today, so
    orders in ``on_fill`` never reach the engine. A strategy whose only
    submissions sit there must be rejected."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                pass  # never emits an entry order
            def on_fill(self, ctx, fill):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no entry path" in c for c in criticals), criticals


def test_submit_order_only_in_on_end_is_critical_no_entry() -> None:
    """``send_end`` responses are dropped by the trading service today,
    matching ``send_start`` / ``send_fill``. Orders in ``on_end`` never
    reach the engine."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                pass
            def on_end(self, ctx):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no entry path" in c for c in criticals), criticals


def test_on_bar_entry_with_on_fill_exit_is_critical_no_exit() -> None:
    """A common temptation is entry in ``on_bar`` + exit-on-fill in
    ``on_fill``. The on_fill response is dropped, so only the entry
    reaches the engine — one-sided, no real exit. Reject."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
            def on_fill(self, ctx, fill):
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no exit path" in c for c in criticals), criticals


def test_submit_order_in_invoked_local_closure_satisfies_gate() -> None:
    """A nested ``def`` inside ``on_bar`` that IS called by name within
    the same scope is engine-reachable — the closure captures ``ctx`` from
    the outer frame. Common idiom: ``def enter(): ctx.submit_order(...);
    enter()``."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                def enter():
                    ctx.submit_order(symbol='X', qty=1, side='LONG')
                def exit_position():
                    ctx.submit_order(symbol='X', qty=1, side='SHORT')
                if bar.close > 100:
                    enter()
                else:
                    exit_position()
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c for c in criticals), criticals


def test_submit_order_in_uninvoked_nested_def_does_not_satisfy_gate() -> None:
    """Nested function bodies declared inside ``on_bar`` only run if the
    enclosing scope calls them. An uninvoked local ``def`` containing
    ``ctx.submit_order(...)`` must NOT count toward the order-flow gate
    because Python never executes the body of a function it never calls.
    """
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                def _unused():
                    ctx.submit_order(symbol='X', qty=1, side='LONG')
                    ctx.submit_order(symbol='X', qty=1, side='SHORT')
                # _unused is defined but never invoked, so neither call runs.
                pass
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no entry path" in c for c in criticals), criticals


def test_submit_order_in_helper_called_from_on_bar_satisfies_order_flow_gate() -> None:
    """``on_bar`` that delegates to ``self._enter(ctx, bar)`` must be
    recognised — the helper is reachable from the engine via the hook."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def _enter(self, ctx, bar):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
            def _exit(self, ctx, bar):
                ctx.submit_order(symbol='X', qty=1, side='FLAT')
            def on_bar(self, ctx, bar):
                if bar.close > 100:
                    self._enter(ctx, bar)
                else:
                    self._exit(ctx, bar)
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c for c in criticals), criticals


def test_two_same_side_submit_orders_is_critical_no_exit() -> None:
    """Two ``side='LONG'`` calls do not form an entry+exit pair — both are
    entries. The gate must reject this as having no exit path."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='Y', qty=1, side='LONG')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no real exit leg" in c or "no exit path" in c for c in criticals), criticals


def test_long_and_short_submit_orders_pass_order_flow_gate() -> None:
    """``side='LONG'`` + ``side='SHORT'`` is a valid entry+opposite-exit pair."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.close > 100:
                    ctx.submit_order(symbol='X', qty=1, side='LONG')
                else:
                    ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c or "exit leg" in c for c in criticals), (
        criticals
    )


def test_dynamic_side_is_treated_optimistically() -> None:
    """When the ``side`` is computed at runtime (not a literal), we cannot
    statically tell whether the pair is one-sided, so we accept the strategy
    rather than false-fail it. Two calls with dynamic sides pass."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                side = self._pick_side(bar)
                ctx.submit_order(symbol='X', qty=1, side=side)
                ctx.submit_order(symbol='Y', qty=1, side=side)
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c or "exit leg" in c for c in criticals), (
        criticals
    )


def test_single_dynamic_side_call_is_accepted() -> None:
    """A single ctx.submit_order(..., side=<dynamic>) that routes both entry
    and exit through one call site (e.g. by inspecting ctx.position state)
    is a legitimate pattern. The gate must not false-fail it just because
    the side is not a literal LONG/SHORT."""
    code = textwrap.dedent("""
        from contract import Strategy
        from contract import OrderSide

        class S(Strategy):
            def on_bar(self, ctx, bar):
                pos = ctx.position(bar.symbol)
                side = OrderSide.SHORT if pos else OrderSide.LONG
                ctx.submit_order(symbol=bar.symbol, qty=1, side=side)
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c or "exit leg" in c for c in criticals), (
        criticals
    )


def test_non_order_side_literal_is_treated_as_unknown() -> None:
    """OrderSide only defines LONG/SHORT. Strings like 'FLAT', 'CLOSE',
    'BUY', 'SELL' would crash at runtime under OrderSide(side). The gate
    treats them as unknown sides (not as recognised entry/exit markers),
    so two same-side LONG calls do NOT slip through because the second
    happens to be 'FLAT'."""
    # Two calls both with non-OrderSide literals → both unknown → optimism
    # rule (any unknown side) accepts. The runtime will surface the real
    # error.
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol='X', qty=1, side='FLAT')
                ctx.submit_order(symbol='X', qty=1, side='CLOSE')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    # Both unknown → has_unknown=True → optimism. No "exit leg" critical.
    assert not any("exit leg" in c for c in criticals), criticals

    # But LONG + LONG (both recognised, same side, no unknown) still fails.
    bad_code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='LONG')
    """)
    bad_results = CodeSafetyChecker().check(bad_code)
    bad_criticals = _critical_details(bad_results)
    assert any("no real exit leg" in c for c in bad_criticals), bad_criticals


def test_submit_order_on_bar_parameter_does_not_satisfy_gate() -> None:
    """The engine calls hooks positionally — the SECOND positional after
    ``self`` is always the context. A strategy that puts ``submit_order``
    on the wrong parameter (``bar.submit_order`` when the second positional
    is ``ctx``) will crash at runtime because ``Bar`` has no submit_order.
    The gate must reject this rather than accept the misplaced call.
    """
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                bar.submit_order(symbol='X', qty=1, side='LONG')
                bar.submit_order(symbol='X', qty=1, side='FLAT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    # ``ctx`` is the accepted receiver for the hook; ``bar.submit_order``
    # is on the wrong parameter and is ignored, so the gate sees zero
    # valid calls and emits "no entry path".
    assert any("no entry path" in c for c in criticals), criticals


def test_on_bar_with_swapped_signature_uses_second_positional_as_ctx() -> None:
    """The engine calls hooks positionally — ``def on_bar(self, bar, ctx)``
    binds the runtime ctx to the parameter named ``bar`` (second positional).
    So ``bar.submit_order(...)`` is actually valid runtime code, and the
    gate must accept it. Conversely, the unused-name parameter ``ctx`` is
    bound to the Bar object and any ``ctx.submit_order`` would crash."""
    # Legitimate case: ``bar`` is second positional → it IS the runtime ctx.
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, bar, ctx):
                bar.submit_order(symbol='X', qty=1, side='LONG')
                bar.submit_order(symbol='X', qty=1, side='FLAT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c for c in criticals), criticals

    # Buggy case: same swapped signature but submit_order on ``ctx``, which
    # is now bound to the Bar object by the engine's positional dispatch.
    bad_code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, bar, ctx):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='FLAT')
    """)
    bad_results = CodeSafetyChecker().check(bad_code)
    bad_criticals = _critical_details(bad_results)
    assert any("no entry path" in c for c in bad_criticals), bad_criticals


def test_helper_receiver_bound_to_call_site_argument_rejects_bar_only() -> None:
    """The walker must propagate the call-site argument that corresponds
    to the hook's context — not blindly accept every helper parameter as
    a possible receiver. ``def _trade(self, bar): bar.submit_order(...)``
    called as ``self._trade(bar)`` would crash at runtime (bar is the
    Bar object, not the StrategyContext) and must NOT pass the gate."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def _trade(self, bar):
                bar.submit_order(symbol='X', qty=1, side='LONG')
                bar.submit_order(symbol='X', qty=1, side='SHORT')
            def on_bar(self, ctx, bar):
                self._trade(bar)
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no entry path" in c for c in criticals), criticals


def test_helper_receiver_bound_via_keyword_argument() -> None:
    """When the helper is called with the context as a keyword argument
    (``self._trade(ctx=ctx, bar=bar)``), the helper's ``ctx`` parameter
    is correctly bound to the runtime context and ``ctx.submit_order``
    inside the helper satisfies the gate."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def _trade(self, bar, ctx):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
            def on_bar(self, ctx, bar):
                self._trade(ctx=ctx, bar=bar)
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c for c in criticals), criticals


def test_helper_with_renamed_ctx_parameter_is_recognised() -> None:
    """When the helper renames its context parameter (``def _trade(self,
    my_ctx): my_ctx.submit_order(...)``), passing the hook's ``ctx`` as
    that positional argument must propagate the binding."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def _trade(self, my_ctx):
                my_ctx.submit_order(symbol='X', qty=1, side='LONG')
                my_ctx.submit_order(symbol='X', qty=1, side='SHORT')
            def on_bar(self, ctx, bar):
                self._trade(ctx)
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c for c in criticals), criticals


def test_transitive_helper_submit_orders_are_recognised() -> None:
    """Helpers called from helpers (transitively reachable from a hook)
    are still in the engine-reachable call graph and must count. The
    second submit_order lives two hops away from ``on_bar``."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def _emit_exit(self, ctx):
                ctx.submit_order(symbol='X', qty=1, side='FLAT')
            def _trade(self, ctx, bar):
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                self._emit_exit(ctx)
            def on_bar(self, ctx, bar):
                self._trade(ctx, bar)
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c for c in criticals), criticals
