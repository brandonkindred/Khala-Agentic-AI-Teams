"""Unit tests for shared_app.bootstrap_syspath."""

from __future__ import annotations

import sys

import pytest

from shared_app import bootstrap_syspath


@pytest.fixture
def clean_syspath(monkeypatch):
    """Give each test an isolated copy of ``sys.path`` to mutate."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    return sys.path


def test_inserts_existing_dirs_front_in_reverse_order(clean_syspath, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    bootstrap_syspath(a, b)

    # Each is inserted at index 0, so the last arg ends up first (matches the
    # hand-rolled two-insert idiom this replaces).
    assert clean_syspath[0] == str(b)
    assert clean_syspath[1] == str(a)


def test_idempotent_no_duplicates(clean_syspath, tmp_path):
    a = tmp_path / "a"
    a.mkdir()

    bootstrap_syspath(a)
    bootstrap_syspath(a)

    assert clean_syspath.count(str(a)) == 1


def test_already_present_dir_is_left_in_place(clean_syspath, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    clean_syspath.append(str(a))  # present, but not at the front

    bootstrap_syspath(a)

    assert clean_syspath.count(str(a)) == 1
    assert clean_syspath[0] != str(a)  # not moved to the front


def test_must_exist_skips_missing_dirs(clean_syspath, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    missing = tmp_path / "missing"

    bootstrap_syspath(missing, real, must_exist=True)

    assert str(real) in clean_syspath
    assert str(missing) not in clean_syspath


def test_without_must_exist_inserts_regardless(clean_syspath, tmp_path):
    missing = tmp_path / "missing"

    bootstrap_syspath(missing)

    assert clean_syspath[0] == str(missing)


def test_accepts_str_and_path(clean_syspath, tmp_path):
    a = tmp_path / "a"
    a.mkdir()

    bootstrap_syspath(str(a))

    assert clean_syspath[0] == str(a)
