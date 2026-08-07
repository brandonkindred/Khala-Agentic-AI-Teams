"""Tests for on-disk previous-content resolution (``code_review_agent.previous_content``)."""

from __future__ import annotations

import os

import pytest
from code_review_agent.previous_content import (
    PreviousContentDiskResult,
    read_previous_content_from_disk,
)


def _write(root, rel: str, content: str) -> None:
    path = os.path.join(root, rel)
    parent = os.path.dirname(path)
    if parent and parent != str(root):
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_hit_returns_on_disk_text(tmp_path) -> None:
    _write(tmp_path, "a.py", "old = 1\n")
    result = read_previous_content_from_disk(str(tmp_path), ["a.py"])
    assert isinstance(result, PreviousContentDiskResult)
    assert result.contents == {"a.py": "old = 1\n"}
    assert "a.py" not in result.misses
    assert result.misses == frozenset()


def test_miss_for_absent_path(tmp_path) -> None:
    result = read_previous_content_from_disk(str(tmp_path), ["missing.py"])
    assert "missing.py" not in result.contents
    assert result.misses == frozenset({"missing.py"})


def test_fail_open_batch_one_hit_one_miss(tmp_path) -> None:
    _write(tmp_path, "present.py", "x = 1\n")
    result = read_previous_content_from_disk(
        str(tmp_path),
        ["present.py", "absent.py"],
    )
    assert result.contents == {"present.py": "x = 1\n"}
    assert result.misses == frozenset({"absent.py"})


def test_blank_repo_path_raises() -> None:
    with pytest.raises(ValueError):
        read_previous_content_from_disk("", ["a.py"])
    with pytest.raises(ValueError):
        read_previous_content_from_disk("   ", ["a.py"])


def test_empty_paths_returns_empty_result(tmp_path) -> None:
    result = read_previous_content_from_disk(str(tmp_path), [])
    assert result.contents == {}
    assert result.misses == frozenset()


def test_duplicate_path_strings_read_once(tmp_path) -> None:
    _write(tmp_path, "a.py", "once\n")
    result = read_previous_content_from_disk(str(tmp_path), ["a.py", "a.py"])
    assert result.contents == {"a.py": "once\n"}
    assert result.misses == frozenset()
    assert len(result.contents) + len(result.misses) == 1


def test_blank_path_string_is_miss(tmp_path) -> None:
    result = read_previous_content_from_disk(str(tmp_path), [""])
    assert result.contents == {}
    assert result.misses == frozenset({""})
