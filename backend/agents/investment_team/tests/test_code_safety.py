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
    assert any("bar.next" in c for c in criticals)


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
    assert not any("bar.next" in c or "ctx.future_" in c or "ctx.peek" in c for c in criticals)


def test_bar_tomorrow_close_is_critical() -> None:
    """``bar.tomorrowClose`` — camel-case forward-attribute variant the
    underscore-only legacy regex used to miss."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.tomorrowClose > bar.close:
                    ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("tomorrow" in c for c in criticals)


def test_bar_forthcoming_open_is_critical() -> None:
    """``bar.forthcomingOpen`` — second new forward-prefix the widened
    pattern adds. Both camel-case and snake-case variants must trip."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.forthcoming_open > bar.open:
                    ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("forthcoming" in c for c in criticals)


def test_bar_next_attribute_without_underscore_is_critical() -> None:
    """The widened pattern catches ``bar.next`` even when no underscore /
    suffix follows — the legacy ``next_\\w+`` form required an explicit
    separator and silently allowed bare ``bar.next``."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                forecast = bar.next
                if forecast > 0:
                    ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("bar.next" in c for c in criticals)


def test_getattr_on_bar_is_warning_not_critical() -> None:
    """``getattr(bar, ...)`` dodges the harness's AttributeError trap. It's
    occasionally used in defensive idioms (test fixtures, optional fields),
    so the gate flags it as a warning — not a critical that vetoes the run.
    """
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                _ = getattr(bar, 'next_close', 0.0)
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    criticals = _critical_details(results)
    assert any("getattr" in w for w in warnings), warnings
    # Must NOT also fire as critical — defensive idioms shouldn't veto.
    assert not any("getattr" in c for c in criticals)


def test_getattr_on_ctx_is_warning() -> None:
    """The same warning fires for ``ctx`` because the runtime trap covers
    every attribute access on either receiver."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                _ = getattr(ctx, 'future_history', None)
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    assert any("getattr" in w for w in warnings)


def test_try_except_attribute_error_around_bar_is_warning() -> None:
    """Swallowing ``AttributeError`` on a ``bar.*`` access silently dismisses
    the runtime lookahead_violation trap. The AST rule flags this as a
    warning so reviewers can decide whether to keep the defensive shape."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                try:
                    forecast = bar.future_close
                except AttributeError:
                    forecast = bar.close
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    # The forward-access body ALSO trips the regex critical for
    # ``bar.future_close``; the AST rule's contribution is the warning
    # about the surrounding try/except.
    assert any("try/except" in w for w in warnings), warnings


def test_try_except_attribute_error_tuple_is_also_flagged() -> None:
    """``except (AttributeError, KeyError):`` swallows the trap just as
    cleanly as the bare form — the rule walks the handler's tuple."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                try:
                    extra = bar.something
                except (AttributeError, KeyError):
                    extra = None
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    assert any("try/except" in w for w in warnings)


def test_try_except_attribute_error_without_bar_or_ctx_is_not_flagged() -> None:
    """An ``except AttributeError`` block that doesn't touch ``bar``/``ctx``
    in its body has nothing to do with look-ahead — the rule must stay
    silent so it doesn't fire on unrelated defensive code."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                try:
                    self._counter += 1
                except AttributeError:
                    self._counter = 1
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    assert not any("try/except" in w for w in warnings), warnings


def test_subscript_with_positive_offset_on_self_collection_is_warning() -> None:
    """Reading ``self._closes[i + 1]`` is a structural look-ahead inside a
    preloaded series — the engine's per-bar dispatch alone can't see this,
    but the AST rule should."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def __init__(self):
                self._closes = []

            def on_bar(self, ctx, bar):
                self._closes.append(bar.close)
                i = len(self._closes) - 1
                if i + 1 < len(self._closes) and self._closes[i + 1] > bar.close:
                    ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    assert any("Subscript" in w and "positive offset" in w for w in warnings), warnings


def test_subscript_with_negative_offset_is_not_flagged() -> None:
    """``self._closes[i - 1]`` reads the prior bar — perfectly valid under
    the contract. The rule must not fire on backward offsets."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def __init__(self):
                self._closes = []

            def on_bar(self, ctx, bar):
                self._closes.append(bar.close)
                i = len(self._closes) - 1
                if i - 1 >= 0 and self._closes[i - 1] < bar.close:
                    ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    assert not any("Subscript" in w for w in warnings), warnings


def test_subscript_on_non_self_collection_is_not_flagged() -> None:
    """The rule scopes to ``self.<known-collection>``; local variables and
    parameters are out of scope (we can't tell whether they're preloaded
    series or just live iterables)."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                local_list = [bar.close]
                i = 0
                # Subscript on a local — the rule must not infer look-ahead.
                if i + 1 < len(local_list):
                    pass
                ctx.submit_order(symbol='X', qty=1, side='LONG')
                ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    assert not any("Subscript" in w for w in warnings), warnings


