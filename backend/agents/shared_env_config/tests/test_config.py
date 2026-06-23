"""Unit tests for the shared typed env-config helpers (DB-free)."""

from __future__ import annotations

import pytest

from shared_env_config import env_bool, env_float, env_int

_KNOB = "SHARED_TEST_ENV_KNOB"


def test_env_bool_unset_returns_default(monkeypatch) -> None:
    monkeypatch.delenv(_KNOB, raising=False)
    assert env_bool(_KNOB) is False
    assert env_bool(_KNOB, default=True) is True


@pytest.mark.parametrize("raw", ["true", "1", "YES", " On ", "TrUe"])
def test_env_bool_truthy(monkeypatch, raw) -> None:
    monkeypatch.setenv(_KNOB, raw)
    assert env_bool(_KNOB) is True


@pytest.mark.parametrize("raw", ["false", "0", "No", " off "])
def test_env_bool_falsy(monkeypatch, raw) -> None:
    monkeypatch.setenv(_KNOB, raw)
    assert env_bool(_KNOB, default=True) is False


def test_env_bool_unrecognized_returns_default(monkeypatch) -> None:
    monkeypatch.setenv(_KNOB, "maybe")
    assert env_bool(_KNOB) is False
    assert env_bool(_KNOB, default=True) is True


def test_env_bool_logs_on_set_but_unrecognized(monkeypatch, caplog) -> None:
    # Mirrors env_int/env_float: a set-but-unrecognized value is surfaced; unset is silent.
    import logging

    monkeypatch.delenv(_KNOB, raising=False)
    with caplog.at_level(logging.WARNING, logger="shared_env_config.config"):
        env_bool(_KNOB)
    assert not caplog.records  # unset: no warning
    monkeypatch.setenv(_KNOB, "maybe")
    with caplog.at_level(logging.WARNING, logger="shared_env_config.config"):
        assert env_bool(_KNOB) is False
    assert any("Unrecognized bool" in r.message for r in caplog.records)


def test_env_bool_rejects_non_bool_default() -> None:
    with pytest.raises(ValueError, match="default must be a bool"):
        env_bool(_KNOB, default="yes")  # type: ignore[arg-type]


@pytest.mark.parametrize("reader", [env_bool, env_int, env_float])
def test_readers_reject_empty_name(reader) -> None:
    # Empty name is a caller contract violation, enforced (raised, not silently defaulted).
    with pytest.raises(ValueError, match="non-empty"):
        reader("", 1) if reader is not env_bool else reader("")


def test_env_int_unset_and_garbage_return_default(monkeypatch) -> None:
    monkeypatch.delenv(_KNOB, raising=False)
    assert env_int(_KNOB, 7, 1) == 7
    monkeypatch.setenv(_KNOB, "not-an-int")
    assert env_int(_KNOB, 7, 1) == 7


def test_env_int_logs_on_set_but_unparseable(monkeypatch, caplog) -> None:
    # A set-but-unparseable value is a misconfiguration and is logged; unset is silent.
    import logging

    monkeypatch.delenv(_KNOB, raising=False)
    with caplog.at_level(logging.WARNING, logger="shared_env_config.config"):
        env_int(_KNOB, 7, 1)
    assert not caplog.records  # unset: no warning
    monkeypatch.setenv(_KNOB, "1O24")
    with caplog.at_level(logging.WARNING, logger="shared_env_config.config"):
        assert env_int(_KNOB, 7, 1) == 7
    assert any("Invalid int" in r.message for r in caplog.records)


def test_env_float_logs_on_unparseable_and_nonfinite(monkeypatch, caplog) -> None:
    import logging

    monkeypatch.setenv(_KNOB, "abc")
    with caplog.at_level(logging.WARNING, logger="shared_env_config.config"):
        assert env_float(_KNOB, 1.5, 0.0) == pytest.approx(1.5)
    assert any("Invalid float" in r.message for r in caplog.records)
    caplog.clear()
    monkeypatch.setenv(_KNOB, "inf")
    with caplog.at_level(logging.WARNING, logger="shared_env_config.config"):
        assert env_float(_KNOB, 1.5, 0.0) == pytest.approx(1.5)
    assert any("Non-finite" in r.message for r in caplog.records)


def test_env_int_parses_and_clamps(monkeypatch) -> None:
    monkeypatch.setenv(_KNOB, "42")
    assert env_int(_KNOB, 7, 1) == 42
    monkeypatch.setenv(_KNOB, "0")  # below floor
    assert env_int(_KNOB, 7, 1) == 1
    monkeypatch.setenv(_KNOB, "99")  # above ceiling
    assert env_int(_KNOB, 5, 0, 50) == 50
    monkeypatch.setenv(_KNOB, " 12 ")  # whitespace tolerated
    assert env_int(_KNOB, 5, 0, 50) == 12


def test_env_int_default_must_respect_bounds() -> None:
    # Raised (not asserted) so the contract holds under `python -O`.
    with pytest.raises(ValueError, match="floor"):
        env_int(_KNOB, 0, floor=1)
    with pytest.raises(ValueError, match="ceiling"):
        env_int(_KNOB, 100, ceiling=50)


def test_clamp_rejects_inverted_bounds() -> None:
    # floor > ceiling is a caller bug; _clamp raises ValueError (not assert) so
    # it is enforced even under `python -O`, where the default-respects-bounds
    # asserts in env_int/env_float are stripped.
    from shared_env_config.config import _clamp

    with pytest.raises(ValueError, match="floor"):
        _clamp(7.0, 10.0, 5.0)
    # one-sided bounds and floor == ceiling are fine
    assert _clamp(7.0, None, 5.0) == 5.0
    assert _clamp(1.0, 3.0, None) == 3.0
    assert _clamp(9.0, 5.0, 5.0) == 5.0


def test_env_float_unset_and_garbage_return_default(monkeypatch) -> None:
    monkeypatch.delenv(_KNOB, raising=False)
    assert env_float(_KNOB, 30.0, 0.0) == pytest.approx(30.0)
    monkeypatch.setenv(_KNOB, "garbage")
    assert env_float(_KNOB, 30.0, 0.0) == pytest.approx(30.0)


def test_env_float_parses_and_clamps(monkeypatch) -> None:
    monkeypatch.setenv(_KNOB, "2.5")
    assert env_float(_KNOB, 1.0, 0.0) == pytest.approx(2.5)
    monkeypatch.setenv(_KNOB, "-5")  # below floor
    assert env_float(_KNOB, 1.0, 0.0) == pytest.approx(0.0)
    monkeypatch.setenv(_KNOB, "9.9")  # above ceiling
    assert env_float(_KNOB, 1.0, 0.0, 5.0) == pytest.approx(5.0)


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan"])
def test_env_float_non_finite_returns_default(monkeypatch, raw) -> None:
    monkeypatch.setenv(_KNOB, raw)
    assert env_float(_KNOB, 30.0, 0.0) == pytest.approx(30.0)


def test_env_float_default_must_respect_bounds() -> None:
    with pytest.raises(ValueError, match="floor"):
        env_float(_KNOB, -1.0, floor=0.0)
    with pytest.raises(ValueError, match="ceiling"):
        env_float(_KNOB, 10.0, ceiling=5.0)
