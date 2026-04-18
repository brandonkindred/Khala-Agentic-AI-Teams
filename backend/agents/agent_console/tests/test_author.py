"""Unit tests for ``resolve_author()``. Mocks the profile loader so the
test is hermetic (no YAML files on disk)."""

from __future__ import annotations

import sys
import types

import pytest

from agent_console.author import ANONYMOUS, resolve_author


@pytest.fixture(autouse=True)
def reset_cache():
    resolve_author.cache_clear()
    yield
    resolve_author.cache_clear()


def _install_fake_profile(short: str = "", full: str = "") -> None:
    """Inject a minimal blogging.author_profile stub into sys.modules."""
    identity = types.SimpleNamespace(short_name=short, full_name=full)
    profile = types.SimpleNamespace(identity=identity)

    fake_module = types.ModuleType("blogging.author_profile")
    fake_module.load_author_profile = lambda: profile
    sys.modules["blogging.author_profile"] = fake_module


def _remove_fake_profile() -> None:
    sys.modules.pop("blogging.author_profile", None)


def test_short_name_wins_when_present() -> None:
    _install_fake_profile(short="bkindred", full="Brandon Kindred")
    try:
        assert resolve_author() == "bkindred"
    finally:
        _remove_fake_profile()


def test_full_name_used_when_short_is_empty() -> None:
    _install_fake_profile(short="", full="Brandon Kindred")
    try:
        assert resolve_author() == "Brandon Kindred"
    finally:
        _remove_fake_profile()


def test_anonymous_when_both_empty() -> None:
    _install_fake_profile(short="", full="")
    try:
        assert resolve_author() == ANONYMOUS
    finally:
        _remove_fake_profile()


def test_anonymous_when_loader_raises(monkeypatch) -> None:
    def _boom():
        raise RuntimeError("profile gone")

    fake_module = types.ModuleType("blogging.author_profile")
    fake_module.load_author_profile = _boom
    sys.modules["blogging.author_profile"] = fake_module
    try:
        assert resolve_author() == ANONYMOUS
    finally:
        _remove_fake_profile()


def test_anonymous_when_loader_raises_attribute_error() -> None:
    _remove_fake_profile()
    # The real blogging package may be importable in this tree; inject a
    # stub whose loader raises to simulate a broken profile module.
    fake_module = types.ModuleType("blogging.author_profile")

    def _missing(*args, **kwargs):  # noqa: ANN401, ARG001
        raise AttributeError("load_author_profile not available")

    fake_module.load_author_profile = _missing  # type: ignore[assignment]
    sys.modules["blogging.author_profile"] = fake_module
    try:
        assert resolve_author() == ANONYMOUS
    finally:
        _remove_fake_profile()