def test_getattr_on_renamed_bar_parameter_is_still_flagged() -> None:
    """The AST rule must resolve the actual ``on_bar`` parameter names
    rather than hardcoding ``bar``/``ctx``. A strategy that renames its
    parameters — ``def on_bar(self, c, b):`` — must still trip the
    getattr warning when the *renamed* receiver is the target.
    """
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, c, b):
                _ = getattr(b, 'next_close', 0.0)
                c.submit_order(symbol='X', qty=1, side='LONG')
                c.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    assert any("getattr" in w for w in warnings), warnings


def test_try_except_on_renamed_ctx_parameter_is_still_flagged() -> None:
    """Same concern for the try/except rule: ``try: c.future_data``
    where ``c`` is the renamed ctx must still trip the warning."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, c, b):
                try:
                    forecast = b.future_close
                except AttributeError:
                    forecast = b.close
                c.submit_order(symbol='X', qty=1, side='LONG')
                c.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    assert any("try/except" in w for w in warnings), warnings


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


def test_simple_ctx_alias_is_tracked() -> None:
    """A strategy may alias the context before submitting orders. The
    walker should treat the alias as equivalent to the original
    receiver name for the rest of the scope's body."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                trade_ctx = ctx
                trade_ctx.submit_order(symbol='X', qty=1, side='LONG')
                trade_ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c for c in criticals), criticals


def test_dynamic_symbol_pairs_with_literal_symbol_passes() -> None:
    """A generated strategy may enter with ``symbol=bar.symbol`` and exit
    with a configured literal symbol (or vice versa). On a single-symbol
    backtest run those calls target the same runtime position, so the
    gate must treat the dynamic call as augmenting every literal-symbol
    group rather than splitting them into two failing buckets."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.close > 100:
                    ctx.submit_order(symbol=bar.symbol, qty=1, side='LONG')
                else:
                    ctx.submit_order(symbol='SPY', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c or "exit leg" in c for c in criticals), (
        criticals
    )


def test_call_before_alias_assignment_is_not_credited() -> None:
    """A ``trade_ctx.submit_order(...)`` call that appears BEFORE the
    ``trade_ctx = ctx`` assignment must NOT be credited as a runtime
    order — at that point ``trade_ctx`` either doesn't exist or refers
    to something else. The flow-sensitive alias tracker only adds the
    alias to the live receiver set when the assignment is reached."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                # Two submits BEFORE the alias binding — invalid uses
                # of the unbound name. Should not be credited.
                trade_ctx.submit_order(symbol='X', qty=1, side='LONG')
                trade_ctx.submit_order(symbol='X', qty=1, side='SHORT')
                trade_ctx = ctx  # alias bound AFTER the calls above
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no entry path" in c for c in criticals), criticals


