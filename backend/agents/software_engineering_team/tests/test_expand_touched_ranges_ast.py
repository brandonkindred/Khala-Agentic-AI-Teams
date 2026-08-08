"""Python AST path for ``expand_touched_ranges`` (#5419)."""

from __future__ import annotations

from software_engineering_team.code_review_agent.change_surface import (
    LineRange,
    expand_touched_ranges,
)

_TOP_LEVEL = """\
def outer():
    return 1

x = 1
"""

_NESTED = """\
class Widget:
    def method(self):
        return self

    def other(self):
        return 2
"""

_DECORATED = """\
def deco(fn):
    return fn

@deco
def decorated():
    return 3
"""


def test_expand_top_level_function() -> None:
    ranges = expand_touched_ranges(_TOP_LEVEL, {2}, path="mod.py")
    assert ranges == (LineRange(1, 2),)


def test_expand_nested_method() -> None:
    # Line 3 is inside ``Widget.method`` (innermost), not the whole class.
    ranges = expand_touched_ranges(_NESTED, {3}, path="widget.py")
    assert ranges == (LineRange(2, 3),)


def test_expand_decorated_function_includes_decorator_lines() -> None:
    # ``decorated`` body is line 6; construct starts at ``@deco`` (line 4).
    ranges = expand_touched_ranges(_DECORATED, {6}, path="decorated.py")
    assert ranges == (LineRange(4, 6),)


def test_expand_multiple_touched_lines_same_function_dedupes() -> None:
    content = "def f():\n    a = 1\n    b = 2\n    return a + b\n"
    ranges = expand_touched_ranges(content, {2, 3, 4}, path="f.py")
    assert ranges == (LineRange(1, 4),)


def test_expand_module_level_line_uses_fallback() -> None:
    ranges = expand_touched_ranges(_TOP_LEVEL, {4}, path="mod.py")
    assert ranges == (LineRange(4, 4),)


def test_expand_unparseable_python_uses_fallback() -> None:
    ranges = expand_touched_ranges("def broken(\n", {1}, path="broken.py")
    assert ranges == (LineRange(1, 1),)


def test_expand_empty_path_parseable_python_uses_ast() -> None:
    ranges = expand_touched_ranges(_TOP_LEVEL, {2}, path="")
    assert ranges == (LineRange(1, 2),)


def test_expand_pyi_suffix_uses_ast() -> None:
    ranges = expand_touched_ranges("def f(): ...\n", {1}, path="stubs.pyi")
    assert ranges == (LineRange(1, 1),)


def test_expand_empty_path_unparseable_uses_fallback() -> None:
    ranges = expand_touched_ranges("function f() { return 1; }\n", {1}, path="")
    assert ranges == (LineRange(1, 1),)


def test_expand_ignores_non_positive_touched_lines() -> None:
    ranges = expand_touched_ranges("def f():\n    return 1\n", {0, -1, 2}, path="f.py")
    assert ranges == (LineRange(1, 2),)
