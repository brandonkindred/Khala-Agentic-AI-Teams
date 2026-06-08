"""Tests for the shared runtime-config helpers."""

from __future__ import annotations

import pytest

from agent_cognition.runtime_config import (
    read_int_with_floor,
    read_positive_int,
)


def test_read_positive_int_unset(monkeypatch):
    monkeypatch.delenv("X_INT", raising=False)
    assert read_positive_int("X_INT", 10) == 10


@pytest.mark.parametrize("raw,expected", [("3", 3), ("0", 10), ("-1", 10), ("junk", 10), ("", 10)])
def test_read_positive_int_values(monkeypatch, raw, expected):
    monkeypatch.setenv("X_INT", raw)
    assert read_positive_int("X_INT", 10) == expected


def test_read_int_with_floor_unset(monkeypatch):
    monkeypatch.delenv("X_FLOOR", raising=False)
    assert read_int_with_floor("X_FLOOR", 300, 30) == 300


@pytest.mark.parametrize(
    "raw,expected",
    [("400", 400), ("5", 30), ("0", 30), ("-9", 30), ("junk", 300), ("", 300)],
)
def test_read_int_with_floor_values(monkeypatch, raw, expected):
    monkeypatch.setenv("X_FLOOR", raw)
    assert read_int_with_floor("X_FLOOR", 300, 30) == expected


def test_garbage_value_logs_warning(monkeypatch, caplog):
    # A set-but-unparseable value warns (so an operator notices a typo); an unset
    # value stays silent. Covers both helpers.
    monkeypatch.setenv("X_FLOOR", "oops")
    monkeypatch.setenv("X_INT", "nope")
    with caplog.at_level("WARNING"):
        read_int_with_floor("X_FLOOR", 300, 30)
        read_positive_int("X_INT", 10)
    assert "Invalid X_FLOOR='oops'" in caplog.text
    assert "Invalid X_INT='nope'" in caplog.text


def test_unset_value_does_not_warn(monkeypatch, caplog):
    monkeypatch.delenv("X_FLOOR", raising=False)
    with caplog.at_level("WARNING"):
        read_int_with_floor("X_FLOOR", 300, 30)
    assert "Invalid" not in caplog.text
