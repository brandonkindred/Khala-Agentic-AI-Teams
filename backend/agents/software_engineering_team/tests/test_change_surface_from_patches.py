"""Assembly tests for ``build_change_surface_from_patches``."""

from __future__ import annotations

from collections import OrderedDict

from software_engineering_team.code_review_agent.change_surface import (
    LineRange,
    _merge_line_ranges,
    _pre_number_ranges,
    build_change_surface_from_patches,
)

_PY_CONTENT = "def outer():\n    return 1\n\nx = 1\n"
# Touch the body line of ``outer`` (new-file line 2).
_PY_PATCH = "@@ -1,2 +1,2 @@\n def outer():\n-    return 0\n+    return 1\n"


def test_merge_line_ranges_overlaps_and_adjacent() -> None:
    ranges = (
        LineRange(6, 7),
        LineRange(1, 2),
        LineRange(3, 4),  # adjacent to 1-2 → merge to 1-4
        LineRange(6, 9),  # overlaps 6-7 → 6-9
    )
    assert _merge_line_ranges(ranges) == (LineRange(1, 4), LineRange(6, 9))


def test_merge_line_ranges_empty() -> None:
    assert _merge_line_ranges(()) == ()
    assert _merge_line_ranges([]) == ()


def test_pre_number_ranges_single_span() -> None:
    content = "a\nb\nc\n"
    body = _pre_number_ranges(content, (LineRange(2, 3),))
    assert body == "2: b\n3: c"


def test_pre_number_ranges_inserts_gap_marker() -> None:
    content = "a\nb\nc\nd\ne\n"
    body = _pre_number_ranges(content, (LineRange(1, 1), LineRange(4, 5)))
    assert body == "1: a\n...\n4: d\n5: e"


def test_build_from_patches_single_file_expands_construct() -> None:
    surface = build_change_surface_from_patches(
        {"mod.py": _PY_PATCH},
        new_contents={"mod.py": _PY_CONTENT},
    )
    assert not surface.is_empty
    assert list(surface.blocks.keys()) == ["mod.py"]
    # AST expansion of line 2 → enclosing ``outer`` (lines 1-2).
    assert surface.blocks["mod.py"] == "1: def outer():\n2:     return 1"
    assert surface.code == "### mod.py ###\n1: def outer():\n2:     return 1"


def test_build_from_patches_multi_file() -> None:
    ts_content = "function f() {\n  return 1;\n}\n"
    ts_patch = "@@ -1,3 +1,3 @@\n function f() {\n-  return 0;\n+  return 1;\n }\n"
    surface = build_change_surface_from_patches(
        OrderedDict(
            [
                ("mod.py", _PY_PATCH),
                ("f.ts", ts_patch),
            ]
        ),
        new_contents={"mod.py": _PY_CONTENT, "f.ts": ts_content},
    )
    assert list(surface.blocks.keys()) == ["mod.py", "f.ts"]
    assert "### mod.py ###" in surface.code
    assert "### f.ts ###" in surface.code


def test_build_from_patches_omits_without_new_contents() -> None:
    surface = build_change_surface_from_patches(
        {"mod.py": _PY_PATCH},
        new_contents=None,
    )
    assert surface.is_empty


def test_build_from_patches_omits_path_missing_content_keeps_other() -> None:
    surface = build_change_surface_from_patches(
        OrderedDict([("skip.py", _PY_PATCH), ("mod.py", _PY_PATCH)]),
        new_contents={"mod.py": _PY_CONTENT},
    )
    assert list(surface.blocks.keys()) == ["mod.py"]


def test_build_from_patches_omits_when_no_added_lines() -> None:
    # Context-only hunk: no '+' lines → empty touched set → omit.
    patch = "@@ -1,2 +1,2 @@\n def outer():\n     return 1\n"
    surface = build_change_surface_from_patches(
        {"mod.py": patch},
        new_contents={"mod.py": _PY_CONTENT},
    )
    assert surface.is_empty


def test_build_from_patches_two_hunks_same_function_emits_one_span() -> None:
    """Two hunks in one function must not duplicate the expanded construct."""
    content = "def f():\n    a = 1\n    b = 2\n    return a + b\n"
    # Hunk 1 touches new-file line 2; hunk 2 touches new-file line 4.
    patch = (
        "@@ -1,3 +1,3 @@\n"
        " def f():\n"
        "-    a = 0\n"
        "+    a = 1\n"
        "     b = 2\n"
        "@@ -3,2 +3,2 @@\n"
        "     b = 2\n"
        "-    return a\n"
        "+    return a + b\n"
    )
    surface = build_change_surface_from_patches(
        {"f.py": patch},
        new_contents={"f.py": content},
    )
    assert not surface.is_empty
    body = surface.blocks["f.py"]
    assert body.count("def f():") == 1
    assert "..." not in body
    assert body == (
        "1: def f():\n"
        "2:     a = 1\n"
        "3:     b = 2\n"
        "4:     return a + b"
    )
    assert surface.code == f"### f.py ###\n{body}"
