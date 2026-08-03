"""Tests for the per-team reverse-proxy circuit breaker."""

from __future__ import annotations

import sys
from pathlib import Path

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from unified_api.circuit_breaker import CircuitBreaker, CircuitState


def test_new_team_circuit_starts_closed() -> None:
    """A team with no recorded history has a CLOSED circuit and is not open."""
    breaker = CircuitBreaker()
    assert breaker.get_state("blogging") == CircuitState.CLOSED
    assert breaker.is_open("blogging") is False


def test_record_failure_below_threshold_stays_closed() -> None:
    """Fewer failures than the threshold keep the circuit CLOSED."""
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.record_failure("blogging")
    breaker.record_failure("blogging")
    assert breaker.get_state("blogging") == CircuitState.CLOSED
    assert breaker.is_open("blogging") is False


def test_record_failure_at_threshold_opens_circuit() -> None:
    """Reaching the failure threshold opens the circuit and rejects requests."""
    breaker = CircuitBreaker(failure_threshold=3)
    for _ in range(3):
        breaker.record_failure("blogging")
    assert breaker.get_state("blogging") == CircuitState.OPEN
    assert breaker.is_open("blogging") is True


def test_record_success_resets_open_circuit_to_closed() -> None:
    """A success after failures resets the circuit and its failure count."""
    breaker = CircuitBreaker(failure_threshold=2)
    breaker.record_failure("blogging")
    breaker.record_failure("blogging")
    assert breaker.get_state("blogging") == CircuitState.OPEN

    breaker.record_success("blogging")
    assert breaker.get_state("blogging") == CircuitState.CLOSED
    assert breaker.is_open("blogging") is False


def test_open_circuit_transitions_to_half_open_after_recovery_timeout(monkeypatch) -> None:
    """is_open transitions OPEN -> HALF_OPEN once recovery_timeout has elapsed."""
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0)
    clock = {"t": 100.0}
    monkeypatch.setattr("unified_api.circuit_breaker.time.monotonic", lambda: clock["t"])

    breaker.record_failure("blogging")
    assert breaker.get_state("blogging") == CircuitState.OPEN
    assert breaker.is_open("blogging") is True

    clock["t"] += 10.0
    assert breaker.is_open("blogging") is False
    assert breaker.get_state("blogging") == CircuitState.HALF_OPEN


def test_half_open_probe_failure_reopens_circuit(monkeypatch) -> None:
    """A failed probe request while HALF_OPEN sends the circuit back to OPEN."""
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=5.0)
    clock = {"t": 0.0}
    monkeypatch.setattr("unified_api.circuit_breaker.time.monotonic", lambda: clock["t"])

    breaker.record_failure("blogging")
    clock["t"] += 5.0
    assert breaker.is_open("blogging") is False
    assert breaker.get_state("blogging") == CircuitState.HALF_OPEN

    breaker.record_failure("blogging")
    assert breaker.get_state("blogging") == CircuitState.OPEN


def test_half_open_probe_success_closes_circuit(monkeypatch) -> None:
    """A successful probe request while HALF_OPEN closes the circuit."""
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=5.0)
    clock = {"t": 0.0}
    monkeypatch.setattr("unified_api.circuit_breaker.time.monotonic", lambda: clock["t"])

    breaker.record_failure("blogging")
    clock["t"] += 5.0
    assert breaker.is_open("blogging") is False
    assert breaker.get_state("blogging") == CircuitState.HALF_OPEN

    breaker.record_success("blogging")
    assert breaker.get_state("blogging") == CircuitState.CLOSED


def test_teams_have_independent_circuits() -> None:
    """Failures recorded for one team never affect another team's circuit."""
    breaker = CircuitBreaker(failure_threshold=1)
    breaker.record_failure("blogging")
    assert breaker.get_state("blogging") == CircuitState.OPEN
    assert breaker.get_state("investment") == CircuitState.CLOSED
    assert breaker.is_open("investment") is False


def test_get_all_states_reports_every_tracked_team() -> None:
    """get_all_states returns a state snapshot keyed by team, only for teams seen so far."""
    breaker = CircuitBreaker(failure_threshold=5)
    breaker.record_success("blogging")
    breaker.record_failure("investment")
    states = breaker.get_all_states()
    assert states == {"blogging": "closed", "investment": "closed"}


def test_get_all_states_empty_when_no_teams_tracked() -> None:
    """get_all_states returns an empty dict before any team has recorded activity."""
    breaker = CircuitBreaker()
    assert breaker.get_all_states() == {}
