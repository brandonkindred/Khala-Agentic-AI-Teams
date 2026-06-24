"""Coverage for the DesignAgent expectancy-forecast contract.

Pins the dual-objective design change:

* ``ExpectancyForecast`` clamps out-of-band values rather than rejecting them
  (the forecast is advisory, never gated).
* ``StrategySpec`` carries the optional forecast and stays backward-compatible
  with persisted rows that predate it.
* ``_coerce_expectancy_forecast`` tolerates missing / malformed blobs.
* ``_build_spec_from_dict`` threads a designer-emitted forecast onto the spec.
"""

from __future__ import annotations

from investment_team.models import ExpectancyForecast, StrategySpec
from investment_team.strategy_lab.orchestrator import (
    StrategyLabOrchestrator,
    _coerce_expectancy_forecast,
)

# ---------------------------------------------------------------------------
# ExpectancyForecast — defaults + clamping validators
# ---------------------------------------------------------------------------


def test_expectancy_forecast_defaults_are_zero() -> None:
    fc = ExpectancyForecast()
    assert fc.forecast_win_rate == 0.0
    assert fc.reward_risk == 0.0
    assert fc.trades_per_year == 0.0
    assert fc.projected_annual_return_pct == 0.0
    assert fc.consistency_note == ""


def test_expectancy_forecast_keeps_in_range_values() -> None:
    fc = ExpectancyForecast(
        forecast_win_rate=0.55,
        reward_risk=2.0,
        trades_per_year=30,
        projected_annual_return_pct=14.0,
        consistency_note="coherent",
    )
    assert fc.forecast_win_rate == 0.55
    assert fc.reward_risk == 2.0
    assert fc.trades_per_year == 30
    assert fc.projected_annual_return_pct == 14.0


def test_expectancy_forecast_clamps_win_rate_above_one() -> None:
    # A model slip — 84 emitted for "84%" — is clamped to the [0, 1] band.
    assert ExpectancyForecast(forecast_win_rate=84).forecast_win_rate == 1.0


def test_expectancy_forecast_clamps_negative_win_rate() -> None:
    assert ExpectancyForecast(forecast_win_rate=-0.2).forecast_win_rate == 0.0


def test_expectancy_forecast_floors_negative_reward_and_frequency() -> None:
    fc = ExpectancyForecast(reward_risk=-3.0, trades_per_year=-1.0)
    assert fc.reward_risk == 0.0
    assert fc.trades_per_year == 0.0


def test_expectancy_forecast_allows_negative_projected_return() -> None:
    # A projected loss is a legitimate (if undesirable) forecast — not clamped.
    assert ExpectancyForecast(projected_annual_return_pct=-5.0).projected_annual_return_pct == -5.0


# ---------------------------------------------------------------------------
# _coerce_expectancy_forecast — tolerant coercion
# ---------------------------------------------------------------------------


def test_coerce_none_returns_none() -> None:
    assert _coerce_expectancy_forecast(None) is None


def test_coerce_dict_builds_forecast_with_clamping() -> None:
    fc = _coerce_expectancy_forecast({"forecast_win_rate": 2.0, "reward_risk": 1.5})
    assert isinstance(fc, ExpectancyForecast)
    assert fc.forecast_win_rate == 1.0  # clamped
    assert fc.reward_risk == 1.5


def test_coerce_passes_through_instance() -> None:
    fc = ExpectancyForecast(forecast_win_rate=0.5)
    assert _coerce_expectancy_forecast(fc) is fc


def test_coerce_non_dict_returns_none() -> None:
    assert _coerce_expectancy_forecast(["not", "a", "dict"]) is None
    assert _coerce_expectancy_forecast("0.5") is None


def test_coerce_unusable_dict_returns_none(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="investment_team.strategy_lab.orchestrator"):
        result = _coerce_expectancy_forecast({"forecast_win_rate": "not-a-number"})
    assert result is None
    assert any("expectancy_forecast" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# StrategySpec persistence + backward compatibility
# ---------------------------------------------------------------------------


def _minimal_spec_kwargs() -> dict:
    return {
        "strategy_id": "s1",
        "authored_by": "test",
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "timeframe": "1d",
    }


def test_strategy_spec_defaults_forecast_to_none() -> None:
    spec = StrategySpec(**_minimal_spec_kwargs())
    assert spec.expectancy_forecast is None


def test_strategy_spec_carries_forecast() -> None:
    spec = StrategySpec(
        **_minimal_spec_kwargs(),
        expectancy_forecast=ExpectancyForecast(forecast_win_rate=0.6, reward_risk=2.0),
    )
    assert spec.expectancy_forecast is not None
    assert spec.expectancy_forecast.forecast_win_rate == 0.6


def test_strategy_spec_round_trips_forecast_through_json() -> None:
    spec = StrategySpec(
        **_minimal_spec_kwargs(),
        expectancy_forecast=ExpectancyForecast(
            forecast_win_rate=0.55, projected_annual_return_pct=12.0
        ),
    )
    reloaded = StrategySpec.model_validate(spec.model_dump())
    assert reloaded.expectancy_forecast is not None
    assert reloaded.expectancy_forecast.projected_annual_return_pct == 12.0


def test_legacy_persisted_spec_without_forecast_loads_as_none() -> None:
    # A row authored before this field existed has no ``expectancy_forecast`` key.
    legacy = {
        "strategy_id": "s1",
        "authored_by": "legacy",
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "timeframe": "1d",
        "entry_rules": [],
        "exit_rules": [],
    }
    spec = StrategySpec.parse_persisted(legacy)
    assert spec.expectancy_forecast is None


# ---------------------------------------------------------------------------
# _build_spec_from_dict — threads the forecast onto the spec
# ---------------------------------------------------------------------------


def test_build_spec_from_dict_threads_forecast() -> None:
    orch = StrategyLabOrchestrator()
    strategy_dict = {
        "asset_class": "stocks",
        "timeframe": "1d",
        "expectancy_forecast": {
            "forecast_win_rate": 0.6,
            "reward_risk": 2.0,
            "trades_per_year": 30,
            "projected_annual_return_pct": 14.0,
            "consistency_note": "coherent",
        },
    }
    spec = orch._build_spec_from_dict(strategy_dict, strategy_id="s1")
    assert spec.expectancy_forecast is not None
    assert spec.expectancy_forecast.forecast_win_rate == 0.6


def test_build_spec_from_dict_without_forecast_is_none() -> None:
    orch = StrategyLabOrchestrator()
    spec = orch._build_spec_from_dict(
        {"asset_class": "stocks", "timeframe": "1d"}, strategy_id="s1"
    )
    assert spec.expectancy_forecast is None


def test_build_spec_from_dict_drops_garbage_forecast() -> None:
    orch = StrategyLabOrchestrator()
    spec = orch._build_spec_from_dict(
        {"asset_class": "stocks", "timeframe": "1d", "expectancy_forecast": "garbage"},
        strategy_id="s1",
    )
    assert spec.expectancy_forecast is None
