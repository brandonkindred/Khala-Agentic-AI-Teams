"""read_repo_code_budgeted / find_repo_files are best-effort: mid-walk errors degrade, never raise."""

from __future__ import annotations

import os
from pathlib import Path

import shared.repo_context.repo_utils as repo_utils_mod
from shared.repo_context import find_repo_files, read_repo_code_budgeted


def test_unreadable_file_is_skipped_not_raised(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "good.py").write_text("print('ok')")
    (tmp_path / "bad.py").write_text("print('secret')")

    real_read_text = Path.read_text

    def _read(self: Path, *a, **k):
        if self.name == "bad.py":
            raise PermissionError("denied")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _read)
    out = read_repo_code_budgeted(
        tmp_path, extensions={".py"}, exclude_dirs={".git"}, max_chars=10_000
    )
    assert "good.py" in out
    assert "secret" not in out


def test_mid_walk_error_degrades_to_partial_briefing(tmp_path: Path, monkeypatch) -> None:
    """A filesystem race mid-scan (e.g. a dir deleted by a parallel build) must not
    escape the scanner and fail the calling workflow; it degrades to a briefing
    built from the entries enumerated *before* the error — real partial content,
    not the empty sentinel."""
    (tmp_path / "a.py").write_text("A = 1")

    def _boom(_top: Path):
        # Yield the root frame (carrying a.py) before the walk "vanishes".
        yield (str(tmp_path), [], ["a.py"])
        raise FileNotFoundError("directory vanished mid-walk")

    monkeypatch.setattr(repo_utils_mod.os, "walk", _boom)
    out = read_repo_code_budgeted(
        tmp_path, extensions={".py"}, exclude_dirs={".git"}, max_chars=10_000
    )
    # The entry yielded before the walk error is still read: partial, not empty.
    assert "a.py" in out
    assert "A = 1" in out


def test_walk_error_before_any_entry_returns_empty(tmp_path: Path, monkeypatch) -> None:
    """When the walk raises before yielding anything, there is nothing to salvage,
    so the briefing degrades to the empty sentinel — still no exception escapes."""

    def _boom(_top: Path):
        raise FileNotFoundError("directory vanished before scan")
        yield  # pragma: no cover - unreachable, makes _boom a generator

    monkeypatch.setattr(repo_utils_mod.os, "walk", _boom)
    out = read_repo_code_budgeted(
        tmp_path, extensions={".py"}, exclude_dirs={".git"}, max_chars=10_000
    )
    assert out == "# No code files found"


