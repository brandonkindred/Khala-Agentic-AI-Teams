"""Unit tests for the runtime AST instrumenter (#449)."""

from __future__ import annotations

import ast
import textwrap

import pytest

from investment_team.models import RuleIndex
from investment_team.strategy_lab.coverage_probe import instrument_strategy_code
from investment_team.strategy_lab.quality_gates.code_safety import CodeSafetyChecker


def _wrap_in_strategy(on_bar_body: str) -> str:
    body = textwrap.indent(textwrap.dedent(on_bar_body).strip("\n"), " " * 8)
    return (
        textwrap.dedent(
            """\
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
        """
        )
        + body
        + "\n"
    )


def _probe_calls(code: str) -> list[ast.Call]:
    tree = ast.parse(code)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "__probe_record__"
    ]


def test_returns_rule_index_for_simple_if() -> None:
    code = _wrap_in_strategy(
        """
        rsi = 10
        if rsi < 25:
            return
        """
    )
    rewritten, index = instrument_strategy_code(code)

    assert isinstance(index, RuleIndex)
    assert len(index.rules) == 1
    label = next(iter(index.rules.values()))
    assert label == "rsi < 25"
    assert len(_probe_calls(rewritten)) == 1


def test_boolop_and_splits_into_legs() -> None:
    code = _wrap_in_strategy(
        """
        a = 1
        b = 0
        if a > 0 and b < 1:
            return
        """
    )
    rewritten, index = instrument_strategy_code(code)

    assert len(index.rules) == 2
    assert "a > 0" in index.rules.values()
    assert "b < 1" in index.rules.values()

    tree = ast.parse(rewritten)
    if_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.If)]
    assert any(isinstance(n.test, ast.BoolOp) and isinstance(n.test.op, ast.And) for n in if_nodes)


def test_nested_boolop_recurses() -> None:
    code = _wrap_in_strategy(
        """
        a = 1
        b = 0
        c = 1
        if (a or b) and c:
            return
        """
    )
    _, index = instrument_strategy_code(code)
    assert len(index.rules) == 3
    assert {"a", "b", "c"}.issubset(set(index.rules.values()))


def test_unaryop_not_is_single_leaf() -> None:
    code = _wrap_in_strategy(
        """
        x = False
        if not x:
            return
        """
    )
    _, index = instrument_strategy_code(code)
    assert len(index.rules) == 1
    assert next(iter(index.rules.values())) == "not x"


def test_elif_chain_each_branch_indexed() -> None:
    code = _wrap_in_strategy(
        """
        x = 0
        if x < 0:
            return
        elif x == 0:
            return
        elif x > 100:
            return
        else:
            return
        """
    )
    _, index = instrument_strategy_code(code)
    labels = set(index.rules.values())
    assert labels == {"x < 0", "x == 0", "x > 100"}


def test_idempotent_second_pass_is_noop() -> None:
    code = _wrap_in_strategy(
        """
        a = 1
        if a > 0:
            return
        """
    )
    once, index_once = instrument_strategy_code(code)
    twice, index_twice = instrument_strategy_code(once)

    assert once == twice
    assert index_once.rules == index_twice.rules


def test_malformed_source_returns_original_with_empty_index_and_warns() -> None:
    bad_code = "def x("
    with pytest.warns(UserWarning):
        rewritten, index = instrument_strategy_code(bad_code)
    assert rewritten == bad_code
    assert index.rules == {}


def test_no_on_bar_returns_original_unchanged() -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            pass
        """
    )
    rewritten, index = instrument_strategy_code(code)
    assert rewritten == code
    assert index.rules == {}


def test_helper_function_if_is_not_wrapped() -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                def _filter(x):
                    if x > 0:
                        return True
                    return False
                if _filter(1):
                    return
        """
    )
    _, index = instrument_strategy_code(code)
    # Only the on_bar's top-level if is wrapped; the nested helper's if is not.
    assert len(index.rules) == 1
    assert next(iter(index.rules.values())) == "_filter(1)"


