"""Tests for the shared 429 rate-limit backoff policy (llm_service.backoff)."""

import pytest

from llm_service.backoff import parse_rate_limit_retry_config, rate_limit_retry_delay

# ---------------------------------------------------------------------------
# parse_rate_limit_retry_config
# ---------------------------------------------------------------------------


def test_parse_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LLM_RATE_LIMIT_MAX_RETRIES",
        "LLM_RATE_LIMIT_BACKOFF_INITIAL",
        "LLM_RATE_LIMIT_BACKOFF_MAX",
    ):
        monkeypatch.delenv(var, raising=False)
    assert parse_rate_limit_retry_config() == (3, 30.0, 120.0)


def test_parse_valid_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "3")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "120")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_MAX", "1800")
    assert parse_rate_limit_retry_config() == (3, 120.0, 1800.0)


def test_parse_garbage_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "not-an-int")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "abc")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_MAX", "xyz")
    assert parse_rate_limit_retry_config() == (3, 30.0, 120.0)


def test_parse_empty_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_MAX", "")
    assert parse_rate_limit_retry_config() == (3, 30.0, 120.0)


def test_parse_negative_retries_floored_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "-3")
    retries, _, _ = parse_rate_limit_retry_config()
    assert retries == 0


def test_parse_nonpositive_initial_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "0")
    _, initial, _ = parse_rate_limit_retry_config()
    assert initial == 30.0


def test_parse_cap_below_initial_clamped_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_MAX", "100")
    _, initial, cap = parse_rate_limit_retry_config()
    assert cap >= initial  # postcondition holds even on misconfiguration


# ---------------------------------------------------------------------------
# rate_limit_retry_delay
# ---------------------------------------------------------------------------


def test_delay_first_retry_at_least_floor() -> None:
    # Many samples: additive jitter can never drop below the 300s floor.
    for _ in range(200):
        d = rate_limit_retry_delay(0, 300.0, 3600.0)
        assert 300.0 <= d <= 302.0


def test_delay_doubling_progression() -> None:
    assert 300.0 <= rate_limit_retry_delay(0, 300.0, 3600.0) <= 302.0
    assert 600.0 <= rate_limit_retry_delay(1, 300.0, 3600.0) <= 602.0
    assert 1200.0 <= rate_limit_retry_delay(2, 300.0, 3600.0) <= 1202.0
    assert 2400.0 <= rate_limit_retry_delay(3, 300.0, 3600.0) <= 2402.0


def test_delay_cap_saturation() -> None:
    # 300 * 2**4 = 4800 > 3600 cap.
    assert rate_limit_retry_delay(4, 300.0, 3600.0) == 3600.0
    assert rate_limit_retry_delay(9, 300.0, 3600.0) == 3600.0


def test_delay_retry_after_extends_wait() -> None:
    d = rate_limit_retry_delay(0, 300.0, 3600.0, retry_after_seconds=900.0)
    assert d == 900.0  # larger than the ~300s computed wait, below cap


def test_delay_retry_after_below_floor_ignored() -> None:
    # A Retry-After smaller than the computed floor cannot shorten the wait.
    for _ in range(50):
        d = rate_limit_retry_delay(0, 300.0, 3600.0, retry_after_seconds=10.0)
        assert d >= 300.0


def test_delay_retry_after_capped() -> None:
    d = rate_limit_retry_delay(0, 300.0, 3600.0, retry_after_seconds=99999.0)
    assert d == 3600.0  # Retry-After cannot exceed the cap


def test_delay_retry_after_none_ignored() -> None:
    d = rate_limit_retry_delay(1, 300.0, 3600.0, retry_after_seconds=None)
    assert 600.0 <= d <= 602.0


def test_delay_negative_index_raises() -> None:
    with pytest.raises(ValueError):
        rate_limit_retry_delay(-1, 300.0, 3600.0)


def test_delay_nonpositive_initial_raises() -> None:
    with pytest.raises(ValueError):
        rate_limit_retry_delay(0, 0.0, 3600.0)


def test_delay_cap_below_initial_raises() -> None:
    with pytest.raises(ValueError):
        rate_limit_retry_delay(0, 300.0, 100.0)


def test_default_worst_case_total_wait_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the default schedule, the summed backoff across all retries is a few
    minutes — not the hours the old 300s/3600s/5 defaults produced — so a
    rate-limited call fails fast instead of hanging."""
    for var in (
        "LLM_RATE_LIMIT_MAX_RETRIES",
        "LLM_RATE_LIMIT_BACKOFF_INITIAL",
        "LLM_RATE_LIMIT_BACKOFF_MAX",
    ):
        monkeypatch.delenv(var, raising=False)
    retries, initial, cap = parse_rate_limit_retry_config()
    assert retries == 3
    total = sum(rate_limit_retry_delay(i, initial, cap) for i in range(retries))
    assert total <= 240.0  # ~3.6 min worst case, comfortably under 4 min
    assert total <= retries * cap  # never exceeds the hard ceiling
