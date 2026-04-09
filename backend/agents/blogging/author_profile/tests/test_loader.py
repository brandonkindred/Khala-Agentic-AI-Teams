"""Tests for the author profile loader."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from author_profile import EXAMPLE_PROFILE_PATH, AuthorProfile, load_author_profile
from author_profile import loader as loader_mod

_SAMPLE = """\
identity:
  full_name: Test User
  short_name: Test
professional:
  current_title: Tester
"""


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("AUTHOR_PROFILE_PATH", raising=False)
    monkeypatch.delenv("AUTHOR_PROFILE_STRICT", raising=False)
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    loader_mod.clear_cache()
    yield
    loader_mod.clear_cache()


def test_explicit_path_wins(tmp_path: Path):
    f = tmp_path / "p.yaml"
    f.write_text(_SAMPLE, encoding="utf-8")
    profile = load_author_profile(f)
    assert isinstance(profile, AuthorProfile)
    assert profile.identity.full_name == "Test User"
    assert profile.author_name == "Test"


def test_env_var_path(tmp_path: Path, monkeypatch):
    f = tmp_path / "p.yaml"
    f.write_text(_SAMPLE, encoding="utf-8")
    monkeypatch.setenv("AUTHOR_PROFILE_PATH", str(f))
    assert load_author_profile().identity.full_name == "Test User"


def test_agent_cache_fallback(tmp_path: Path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "author_profile.yaml").write_text(_SAMPLE, encoding="utf-8")
    monkeypatch.setenv("AGENT_CACHE", str(cache))
    assert load_author_profile().identity.full_name == "Test User"


def test_example_fallback_when_unconfigured(caplog):
    profile = load_author_profile()
    assert profile.identity.full_name == "Example Author"
    assert any("bundled example" in r.message for r in caplog.records)


def test_strict_mode_raises_when_unconfigured(monkeypatch):
    monkeypatch.setenv("AUTHOR_PROFILE_STRICT", "true")
    with pytest.raises(FileNotFoundError):
        load_author_profile()


def test_strict_mode_raises_when_env_path_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTHOR_PROFILE_PATH", str(tmp_path / "nope.yaml"))
    monkeypatch.setenv("AUTHOR_PROFILE_STRICT", "1")
    with pytest.raises(FileNotFoundError):
        load_author_profile()


def test_cache_invalidates_on_mtime_change(tmp_path: Path):
    f = tmp_path / "p.yaml"
    f.write_text(_SAMPLE, encoding="utf-8")
    p1 = load_author_profile(f)
    assert p1.identity.full_name == "Test User"

    # Rewrite with new content + bump mtime.
    f.write_text(_SAMPLE.replace("Test User", "Updated User"), encoding="utf-8")
    new_mtime = f.stat().st_mtime_ns + 1_000_000
    os.utime(f, ns=(new_mtime, new_mtime))

    p2 = load_author_profile(f)
    assert p2.identity.full_name == "Updated User"


def test_example_file_validates():
    profile = AuthorProfile.from_yaml_file(EXAMPLE_PROFILE_PATH)
    assert profile.identity.full_name == "Example Author"
