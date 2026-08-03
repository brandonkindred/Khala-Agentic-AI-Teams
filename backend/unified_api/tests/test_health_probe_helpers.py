"""Tests for the pure-logic pieces of unified_api.main's health-probe machinery.

The Postgres-dependent probes themselves (``_probe_postgres_live``,
``_verify_in_process_schema_present``, ``_retry_in_process_schema_registration``,
the ``lifespan`` startup, and the background ``_health_check_loop``) require a
live Postgres/Temporal environment and are marked ``# pragma: no cover`` in
``main.py`` with that rationale; this file covers the parts that don't.
"""

from __future__ import annotations

import sys
from pathlib import Path

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

import pytest
from fastapi.testclient import TestClient

from unified_api import main as unified_main
from unified_api.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# _get_probe_executor / _shutdown_probe_executor
# ---------------------------------------------------------------------------


def test_get_probe_executor_returns_same_instance_until_shutdown() -> None:
    """_get_probe_executor lazily creates one executor and reuses it across calls."""
    unified_main._shutdown_probe_executor()
    first = unified_main._get_probe_executor()
    second = unified_main._get_probe_executor()
    assert first is second
    unified_main._shutdown_probe_executor()


def test_shutdown_probe_executor_allows_recreation() -> None:
    """_shutdown_probe_executor clears the slot so the next call gets a fresh executor."""
    first = unified_main._get_probe_executor()
    unified_main._shutdown_probe_executor()
    assert unified_main._PROBE_EXECUTOR is None
    second = unified_main._get_probe_executor()
    assert second is not first
    unified_main._shutdown_probe_executor()


def test_shutdown_probe_executor_is_idempotent() -> None:
    """Calling _shutdown_probe_executor when nothing was created is a no-op."""
    unified_main._shutdown_probe_executor()
    unified_main._shutdown_probe_executor()
    assert unified_main._PROBE_EXECUTOR is None


# ---------------------------------------------------------------------------
# _expected_tables_for
# ---------------------------------------------------------------------------


def test_expected_tables_for_product_delivery_returns_schema_table_names() -> None:
    """_expected_tables_for('product_delivery') matches its registered SCHEMA.table_names."""
    from product_delivery.postgres import SCHEMA as PRODUCT_DELIVERY_SCHEMA

    tables = unified_main._expected_tables_for("product_delivery")
    assert tables == list(PRODUCT_DELIVERY_SCHEMA.table_names)
    assert tables, "expected product_delivery to declare at least one table"


def test_expected_tables_for_unknown_team_returns_empty_list() -> None:
    """_expected_tables_for returns [] for teams with no declared table-presence check."""
    assert unified_main._expected_tables_for("blogging") == []
    assert unified_main._expected_tables_for("_no_such_team") == []


# ---------------------------------------------------------------------------
# GET /health — proxied (non-in-process) team liveness branches
# ---------------------------------------------------------------------------


@pytest.fixture
def _fake_registered_team(monkeypatch: pytest.MonkeyPatch):
    """Register a fake, non-in-process team in TEAM_CONFIGS + _registered_teams.

    Yields the fake team's key so tests can drive its liveness state; all
    global registries are restored afterward.
    """
    from unified_api.config import TEAM_CONFIGS, TeamConfig

    fake_key = "_test_proxy_liveness_team"
    TEAM_CONFIGS[fake_key] = TeamConfig(name="Fake Proxy", prefix="/api/fake-proxy", description="test", enabled=True)
    unified_main._registered_teams[fake_key] = True
    try:
        yield fake_key
    finally:
        del TEAM_CONFIGS[fake_key]
        unified_main._registered_teams.pop(fake_key, None)
        unified_main._team_liveness.pop(fake_key, None)


def _team_status(fake_key: str) -> str:
    resp = client.get("/health")
    assert resp.status_code == 200
    team = next(t for t in resp.json()["teams"] if t["name"] == "Fake Proxy")
    return team["status"]


def test_health_reports_healthy_when_registered_and_liveness_healthy(_fake_registered_team: str) -> None:
    """A registered proxied team with liveness 'healthy' reports overall status 'healthy'."""
    unified_main._team_liveness[_fake_registered_team] = "healthy"
    assert _team_status(_fake_registered_team) == "healthy"


def test_health_reports_healthy_when_liveness_not_yet_checked(_fake_registered_team: str) -> None:
    """A registered proxied team with no liveness entry yet is optimistically 'healthy'."""
    unified_main._team_liveness.pop(_fake_registered_team, None)
    assert _team_status(_fake_registered_team) == "healthy"


def test_health_reports_unhealthy_when_registered_and_liveness_unhealthy(_fake_registered_team: str) -> None:
    """A registered proxied team whose last liveness probe failed reports 'unhealthy'."""
    unified_main._team_liveness[_fake_registered_team] = "unhealthy"
    assert _team_status(_fake_registered_team) == "unhealthy"
