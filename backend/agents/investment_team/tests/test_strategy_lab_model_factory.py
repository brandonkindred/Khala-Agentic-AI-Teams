"""Tests for :func:`model_factory._resolve_temperature`.

Covers the per-agent-key default map, the per-key / global env-var override
precedence, and clamping — see ``model_factory._resolve_temperature`` for the
documented contract.
"""

import pytest

from investment_team.strategy_lab.agents import model_factory


def _clear_temperature_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRATEGY_LAB_LLM_TEMPERATURE", raising=False)
    monkeypatch.delenv("STRATEGY_LAB_LLM_TEMPERATURE_STRATEGY_DESIGN", raising=False)
    monkeypatch.delenv("STRATEGY_LAB_LLM_TEMPERATURE_STRATEGY_IDEATION", raising=False)
    monkeypatch.delenv("STRATEGY_LAB_LLM_TEMPERATURE_SOME_OTHER_KEY", raising=False)


def test_resolve_temperature_default_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env overrides, only ``strategy_design`` samples above zero."""
    _clear_temperature_env(monkeypatch)

    assert model_factory._resolve_temperature("strategy_design") == 0.6
    assert model_factory._resolve_temperature("strategy_ideation") == 0.0
    assert model_factory._resolve_temperature("some_other_key") == 0.0


def test_resolve_temperature_per_key_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-key env var beats both the global env var and the per-key default."""
    _clear_temperature_env(monkeypatch)
    monkeypatch.setenv("STRATEGY_LAB_LLM_TEMPERATURE", "0.3")
    monkeypatch.setenv("STRATEGY_LAB_LLM_TEMPERATURE_STRATEGY_DESIGN", "1.2")

    assert model_factory._resolve_temperature("strategy_design") == 1.2
    # A key with no matching per-key var still picks up the global override.
    assert model_factory._resolve_temperature("strategy_ideation") == 0.3


def test_resolve_temperature_global_override_without_per_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global override applies uniformly (including to non-design keys) when
    no per-key var is set — this is a deliberate operator escape hatch, not a
    diversity-only knob."""
    _clear_temperature_env(monkeypatch)
    monkeypatch.setenv("STRATEGY_LAB_LLM_TEMPERATURE", "0.9")

    assert model_factory._resolve_temperature("strategy_design") == 0.9
    assert model_factory._resolve_temperature("strategy_ideation") == 0.9
    assert model_factory._resolve_temperature("some_other_key") == 0.9


@pytest.mark.parametrize(
    "raw,expected",
    [("5.0", 2.0), ("-1", 0.0), ("999", 2.0), ("-999.5", 0.0)],
)
def test_resolve_temperature_clamps_per_key_override(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    """An out-of-range per-key override clamps to ``[0.0, 2.0]`` rather than
    passing a runaway value to the LLM client."""
    _clear_temperature_env(monkeypatch)
    monkeypatch.setenv("STRATEGY_LAB_LLM_TEMPERATURE_STRATEGY_DESIGN", raw)

    assert model_factory._resolve_temperature("strategy_design") == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("5.0", 2.0), ("-1", 0.0)],
)
def test_resolve_temperature_clamps_global_override(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    """An out-of-range global override also clamps to ``[0.0, 2.0]``."""
    _clear_temperature_env(monkeypatch)
    monkeypatch.setenv("STRATEGY_LAB_LLM_TEMPERATURE", raw)

    assert model_factory._resolve_temperature("some_other_key") == expected


def test_resolve_temperature_non_design_key_resolves_to_zero_with_no_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The determinism-preserving guarantee: any key other than
    ``strategy_design`` — critically ``strategy_ideation``, which drives the
    deterministic repair/analysis agents — resolves to exactly ``0.0`` when no
    env override is set."""
    _clear_temperature_env(monkeypatch)

    assert model_factory._resolve_temperature("strategy_ideation") == 0.0
