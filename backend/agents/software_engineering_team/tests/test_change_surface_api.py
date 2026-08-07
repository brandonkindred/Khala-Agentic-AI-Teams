"""API-surface contracts for ``code_review_agent.change_surface`` (#5388).

These tests lock types, empty/no-op postconditions, and stub behavior so later
leaves (#5389–#5392) can implement against a stable importable contract.
"""

from __future__ import annotations

from collections import OrderedDict

import pytest

from software_engineering_team.code_review_agent.change_surface import (
    ChangeSurface,
    LineRange,
    build_change_surface_from_pairs,
    build_change_surface_from_patches,
    expand_touched_ranges,
    format_change_surface_code,
)


def test_module_imports_without_llm_stack() -> None:
    """``change_surface`` must stay pure (no strands / llm_service side effects)."""
    import software_engineering_team.code_review_agent.change_surface as mod

    assert hasattr(mod, "ChangeSurface")
    assert hasattr(mod, "build_change_surface_from_patches")


def test_line_range_accepts_valid_inclusive_range() -> None:
    r = LineRange(start_line=3, end_line=10)
    assert r.start_line == 3
    assert r.end_line == 10


def test_line_range_rejects_zero_or_inverted() -> None:
    with pytest.raises(ValueError):
        LineRange(start_line=0, end_line=1)
    with pytest.raises(ValueError):
        LineRange(start_line=5, end_line=4)


def test_line_range_rejects_non_int_or_bool() -> None:
    with pytest.raises(ValueError):
        LineRange(start_line=True, end_line=2)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        LineRange(start_line=1, end_line=False)  # type: ignore[arg-type]


def test_format_change_surface_code_empty() -> None:
    assert format_change_surface_code({}) == ""


def test_format_change_surface_code_joins_headers_like_pr_builder() -> None:
    blocks = OrderedDict(
        [
            ("app/a.py", "1: x = 1"),
            ("app/b.py", "2: y = 2\n3: z = 3"),
        ]
    )
    assert format_change_surface_code(blocks) == (
        "### app/a.py ###\n1: x = 1\n\n### app/b.py ###\n2: y = 2\n3: z = 3"
    )


def test_change_surface_empty_helpers() -> None:
    surface = ChangeSurface(blocks={})
    assert surface.is_empty
    assert surface.files_reviewed == 0
    assert surface.code == ""


def test_change_surface_derives_code_and_counts() -> None:
    surface = ChangeSurface(blocks=OrderedDict([("f.py", "10: pass")]))
    assert not surface.is_empty
    assert surface.files_reviewed == 1
    assert surface.code == "### f.py ###\n10: pass"


def test_build_from_patches_empty_mapping() -> None:
    surface = build_change_surface_from_patches({})
    assert surface.is_empty
    assert surface.blocks == {}
    assert surface.code == ""


def test_build_from_patches_all_blank_patches() -> None:
    surface = build_change_surface_from_patches({"a.py": "", "b.py": "   \n"})
    assert surface.is_empty


def test_build_from_patches_nonempty_assembles_when_content_provided() -> None:
    content = "def outer():\n    return 1\n"
    patch = "@@ -1,2 +1,2 @@\n def outer():\n-    return 0\n+    return 1\n"
    surface = build_change_surface_from_patches(
        {"a.py": patch},
        new_contents={"a.py": content},
    )
    assert not surface.is_empty
    assert "### a.py ###" in surface.code


def test_build_from_pairs_empty_new_contents() -> None:
    surface = build_change_surface_from_pairs({})
    assert surface.is_empty


def test_build_from_pairs_empty_new_with_old_still_empty() -> None:
    surface = build_change_surface_from_pairs({}, old_contents={"a.py": "old"})
    assert surface.is_empty


def test_build_from_pairs_nonempty_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        build_change_surface_from_pairs({"a.py": "new"})


def test_build_from_pairs_nonempty_with_old_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        build_change_surface_from_pairs({"a.py": "new"}, old_contents={"a.py": "old"})


def test_expand_touched_ranges_empty() -> None:
    assert expand_touched_ranges("def f():\n    pass\n", []) == ()
    assert expand_touched_ranges("def f():\n    pass\n", set()) == ()


def test_expand_touched_ranges_non_python_uses_fallback() -> None:
    ranges = expand_touched_ranges("function f() {\n  return 1;\n}\n", {2}, path="f.ts")
    assert ranges == (LineRange(1, 3),)