def test_call_after_alias_rebound_is_not_credited() -> None:
    """When the alias name is later reassigned to a non-receiver, calls
    after the rebind no longer reference the runtime context. The
    flow-sensitive walker drops the alias from state on rebind."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                trade_ctx = ctx
                # rebound to a non-receiver before any submit_order calls
                trade_ctx = bar
                trade_ctx.submit_order(symbol='X', qty=1, side='LONG')
                trade_ctx.submit_order(symbol='X', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no entry path" in c for c in criticals), criticals


def test_long_on_one_symbol_short_on_another_is_critical_no_exit() -> None:
    """Pair-trading-style strategy that opens LONG on SPY and SHORT on
    TLT has two unrelated entries, not an entry+exit pair. The engine
    closes positions per-symbol (``portfolio.positions[bar.symbol]``),
    so neither symbol has a real exit and the gate must reject it."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol='SPY', qty=1, side='LONG')
                ctx.submit_order(symbol='TLT', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no exit path" in c or "no real exit leg" in c for c in criticals), criticals


def test_long_and_short_on_same_symbol_passes() -> None:
    """LONG + SHORT on the SAME literal symbol is a valid entry+exit pair."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.close > 100:
                    ctx.submit_order(symbol='SPY', qty=1, side='LONG')
                else:
                    ctx.submit_order(symbol='SPY', qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c or "exit leg" in c for c in criticals), (
        criticals
    )


def test_dynamic_symbol_groups_together_and_passes() -> None:
    """``symbol=bar.symbol`` resolves to the same runtime symbol within
    one ``on_bar`` call, so dynamic-symbol calls share a single group.
    LONG + SHORT on dynamic symbol passes."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.close > 100:
                    ctx.submit_order(symbol=bar.symbol, qty=1, side='LONG')
                else:
                    ctx.submit_order(symbol=bar.symbol, qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c or "exit leg" in c for c in criticals), (
        criticals
    )


def test_closure_parameter_shadowing_outer_ctx_is_not_captured() -> None:
    """A nested closure that names its own parameter ``ctx`` rebinds the
    name inside the closure scope — the outer ``ctx`` is no longer
    reachable as a receiver inside. So ``def enter(ctx): ctx.submit_order
    (...); enter(bar)`` does NOT count as a valid entry (the inner ``ctx``
    is bound to the Bar at runtime)."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                def enter(ctx):
                    ctx.submit_order(symbol='X', qty=1, side='LONG')
                def exit_position(ctx):
                    ctx.submit_order(symbol='X', qty=1, side='SHORT')
                enter(bar)
                exit_position(bar)
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert any("no entry path" in c for c in criticals), criticals


def test_local_closure_with_parameter_is_bound_at_call_site() -> None:
    """A nested closure that accepts the context as a parameter and is
    invoked with the runtime ctx should have that parameter bound:
    ``def enter(c): c.submit_order(...); enter(ctx)``. Previously the
    walker reused the outer receiver names and failed to count
    ``c.submit_order``."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                def enter(c):
                    c.submit_order(symbol='X', qty=1, side='LONG')
                def exit_position(c):
                    c.submit_order(symbol='X', qty=1, side='SHORT')
                enter(ctx)
                exit_position(ctx)
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c for c in criticals), criticals


def test_helper_revisited_with_distinct_bindings() -> None:
    """When the same helper is invoked twice — once without the context
    (``self._trade(bar)``) and once with (``self._trade(ctx)``) — the
    walker must analyse both bindings rather than dedupe on the helper's
    AST identity. The ctx-bound call's submit_order is what reaches
    the engine, so the gate must recognise it."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def _trade(self, c):
                c.submit_order(symbol='X', qty=1, side='LONG')
                c.submit_order(symbol='X', qty=1, side='SHORT')
            def on_bar(self, ctx, bar):
                if bar.close > 100:
                    # First call binds `c` to `bar` (not in receivers).
                    self._trade(bar)
                else:
                    # Second call binds `c` to `ctx` — the gate must
                    # recognise this even though _trade has been visited
                    # once with the empty-binding from the first call.
                    self._trade(ctx)
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("entry path" in c or "exit path" in c for c in criticals), criticals


def test_chained_ctx_aliases_are_tracked() -> None:
    """Alias chains (``a = ctx; b = a``) propagate at the fixed point."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                a = ctx
                b = a
                b.submit_order(symbol='X', qty=1, side='LONG')
                b.submit_order(symbol='X', qty=1, side='SHORT')
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


# ---------------------------------------------------------------------------
# Issue #524 — symbol-universe guard required when spec.target_symbols set
# ---------------------------------------------------------------------------


class _StubSpec:
    """Minimal duck-typed spec for the universe-guard rule.

    The real ``StrategySpec`` pulls in pydantic + dsl modules; the rule
    only reads ``getattr(spec, 'target_symbols', None)`` so a stub keeps
    these tests free of heavy imports.
    """

    def __init__(self, target_symbols):
        self.target_symbols = target_symbols


def test_universe_guard_required_when_target_symbols_set() -> None:
    """Spec with non-empty target_symbols + code with UNIVERSE + guard passes."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"GLD"})
            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                ctx.submit_order(symbol=bar.symbol, qty=1, side='LONG')
                ctx.submit_order(symbol=bar.symbol, qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code, _StubSpec(["GLD"]))
    criticals = _critical_details(results)
    assert not any("UNIVERSE" in c for c in criticals), criticals


def test_missing_universe_constant_is_critical_when_target_symbols_set() -> None:
    """Spec with non-empty target_symbols + code missing UNIVERSE → critical."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side='LONG')
                ctx.submit_order(symbol=bar.symbol, qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code, _StubSpec(["GLD"]))
    criticals = _critical_details(results)
    assert any("missing a UNIVERSE" in c for c in criticals), criticals


