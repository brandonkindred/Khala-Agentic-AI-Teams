"""White-box tests for ``_fake_postgres``'s own SQL dispatch table.

Store-level tests (``test_pipeline_store.py``, ``test_team_agents.py``, etc.)
exercise the fake through the real store classes, which can't observe
``cur.rowcount`` for handlers no current caller reads, and can't prove matcher
predicates are mutually exclusive independent of the ``_dispatch()`` list's
registration order without editing production code. These tests go straight at
the dispatch table and cursor instead: matcher functions are pulled out by
their handler's ``__name__`` and called directly with hand-normalized SQL, and
handlers are exercised via a directly-constructed ``FakeCursor`` so rowcount
can be asserted regardless of whether any store method currently surfaces it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import _default_db, _dispatch
from shared.postgres.fake import FakeCursor


def _matchers() -> dict:
    return {handler.__name__: matcher for matcher, handler in _dispatch()}


def _cursor() -> FakeCursor:
    return FakeCursor(_default_db(), _dispatch())


# -- matcher mutual exclusivity ------------------------------------------------


def test_match_advisory_anchored_to_lock_call():
    matchers = _matchers()
    advisory = matchers["handle_advisory"]

    assert advisory("select pg_try_advisory_xact_lock(%s)") is True
    assert advisory("select pg_advisory_lock(%s)") is True
    assert (
        advisory(
            "update agentic_test_pipeline_runs set status = 'failed' "
            "where run_id = %s and error = 'advisory review needed'"
        )
        is False
    )


def test_expire_and_fail_active_are_mutually_exclusive():
    matchers = _matchers()
    fail_active = matchers["handle_fail_pipeline_active"]
    expire = matchers["handle_expire_pipeline"]

    fail_active_sql = (
        "update agentic_test_pipeline_runs set status = 'failed', error = %s, finished_at = %s "
        "where run_id = %s and status in ('running', 'waiting_for_input')"
    )
    expire_sql = (
        "update agentic_test_pipeline_runs set status = 'failed', error = %s, finished_at = %s "
        "where run_id = %s and status = 'waiting_for_input'"
    )

    assert fail_active(fail_active_sql) is True
    assert expire(fail_active_sql) is False
    assert expire(expire_sql) is True
    assert fail_active(expire_sql) is False


def test_get_and_list_pipeline_run_are_mutually_exclusive():
    matchers = _matchers()
    get_run = matchers["handle_get_pipeline_run"]
    list_runs = matchers["handle_list_pipeline_runs"]

    get_sql = (
        "select run_id, team_id, process_id, status, current_step_id "
        "from agentic_test_pipeline_runs where run_id = %s"
    )
    list_sql = (
        "select run_id, team_id, process_id, status, current_step_id "
        "from agentic_test_pipeline_runs where team_id = %s order by started_at desc limit %s"
    )

    assert get_run(get_sql) is True
    assert list_runs(get_sql) is False
    assert list_runs(list_sql) is True
    assert get_run(list_sql) is False


def test_resume_and_resume_temporal_are_mutually_exclusive():
    matchers = _matchers()
    resume = matchers["handle_resume_pipeline"]
    resume_temporal = matchers["handle_resume_pipeline_temporal"]

    resume_sql = (
        "update agentic_test_pipeline_runs set status = 'running', human_prompt = null, "
        "human_input = %s, heartbeat_at = %s where run_id = %s and status = 'waiting_for_input' "
        "and heartbeat_at is not null and heartbeat_at >= %s"
    )
    resume_temporal_sql = (
        "update agentic_test_pipeline_runs set status = 'running', human_prompt = null, "
        "human_input = %s where run_id = %s and status = 'waiting_for_input'"
    )

    assert resume(resume_sql) is True
    assert resume_temporal(resume_sql) is False
    assert resume_temporal(resume_temporal_sql) is True
    assert resume(resume_temporal_sql) is False


def test_upsert_and_insert_team_agent_are_mutually_exclusive():
    """Plain INSERT and ON CONFLICT upsert matchers must not both match upsert SQL.

    Preconditions: ``_dispatch()`` exposes ``handle_upsert_team_agent`` and
        ``handle_insert_team_agent`` matchers.
    Postconditions: upsert SQL matches only the upsert matcher; plain INSERT
        SQL matches only the insert matcher.
    """
    matchers = _matchers()
    upsert = matchers["handle_upsert_team_agent"]
    insert = matchers["handle_insert_team_agent"]

    upsert_sql = (
        "insert into agentic_team_agents "
        "(team_id, agent_name, data_json, created_at, updated_at) "
        "values (%s, %s, %s, %s, %s) "
        "on conflict (team_id, agent_name) do update set "
        "data_json = excluded.data_json, updated_at = excluded.updated_at"
    )
    insert_sql = (
        "insert into agentic_team_agents "
        "(team_id, agent_name, data_json, created_at, updated_at) "
        "values (%s, %s, %s, %s, %s)"
    )

    assert upsert(upsert_sql) is True
    assert insert(upsert_sql) is False
    assert insert(insert_sql) is True
    assert upsert(insert_sql) is False


def test_delete_team_agent_and_prune_are_mutually_exclusive():
    """Single-row delete (RETURNING) and full-roster prune must not both match.

    Preconditions: ``_dispatch()`` exposes ``handle_delete_team_agent`` and
        ``handle_prune_team_agents`` matchers.
    Postconditions: the single-row delete SQL matches only the single-row
        matcher; the prune SQL matches only the prune matcher.
    """
    matchers = _matchers()
    delete_one = matchers["handle_delete_team_agent"]
    prune = matchers["handle_prune_team_agents"]

    delete_sql = (
        "delete from agentic_team_agents where team_id = %s and agent_name = %s returning data_json"
    )
    prune_sql = "delete from agentic_team_agents where team_id = %s and agent_name <> all(%s)"

    assert delete_one(delete_sql) is True
    assert prune(delete_sql) is False
    assert prune(prune_sql) is True
    assert delete_one(prune_sql) is False


# -- rowcount fidelity ----------------------------------------------------------


def test_update_env_provision_status_rowcount():
    cur = _cursor()
    now = datetime.now(tz=timezone.utc)
    cur.db["env_provisions"][("t1", "key1")] = {
        "team_id": "t1",
        "stable_key": "key1",
        "status": "running",
        "error_message": None,
        "updated_at": now,
    }

    cur.execute(
        "UPDATE agentic_env_provisions SET "
        "status = %s, error_message = %s, updated_at = %s "
        "WHERE team_id = %s AND stable_key = %s",
        ("completed", None, now, "t1", "key1"),
    )
    assert cur.rowcount == 1
    assert cur.db["env_provisions"][("t1", "key1")]["status"] == "completed"

    cur.execute(
        "UPDATE agentic_env_provisions SET "
        "status = %s, error_message = %s, updated_at = %s "
        "WHERE team_id = %s AND stable_key = %s",
        ("completed", None, now, "t1", "missing-key"),
    )
    assert cur.rowcount == 0


def test_insert_conversation_sets_rowcount_one():
    cur = _cursor()
    now = datetime.now(tz=timezone.utc)

    cur.execute(
        "INSERT INTO agentic_conversations (conversation_id, team_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s)",
        ("c1", "t1", now, now),
    )
    assert cur.rowcount == 1
    assert cur.db["conversations"]["c1"]["team_id"] == "t1"


def test_update_conv_process_id_rowcount_found_and_missing():
    cur = _cursor()
    now = datetime.now(tz=timezone.utc)
    cur.db["conversations"]["c1"] = {
        "conversation_id": "c1",
        "team_id": "t1",
        "process_id": None,
        "created_at": now,
        "updated_at": now,
    }

    cur.execute(
        "UPDATE agentic_conversations SET process_id = %s, updated_at = %s WHERE conversation_id = %s",
        ("p1", now, "c1"),
    )
    assert cur.rowcount == 1
    assert cur.db["conversations"]["c1"]["process_id"] == "p1"

    cur.execute(
        "UPDATE agentic_conversations SET process_id = %s, updated_at = %s WHERE conversation_id = %s",
        ("p1", now, "missing"),
    )
    assert cur.rowcount == 0


def test_update_conv_updated_at_rowcount_found_and_missing():
    cur = _cursor()
    now = datetime.now(tz=timezone.utc)
    cur.db["conversations"]["c1"] = {
        "conversation_id": "c1",
        "team_id": "t1",
        "process_id": None,
        "created_at": now,
        "updated_at": now,
    }

    cur.execute(
        "UPDATE agentic_conversations SET updated_at = %s WHERE conversation_id = %s",
        (now, "c1"),
    )
    assert cur.rowcount == 1

    cur.execute(
        "UPDATE agentic_conversations SET updated_at = %s WHERE conversation_id = %s",
        (now, "missing"),
    )
    assert cur.rowcount == 0


def test_insert_conv_message_sets_rowcount_one():
    cur = _cursor()
    now = datetime.now(tz=timezone.utc)

    cur.execute(
        "INSERT INTO agentic_conv_messages (conversation_id, role, content, timestamp) "
        "VALUES (%s, %s, %s, %s)",
        ("c1", "user", "hi", now),
    )
    assert cur.rowcount == 1
    assert len(cur.db["conv_messages"]) == 1


def test_prune_team_agents_rowcount_is_removed_count():
    cur = _cursor()
    cur.db["team_agents"][("t1", "a1")] = {"agent_name": "a1"}
    cur.db["team_agents"][("t1", "a2")] = {"agent_name": "a2"}
    cur.db["team_agents"][("t1", "a3")] = {"agent_name": "a3"}

    cur.execute(
        "DELETE FROM agentic_team_agents WHERE team_id = %s AND agent_name <> ALL(%s)",
        ("t1", ["a1"]),
    )
    assert cur.rowcount == 2
    assert set(cur.db["team_agents"].keys()) == {("t1", "a1")}


def test_delete_team_agents_rowcount_is_removed_count():
    cur = _cursor()
    cur.db["team_agents"][("t1", "a1")] = {"agent_name": "a1"}
    cur.db["team_agents"][("t1", "a2")] = {"agent_name": "a2"}
    cur.db["team_agents"][("t2", "a1")] = {"agent_name": "a1"}

    cur.execute("DELETE FROM agentic_team_agents WHERE team_id = %s", ("t1",))
    assert cur.rowcount == 2
    assert set(cur.db["team_agents"].keys()) == {("t2", "a1")}
