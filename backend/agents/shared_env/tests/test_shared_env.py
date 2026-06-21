"""Tests for the shared env-var parser (defensive defaults + clamping)."""

from __future__ import annotations

import pytest

from shared_env import env_flag_enabled, parse_float, parse_int

# ---------------------------------------------------------------------------
# env_flag_enabled
# ---------------------------------------------------------------------------


def test_flag_default_on_when_unset(monkeypatch):
    monkeypatch.delenv("FLAG_X", raising=False)
    assert env_flag_enabled("FLAG_X") is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", " No "])
def test_flag_off_for_explicit_falsy(monkeypatch, value):
    monkeypatch.setenv("FLAG_X", value)
    assert env_flag_enabled("FLAG_X") is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "", "garbage"])
def test_flag_on_for_unset_or_other(monkeypatch, value):
    monkeypatch.setenv("FLAG_X", value)
    assert env_flag_enabled("FLAG_X") is True


def test_flag_requires_name():
    with pytest.raises(AssertionError):
        env_flag_enabled("")


# ---------------------------------------------------------------------------
# parse_int
# ---------------------------------------------------------------------------


def test_int_unset_returns_default(monkeypatch):
    monkeypatch.delenv("N", raising=False)
    assert parse_int("N", 7) == 7


@pytest.mark.parametrize("value", ["", "   ", "abc", "1.5", "0x10"])
def test_int_unparseable_returns_default(monkeypatch, value):
    monkeypatch.setenv("N", value)
    assert parse_int("N", 7) == 7


def test_int_parses_value(monkeypatch):
    monkeypatch.setenv("N", " 42 ")
    assert parse_int("N", 7) == 42


def test_int_clamps_to_minimum(monkeypatch):
    monkeypatch.setenv("N", "1")
    assert parse_int("N", 7, minimum=5) == 5


def test_int_clamps_to_maximum(monkeypatch):
    monkeypatch.setenv("N", "100")
    assert parse_int("N", 7, maximum=10) == 10


def test_int_default_is_also_clamped(monkeypatch):
    monkeypatch.delenv("N", raising=False)
    assert parse_int("N", 1, minimum=5) == 5  # default below floor -> clamped up


def test_int_rejects_inverted_bounds(monkeypatch):
    with pytest.raises(AssertionError):
        parse_int("N", 5, minimum=10, maximum=1)


# ---------------------------------------------------------------------------
# parse_float
# ---------------------------------------------------------------------------


def test_float_unset_returns_default(monkeypatch):
    monkeypatch.delenv("F", raising=False)
    assert parse_float("F", 1.5) == 1.5


@pytest.mark.parametrize("value", ["", "abc", "nanan"])
def test_float_unparseable_returns_default(monkeypatch, value):
    monkeypatch.setenv("F", value)
    assert parse_float("F", 2.5) == 2.5


def test_float_parses_and_clamps(monkeypatch):
    monkeypatch.setenv("F", "0.05")
    assert parse_float("F", 1.0, minimum=0.1) == 0.1
    monkeypatch.setenv("F", "9.0")
    assert parse_float("F", 1.0, maximum=3.0) == 3.0


def test_float_returns_float_type(monkeypatch):
    monkeypatch.setenv("F", "3")
    out = parse_float("F", 1.0)
    assert out == 3.0
    assert isinstance(out, float)


@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "Infinity", "NaN"])
def test_float_rejects_non_finite(monkeypatch, value):
    # A non-finite env value (inf/nan) must fall back to the finite default,
    # not propagate to a consumer that uses it as an interval/limit.
    monkeypatch.setenv("F", value)
    assert parse_float("F", 2.5) == 2.5
