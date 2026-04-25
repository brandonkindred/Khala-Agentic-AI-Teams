"""Unit tests for ``user_agent_founder.store`` (PR 3, Postgres backend).

Same pattern as ``startup_advisor/tests/test_store.py``: a small
dict-backed fake routes each SQL statement the store issues to an
in-process handler, so the tests verify the real control flow
(INSERT/SELECT/UPDATE, RETURNING id, dynamic SET clause ordering)
without needing a live Postgres. Integration coverage against a real
``postgres:18`` service container runs in the ``test-shared-postgres``
CI job from PR 0.
"""

from __future__ import annotations

import itertools
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Minimal fake Postgres cursor
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, db: dict[str, Any], ids: itertools.count) -> None:
        self._db = db
        self._ids = ids
        self.rowcount = 0
        self._last_fetch_one: tuple | dict | None = None
        self._last_fetch_all: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, params: tuple | list = ()) -> None:
        sql_l = " ".join(sql.split()).lower()
        params = tuple(params)

        # INSERT into runs
        if sql_l.startswith("insert into user_agent_founder_runs"):
            run_id, status, target_team_key, created_at, updated_at = params
            self._db["runs"][run_id] = {
                "run_id": run_id,
                "status": status,
                "se_job_id": None,
                "analysis_job_id": None,
                "spec_content": None,
                "repo_path": None,
                "target_team_key": target_team_key,
                "created_at": created_at,
                "updated_at": updated_at,
                "error": None,
            }
            self.rowcount = 1
            return

        # SELECT full row FROM runs WHERE run_id
        if sql_l.startswith("select run_id, status, se_job_id") and "where run_id = %s" in sql_l:
            (run_id,) = params
            self._last_fetch_one = self._db["runs"].get(run_id)
            return

        # SELECT full row FROM runs ORDER BY created_at DESC (list_runs)
        if (
            sql_l.startswith("select run_id, status, se_job_id")
            and "order by created_at desc" in sql_l
        ):
            self._last_fetch_all = sorted(
                self._db["runs"].values(),
                key=lambda r: r["created_at"],
                reverse=True,
            )
            return

        # UPDATE runs — dynamic SET clause
        if sql_l.startswith("update user_agent_founder_runs set "):
            # Parse the column list out of the SQL to reconstruct the
            # column→value mapping in the order the store issued them.
            # Format: UPDATE ... SET col1 = %s, col2 = %s, updated_at = %s WHERE run_id = %s
            set_part = sql[sql.lower().index(" set ") + 5 : sql.lower().rindex(" where ")]
            cols = [c.strip().split(" ")[0] for c in set_part.split(",")]
            run_id = params[-1]
            col_values = dict(zip(cols, params[:-1], strict=True))
            conv = self._db["runs"].get(run_id)
            if conv is None:
                self.rowcount = 0
                return
            conv.update(col_values)
            self.rowcount = 1
            return

        # INSERT decision ... RETURNING id
        if sql_l.startswith("insert into user_agent_founder_decisions"):
            run_id, qid, qtext, atext, rationale, ts = params
            dec_id = next(self._ids)
            self._db["decisions"].append(
                {
                    "id": dec_id,
                    "run_id": run_id,
                    "question_id": qid,
                    "question_text": qtext,
                    "answer_text": atext,
                    "rationale": rationale,
                    "timestamp": ts,
                }
            )
            self._last_fetch_one = (dec_id,)
            self.rowcount = 1
            return

        # SELECT decisions WHERE run_id ORDER BY id
        if sql_l.startswith("select id, run_id, question_id"):
            (run_id,) = params
            rows = [dict(d) for d in self._db["decisions"] if d["run_id"] == run_id]
            self._last_fetch_all = rows
            return

        raise AssertionError(f"unexpected SQL in fake cursor: {sql!r}")

    def fetchone(self):
        return self._last_fetch_one

    def fetchall(self):
        return self._last_fetch_all


class _FakeConn:
    def __init__(self, db: dict[str, Any], ids: itertools.count) -> None:
        self._db = db
        self._ids = ids

    def cursor(self, row_factory=None):  # noqa: ANN001
        return _FakeCursor(self._db, self._ids)


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch):
    db: dict[str, Any] = {"runs": {}, "decisions": []}
    ids = itertools.count(1)

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db, ids)

    import user_agent_founder.store as store_mod

    monkeypatch.setattr(store_mod, "get_conn", _fake_get_conn)
    yield db


@pytest.fixture
def store(fake_pg):
    from user_agent_founder.store import FounderRunStore

    return FounderRunStore()


# ---------------------------------------------------------------------------
# create_run / get_run / list_runs
# ---------------------------------------------------------------------------


def test_create_run_returns_uuid_and_inserts_pending_row(store, fake_pg):
    run_id = store.create_run()
    assert run_id in fake_pg["runs"]
    row = fake_pg["runs"][run_id]
    assert row["status"] == "pending"
    assert row["se_job_id"] is None
    assert row["spec_content"] is None
    assert row["target_team_key"] == "software_engineering"
    assert isinstance(row["created_at"], datetime)
    assert row["created_at"].tzinfo is timezone.utc


def test_create_run_persists_explicit_target_team_key(store, fake_pg):
    run_id = store.create_run(target_team_key="some_other_team")
    assert fake_pg["runs"][run_id]["target_team_key"] == "some_other_team"