def test_constant_test_is_not_wrapped() -> None:
    code = _wrap_in_strategy(
        """
        if True:
            return
        """
    )
    _, index = instrument_strategy_code(code)
    assert index.rules == {}


def test_instrumented_code_passes_code_safety_gate() -> None:
    from .golden.strategies import SMA_CROSSOVER_CODE

    rewritten, _ = instrument_strategy_code(SMA_CROSSOVER_CODE)
    results = CodeSafetyChecker().check(rewritten)
    criticals = [r for r in results if r.severity == "critical"]
    assert criticals == [], [r.details for r in criticals]


def test_no_harness_runs_with_identity_default() -> None:
    code = _wrap_in_strategy(
        """
        a = 5
        b = 1
        if a > 0 and b < 10:
            self.fired = True
        """
    )
    rewritten, _ = instrument_strategy_code(code)
    # Replace the contract import + class so we can exec-and-call without the
    # real Strategy base. We just need the on_bar body to execute under the
    # bootstrap prelude.
    standalone = rewritten.replace("from contract import Strategy", "Strategy = object")
    namespace: dict = {}
    exec(compile(standalone, "<probe-test>", "exec"), namespace)
    instance = namespace["S"]()
    instance.on_bar(None, None)
    assert getattr(instance, "fired", False) is True


def test_index_is_rebuilt_when_already_instrumented() -> None:
    code = _wrap_in_strategy(
        """
        a = 1
        if a > 0:
            return
        """
    )
    once, index_once = instrument_strategy_code(code)
    _, index_again = instrument_strategy_code(once)
    assert index_again.rules == index_once.rules


def test_future_import_stays_first_after_instrumentation() -> None:
    code = textwrap.dedent(
        '''
        """Top-level module docstring."""

        from __future__ import annotations

        from contract import Strategy


        class S(Strategy):
            def on_bar(self, ctx, bar):
                a = 1
                if a > 0:
                    return
        '''
    ).lstrip()
    rewritten, index = instrument_strategy_code(code)
    assert len(index.rules) == 1

    # Must compile (the actual constraint __future__ enforces).
    compile(rewritten, "<probe-test>", "exec")

    # And the __future__ import must still appear before any non-docstring
    # non-future statement in the rewritten module.
    tree = ast.parse(rewritten)
    seen_future = False
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
            seen_future = True
            continue
        # Once we hit any other statement, the future import must already
        # have been seen.
        assert seen_future, (
            f"non-future stmt {type(stmt).__name__} appeared before __future__ import"
        )
        break


def test_strategy_local_bar_index_does_not_capture_probe_reference() -> None:
    """A strategy that binds ``bar_index`` locally must not break the probe.

    Regression for the codex-connector P2 finding: if the probe used a
    plain ``bar_index`` name, Python's symbol-table analysis would treat
    every reference inside ``on_bar`` as a local (LOAD_FAST), shadowing
    the harness global. Using the dunder name ``__probe_bar_index__``
    avoids the collision entirely.
    """
    code = _wrap_in_strategy(
        """
        for bar_index in range(3):
            pass
        a = 1
        if a > 0:
            self.fired = True
        """
    )
    rewritten, _ = instrument_strategy_code(code)
    standalone = rewritten.replace("from contract import Strategy", "Strategy = object")
    namespace: dict = {}
    exec(compile(standalone, "<probe-test>", "exec"), namespace)
    instance = namespace["S"]()
    instance.on_bar(None, None)
    assert getattr(instance, "fired", False) is True


def test_rule_ids_are_stable_and_sequential() -> None:
    code = _wrap_in_strategy(
        """
        a = 1
        b = 2
        c = 3
        if a > 0 and b > 0:
            return
        if c > 0:
            return
        """
    )
    _, index = instrument_strategy_code(code)
    assert sorted(index.rules.keys()) == ["r0", "r1", "r2"]
