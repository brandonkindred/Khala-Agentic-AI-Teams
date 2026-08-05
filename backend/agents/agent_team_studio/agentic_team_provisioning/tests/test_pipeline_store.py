"""Store-level tests for pipeline-run persistence, CAS, heartbeat, and reaping.

Exercises the real ``AgenticTestStore`` SQL against the shared dict-backed
``_fake_postgres`` fake (extended to model ``agentic_test_pipeline_runs``), so the
compare-and-swap and staleness logic added for WAIT-state restart reliability is
covered without a live Postgres.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_team_studio.agentic_team_provisioning.testing.store import AgenticTestStore
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def _seed_team(db: dict, team_id: str = "t1") -> None:
    now = datetime.now(tz=timezone.utc)
    db["teams"][team_id] = {
        "team_id": team_id,
        "name": "T",
        "description": "",
        "created_at": now,
        "updated_at": now,
    }


def test_create_get_update_list_roundtrip(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()

    created = store.create_pipeline_run("r1", "t1", "p1", initial_input="hi")
    assert created["status"] == "running"

    got = store.get_pipeline_run("r1")
    assert got["initial_input"] == "hi"
    assert got["step_results"] == []

    assert store.update_pipeline_run("r1", status="waiting_for_input", human_prompt="?") is True
    assert store.get_pipeline_run("r1")["status"] == "waiting_for_input"

    # step_results goes through the Json wrapper path.
    assert store.update_pipeline_run("r1", step_results=[{"step_id": "s1"}]) is True
    assert store.get_pipeline_run("r1")["step_results"] == [{"step_id": "s1"}]

    runs = store.list_pipeline_runs("t1")
    assert [r["run_id"] for r in runs] == ["r1"]

    assert store.get_pipeline_run("missing") is None
    assert store.update_pipeline_run("r1") is False  # no fields -> no-op


def test_try_resume_is_a_compare_and_swap(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")  # heartbeat_at seeded fresh at create

    # Not waiting yet -> CAS loses.
    assert store.try_resume_pipeline_run("r1", "answer", 3600) is False

    store.update_pipeline_run("r1", status="waiting_for_input")
    assert store.try_resume_pipeline_run("r1", "answer", 3600) is True
    row = store.get_pipeline_run("r1")
    assert row["status"] == "running"
    assert row["human_prompt"] is None
    assert store.get_pipeline_status("r1")["human_input"] == "answer"

    # Second resume loses (already left waiting_for_input).
    assert store.try_resume_pipeline_run("r1", "again", 3600) is False


def test_try_resume_rejects_stale_orphan(fake_pg: dict) -> None:
    """A waiting run whose heartbeat has gone stale is not resumable (its worker died);
    resume refuses it just as the reaper would fail it, so submit surfaces a 409."""
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    store.update_pipeline_run(
        "r1",
        status="waiting_for_input",
        heartbeat_at=datetime.now(tz=timezone.utc) - timedelta(seconds=120),
    )
    assert store.try_resume_pipeline_run("r1", "answer", 30) is False
    assert store.get_pipeline_run("r1")["status"] == "waiting_for_input"

    # NULL heartbeat (pre-feature / never heartbeated) is likewise not resumable.
    store.update_pipeline_run("r1", heartbeat_at=None)
    assert store.try_resume_pipeline_run("r1", "answer", 30) is False


def test_try_resume_temporal_is_a_compare_and_swap_without_heartbeat_guard(fake_pg: dict) -> None:
    """The Temporal resume CAS flips waiting -> running + persists the input, wins for
    exactly one caller, ignores heartbeat freshness (Temporal owns liveness), and never
    revives a terminal run."""
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1", temporal_owned=True)

    # Not waiting yet -> CAS loses.
    assert store.try_resume_pipeline_run_temporal("r1", "answer") is False

    # Waiting with a STALE heartbeat is still resumable (no freshness guard).
    store.update_pipeline_run(
        "r1",
        status="waiting_for_input",
        heartbeat_at=datetime.now(tz=timezone.utc) - timedelta(seconds=3600),
    )
    assert store.try_resume_pipeline_run_temporal("r1", "answer") is True
    row = store.get_pipeline_run("r1")
    assert row["status"] == "running"
    assert row["human_prompt"] is None
    assert store.get_pipeline_status("r1")["human_input"] == "answer"

    # Second resume loses (already left waiting_for_input).
    assert store.try_resume_pipeline_run_temporal("r1", "again") is False

    # A cancelled run cannot be revived by resume.
    store.create_pipeline_run("r2", "t1", "p1", temporal_owned=True)
    store.update_pipeline_run("r2", status="waiting_for_input")
    store.try_cancel_pipeline_run("r2")
    assert store.try_resume_pipeline_run_temporal("r2", "answer") is False
    assert store.get_pipeline_run("r2")["status"] == "cancelled"


def test_try_expire_is_a_compare_and_swap(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")

    assert store.try_expire_pipeline_run("r1", "wait_timeout: x") is False  # not waiting

    store.update_pipeline_run("r1", status="waiting_for_input")
    assert store.try_expire_pipeline_run("r1", "wait_timeout: x") is True
    row = store.get_pipeline_run("r1")
    assert row["status"] == "failed"
    assert row["error"] == "wait_timeout: x"
    assert row["finished_at"] is not None

    # Resume must lose against an expired run.
    assert store.try_resume_pipeline_run("r1", "late", 3600) is False


def test_try_complete_only_from_running(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")  # status='running'

    assert store.try_complete_pipeline_run("r1", [{"step_id": "s1"}]) is True
    row = store.get_pipeline_run("r1")
    assert row["status"] == "completed"
    assert row["finished_at"] is not None

    # A run already terminal (e.g. cancelled) is not clobbered back to completed.
    store.create_pipeline_run("r2", "t1", "p1")
    store.update_pipeline_run("r2", status="cancelled")
    assert store.try_complete_pipeline_run("r2", []) is False
    assert store.get_pipeline_run("r2")["status"] == "cancelled"


def test_try_cancel_only_from_active(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")  # running
    assert store.try_cancel_pipeline_run("r1") is True
    assert store.get_pipeline_run("r1")["status"] == "cancelled"

    store.create_pipeline_run("r2", "t1", "p1")
    store.update_pipeline_run("r2", status="completed")
    assert store.try_cancel_pipeline_run("r2") is False  # terminal -> no clobber
    assert store.get_pipeline_run("r2")["status"] == "completed"


def test_try_fail_only_from_active(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")  # running
    assert store.try_fail_pipeline_run("r1", "boom") is True
    row = store.get_pipeline_run("r1")
    assert row["status"] == "failed"
    assert row["error"] == "boom"

    # An already-terminal run (cancelled) is not clobbered to failed.
    store.create_pipeline_run("r2", "t1", "p1")
    store.update_pipeline_run("r2", status="cancelled")
    assert store.try_fail_pipeline_run("r2", "boom") is False
    assert store.get_pipeline_run("r2")["status"] == "cancelled"


def test_get_pipeline_status(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    # Fresh run: running, no answer yet.
    assert store.get_pipeline_status("r1") == {"status": "running", "human_input": ""}
    assert store.get_pipeline_status("missing") is None

    store.update_pipeline_run("r1", status="waiting_for_input")
    assert store.try_resume_pipeline_run("r1", "the answer", 3600) is True
    # After resume the status read carries the persisted answer (no second SELECT).
    assert store.get_pipeline_status("r1") == {"status": "running", "human_input": "the answer"}


def test_advance_pipeline_step_only_when_running(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")  # running
    assert store.advance_pipeline_step("r1", "s1") is True
    assert store.get_pipeline_run("r1")["current_step_id"] == "s1"

    # A terminal run is not advanced (signals the executor to stop).
    store.update_pipeline_run("r1", status="cancelled")
    assert store.advance_pipeline_step("r1", "s2") is False
    assert store.get_pipeline_run("r1")["current_step_id"] == "s1"


def test_heartbeat_only_touches_active_runs(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")

    store.heartbeat_pipeline_run("r1")
    assert store.get_pipeline_run("r1")["heartbeat_at"] is not None

    store.update_pipeline_run("r1", status="completed", heartbeat_at=None)
    store.heartbeat_pipeline_run("r1")  # terminal -> no-op
    assert store.get_pipeline_run("r1")["heartbeat_at"] is None


def test_reap_orphaned_runs_by_staleness(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    fresh = datetime.now(tz=timezone.utc)
    stale = fresh - timedelta(seconds=3600)

    store.create_pipeline_run("live", "t1", "p1")
    store.update_pipeline_run("live", status="waiting_for_input", heartbeat_at=fresh)
    store.create_pipeline_run("orphan", "t1", "p1")
    store.update_pipeline_run("orphan", status="running", heartbeat_at=stale)
    store.create_pipeline_run("null_hb", "t1", "p1")
    store.update_pipeline_run("null_hb", status="waiting_for_input", heartbeat_at=None)
    store.create_pipeline_run("done", "t1", "p1")
    store.update_pipeline_run("done", status="completed", heartbeat_at=None)

    reaped = store.reap_orphaned_pipeline_runs("orphaned: restart", stale_seconds=30)

    assert reaped == 2
    assert store.get_pipeline_run("live")["status"] == "waiting_for_input"
    assert store.get_pipeline_run("orphan")["status"] == "failed"
    assert store.get_pipeline_run("orphan")["error"] == "orphaned: restart"
    assert store.get_pipeline_run("null_hb")["status"] == "failed"
    assert store.get_pipeline_run("done")["status"] == "completed"


def test_reap_rejects_nonpositive_stale_seconds(fake_pg: dict) -> None:
    store = AgenticTestStore()
    with pytest.raises(AssertionError):
        store.reap_orphaned_pipeline_runs("x", stale_seconds=0)


def test_reaper_skips_temporal_owned_runs(fake_pg: dict) -> None:
    """A Temporal-owned run has no heartbeat thread — Temporal owns its recovery, so
    the DB reaper must never fail it on staleness, while a stale thread-owned run in the
    same sweep is still reaped."""
    _seed_team(fake_pg)
    store = AgenticTestStore()
    stale = datetime.now(tz=timezone.utc) - timedelta(seconds=3600)

    store.create_pipeline_run("temporal", "t1", "p1", temporal_owned=True)
    store.update_pipeline_run("temporal", status="running", heartbeat_at=stale)
    store.create_pipeline_run("thread", "t1", "p1")  # temporal_owned defaults False
    store.update_pipeline_run("thread", status="running", heartbeat_at=stale)

    reaped = store.reap_orphaned_pipeline_runs("orphaned: restart", stale_seconds=30)

    assert reaped == 1
    assert store.get_pipeline_run("temporal")["status"] == "running"  # untouched
    assert store.get_pipeline_run("thread")["status"] == "failed"


def test_temporal_owned_flag_persists_and_defaults(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()

    store.create_pipeline_run("owned", "t1", "p1", temporal_owned=True)
    store.create_pipeline_run("plain", "t1", "p1")

    assert store.is_pipeline_run_temporal_owned("owned") is True
    assert store.is_pipeline_run_temporal_owned("plain") is False
    assert store.is_pipeline_run_temporal_owned("missing") is False
