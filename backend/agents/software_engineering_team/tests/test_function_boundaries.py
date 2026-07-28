"""Tests for structured enclosing-construct lookup (function_boundaries.py)."""

from __future__ import annotations

from code_review_agent.function_boundaries import (
    enclosing_construct,
    enclosing_construct_start_heuristic,
    strip_numbered_prefixes,
)

# --------------------------------------------------------------------------- enclosing_construct


def test_top_level_function() -> None:
    content = "def foo():\n    x = 1\n    return x\n"
    result = enclosing_construct(content, 2)
    assert result is not None
    assert result.name == "foo"
    assert result.kind == "function"
    assert result.start_line == 1
    assert result.end_line == 3


def test_nested_function_picks_innermost() -> None:
    content = "\n".join(
        [
            "def outer():",  # 1
            "    def inner():",  # 2
            "        return 1",  # 3
            "    return inner()",  # 4
            "",
        ]
    )
    result = enclosing_construct(content, 3)
    assert result is not None
    assert result.name == "inner"
    assert result.start_line == 2
    assert result.end_line == 3


def test_class_method_is_qualified_with_class_name() -> None:
    content = "\n".join(
        [
            "class Widget:",  # 1
            "    def draw(self):",  # 2
            "        return 1",  # 3
            "",
        ]
    )
    result = enclosing_construct(content, 3)
    assert result is not None
    assert result.name == "Widget.draw"
    assert result.kind == "function"


def test_decorated_function_start_includes_decorator_line() -> None:
    content = "\n".join(
        [
            "@decorator",  # 1
            "def foo():",  # 2
            "    return 1",  # 3
            "",
        ]
    )
    result = enclosing_construct(content, 3)
    assert result is not None
    assert result.start_line == 1


def test_module_level_line_returns_none() -> None:
    content = "x = 1\ny = 2\n"
    assert enclosing_construct(content, 1) is None


def test_syntax_error_returns_none() -> None:
    content = "def foo(:\n    pass\n"
    assert enclosing_construct(content, 1) is None


def test_line_outside_any_construct_returns_none() -> None:
    content = "def foo():\n    return 1\n\nx = foo()\n"
    assert enclosing_construct(content, 4) is None


# --------------------------------------------------------------------------- enclosing_construct_start_heuristic


def test_heuristic_finds_column_zero_declaration() -> None:
    content = "\n".join(
        [
            "function foo() {",  # 1
            "  return 1;",  # 2
            "}",  # 3
            "",
        ]
    )
    assert enclosing_construct_start_heuristic(content, 2) == 1


def test_heuristic_skips_closing_brackets_and_comments() -> None:
    content = "\n".join(
        [
            "function foo() {",  # 1
            "  return 1;",  # 2
            "}",  # 3
            "// a comment",  # 4
            "function bar() {",  # 5
            "  return 2;",  # 6
            "}",  # 7
            "",
        ]
    )
    assert enclosing_construct_start_heuristic(content, 6) == 5


def test_heuristic_no_construct_returns_none() -> None:
    content = "  indented only\n  still indented\n"
    assert enclosing_construct_start_heuristic(content, 2) is None


# --------------------------------------------------------------------------- strip_numbered_prefixes


def test_strip_numbered_prefixes_plain_content_unchanged() -> None:
    content = "def foo():\n    return 1\n"
    stripped, physical, mapper = strip_numbered_prefixes(content, line_number=2)
    assert stripped == content
    assert physical == 2
    assert mapper is None


def test_strip_numbered_prefixes_detects_and_strips() -> None:
    content = "4240: def foo():\n4241:     x = 1\n4242:     return x\n"
    stripped, physical, mapper = strip_numbered_prefixes(content, line_number=4242)
    assert stripped == "def foo():\n    x = 1\n    return x"
    assert physical == 3
    assert mapper is not None
    assert mapper(3) == 4242


def test_strip_numbered_prefixes_empty_content() -> None:
    stripped, physical, mapper = strip_numbered_prefixes("", line_number=1)
    assert stripped == ""
    assert physical == 1
    assert mapper is None
