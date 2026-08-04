"""Tests for token → USD cost estimation (pricing.py)."""

from __future__ import annotations

import pytest

from llm_service.pricing import (  # noqa: PLC2701 - _normalize_model_for_env is unit under test
    MODEL_PRICING,
    _normalize_model_for_env,
    estimate_cost_usd,
)


def test_known_model_cost() -> None:
    price = MODEL_PRICING["deepseek-v4-pro:cloud"]
    cost = estimate_cost_usd("deepseek-v4-pro:cloud", 1000, 2000)
    assert cost == pytest.approx(price.usd_per_1k_input * 1 + price.usd_per_1k_output * 2)


def test_unknown_model_is_zero() -> None:
    assert estimate_cost_usd("no-such-model:v9", 5000, 5000) == 0.0


def test_zero_tokens_is_zero() -> None:
    assert estimate_cost_usd("deepseek-v4-pro:cloud", 0, 0) == 0.0


def test_local_model_priced_zero() -> None:
    assert estimate_cost_usd("glm-5.2:cloud", 100000, 100000) == 0.0


def test_negative_tokens_raise() -> None:
    with pytest.raises(ValueError):
        estimate_cost_usd("deepseek-v4-pro:cloud", -1, 0)
    with pytest.raises(ValueError):
        estimate_cost_usd("deepseek-v4-pro:cloud", 0, -1)


def test_normalize_model_for_env() -> None:
    assert _normalize_model_for_env("deepseek-v4-pro:cloud") == "DEEPSEEK_V4_PRO_CLOUD"
    assert _normalize_model_for_env("qwen3.5:397b-cloud") == "QWEN3_5_397B_CLOUD"


def test_env_override_applies(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PRICE_MYMODEL", "0.001/0.002")
    cost = estimate_cost_usd("mymodel", 1000, 1000)
    assert cost == pytest.approx(0.001 + 0.002)


def test_env_override_beats_table(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PRICE_DEEPSEEK_V4_PRO_CLOUD", "1/1")
    cost = estimate_cost_usd("deepseek-v4-pro:cloud", 1000, 1000)
    assert cost == pytest.approx(2.0)


def test_non_finite_env_override_rejected(monkeypatch) -> None:
    # inf/nan (e.g. a 1e400 typo) must not yield an infinite cost; fall back to table.
    price = MODEL_PRICING["deepseek-v4-pro:cloud"]
    monkeypatch.setenv("LLM_PRICE_DEEPSEEK_V4_PRO_CLOUD", "inf/0.001")
    assert estimate_cost_usd("deepseek-v4-pro:cloud", 1000, 0) == pytest.approx(
        price.usd_per_1k_input
    )
    monkeypatch.setenv("LLM_PRICE_DEEPSEEK_V4_PRO_CLOUD", "1e400/0")
    cost = estimate_cost_usd("deepseek-v4-pro:cloud", 1000, 1000)
    import math

    assert math.isfinite(cost)


def test_malformed_env_override_falls_back(monkeypatch) -> None:
    # Malformed override is ignored; falls back to the table value (not an error).
    monkeypatch.setenv("LLM_PRICE_DEEPSEEK_V4_PRO_CLOUD", "garbage")
    price = MODEL_PRICING["deepseek-v4-pro:cloud"]
    assert estimate_cost_usd("deepseek-v4-pro:cloud", 1000, 0) == pytest.approx(
        price.usd_per_1k_input
    )