def test_get_run_exposes_target_team_key(store):
    run_id = store.create_run(target_team_key="software_engineering")
    run = store.get_run(run_id)
    assert run is not None
    assert run.target_team_key == "software_engineering"


def test_update_run_can_change_target_team_key(store, fake_pg):
    run_id = store.create_run()
    assert store.update_run(run_id, target_team_key="new_team") is True
    assert fake_pg["runs"][run_id]["target_team_key"] == "new_team"


def test_get_run_returns_stored_run_dataclass(store):
    run_id = store.create_run()
    run = store.get_run(run_id)
    assert run is not None
    assert run.run_id == run_id
    assert run.status == "pending"
    assert isinstance(run.created_at, str)
    assert isinstance(run.updated_at, str)
    # String timestamp means the store converted the datetime for us.
    assert "T" in run.created_at


def test_get_run_returns_none_for_unknown(store):
    assert store.get_run("nope") is None


def test_list_runs_orders_by_created_at_desc(store):
    first = store.create_run()
    second = store.create_run()
    rows = store.list_runs()
    # Most recently created first.
    assert [r.run_id for r in rows[:2]] == [second, first]


# ---------------------------------------------------------------------------
# update_run — whitelisting, SET clause construction, timestamp bump
# ---------------------------------------------------------------------------


def test_update_run_returns_false_when_no_kwargs(store):
    run_id = store.create_run()
    assert store.update_run(run_id) is False


def test_update_run_returns_false_when_only_unknown_keys(store):
    run_id = store.create_run()
    assert store.update_run(run_id, total_rubbish="x", also_bad=1) is False


def test_update_run_ignores_unknown_keys_but_writes_allowed_ones(store, fake_pg):
    run_id = store.create_run()
    assert store.update_run(run_id, status="running", not_a_real_field="ignored") is True
    row = fake_pg["runs"][run_id]
    assert row["status"] == "running"
    assert "not_a_real_field" not in row


def test_update_run_bumps_updated_at(store, fake_pg):
    run_id = store.create_run()
    original_updated = fake_pg["runs"][run_id]["updated_at"]
    store.update_run(run_id, status="running")
    new_updated = fake_pg["runs"][run_id]["updated_at"]
    assert new_updated >= original_updated


def test_update_run_returns_false_when_run_missing(store):
    assert store.update_run("missing-run", status="complete") is False


def test_update_run_writes_multiple_columns_in_one_query(store, fake_pg):
    run_id = store.create_run()
    assert (
        store.update_run(
            run_id,
            status="running",
            se_job_id="se-1",
            spec_content="spec text",
        )
        is True
    )
    row = fake_pg["runs"][run_id]
    assert row["status"] == "running"
    assert row["se_job_id"] == "se-1"
    assert row["spec_content"] == "spec text"


def test_update_run_rejects_sql_injection_via_kwargs_key(store, fake_pg):
    """Unknown keys (including injection attempts) are dropped silently.

    The whitelist is the only thing standing between a caller and raw
    SQL column interpolation, so this test locks the contract.
    """
    run_id = store.create_run()
    payload = {"status; DROP TABLE user_agent_founder_runs; --": "nope"}
    # The bad key is filtered out, leaving zero allowed keys -> returns False.
    assert store.update_run(run_id, **payload) is False
    # Table still has the row.
    assert run_id in fake_pg["runs"]


# ---------------------------------------------------------------------------
# add_decision / get_decisions
# ---------------------------------------------------------------------------


def test_add_decision_returns_id_and_persists_row(store, fake_pg):
    run_id = store.create_run()
    dec_id = store.add_decision(
        run_id,
        question_id="q1",
        question_text="pick a language",
        answer_text="python",
        rationale="team familiarity",
    )
    assert isinstance(dec_id, int)
    assert len(fake_pg["decisions"]) == 1
    stored = fake_pg["decisions"][0]
    assert stored["run_id"] == run_id
    assert stored["question_id"] == "q1"
    assert stored["answer_text"] == "python"


def test_get_decisions_returns_in_insert_order(store):
    run_id = store.create_run()
    store.add_decision(run_id, "q1", "first?", "yes", "r1")
    store.add_decision(run_id, "q2", "second?", "no", "r2")
    store.add_decision(run_id, "q3", "third?", "maybe", "r3")

    decisions = store.get_decisions(run_id)
    assert [d.question_id for d in decisions] == ["q1", "q2", "q3"]
    assert all(isinstance(d.decision_id, int) for d in decisions)
    assert all(isinstance(d.timestamp, str) for d in decisions)


def test_get_decisions_filters_by_run_id(store):
    a = store.create_run()
    b = store.create_run()
    store.add_decision(a, "qa", "?", "a", "")
    store.add_decision(b, "qb", "?", "b", "")
    assert [d.question_id for d in store.get_decisions(a)] == ["qa"]
    assert [d.question_id for d in store.get_decisions(b)] == ["qb"]


def test_get_decisions_returns_empty_for_unknown_run(store):
    assert store.get_decisions("never-existed") == []


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------


def test_get_founder_store_is_lazy_and_cached(fake_pg, monkeypatch):
    import user_agent_founder.store as store_mod

    monkeypatch.setattr(store_mod, "_default_store", None)
    a = store_mod.get_founder_store()
    b = store_mod.get_founder_store()
    assert a is b
