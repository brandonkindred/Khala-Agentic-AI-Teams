"""Tests for ``build_change_surface_from_pairs``."""

from __future__ import annotations

from software_engineering_team.code_review_agent.change_surface import (
    build_change_surface_from_pairs,
    build_change_surface_from_patches,
    unified_diffs_from_pairs,
)

_OLD = "def outer():\n    return 0\n"
_NEW = "def outer():\n    return 1\n"


def test_empty_new_contents() -> None:
    assert build_change_surface_from_pairs({}).is_empty
    assert build_change_surface_from_pairs({}, old_contents={"a.py": "x"}).is_empty


def test_identical_old_new_yields_empty_surface() -> None:
    text = "def f():\n    return 1\n"
    surface = build_change_surface_from_pairs(
        {"mod.py": text},
        old_contents={"mod.py": text},
    )
    assert surface.is_empty


def test_new_file_when_old_contents_none() -> None:
    surface = build_change_surface_from_pairs({"a.py": _NEW}, old_contents=None)
    assert not surface.is_empty
    assert "a.py" in surface.blocks


def test_new_file_when_key_missing_from_old_map() -> None:
    surface = build_change_surface_from_pairs(
        {"b.py": _NEW},
        old_contents={"other.py": "x\n"},
    )
    assert not surface.is_empty
    assert "b.py" in surface.blocks


def test_modified_file_golden_parity_with_patch_path() -> None:
    new_contents = {"mod.py": _NEW}
    old_contents = {"mod.py": _OLD}
    patches = unified_diffs_from_pairs(new_contents, old_contents)
    via_pairs = build_change_surface_from_pairs(new_contents, old_contents)
    via_patches = build_change_surface_from_patches(
        patches,
        new_contents=new_contents,
    )
    assert not via_pairs.is_empty
    assert via_pairs == via_patches
