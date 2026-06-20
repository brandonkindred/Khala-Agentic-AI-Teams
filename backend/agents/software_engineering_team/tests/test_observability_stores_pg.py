"""Postgres-backed round-trip tests for the SE observability stores.

Skipped unless run with ``-m integration`` against a live Postgres.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def _schema():
    from shared_postgres import get_conn, is_postgres_enabled, register_team_schemas

    if not is_postgres_enabled():
        pytest.skip("Postgres not configured")
    from software_engineering_team.postgres import SCHEMA

    register_team_schemas(SCHEMA)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE se_learnings, se_events, se_agent_traces")
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE se_learnings, se_events, se_agent_traces")


def test_learnings_upsert_dedup_and_retrieve(_schema) -> None:
    from software_engineering_team.shared import learnings_store as ls

    assert ls.upsert_learning(
        pattern="security rejection", trigger="hardcoded api key", counter_measure="use env"
    )
    # Same fingerprint → occurrences bump, not a new row.
    assert ls.upsert_learning(
        pattern="Security Rejection", trigger="Hardcoded API key", counter_measure="use vault"
    )
    assert ls.count_learnings() == 1

    hits = ls.retrieve_learnings("hardcoded api key handling")
    assert len(hits) == 1
    assert hits[0].occurrences == 2
    assert hits[0].counter_measure == "use vault"  # refreshed on upsert


def test_learnings_category_filter(_schema) -> None:
    from software_engineering_team.shared import learnings_store as ls

    ls.upsert_learning(pattern="qa flake", trigger="timing flaky test", category="qa")
    ls.upsert_learning(pattern="sec issue", trigger="flaky injection", category="security")
    qa_only = ls.retrieve_learnings("flaky", category="qa")
    assert [h.category for h in qa_only] == ["qa"]


def test_events_roundtrip_and_dora(_schema) -> None:
    from software_engineering_team.metrics.dora import compute_dora
    from software_engineering_team.shared import se_events

    assert se_events.record_event(se_events.MERGE_TO_MAIN, job_id="j1")
    assert se_events.record_event(se_events.TASK_CREATED, job_id="j1", task_id="t1")
    assert se_events.record_event(se_events.TASK_MERGED, job_id="j1", task_id="t1")

    rows = se_events.fetch_events_since(datetime.now(tz=timezone.utc) - timedelta(days=1))
    assert len(rows) == 3

    m = compute_dora(30.0)
    assert m.deployment_count == 1
    assert m.merged_count == 1


def test_trace_write_and_cost(_schema, monkeypatch) -> None:
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")
    from software_engineering_team.shared import trace_store

    class _Rec:
        timestamp = datetime.now(tz=timezone.utc).timestamp()
        team = "software_engineering"
        agent_key = "backend"
        job_id = "j9"
        task_id = "t1"
        phase = "execution"
        model = "deepseek-v4-pro:cloud"
        prompt_tokens = 1000
        completion_tokens = 500
        total_tokens = 1500
        cost_usd = 0.42
        latency_ms = 1200
        status = "success"
        outcome = "success"
        objective = "write code"
        request_id = "rid1"

    assert trace_store.write_trace(_Rec()) is True
    summary = trace_store.fetch_cost_since(datetime.now(tz=timezone.utc) - timedelta(days=1))
    assert summary["total_cost_usd"] == pytest.approx(0.42)
    assert summary["by_job"]["j9"] == pytest.approx(0.42)
