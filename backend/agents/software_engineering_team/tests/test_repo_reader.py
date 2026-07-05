"""Tests for the disk-backed repository reader (``code_review_agent.repo_reader``).

The reader is read-only, path-confined, size-bounded, and fail-safe: an absent,
out-of-bounds, or oversized path reads as ``None`` (never raises), so the
false-positive verifier can only ever *keep* a finding when a read fails, never
drop a real one.
"""

from __future__ import annotations

import os

from code_review_agent.repo_reader import DiskRepoReader, RepoReader


def _write(root, rel: str, content: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_reads_file_under_root(tmp_path) -> None:
    _write(tmp_path, "pkg/models.py", "class Model:\n    pass\n")
    reader = DiskRepoReader(str(tmp_path))
    assert isinstance(reader, RepoReader)  # runtime-checkable Protocol
    assert reader.read_file("pkg/models.py") == "class Model:\n    pass\n"
    # A leading slash is tolerated (treated as repo-relative).
    assert reader.read_file("/pkg/models.py").startswith("class Model")


def test_missing_and_directory_read_none(tmp_path) -> None:
    _write(tmp_path, "pkg/models.py", "x = 1\n")
    reader = DiskRepoReader(str(tmp_path))
    assert reader.read_file("pkg/does_not_exist.py") is None
    assert reader.read_file("pkg") is None  # a directory is not a file
    assert reader.read_file("") is None
    assert reader.read_file("   ") is None


def test_path_traversal_is_refused(tmp_path) -> None:
    # A secret file outside the root must not be readable via traversal.
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("TOP SECRET")
    _write(tmp_path, "a.py", "x = 1\n")
    reader = DiskRepoReader(str(tmp_path))
    assert reader.read_file("../secret.txt") is None
    assert reader.read_file("../../etc/passwd") is None


def test_oversize_file_reads_none(tmp_path) -> None:
    _write(tmp_path, "big.py", "y = 2\n")
    reader = DiskRepoReader(str(tmp_path), max_file_bytes=3)
    assert reader.read_file("big.py") is None


def test_read_is_cached(tmp_path) -> None:
    target = tmp_path / "a.py"
    target.write_text("first\n")
    reader = DiskRepoReader(str(tmp_path))
    assert reader.read_file("a.py") == "first\n"
    # Mutating the file after the first read must not change the cached result.
    target.write_text("second\n")
    assert reader.read_file("a.py") == "first\n"


def test_list_files_walks_and_skips_vcs_dirs(tmp_path) -> None:
    _write(tmp_path, "pkg/a.py", "1")
    _write(tmp_path, "pkg/b.py", "2")
    _write(tmp_path, ".git/config", "ignored")
    _write(tmp_path, "node_modules/dep/index.js", "ignored")
    reader = DiskRepoReader(str(tmp_path))
    listed = reader.list_files()
    assert "pkg/a.py" in listed and "pkg/b.py" in listed
    assert not any(p.startswith(".git/") for p in listed)
    assert not any("node_modules" in p for p in listed)


def test_list_files_is_capped(tmp_path) -> None:
    for i in range(10):
        _write(tmp_path, f"f{i}.py", "x")
    reader = DiskRepoReader(str(tmp_path), max_listed_files=4)
    assert len(reader.list_files()) == 4


def test_list_files_cached(tmp_path) -> None:
    _write(tmp_path, "a.py", "1")
    reader = DiskRepoReader(str(tmp_path))
    first = reader.list_files()
    _write(tmp_path, "b.py", "2")
    # Listing is memoized: a file added after the first call is not re-walked.
    assert reader.list_files() == first
