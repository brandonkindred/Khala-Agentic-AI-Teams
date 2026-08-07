"""Tests for structured enclosing-construct lookup (function_boundaries.py)."""

from __future__ import annotations

from code_review_agent.function_boundaries import (
    enclosing_construct,
    enclosing_construct_start_heuristic,
    iter_constructs,
    segment_containing_line,
    strip_numbered_prefixes,
)

# --------------------------------------------------------------------------- enclosing_construct


def test_top_level_function() -> None:
    """A body line inside a top-level ``def`` resolves to that function's name and span."""
    content = "def foo():\n    x = 1\n    return x\n"
    result = enclosing_construct(content, 2)
    assert result is not None
    assert result.name == "foo"
    assert result.kind == "function"
    assert result.start_line == 1
    assert result.end_line == 3


def test_nested_function_picks_innermost() -> None:
    """A line inside a nested function resolves to the innermost construct, not the outer."""
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
    """A method body line returns a ``Class.method`` qualified name with kind ``function``."""
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


def test_class_body_line_resolves_to_class() -> None:
    """A line inside a class body but outside any method resolves to the class."""
    content = "\n".join(
        [
            "class Widget:",  # 1
            "    CLASS_CONST = 1",  # 2
            "",
        ]
    )
    result = enclosing_construct(content, 2)
    assert result is not None
    assert result.name == "Widget"
    assert result.kind == "class"
    assert result.start_line == 1
    assert result.end_line == 2


def test_nested_class_body_resolves_to_inner_class() -> None:
    """A line inside a nested class resolves to the innermost class, not the outer."""
    content = "\n".join(
        [
            "class Outer:",  # 1
            "    class Inner:",  # 2
            "        x = 1",  # 3
            "    y = 2",  # 4
            "",
        ]
    )
    inner = enclosing_construct(content, 3)
    assert inner is not None
    assert inner.name == "Inner"
    assert inner.kind == "class"
    assert inner.start_line == 2
    assert inner.end_line == 3

    outer_attr = enclosing_construct(content, 4)
    assert outer_attr is not None
    assert outer_attr.name == "Outer"
    assert outer_attr.kind == "class"
    assert outer_attr.start_line == 1
    assert outer_attr.end_line == 4


def test_decorated_function_start_includes_decorator_line() -> None:
    """A finding on a decorated function's body is grouped under the function starting at the decorator line, not the ``def`` line."""
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
    """A line with no enclosing function or class returns ``None``."""
    content = "x = 1\ny = 2\n"
    assert enclosing_construct(content, 1) is None


def test_syntax_error_returns_none() -> None:
    """Unparseable Python content returns ``None`` rather than raising."""
    content = "def foo(:\n    pass\n"
    assert enclosing_construct(content, 1) is None


def test_line_outside_any_construct_returns_none() -> None:
    """A module-level line after a function (outside any construct span) returns ``None``."""
    content = "def foo():\n    return 1\n\nx = foo()\n"
    assert enclosing_construct(content, 4) is None


def test_enclosing_construct_maps_pre_numbered_lines() -> None:
    """Prefixed ``N: `` content strips to physical lines before AST lookup; spans are 1-based physical."""
    content = "4240: def foo():\n4241:     return 1\n"
    stripped, physical, _mapper = strip_numbered_prefixes(content, line_number=4241)
    result = enclosing_construct(stripped, physical)
    assert result is not None
    assert result.name == "foo"
    assert result.start_line == 1
    assert result.end_line == 2


# --------------------------------------------------------------------------- enclosing_construct_start_heuristic


def test_heuristic_finds_column_zero_declaration() -> None:
    """Non-Python heuristic returns the nearest preceding column-0 declaration line."""
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
    """Column-0 closers and comments are ignored so the heuristic picks the real declaration."""
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
    """Indented-only content with no column-0 declaration returns ``None``."""
    content = "  indented only\n  still indented\n"
    assert enclosing_construct_start_heuristic(content, 2) is None


# --------------------------------------------------------------------------- strip_numbered_prefixes


def test_strip_numbered_prefixes_plain_content_unchanged() -> None:
    """Plain (unprefixed) content is returned as-is with the original line and no mapper."""
    content = "def foo():\n    return 1\n"
    stripped, physical, mapper = strip_numbered_prefixes(content, line_number=2)
    assert stripped == content
    assert physical == 2
    assert mapper is None


def test_strip_numbered_prefixes_detects_and_strips() -> None:
    """Prefixed hunks strip ``N: ``, remap the absolute target to a physical line, and expose a mapper."""
    content = "4240: def foo():\n4241:     x = 1\n4242:     return x\n"
    stripped, physical, mapper = strip_numbered_prefixes(content, line_number=4242)
    assert stripped == "def foo():\n    x = 1\n    return x"
    assert physical == 3
    assert mapper is not None
    assert mapper(3) == 4242


