"""Tests for the shared incremental repo-context cache (``shared.repo_context_cache``).

Pins the byte-identical-to-fresh-walk invariant and the incremental re-read
behaviour (unchanged files are not re-read; changed/new files are; removed files
are evicted), mirroring the coding-team ``_RepoContextCache`` guarantees but for
the budgeted briefing the v2 development agents consume.

Preconditions:
    - The cache is constructed with the same extensions/exclude_dirs/max_chars/empty
      the matching ``read_repo_code_budgeted`` call uses.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from shared_repo_context.repo_utils import read_repo_code_budgeted
from software_engineering_team.shared.repo_context_cache import RepoContextCache


def _kwargs() -> dict:
    return {"extensions": {".py"}, "exclude_dirs": {"node_modules", ".git"}, "max_chars": 100_000}


def test_cache_output_matches_fresh_walk(tmp_path: Path) -> None:
    """For the same on-disk state, cache.read() == read_repo_code_budgeted()."""
    (tmp_path / "a.py").write_text("A = 1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("B = 2")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("X = 9")  # excluded

    cache = RepoContextCache(**_kwargs())
    cached = cache.read(tmp_path)
    fresh = read_repo_code_budgeted(tmp_path, **_kwargs())
    assert cached == fresh
    assert "a.py" in cached and "b.py" in cached and "x.py" not in cached


def test_unchanged_files_not_re_read_on_second_read(tmp_path: Path) -> None:
    """A second read with no on-disk change reuses cached parts (no read_text calls)."""
    (tmp_path / "a.py").write_text("A = 1")
    (tmp_path / "b.py").write_text("B = 2")

    cache = RepoContextCache(**_kwargs())
    cache.read(tmp_path)  # warm the cache (reads both files)

    reads: list[Path] = []
    real_read_text = Path.read_text

    def _spy(self: Path, *a, **k):
        reads.append(self)
        return real_read_text(self, *a, **k)

    with patch.object(Path, "read_text", _spy):
        out = cache.read(tmp_path)
    assert "a.py" in out and "b.py" in out
    assert reads == []  # both files served from cache; no re-read


def test_changed_file_is_re_read(tmp_path: Path) -> None:
    """A file whose mtime+size changed is re-rendered (the cache key is (mtime_ns, size))."""
    (tmp_path / "a.py").write_text("A = 1")
    (tmp_path / "b.py").write_text("B = 2")

    cache = RepoContextCache(**_kwargs())
    first = cache.read(tmp_path)
    assert "A = 1" in first

    # Change a.py's contents AND advance mtime so the (mtime_ns, size) key differs.
    p = tmp_path / "a.py"
    p.write_text("A = 99")
    # Bump mtime forward to guarantee a new st_mtime_ns even on coarse filesystems.
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    reads: list[Path] = []
    real_read_text = Path.read_text

    def _spy(self: Path, *a, **k):
        reads.append(self)
        return real_read_text(self, *a, **k)

    with patch.object(Path, "read_text", _spy):
        out = cache.read(tmp_path)
    assert "A = 99" in out
    # Only the changed file was re-read; b.py was served from cache.
    re_read_names = {p.name for p in reads}
    assert re_read_names == {"a.py"}


def test_removed_file_is_evicted(tmp_path: Path) -> None:
    """A file removed from disk is dropped from the briefing (no stale content)."""
    (tmp_path / "a.py").write_text("A = 1")
    (tmp_path / "b.py").write_text("B = 2")

    cache = RepoContextCache(**_kwargs())
    cache.read(tmp_path)
    (tmp_path / "b.py").unlink()

    out = cache.read(tmp_path)
    assert "b.py" not in out and "a.py" in out
    # And it still matches a fresh walk of the new state.
    assert out == read_repo_code_budgeted(tmp_path, **_kwargs())


def test_char_budget_matches_fresh_walk(tmp_path: Path) -> None:
    """The cache applies the same whole-file char budget as the fresh walk."""
    (tmp_path / "a.py").write_text("A" * 50)
    (tmp_path / "b.py").write_text("B" * 50)
    (tmp_path / "c.py").write_text("C" * 50)
    kwargs = {"extensions": {".py"}, "exclude_dirs": set(), "max_chars": 130}

    cache = RepoContextCache(**kwargs)
    cached = cache.read(tmp_path)
    fresh = read_repo_code_budgeted(tmp_path, **kwargs)
    assert cached == fresh
    # The budget stops before the third file (whole-files only): two 63-char
    # chunks fit (126 <= 130), the third (126 + 63 = 189 > 130) does not.
    assert "a.py" in cached and "b.py" in cached and "c.py" not in cached


def test_empty_repo_returns_empty_sentinel(tmp_path: Path) -> None:
    """An empty repo yields the empty sentinel, matching the fresh walk."""
    cache = RepoContextCache(**_kwargs())
    assert cache.read(tmp_path) == "# No code files found"
    assert cache.read(tmp_path) == read_repo_code_budgeted(tmp_path, **_kwargs())


def test_stat_failure_skips_file_without_raising(tmp_path: Path) -> None:
    """A file that vanishes *between enumeration and the read-loop stat* is skipped
    (the read-loop ``f.stat()`` raises OSError → except → continue), not raised.

    Note: patching ``Path.stat`` alone would make ``is_file()`` (which calls stat)
    raise during enumeration, exercising the walk's mid-walk abort instead. To hit
    the read-loop branch specifically, ``is_file`` is bypassed for the vanishing
    file so enumeration includes it, then the read-loop stat is the call that fails.
    """
    (tmp_path / "a.py").write_text("A = 1")
    (tmp_path / "b.py").write_text("B = 2")
    cache = RepoContextCache(**_kwargs())

    real_stat = Path.stat
    real_is_file = Path.is_file

    def _stat(self: Path, *a, **k):
        if self.name == "b.py":
            raise OSError("vanished after enumeration, before read-loop stat")
        return real_stat(self, *a, **k)

    def _is_file(self: Path, *a, **k):
        if self.name == "b.py":
            return True  # enumerated without stat; the vanish happens after
        return real_is_file(self, *a, **k)

    with patch.object(Path, "stat", _stat), patch.object(Path, "is_file", _is_file):
        out = cache.read(tmp_path)
    assert "a.py" in out and "b.py" not in out


def test_unreadable_file_is_skipped(tmp_path: Path) -> None:
    """A file read_text fails on is skipped (rendered None), not raised — matching the
    fresh walk's skip-on-error; the unreadable file is not cached."""
    (tmp_path / "a.py").write_text("A = 1")
    (tmp_path / "b.py").write_text("B = 2")
    cache = RepoContextCache(**_kwargs())
    real_read_text = Path.read_text

    def _read(self: Path, *a, **k):
        if self.name == "b.py":
            raise OSError("permission denied")
        return real_read_text(self, *a, **k)

    with patch.object(Path, "read_text", _read):
        out = cache.read(tmp_path)
    assert "a.py" in out and "b.py" not in out


def test_mid_walk_oserror_degrades_to_entries_so_far(tmp_path: Path, monkeypatch) -> None:
    """A mid-walk OSError degrades to a briefing built from entries found so far,
    never escaping to the caller — matching the fresh walk's best-effort contract."""
    (tmp_path / "a.py").write_text("A = 1")
    import software_engineering_team.shared.repo_context_cache as rcc_mod

    def _boom(_top: Path):
        yield (str(tmp_path), [], ["a.py"])
        raise OSError("directory vanished mid-walk")

    monkeypatch.setattr(rcc_mod.os, "walk", _boom)
    cache = RepoContextCache(**_kwargs())
    out = cache.read(tmp_path)
    assert "a.py" in out and "A = 1" in out