def test_streaming_walk_prunes_excluded_dirs_in_place(tmp_path: Path, monkeypatch) -> None:
    """The streamed ``os.walk`` prunes excluded directories in place, so a huge
    ``node_modules``-style subtree is never descended into (the perf win over the
    old materialized ``rglob("*")``), while real source files elsewhere are read."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.py").write_text("K = 1")
    deep = tmp_path / "node_modules" / "deep"
    deep.mkdir(parents=True)
    (deep / "x.py").write_text("X = 1")

    visited: list[str] = []
    real_walk = os.walk

    def _walk(top, *a, **k):
        for dirpath, dirnames, filenames in real_walk(top, *a, **k):
            visited.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(repo_utils_mod.os, "walk", _walk)
    out = read_repo_code_budgeted(
        tmp_path, extensions={".py"}, exclude_dirs={"node_modules"}, max_chars=100_000
    )
    assert "keep.py" in out and "x.py" not in out
    # os.walk never yielded a frame under node_modules — it was pruned, not just
    # filtered after the fact.
    assert not any(os.path.sep + "node_modules" in p or p.endswith("node_modules") for p in visited)


def test_streaming_walk_matches_sorted_output(tmp_path: Path) -> None:
    """Regression guard: the streamed walk emits the same sorted, budgeted chunks
    the materialized-walk version did for a normal repo (same files, order, cut)."""
    (tmp_path / "b.py").write_text("B = 2")
    (tmp_path / "a.py").write_text("A = 1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("C = 3")
    out = read_repo_code_budgeted(
        tmp_path, extensions={".py"}, exclude_dirs={"node_modules"}, max_chars=100_000
    )
    # Sorted by path: a.py, b.py, sub/c.py.
    assert out == "--- a.py ---\nA = 1\n\n--- b.py ---\nB = 2\n\n--- sub/c.py ---\nC = 3\n"


def test_find_repo_files_matches_suffix_and_name_with_pruning(tmp_path: Path, monkeypatch) -> None:
    """find_repo_files returns regular files by suffix or exact basename, and
    prunes excluded dirs in place so a node_modules/.git subtree is never
    descended into (the I/O win over rglob that motivated the helper)."""
    (tmp_path / "keep.py").write_text("K = 1")
    (tmp_path / "pom.xml").write_text("<project/>")
    deep = tmp_path / "node_modules" / "deep"
    deep.mkdir(parents=True)
    (deep / "ignored.py").write_text("X = 1")
    (deep / "pom.xml").write_text("<project/>")

    visited: list[str] = []
    real_walk = os.walk

    def _walk(top, *a, **k):
        for dirpath, dirnames, filenames in real_walk(top, *a, **k):
            visited.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(repo_utils_mod.os, "walk", _walk)
    matched = {f.name for f in find_repo_files(tmp_path, suffixes={".py"}, names={"pom.xml"})}
    assert matched == {"keep.py", "pom.xml"}
    # os.walk never yielded a frame under node_modules — pruned, not filtered.
    assert not any("node_modules" in p for p in visited)


def test_find_repo_files_non_directory_returns_empty(tmp_path: Path) -> None:
    """A non-directory repo_path yields [] rather than raising, so the
    best-effort _detect_language callers can call this unconditionally."""
    missing = tmp_path / "does-not-exist"
    assert find_repo_files(missing, suffixes={".py"}) == []


def test_find_repo_files_mid_walk_error_degrades_to_partial(tmp_path: Path, monkeypatch) -> None:
    """A filesystem race mid-scan must not escape; the helper degrades to the
    entries enumerated before the error, mirroring read_repo_code_budgeted."""
    (tmp_path / "a.py").write_text("A = 1")

    def _boom(_top: Path):
        yield (str(tmp_path), [], ["a.py"])
        raise FileNotFoundError("directory vanished mid-walk")

    monkeypatch.setattr(repo_utils_mod.os, "walk", _boom)
    matched = {f.name for f in find_repo_files(tmp_path, suffixes={".py"})}
    assert matched == {"a.py"}


def test_find_repo_files_requires_is_file(tmp_path: Path) -> None:
    """A directory whose name happens to end in a requested suffix is not
    returned: the is_file() guard keeps directory entries out of the result."""
    (tmp_path / "src.py").mkdir()  # a directory named src.py
    (tmp_path / "real.py").write_text("R = 1")
    matched = {f.name for f in find_repo_files(tmp_path, suffixes={".py"}, exclude_dirs=set())}
    assert matched == {"real.py"}


def test_find_repo_files_accepts_frozenset_exclude_dirs(tmp_path: Path) -> None:
    """Callers may pass an immutable exclude_dirs iterable; the helper copies it
    and must not mutate the original (os.walk pruning uses dirnames, not the
    argument)."""
    (tmp_path / "keep.py").write_text("K = 1")
    deep = tmp_path / "node_modules" / "deep"
    deep.mkdir(parents=True)
    (deep / "ignored.py").write_text("X = 1")
    exclude_dirs = frozenset({"node_modules"})
    before = id(exclude_dirs)
    matched = {f.name for f in find_repo_files(tmp_path, suffixes={".py"}, exclude_dirs=exclude_dirs)}
    assert matched == {"keep.py"}
    assert id(exclude_dirs) == before
    assert exclude_dirs == frozenset({"node_modules"})
