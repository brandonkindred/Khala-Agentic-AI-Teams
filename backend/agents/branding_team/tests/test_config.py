"""Unit tests for the shared env-parsing helpers."""

from __future__ import annotations

import pytest

from branding_team.config import env_bool, env_float, env_int


def test_env_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X", raising=False)
    assert env_int("X", 4) == 4
    monkeypatch.setenv("X", "7")
    assert env_int("X", 4) == 7
    monkeypatch.setenv("X", "garbage")
    assert env_int("X", 4) == 4
    monkeypatch.setenv("X", "0")
    assert env_int("X", 4, minimum=1) == 1


def test_env_float(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("Y", raising=False)
    assert env_float("Y", 2.0) == 2.0
    monkeypatch.setenv("Y", "3.5")
    assert env_float("Y", 2.0) == 3.5
    monkeypatch.setenv("Y", "nope")
    assert env_float("Y", 2.0) == 2.0
    monkeypatch.setenv("Y", "-1")
    assert env_float("Y", 2.0, positive=True) == 2.0
    assert env_float("Y", 2.0) == -1.0


def test_env_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("Z", raising=False)
    assert env_bool("Z") is False
    assert env_bool("Z", default=True) is True
    for truthy in ("1", "true", "YES", "On"):
        monkeypatch.setenv("Z", truthy)
        assert env_bool("Z") is True
    monkeypatch.setenv("Z", "0")
    assert env_bool("Z") is False
