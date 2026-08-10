"""Tests for per-team connection pool configuration in unified_api.team_proxy."""

from __future__ import annotations

import pytest
from unified_api.team_proxy import (
    get_team_client,
    _team_clients,
    DEFAULT_POOL_LIMITS
)

@pytest.fixture(autouse=True)
def _reset_team_clients():
    """Reset the per-team client cache between tests to ensure fresh instantiation."""
    _team_clients.clear()
    yield
    _team_clients.clear()

def test_configured_high_traffic_team():
    """Ensure high-traffic teams map to explicitly raised limits."""
    client = get_team_client("auth_team")
    transport = client._transport

    assert transport._pool._max_connections == 30
    assert transport._pool._max_keepalive_connections == 15

def test_configured_low_traffic_team():
    """Ensure lowest-traffic teams have reduced limits to conserve memory (RSS)."""
    client = get_team_client("reporting_team")
    transport = client._transport

    # Validate exact config match
    assert transport._pool._max_connections == 5
    assert transport._pool._max_keepalive_connections == 2

    # Validate acceptance criteria: reduced below default baseline
    assert transport._pool._max_connections < DEFAULT_POOL_LIMITS["max_connections"]
    assert transport._pool._max_keepalive_connections < DEFAULT_POOL_LIMITS["max_keepalive_connections"]

def test_unconfigured_team_fallback():
    """Ensure missing or newly created teams fall back to the conservative default."""
    client = get_team_client("unknown_experimental_team")
    transport = client._transport

    assert transport._pool._max_connections == DEFAULT_POOL_LIMITS["max_connections"]
    assert transport._pool._max_keepalive_connections == DEFAULT_POOL_LIMITS["max_keepalive_connections"]
