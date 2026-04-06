"""
Per-team circuit breaker for the unified API reverse proxy.

Tracks consecutive failures per team and short-circuits requests when the
failure threshold is exceeded, returning 503 immediately instead of waiting
for the upstream timeout. Auto-recovers when the upstream starts responding.

Usage::

    from unified_api.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
    if breaker.is_open("blogging"):
        return Response("Service unavailable", status_code=503)
    try:
        resp = await proxy_request(...)
        breaker.record_success("blogging")
    except Exception:
        breaker.record_failure("blogging")
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"  # Normal operation — requests flow through
    OPEN = "open"  # Failures exceeded threshold — requests are rejected
    HALF_OPEN = "half_open"  # Recovery probe — one request allowed through


@dataclass
class _TeamCircuit:
    """Internal state for a single team's circuit."""

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0


class CircuitBreaker:
    """Per-team circuit breaker with configurable thresholds.

    Parameters
    ----------
    failure_threshold:
        Number of consecutive failures before the circuit opens.
    recovery_timeout:
        Seconds to wait before transitioning from OPEN to HALF_OPEN
        (allowing a probe request through).
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._circuits: dict[str, _TeamCircuit] = {}
        self._lock = threading.Lock()

    def _get_circuit(self, team_key: str) -> _TeamCircuit:
        if team_key not in self._circuits:
            self._circuits[team_key] = _TeamCircuit()
        return self._circuits[team_key]

    def is_open(self, team_key: str) -> bool:
        """Return True if requests to this team should be rejected.

        Transitions OPEN → HALF_OPEN after recovery_timeout.
        """
        with self._lock:
            circuit = self._get_circuit(team_key)
            if circuit.state == CircuitState.CLOSED:
                return False
            if circuit.state == CircuitState.OPEN:
                elapsed = time.monotonic() - circuit.last_failure_time
                if elapsed >= self.recovery_timeout:
                    circuit.state = CircuitState.HALF_OPEN
                    logger.info(
                        "Circuit breaker for %s: OPEN -> HALF_OPEN (%.1fs since last failure)",
                        team_key,
                        elapsed,
                    )
                    return False  # Allow one probe request
                return True
            # HALF_OPEN: allow the probe request through
            return False

    def get_state(self, team_key: str) -> CircuitState:
        """Return the current circuit state for a team."""
        with self._lock:
            return self._get_circuit(team_key).state

    def record_success(self, team_key: str) -> None:
        """Record a successful request. Resets the circuit to CLOSED."""
        with self._lock:
            circuit = self._get_circuit(team_key)
            if circuit.state != CircuitState.CLOSED:
                logger.info(
                    "Circuit breaker for %s: %s -> CLOSED (success after %d failures)",
                    team_key,
                    circuit.state.value,
                    circuit.consecutive_failures,
                )
            circuit.state = CircuitState.CLOSED
            circuit.consecutive_failures = 0
            circuit.last_success_time = time.monotonic()

    def record_failure(self, team_key: str) -> None:
        """Record a failed request. Opens the circuit if threshold is exceeded."""
        with self._lock:
            circuit = self._get_circuit(team_key)
            circuit.consecutive_failures += 1
            circuit.last_failure_time = time.monotonic()
            if circuit.state == CircuitState.HALF_OPEN:
                # Probe failed — go back to OPEN
                circuit.state = CircuitState.OPEN
                logger.warning(
                    "Circuit breaker for %s: HALF_OPEN -> OPEN (probe failed, %d consecutive failures)",
                    team_key,
                    circuit.consecutive_failures,
                )
            elif circuit.consecutive_failures >= self.failure_threshold:
                if circuit.state != CircuitState.OPEN:
                    logger.warning(
                        "Circuit breaker for %s: CLOSED -> OPEN (%d consecutive failures)",
                        team_key,
                        circuit.consecutive_failures,
                    )
                circuit.state = CircuitState.OPEN

    def get_all_states(self) -> dict[str, str]:
        """Return the circuit state for all tracked teams."""
        with self._lock:
            return {k: v.state.value for k, v in self._circuits.items()}