def test_strip_numbered_prefixes_preserves_inter_hunk_separators() -> None:
    """Bare ``...`` gap markers are kept so annotated hunks resolve independently."""
    content = "\n".join(
        [
            "10: def first():",
            "11:     return 1",
            "...",
            "100:     changed()",
        ]
    )
    stripped, physical, mapper = strip_numbered_prefixes(content, line_number=100)
    assert "..." in stripped.splitlines()
    assert physical == 4
    assert mapper is not None
    assert mapper(4) == 100
    # Later hunk starts mid-function without its declaration — do not invent
    # an enclosing construct by joining across the gap.
    assert enclosing_construct(stripped, physical, annotated_hunks=True) is None
    first = enclosing_construct(stripped, 2, annotated_hunks=True)
    assert first is not None
    assert first.name == "first"


def test_enclosing_construct_does_not_join_across_annotated_hunk_gap() -> None:
    """Indented continuation after annotated ``...`` is not attached to the prior hunk."""
    content = "\n".join(
        [
            "def first():",
            "    return 1",
            "...",
            "    changed()",
        ]
    )
    first = enclosing_construct(content, 2, annotated_hunks=True)
    assert first is not None
    assert first.name == "first"
    assert enclosing_construct(content, 4, annotated_hunks=True) is None


def test_enclosing_construct_preserves_ellipsis_stub_bodies() -> None:
    """Ordinary full-file Ellipsis statements are not treated as hunk separators."""
    content = "def foo():\n    ...\n    return 1\n"
    result = enclosing_construct(content, 3)
    assert result is not None
    assert result.name == "foo"
    assert result.start_line == 1
    assert result.end_line == 3


def test_segment_containing_line_isolates_gap_bounded_hunk() -> None:
    """A target line resolves to only its own hunk segment, not the joined content."""
    content = "\n".join(
        [
            "def first():",  # 1
            "    return 1",  # 2
            "...",  # 3 (separator)
            "x = 1",  # 4
            "...",  # 5 (separator)
            "    changed()",  # 6
        ]
    )
    assert segment_containing_line(content, 2, annotated_hunks=True) == "def first():\n    return 1"
    assert segment_containing_line(content, 4, annotated_hunks=True) == "x = 1"
    assert segment_containing_line(content, 6, annotated_hunks=True) == "    changed()"


def test_segment_containing_line_returns_full_content_when_not_annotated() -> None:
    """Ordinary (non-hunk) content is returned unchanged regardless of ``...`` lines."""
    content = "def foo():\n    ...\n    return 1\n"
    assert segment_containing_line(content, 3) == content
    assert segment_containing_line(content, 3, annotated_hunks=True) == content


def test_strip_numbered_prefixes_empty_content() -> None:
    """Empty content strips to empty with the original line number and no mapper."""
    stripped, physical, mapper = strip_numbered_prefixes("", line_number=1)
    assert stripped == ""
    assert physical == 1
    assert mapper is None


# --------------------------------------------------------------------------- iter_constructs


def test_iter_constructs_qualifies_methods_and_lists_all() -> None:
    src = (
        "class C:\n"
        "    def m(self):\n"
        "        return 1\n"
        "\n"
        "def top():\n"
        "    return 2\n"
    )
    constructs = iter_constructs(src)
    names = {c.name for c in constructs}
    assert names == {"C", "C.m", "top"}
    method = next(c for c in constructs if c.name == "C.m")
    assert method.kind == "function"
    assert method.start_line == 2 and method.end_line == 3


def test_iter_constructs_parse_failure_returns_empty() -> None:
    assert iter_constructs("def broken(\n") == []


def test_iter_constructs_annotated_hunks_skips_unparseable_sibling() -> None:
    """With annotated_hunks, an indented continuation hunk does not hide other defs."""
    content = (
        "def alpha():\n"
        "    return 1\n"
        "...\n"
        "    changed()\n"
        "...\n"
        "def beta():\n"
        "    return 2\n"
    )
    # Whole-file parse fails; hunk-aware listing still finds alpha and beta.
    assert iter_constructs(content) == []
    names = {c.name for c in iter_constructs(content, annotated_hunks=True)}
    assert names == {"alpha", "beta"}
    beta = next(c for c in iter_constructs(content, annotated_hunks=True) if c.name == "beta")
    # beta starts after alpha (2 lines) + separator + continuation (1) + separator
    assert beta.start_line == 6 and beta.end_line == 7
