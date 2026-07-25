"""Tests for the generic HTTP retry/backoff policy (shared.http.retry)."""

import pytest

from shared.http.retry import backoff_sleep, parse_retry_env_config, retry_delay

_MAX_RETRIES_ENV = "TEST_RETRY_MAX_RETRIES"
_INITIAL_ENV = "TEST_RETRY_BACKOFF_INITIAL"
_CAP_ENV = "TEST_RETRY_BACKOFF_MAX"


def _parse(**overrides):
    kwargs = dict(default_max_retries=3, default_initial_seconds=30.0, default_cap_seconds=120.0)
    kwargs.update(overrides)
    return parse_retry_env_config(_MAX_RETRIES_ENV, _INITIAL_ENV, _CAP_ENV, **kwargs)


# ---------------------------------------------------------------------------
# parse_retry_env_config
# ---------------------------------------------------------------------------


def test_parse_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (_MAX_RETRIES_ENV, _INITIAL_ENV, _CAP_ENV):
        monkeypatch.delenv(var, raising=False)
    assert _parse() == (3, 30.0, 120.0)


def test_parse_valid_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MAX_RETRIES_ENV, "3")
    monkeypatch.setenv(_INITIAL_ENV, "120")
    monkeypatch.setenv(_CAP_ENV, "1800")
    assert _parse() == (3, 120.0, 1800.0)


def test_parse_garbage_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MAX_RETRIES_ENV, "not-an-int")
    monkeypatch.setenv(_INITIAL_ENV, "abc")
    monkeypatch.setenv(_CAP_ENV, "xyz")
    assert _parse() == (3, 30.0, 120.0)


def test_parse_empty_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MAX_RETRIES_ENV, "")
    monkeypatch.setenv(_INITIAL_ENV, "")
    monkeypatch.setenv(_CAP_ENV, "")
    assert _parse() == (3, 30.0, 120.0)


def test_parse_negative_retries_floored_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MAX_RETRIES_ENV, "-3")
    retries, _, _ = _parse()
    assert retries == 0


def test_parse_nonpositive_initial_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_INITIAL_ENV, "0")
    _, initial, _ = _parse()
    assert initial == 30.0


def test_parse_cap_below_initial_clamped_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_INITIAL_ENV, "300")
    monkeypatch.setenv(_CAP_ENV, "100")
    _, initial, cap = _parse()
    assert cap >= initial  # postcondition holds even on misconfiguration


def test_parse_uses_caller_supplied_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (_MAX_RETRIES_ENV, _INITIAL_ENV, _CAP_ENV):
        monkeypatch.delenv(var, raising=False)
    assert _parse(default_max_retries=5, default_initial_seconds=1.0, default_cap_seconds=60.0) == (
        5,
        1.0,
        60.0,
    )


# ---------------------------------------------------------------------------
# retry_delay
# ---------------------------------------------------------------------------


def test_delay_first_retry_at_least_floor() -> None:
    # Many samples: additive jitter can never drop below the 300s floor.
    for _ in range(200):
        d = retry_delay(0, 300.0, 3600.0)
        assert 300.0 <= d <= 302.0


def test_delay_doubling_progression() -> None:
    assert 300.0 <= retry_delay(0, 300.0, 3600.0) <= 302.0
    assert 600.0 <= retry_delay(1, 300.0, 3600.0) <= 602.0
    assert 1200.0 <= retry_delay(2, 300.0, 3600.0) <= 1202.0
    assert 2400.0 <= retry_delay(3, 300.0, 3600.0) <= 2402.0


def test_delay_cap_saturation() -> None:
    # 300 * 2**4 = 4800 > 3600 cap.
    assert retry_delay(4, 300.0, 3600.0) == 3600.0
    assert retry_delay(9, 300.0, 3600.0) == 3600.0


def test_delay_retry_after_extends_wait() -> None:
    d = retry_delay(0, 300.0, 3600.0, retry_after_seconds=900.0)
    assert d == 900.0  # larger than the ~300s computed wait, below cap


def test_delay_retry_after_below_floor_ignored() -> None:
    # A Retry-After smaller than the computed floor cannot shorten the wait.
    for _ in range(50):
        d = retry_delay(0, 300.0, 3600.0, retry_after_seconds=10.0)
        assert d >= 300.0


def test_delay_retry_after_capped() -> None:
    d = retry_delay(0, 300.0, 3600.0, retry_after_seconds=99999.0)
    assert d == 3600.0  # Retry-After cannot exceed the cap


def test_delay_retry_after_none_ignored() -> None:
    d = retry_delay(1, 300.0, 3600.0, retry_after_seconds=None)
    assert 600.0 <= d <= 602.0


def test_delay_negative_index_raises() -> None:
    with pytest.raises(ValueError):
        retry_delay(-1, 300.0, 3600.0)


def test_delay_nonpositive_initial_raises() -> None:
    with pytest.raises(ValueError):
        retry_delay(0, 0.0, 3600.0)


def test_delay_cap_below_initial_raises() -> None:
    with pytest.raises(ValueError):
        retry_delay(0, 300.0, 100.0)


def test_default_worst_case_total_wait_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the LLM-style default schedule (3 retries, 30s initial, 120s cap),
    the summed backoff across all retries is a few minutes, not hours — so a
    rate-limited call fails fast instead of hanging."""
    for var in (_MAX_RETRIES_ENV, _INITIAL_ENV, _CAP_ENV):
        monkeypatch.delenv(var, raising=False)
    retries, initial, cap = _parse()
    assert retries == 3
    total = sum(retry_delay(i, initial, cap) for i in range(retries))
    assert total <= 240.0  # ~3.6 min worst case, comfortably under 4 min
    assert total <= retries * cap  # never exceeds the hard ceiling


# ---------------------------------------------------------------------------
# backoff_sleep
# ---------------------------------------------------------------------------


def test_backoff_sleep_sleeps_computed_delay_and_returns_it(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("shared.http.retry.time.sleep", sleeps.append)

    wait = backoff_sleep(0, 3, 30.0, 120.0, provider="TestProvider", request_id="abc123", context="unit-test")

    assert sleeps == [wait]
    assert 30.0 <= wait <= 120.0


def test_backoff_sleep_logs_one_warning(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr("shared.http.retry.time.sleep", lambda _seconds: None)

    with caplog.at_level("WARNING", logger="shared.http.retry"):
        backoff_sleep(1, 3, 30.0, 120.0, provider="TestProvider")

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "TestProvider" in warnings[0].getMessage()


def test_backoff_sleep_propagates_delay_precondition_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.http.retry.time.sleep", lambda _seconds: None)
    with pytest.raises(ValueError):
        backoff_sleep(-1, 3, 30.0, 120.0)
