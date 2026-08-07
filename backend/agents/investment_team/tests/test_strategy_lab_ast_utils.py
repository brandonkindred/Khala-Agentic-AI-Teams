"""Unit tests for strategy_lab.ast_utils shared AST primitives."""

from __future__ import annotations

import ast

from investment_team.strategy_lab.ast_utils.names import call_name, func_name, name_or_attr


def _call(src: str) -> ast.Call:
    mod = ast.parse(src)
    stmt = mod.body[0]
    assert isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
    return stmt.value


class TestNameOrAttr:
    def test_name(self) -> None:
        assert name_or_attr(ast.Name(id="foo", ctx=ast.Load())) == "foo"

    def test_attribute(self) -> None:
        node = ast.Attribute(
            value=ast.Name(id="self", ctx=ast.Load()),
            attr="bar",
            ctx=ast.Load(),
        )
        assert name_or_attr(node) == "bar"

    def test_other_returns_none(self) -> None:
        assert name_or_attr(ast.Constant(value=1)) is None
        assert name_or_attr(None) is None


class TestCallNamePreservesSafetyContract:
    def test_simple_name(self) -> None:
        assert call_name(_call("foo()")) == "foo"

    def test_attribute(self) -> None:
        assert call_name(_call("ctx.submit_order()")) == "submit_order"

    def test_unknown_returns_empty_string(self) -> None:
        call = _call("(lambda: None)()")
        assert call_name(call) == ""


class TestFuncNamePreservesProbeContract:
    def test_lowercases_name(self) -> None:
        assert func_name(ast.Name(id="ATR", ctx=ast.Load())) == "atr"

    def test_lowercases_attribute(self) -> None:
        node = ast.Attribute(
            value=ast.Name(id="self", ctx=ast.Load()),
            attr="Bollinger",
            ctx=ast.Load(),
        )
        assert func_name(node) == "bollinger"

    def test_unknown_returns_none(self) -> None:
        assert func_name(ast.Constant(value="x")) is None
