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
                pass
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
                pass
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
                pass
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
