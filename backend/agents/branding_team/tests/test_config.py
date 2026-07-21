"""Env-parsing behavior the branding team relies on.

The branding team no longer ships its own ``env_int``/``env_float``/``env_bool``;
it consumes the canonical readers from :mod:`shared.env_config`. That module owns
the exhaustive suite — these tests pin the specific semantics branding depends on
after the migration: ``floor`` clamping (replacing the old ``minimum=``/
``positive=True`` guards) and WARNING logging on set-but-unparseable values (the
misconfiguration signal the old silent-fallback helpers lacked).
"""

from __future__ import annotations

import logging

import pytest

from shared.env_config import env_bool, env_float, env_int

_KNOB = "BRANDING_TEST_ENV_KNOB"


def test_env_int_default_parse_and_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """env_int: default when unset/garbage, parses ints, clamps to floor.

    Covers the ``env_int(..., floor=1)`` call sites (max-concurrent-runs,
    coro-offload workers, assistant history window).
    """
    monkeypatch.delenv(_KNOB, raising=False)
    assert env_int(_KNOB, 4, floor=1) == 4
    monkeypatch.setenv(_KNOB, "7")
    assert env_int(_KNOB, 4, floor=1) == 7
    monkeypatch.setenv(_KNOB, "garbage")
    assert env_int(_KNOB, 4, floor=1) == 4
    monkeypatch.setenv(_KNOB, "0")  # below floor -> clamped (not defaulted)
    assert env_int(_KNOB, 4, floor=1) == 1


def test_env_int_warns_on_set_but_unparseable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A set-but-unparseable int is surfaced at WARNING; an unset var is silent."""
    monkeypatch.delenv(_KNOB, raising=False)
    with caplog.at_level(logging.WARNING, logger="shared.env_config.config"):
        env_int(_KNOB, 4, floor=1)
    assert not caplog.records  # unset: no warning
    monkeypatch.setenv(_KNOB, "not-an-int")
    with caplog.at_level(logging.WARNING, logger="shared.env_config.config"):
        assert env_int(_KNOB, 4, floor=1) == 4
    assert any("Invalid int" in r.message for r in caplog.records)


def test_env_float_default_parse_and_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """env_float: default when unset/garbage, parses floats, clamps to floor.

    Covers the ``env_float(..., floor=0.1|1.0)`` poll-interval / timeout /
    heartbeat call sites — where a non-positive value now clamps to the floor
    instead of silently reverting to the default.
    """
    monkeypatch.delenv(_KNOB, raising=False)
    assert env_float(_KNOB, 2.0, floor=0.1) == pytest.approx(2.0)
    monkeypatch.setenv(_KNOB, "3.5")
    assert env_float(_KNOB, 2.0, floor=0.1) == pytest.approx(3.5)
    monkeypatch.setenv(_KNOB, "nope")
    assert env_float(_KNOB, 2.0, floor=0.1) == pytest.approx(2.0)
    monkeypatch.setenv(_KNOB, "-1")  # non-positive -> clamped to floor
    assert env_float(_KNOB, 2.0, floor=0.1) == pytest.approx(0.1)


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan"])
def test_env_float_non_finite_returns_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """A non-finite value falls back to the (clamped) default."""
    monkeypatch.setenv(_KNOB, raw)
    assert env_float(_KNOB, 30.0, floor=1.0) == pytest.approx(30.0)


def test_env_float_warns_on_set_but_unparseable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A set-but-unparseable float is surfaced at WARNING."""
    monkeypatch.setenv(_KNOB, "abc")
    with caplog.at_level(logging.WARNING, logger="shared.env_config.config"):
        assert env_float(_KNOB, 2.0, floor=0.1) == pytest.approx(2.0)
    assert any("Invalid float" in r.message for r in caplog.records)


def test_env_bool_tokens_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """env_bool: truthy/falsey tokens, default when unset."""
    monkeypatch.delenv(_KNOB, raising=False)
    assert env_bool(_KNOB) is False
    assert env_bool(_KNOB, default=True) is True
    for truthy in ("1", "true", "YES", "On"):
        monkeypatch.setenv(_KNOB, truthy)
        assert env_bool(_KNOB) is True
    monkeypatch.setenv(_KNOB, "0")
    assert env_bool(_KNOB, default=True) is False
