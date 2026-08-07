"""Tests for ``unified_diffs_from_pairs``."""

from __future__ import annotations

from collections import OrderedDict

from software_engineering_team.code_review_agent.change_surface import (
    extract_touched_lines,
    unified_diffs_from_pairs,
)


def test_empty_new_contents() -> None:
    assert unified_diffs_from_pairs({}) == {}
    assert unified_diffs_from_pairs({}, old_contents={"a.py": "x"}) == {}


def test_identical_old_new_yields_empty_string() -> None:
    text = "def f():\n    return 1\n"
    out = unified_diffs_from_pairs({"mod.py": text}, old_contents={"mod.py": text})
    assert list(out.keys()) == ["mod.py"]
    assert out["mod.py"] == ""


def test_new_file_when_old_contents_none() -> None:
    new = "hello\n"
    out = unified_diffs_from_pairs({"a.txt": new}, old_contents=None)
    patch = out["a.txt"]
    assert patch.startswith("--- a/a.txt\n+++ b/a.txt\n")
    assert "@@" in patch
    assert "+hello" in patch
    assert extract_touched_lines(patch)


def test_new_file_when_key_missing_from_old_map() -> None:
    new = "only\n"
    out = unified_diffs_from_pairs(
        {"b.txt": new},
        old_contents={"other.txt": "x\n"},
    )
    patch = out["b.txt"]
    assert "--- a/b.txt\n+++ b/b.txt\n" in patch
    assert extract_touched_lines(patch)


def test_modified_file_diff() -> None:
    old = "a\nb\n"
    new = "a\nc\n"
    out = unified_diffs_from_pairs(
        OrderedDict([("m.txt", new)]),
        old_contents={"m.txt": old},
    )
    patch = out["m.txt"]
    assert patch.startswith("--- a/m.txt\n+++ b/m.txt\n")
    assert "-b" in patch
    assert "+c" in patch
    assert extract_touched_lines(patch) == frozenset({2})


def test_preserves_new_contents_key_order() -> None:
    out = unified_diffs_from_pairs(
        OrderedDict([("z.py", "z\n"), ("a.py", "a\n")]),
        old_contents=None,
    )
    assert list(out.keys()) == ["z.py", "a.py"]
