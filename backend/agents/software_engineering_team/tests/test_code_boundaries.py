"""Tests for function/method boundary detection used by review chunking.

Pure-function tests: ``preferred_break_lines`` maps source text to the 1-based
lines that start a top-level construct, and degrades to an empty set whenever it
cannot find any (the coordinator reads that as "split on line boundaries").
"""

from __future__ import annotations

from code_review_agent.code_boundaries import preferred_break_lines

# ---------------------------------------------------------------------------
# Python (ast) detection
# ---------------------------------------------------------------------------


def test_python_returns_each_top_level_construct_start() -> None:
    src = "\n".join(
        [
            "import os",  # 1
            "",  # 2
            "def first():",  # 3
            "    return 1",  # 4
            "",  # 5
            "class Foo:",  # 6
            "    def method(self):",  # 7
            "        return 2",  # 8
            "",  # 9
            "async def third():",  # 10
            "    return 3",  # 11
        ]
    )
    # Top-level def/class/async-def starts; the nested method is not top-level.
    assert preferred_break_lines("m.py", src) == frozenset({3, 6, 10})


def test_python_decorated_construct_breaks_above_the_decorator() -> None:
    src = "\n".join(
        [
            "import functools",  # 1
            "",  # 2
            "@functools.cache",  # 3
            "@staticmethod",  # 4
            "def decorated():",  # 5
            "    return 0",  # 6
        ]
    )
    # The break lands on the first decorator (3), never between a decorator and
    # its def, so cutting there keeps the whole decorated construct together.
    assert preferred_break_lines("m.py", src) == frozenset({3})


def test_python_syntax_error_falls_back_to_heuristic() -> None:
    # Unbalanced paren -> ast fails; the column-0 heuristic still finds the def.
    src = "def broken(:\n    pass\n"
    assert preferred_break_lines("m.py", src) == frozenset({1})


def test_python_no_constructs_falls_back_to_heuristic() -> None:
    # Valid Python with no top-level def/class: ast yields no boundaries, so the
    # column-0 heuristic takes over and treats each statement line as a break.
    src = "print('hello')\nx = 1\n"
    assert preferred_break_lines("m.py", src) == frozenset({1, 2})


# ---------------------------------------------------------------------------
# Heuristic (non-Python) detection
# ---------------------------------------------------------------------------


def test_typescript_column_zero_declarations_are_breaks() -> None:
    src = "\n".join(
        [
            "import { x } from './x';",  # 1
            "",  # 2
            "export function alpha() {",  # 3
            "  return 1;",  # 4
            "}",  # 5 (closing brace, skipped)
            "",  # 6
            "const beta = () => {",  # 7
            "  return 2;",  # 8
            "};",  # 9 (starts with '}' continuation, skipped)
            "",  # 10
            "class Gamma {",  # 11
            "  method() {",  # 12 (indented, skipped)
            "    return 3;",  # 13
            "  }",  # 14
            "}",  # 15
        ]
    )
    assert preferred_break_lines("m.ts", src) == frozenset({1, 3, 7, 11})


def test_heuristic_skips_closing_and_comment_lines() -> None:
    src = "\n".join(
        [
            "function a() {",  # 1
            "  body();",  # 2
            "}",  # 3 skipped: closing brace
            "// a comment",  # 4 skipped: line comment
            "/* block open",  # 5 skipped: block-comment open
            "*/",  # 6 skipped: block-comment close
            "function b() {}",  # 7
        ]
    )
    assert preferred_break_lines("m.js", src) == frozenset({1, 7})


def test_unknown_extension_uses_heuristic() -> None:
    src = "rule one\n    indented\nrule two\n"
    assert preferred_break_lines("config.unknown", src) == frozenset({1, 3})


def test_empty_path_uses_heuristic() -> None:
    # An empty path has no extension, so detection falls to the heuristic.
    assert preferred_break_lines("", "def f(): pass") == frozenset({1})


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


def test_minified_single_line_has_no_interior_breaks() -> None:
    src = "function a(){return 1};function b(){return 2};function c(){return 3}"
    # One physical line -> only line 1, which the splitter never breaks before,
    # so this degrades to line-boundary splitting.
    assert preferred_break_lines("min.js", src) == frozenset({1})


def test_empty_and_whitespace_content_return_empty() -> None:
    assert preferred_break_lines("m.py", "") == frozenset()
    assert preferred_break_lines("m.ts", "   \n\n\t\n") == frozenset()


def test_pure_function_no_io_does_not_mutate_inputs() -> None:
    src = "def f():\n    return 1\n"
    before = src
    preferred_break_lines("m.py", src)
    assert src == before
