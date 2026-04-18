"""Integration test for the retention pruner. Live Postgres required."""

from __future__ import annotations

import pytest

from agent_console.models import RunCreate
from agent_console.postgres import SCHEMA
from agent_console.store import get_store
from shared_postgres import is_postgres_enabled, register_team_schemas
from shared_postgres.testing import truncate_team_tables

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres prune tests",
)


@pytest.fixture(autouse=True)
def _provision_schema() -> None:
    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)
    get_store.cache_clear()


def _run(agent_id: str, idx: int) -> RunCreate:
    return RunCreate(
        agent_id=agent_id,
        team="blogging",
        saved_input_id=None,
        input_data={"idx": idx},
        output_data=None,
        error=None,
        status="ok",
        duration_ms=0,
        trace_id=f"t-{idx}",
        logs_tail=[],
        author="tester",
        sandbox_url=None,
    )


def test_prune_runs_keeps_newest_per_agent() -> None:
    store = get_store()
    for i in range(12):
        store.record_run(_run("agent.a", i))
    for i in range(5):
        store.record_run(_run("agent.b", i))

    deleted = store.prune_runs(keep_per_agent=8)

    # agent.a: 12 rows → keep 8, delete 4. agent.b: 5 rows → keep all.
    assert deleted == 4
    assert len(store.list_runs("agent.a", limit=100)) == 8
    assert len(store.list_runs("agent.b", limit=100)) == 5
    # Newest are kept; the 4 earliest on agent.a are gone.
    remaining = [r.trace_id for r in store.list_runs("agent.a", limit=100)]
    assert "t-11" in remaining
    assert "t-0" not in remaining


def test_prune_is_noop_when_under_threshold() -> None:
    store = get_store()
    for i in range(3):
        store.record_run(_run("agent.c", i))
    assert store.prune_runs(keep_per_agent=10) == 0
    assert len(store.list_runs("agent.c", limit=100)) == 3
