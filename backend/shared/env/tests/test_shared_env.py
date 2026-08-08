"""Tests for the shared env-var parser (defensive defaults + clamping)."""

from __future__ import annotations

import pytest

from shared.env import env_flag_enabled, env_flag_opt_in, parse_float, parse_int

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
    with pytest.raises(ValueError):
        env_flag_enabled("")


# ---------------------------------------------------------------------------
# env_flag_opt_in
# ---------------------------------------------------------------------------


def test_opt_in_default_off_when_unset(monkeypatch):
    monkeypatch.delenv("FLAG_Y", raising=False)
    assert env_flag_opt_in("FLAG_Y") is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "YES", "on", " On "])
def test_opt_in_on_for_explicit_truthy(monkeypatch, value):
    monkeypatch.setenv("FLAG_Y", value)
    assert env_flag_opt_in("FLAG_Y") is True


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "garbage", "maybe"])
def test_opt_in_off_for_blank_or_other(monkeypatch, value):
    monkeypatch.setenv("FLAG_Y", value)
    assert env_flag_opt_in("FLAG_Y") is False


def test_opt_in_requires_name():
    with pytest.raises(ValueError):
        env_flag_opt_in("")


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


def test_int_with_both_bounds_in_range(monkeypatch):
    """A value within [minimum, maximum] passes through unclamped."""
    monkeypatch.setenv("N", "7")
    assert parse_int("N", 1, minimum=5, maximum=10) == 7


def test_int_with_max_only_below_max(monkeypatch):
    """With only maximum set, a value below it is returned unchanged."""
    monkeypatch.setenv("N", "3")
    assert parse_int("N", 1, maximum=10) == 3


def test_int_default_is_also_clamped(monkeypatch):
    monkeypatch.delenv("N", raising=False)
    assert parse_int("N", 1, minimum=5) == 5  # default below floor -> clamped up


def test_int_rejects_inverted_bounds(monkeypatch):
    with pytest.raises(ValueError):
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


def test_float_clamps_with_both_bounds(monkeypatch):
    # Both minimum and maximum supplied: below floor clamps up, above ceiling
    # clamps down, in-range passes through.
    monkeypatch.setenv("F", "0.1")
    assert parse_float("F", 1.0, minimum=0.5, maximum=2.0) == 0.5
    monkeypatch.setenv("F", "3.0")
    assert parse_float("F", 1.0, minimum=0.5, maximum=2.0) == 2.0
    monkeypatch.setenv("F", "1.25")
    assert parse_float("F", 1.0, minimum=0.5, maximum=2.0) == 1.25


def test_float_default_is_also_clamped(monkeypatch):
    # An unset var falls back to the default, which is itself clamped to bounds.
    monkeypatch.delenv("F", raising=False)
    assert parse_float("F", 0.1, minimum=0.5) == 0.5  # default below floor -> clamped up
    assert parse_float("F", 9.0, maximum=2.0) == 2.0  # default above ceiling -> clamped down