def test_missing_guard_in_on_bar_is_critical_when_target_symbols_set() -> None:
    """UNIVERSE present but no guard at the top of on_bar → critical."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"GLD"})
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side='LONG')
                ctx.submit_order(symbol=bar.symbol, qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code, _StubSpec(["GLD"]))
    criticals = _critical_details(results)
    assert any("missing the" in c and "UNIVERSE" in c for c in criticals), criticals


def test_universe_guard_not_required_when_target_symbols_empty() -> None:
    """Universe-agnostic spec (target_symbols=[]) does not require UNIVERSE."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side='LONG')
                ctx.submit_order(symbol=bar.symbol, qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code, _StubSpec([]))
    criticals = _critical_details(results)
    assert not any("UNIVERSE" in c for c in criticals), criticals


def test_universe_guard_not_required_when_spec_omitted() -> None:
    """Legacy callers passing only ``code`` (no spec) keep the old behaviour."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol=bar.symbol, qty=1, side='LONG')
                ctx.submit_order(symbol=bar.symbol, qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code)
    criticals = _critical_details(results)
    assert not any("UNIVERSE" in c for c in criticals), criticals


def test_universe_guard_accepts_set_literal_constant() -> None:
    """``UNIVERSE = {"GLD"}`` (set display) is also accepted — the runtime
    membership test only needs an ``in``-able collection."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = {"GLD"}
            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                ctx.submit_order(symbol=bar.symbol, qty=1, side='LONG')
                ctx.submit_order(symbol=bar.symbol, qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code, _StubSpec(["GLD"]))
    criticals = _critical_details(results)
    assert not any("UNIVERSE" in c for c in criticals), criticals


def test_universe_guard_accepts_renamed_bar_parameter() -> None:
    """The engine dispatches on_bar positionally — the second positional
    after self IS the runtime context, the third IS the Bar. Strategies
    that rename ``bar`` (e.g. ``def on_bar(self, ctx, b)``) still satisfy
    the gate as long as the guard uses that name."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"GLD"})
            def on_bar(self, ctx, b):
                if b.symbol not in self.UNIVERSE:
                    return
                ctx.submit_order(symbol=b.symbol, qty=1, side='LONG')
                ctx.submit_order(symbol=b.symbol, qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code, _StubSpec(["GLD"]))
    criticals = _critical_details(results)
    assert not any("UNIVERSE" in c for c in criticals), criticals


def test_universe_guard_accepts_return_none_form() -> None:
    """Lint-style ``return None`` is semantically identical to a bare
    ``return``; both must satisfy the structural gate."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"GLD"})
            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return None
                ctx.submit_order(symbol=bar.symbol, qty=1, side='LONG')
                ctx.submit_order(symbol=bar.symbol, qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code, _StubSpec(["GLD"]))
    criticals = _critical_details(results)
    assert not any("UNIVERSE" in c for c in criticals), criticals


def test_universe_guard_with_non_return_body_is_critical() -> None:
    """A guard whose body falls through to the signal logic (``pass`` or
    any non-``return`` statement) does not actually short-circuit and must
    be rejected, even when the if-test is structurally correct."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"GLD"})
            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    pass  # falls through to the signal logic — useless guard
                ctx.submit_order(symbol=bar.symbol, qty=1, side='LONG')
                ctx.submit_order(symbol=bar.symbol, qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code, _StubSpec(["GLD"]))
    criticals = _critical_details(results)
    assert any("missing the" in c and "UNIVERSE" in c for c in criticals), criticals


def test_empty_universe_passes_structural_gate_with_non_empty_target_symbols() -> None:
    """When ``target_symbols`` is non-empty, an empty ``UNIVERSE =
    frozenset()`` still passes the structural gate — the gate's job is
    presence, not content equality. The runtime would then reject every
    bar and the BacktestAnomalyDetector / #526 TargetSymbolCoverageGate
    would catch the resulting zero-trade or wrong-symbol outcome."""
    code = textwrap.dedent("""
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset()
            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                ctx.submit_order(symbol=bar.symbol, qty=1, side='LONG')
                ctx.submit_order(symbol=bar.symbol, qty=1, side='SHORT')
    """)
    results = CodeSafetyChecker().check(code, _StubSpec(["GLD"]))
    criticals = _critical_details(results)
    assert not any("UNIVERSE" in c for c in criticals), criticals
