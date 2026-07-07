"""Shared dict-backed fake for ``shared_postgres.get_conn``.

Approximates the Postgres behaviour the agentic_team_provisioning stores
rely on — only as much as tests need. Tests monkey-patch the module-level
``get_conn`` on ``agentic_team_provisioning.assistant.store`` and
``agentic_team_provisioning.infrastructure`` to redirect to this fake.
"""

from __future__ import annotations

import itertools
import re
from contextlib import contextmanager
from typing import Any


def _unwrap_json(value: Any) -> Any:
    """psycopg3's ``Json`` wrapper exposes the dict via ``.obj``."""
    if hasattr(value, "obj"):
        return value.obj
    return value


def _default_db() -> dict[str, Any]:
    return {
        "teams": {},
        "processes": {},
        "conversations": {},
        "conv_messages": [],
        "team_agents": {},  # keyed by (team_id, agent_name)
        "env_provisions": {},  # keyed by (team_id, stable_key)
        "form_data": {},  # keyed by record_id
        "pipeline_runs": {},  # keyed by run_id
    }


class _FakeCursor:
    """A tiny cursor that routes SQL statements to handlers.

    Handles the narrow subset of SQL that the agentic_team_provisioning
    stores issue, plus no-op fallbacks for DDL we don't care about.

    ⚠️ Routing is **tightly coupled to the store's exact query strings**: each
    handler matches on a normalized ``startswith`` / substring of the SQL. Reordering
    clauses, renaming columns, or adding comments in a store query can silently break
    the match — an unmatched statement raises ``AssertionError`` (see the bottom of
    :meth:`execute`), so a drift surfaces as a hard test failure, not a silent pass.
    When you change a store query, update the matching handler here. (A real fix is a
    repository abstraction the store could mock without faking SQL — out of scope.)
    """

    def __init__(self, db: dict[str, Any], ids: itertools.count) -> None:
        self._db = db
        self._ids = ids
        self.rowcount = 0
        self._last_fetch_one: Any = None
        self._last_fetch_all: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        norm = " ".join(sql.split()).lower()

        # -- teams --------------------------------------------------------
        if norm.startswith("insert into agentic_teams"):
            team_id, name, description, created_at, updated_at = params
            self._db["teams"][team_id] = {
                "team_id": team_id,
                "name": name,
                "description": description,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            self.rowcount = 1
            return

        if norm.startswith("update agentic_teams set updated_at"):
            ts, team_id = params
            row = self._db["teams"].get(team_id)
            if row is not None:
                row["updated_at"] = ts
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if norm.startswith(
            "select team_id, name, description, created_at, updated_at from agentic_teams where team_id"
        ):
            (team_id,) = params
            self._last_fetch_one = self._db["teams"].get(team_id)
            return

        # Team-row lock probe used by merge_generated_agents (FOR UPDATE is a no-op
        # in the fake — it only checks existence). Distinct prefix from the full
        # column select above ("select team_id from" vs "select team_id, ...").
        if norm.startswith("select team_id from agentic_teams where team_id"):
            (team_id,) = params
            self._last_fetch_one = (team_id,) if team_id in self._db["teams"] else None
            return

        if "from agentic_teams t" in norm and "order by t.created_at desc" in norm:
            rows = []
            for t in sorted(
                self._db["teams"].values(),
                key=lambda r: r["created_at"],
                reverse=True,
            ):
                process_count = sum(
                    1 for p in self._db["processes"].values() if p["team_id"] == t["team_id"]
                )
                rows.append({**t, "process_count": process_count})
            self._last_fetch_all = rows
            return

        # -- processes ----------------------------------------------------
        if norm.startswith("insert into agentic_processes") and "on conflict" in norm:
            process_id, team_id, data_json, created_at, updated_at = params
            data = _unwrap_json(data_json)
            existing = self._db["processes"].get(process_id)
            if existing:
                existing["data_json"] = data
                existing["updated_at"] = updated_at
            else:
                self._db["processes"][process_id] = {
                    "process_id": process_id,
                    "team_id": team_id,
                    "data_json": data,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            self.rowcount = 1
            return

        if norm.startswith("select data_json from agentic_processes where process_id"):
            (process_id,) = params
            row = self._db["processes"].get(process_id)
            self._last_fetch_one = {"data_json": row["data_json"]} if row else None
            return

        if norm.startswith("select team_id from agentic_processes where process_id"):
            (process_id,) = params
            row = self._db["processes"].get(process_id)
            self._last_fetch_one = (row["team_id"],) if row else None
            return

        if norm.startswith("select data_json from agentic_processes where team_id"):
            (team_id,) = params
            rows = [
                {"data_json": p["data_json"]}
                for p in sorted(
                    (p for p in self._db["processes"].values() if p["team_id"] == team_id),
                    key=lambda p: p["created_at"],
                )
            ]
            self._last_fetch_all = rows
            return

        # -- team_agents --------------------------------------------------
        # Full-roster prune used by _write_team_agents: delete rows whose agent_name
        # is not in the kept set. Must precede the single-row delete (it also matches
        # "agent_name in norm") and the team-wide delete.
        if norm.startswith("delete from agentic_team_agents where team_id") and "<> all" in norm:
            team_id, names = params
            keep = set(names)
            for k in list(self._db["team_agents"].keys()):
                if k[0] == team_id and k[1] not in keep:
                    del self._db["team_agents"][k]
            return

        # Single-row targeted delete (RETURNING the deleted row) — must precede the
        # team-wide delete handler, whose prefix this also matches.
        if (
            norm.startswith("delete from agentic_team_agents where team_id")
            and "agent_name" in norm
        ):
            team_id, agent_name = params
            removed = self._db["team_agents"].pop((team_id, agent_name), None)
            self.rowcount = 1 if removed else 0
            self._last_fetch_one = {"data_json": removed["data_json"]} if removed else None
            return

        if norm.startswith("delete from agentic_team_agents where team_id"):
            (team_id,) = params
            for k in list(self._db["team_agents"].keys()):
                if k[0] == team_id:
                    del self._db["team_agents"][k]
            return

        # Single-row upsert (ON CONFLICT) — preserves the existing created_at, like
        # real Postgres. Must precede the plain INSERT handler.
        if norm.startswith("insert into agentic_team_agents") and "on conflict" in norm:
            team_id, agent_name, data_json, created_at, updated_at = params
            data = _unwrap_json(data_json)
            existing = self._db["team_agents"].get((team_id, agent_name))
            if existing:
                existing["data_json"] = data
                existing["updated_at"] = updated_at
            else:
                self._db["team_agents"][(team_id, agent_name)] = {
                    "team_id": team_id,
                    "agent_name": agent_name,
                    "data_json": data,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            self.rowcount = 1
            return

        if norm.startswith("insert into agentic_team_agents"):
            team_id, agent_name, data_json, created_at, updated_at = params
            data = _unwrap_json(data_json)
            self._db["team_agents"][(team_id, agent_name)] = {
                "team_id": team_id,
                "agent_name": agent_name,
                "data_json": data,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            self.rowcount = 1
            return

        # Single-row read by name (under the team lock) — must precede the team-wide
        # list select below, whose prefix this also matches. Keyed on the WHERE
        # "and agent_name" so the list query's "ORDER BY agent_name" doesn't match.
        if (
            norm.startswith("select data_json from agentic_team_agents where team_id")
            and "and agent_name" in norm
        ):
            team_id, agent_name = params
            row = self._db["team_agents"].get((team_id, agent_name))
            self._last_fetch_one = {"data_json": row["data_json"]} if row else None
            return

        if norm.startswith("select data_json from agentic_team_agents where team_id"):
            (team_id,) = params
            rows = [
                {"data_json": r["data_json"]}
                for r in sorted(
                    (r for (tid, _), r in self._db["team_agents"].items() if tid == team_id),
                    key=lambda r: r["agent_name"],
                )
            ]
            self._last_fetch_all = rows
            return

        # -- conversations ------------------------------------------------
        if norm.startswith("insert into agentic_conversations"):
            conversation_id, team_id, created_at, updated_at = params
            self._db["conversations"][conversation_id] = {
                "conversation_id": conversation_id,
                "team_id": team_id,
                "process_id": None,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return

        if norm.startswith("select team_id from agentic_conversations where conversation_id"):
            (cid,) = params
            row = self._db["conversations"].get(cid)
            self._last_fetch_one = (row["team_id"],) if row else None
            return

        if norm.startswith("select process_id from agentic_conversations where conversation_id"):
            (cid,) = params
            row = self._db["conversations"].get(cid)
            self._last_fetch_one = (row["process_id"],) if row else None
            return

        if norm.startswith("update agentic_conversations set process_id"):
            process_id, ts, cid = params
            row = self._db["conversations"].get(cid)
            if row:
                row["process_id"] = process_id
                row["updated_at"] = ts
            return

        if norm.startswith("update agentic_conversations set updated_at"):
            ts, cid = params
            row = self._db["conversations"].get(cid)
            if row:
                row["updated_at"] = ts
            return

        if norm.startswith("insert into agentic_conv_messages"):
            cid, role, content, ts = params
            self._db["conv_messages"].append(
                {
                    "id": next(self._ids),
                    "conversation_id": cid,
                    "role": role,
                    "content": content,
                    "timestamp": ts,
                }
            )
            return

        if norm.startswith(
            "select role, content, timestamp from agentic_conv_messages where conversation_id"
        ):
            (cid,) = params
            msgs = [
                {"role": m["role"], "content": m["content"], "timestamp": m["timestamp"]}
                for m in self._db["conv_messages"]
                if m["conversation_id"] == cid
            ]
            self._last_fetch_all = msgs
            return

        if "from agentic_conversations c where c.team_id" in norm:
            (team_id,) = params
            rows = []
            for c in sorted(
                (c for c in self._db["conversations"].values() if c["team_id"] == team_id),
                key=lambda c: c["created_at"],
                reverse=True,
            ):
                count = sum(
                    1
                    for m in self._db["conv_messages"]
                    if m["conversation_id"] == c["conversation_id"]
                )
                rows.append({**c, "message_count": count})
            self._last_fetch_all = rows
            return

        # -- env provisions -----------------------------------------------
        if "with prev as" in norm and "agentic_env_provisions" in norm:
            (
                select_team_id,
                select_stable_key,
                insert_team_id,
                insert_stable_key,
                process_id,
                step_id,
                agent_name,
                provisioning_agent_id,
                now_a,
                now_b,
            ) = params
            # The SELECT portion captures the previous row's status.
            prev_row = self._db["env_provisions"].get((select_team_id, select_stable_key))
            prev_status = prev_row["status"] if prev_row else None
            # INSERT ... ON CONFLICT DO UPDATE WHERE status='failed' RETURNING status
            if prev_row is None:
                self._db["env_provisions"][(insert_team_id, insert_stable_key)] = {
                    "team_id": insert_team_id,
                    "stable_key": insert_stable_key,
                    "process_id": process_id,
                    "step_id": step_id,
                    "agent_name": agent_name,
                    "provisioning_agent_id": provisioning_agent_id,
                    "status": "running",
                    "error_message": None,
                    "created_at": now_a,
                    "updated_at": now_b,
                }
                new_status = "running"
            elif prev_row["status"] == "failed":
                prev_row.update(
                    {
                        "provisioning_agent_id": provisioning_agent_id,
                        "process_id": process_id,
                        "step_id": step_id,
                        "agent_name": agent_name,
                        "status": "running",
                        "error_message": None,
                        "updated_at": now_b,
                    }
                )
                new_status = "running"
            else:
                new_status = None
            self._last_fetch_one = {"prev_status": prev_status, "new_status": new_status}
            return

        if norm.startswith("update agentic_env_provisions set status"):
            status, error_message, ts, team_id, stable_key = params
            row = self._db["env_provisions"].get((team_id, stable_key))
            if row:
                row["status"] = status
                row["error_message"] = error_message
                row["updated_at"] = ts
            return

        if norm.startswith(
            "select stable_key, process_id, step_id, agent_name, provisioning_agent_id,"
        ):
            (team_id,) = params
            rows = [
                {
                    "stable_key": r["stable_key"],
                    "process_id": r["process_id"],
                    "step_id": r["step_id"],
                    "agent_name": r["agent_name"],
                    "provisioning_agent_id": r["provisioning_agent_id"],
                    "status": r["status"],
                    "error_message": r["error_message"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
                for r in sorted(
                    (r for (tid, _), r in self._db["env_provisions"].items() if tid == team_id),
                    key=lambda r: r["updated_at"],
                    reverse=True,
                )
            ]
            self._last_fetch_all = rows
            return

        # -- form_data ----------------------------------------------------
        if norm.startswith("insert into agentic_form_data"):
            record_id, team_id, form_key, data_json, created_at, updated_at = params
            data = _unwrap_json(data_json)
            self._db["form_data"][record_id] = {
                "record_id": record_id,
                "team_id": team_id,
                "form_key": form_key,
                "data_json": data,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            self.rowcount = 1
            return

        if (
            norm.startswith(
                "select record_id, form_key, data_json, created_at, updated_at from agentic_form_data"
            )
            and "form_key" in norm
            and "team_id = %s and form_key = %s" in norm
        ):
            team_id, form_key = params
            rows = [
                r
                for r in sorted(self._db["form_data"].values(), key=lambda r: r["created_at"])
                if r["team_id"] == team_id and r["form_key"] == form_key
            ]
            self._last_fetch_all = rows
            return

        if (
            norm.startswith(
                "select record_id, form_key, data_json, created_at, updated_at from agentic_form_data"
            )
            and "team_id = %s and record_id = %s" in norm
        ):
            team_id, record_id = params
            row = self._db["form_data"].get(record_id)
            self._last_fetch_one = row if row and row["team_id"] == team_id else None
            return

        if norm.startswith("update agentic_form_data set data_json"):
            data_json, ts, team_id, record_id = params
            data = _unwrap_json(data_json)
            row = self._db["form_data"].get(record_id)
            if row and row["team_id"] == team_id:
                row["data_json"] = data
                row["updated_at"] = ts
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if norm.startswith("delete from agentic_form_data where team_id = %s and record_id"):
            team_id, record_id = params
            row = self._db["form_data"].get(record_id)
            if row and row["team_id"] == team_id:
                del self._db["form_data"][record_id]
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if norm.startswith("select distinct form_key from agentic_form_data where team_id"):
            (team_id,) = params
            keys = sorted(
                {r["form_key"] for r in self._db["form_data"].values() if r["team_id"] == team_id}
            )
            self._last_fetch_all = [(k,) for k in keys]
            return

        # -- pipeline runs ------------------------------------------------
        # Advisory lock (reap): a no-op that always "acquires" in the single-process
        # fake. Matches the session, xact, and unlock variants. Must precede the
        # generic UPDATE fallthrough below.
        if "advisory" in norm:
            self._last_fetch_one = (True,)
            return

        if norm.startswith("insert into agentic_test_pipeline_runs"):
            (
                run_id,
                team_id,
                process_id,
                status,
                initial_input,
                step_results,
                started_at,
                heartbeat_at,
                temporal_owned,
            ) = params
            self._db["pipeline_runs"][run_id] = {
                "run_id": run_id,
                "team_id": team_id,
                "process_id": process_id,
                "status": status,
                "current_step_id": None,
                "initial_input": initial_input,
                "step_results": _unwrap_json(step_results),
                "human_prompt": None,
                "human_input": None,
                "error": None,
                "started_at": started_at,
                "finished_at": None,
                "heartbeat_at": heartbeat_at,
                "temporal_owned": temporal_owned,
            }
            self.rowcount = 1
            return

        # is_pipeline_run_temporal_owned (single-column read).
        if norm.startswith("select temporal_owned from agentic_test_pipeline_runs"):
            (run_id,) = params
            row = self._db["pipeline_runs"].get(run_id)
            self._last_fetch_one = (
                {"temporal_owned": row.get("temporal_owned", False)} if row else None
            )
            return

        # get_pipeline_status (lightweight status + pending answer read).
        if norm.startswith("select status, human_input from agentic_test_pipeline_runs"):
            (run_id,) = params
            row = self._db["pipeline_runs"].get(run_id)
            self._last_fetch_one = (
                {"status": row["status"], "human_input": row["human_input"]} if row else None
            )
            return

        # advance_pipeline_step (cursor UPDATE gated on status='running'). Must precede
        # the generic update handler, which would otherwise write unconditionally.
        if norm.startswith("update agentic_test_pipeline_runs set current_step_id"):
            step_id, run_id = params
            row = self._db["pipeline_runs"].get(run_id)
            if row and row["status"] == "running":
                row["current_step_id"] = step_id
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # get_pipeline_run (WHERE run_id) vs list_pipeline_runs (WHERE team_id) share
        # the same column list; distinguish on the predicate.
        if norm.startswith("select run_id, team_id, process_id, status, current_step_id"):
            if "where run_id" in norm:
                (run_id,) = params
                self._last_fetch_one = self._db["pipeline_runs"].get(run_id)
            else:  # WHERE team_id ... ORDER BY started_at DESC LIMIT
                team_id, limit = params
                rows = sorted(
                    (r for r in self._db["pipeline_runs"].values() if r["team_id"] == team_id),
                    key=lambda r: r["started_at"],
                    reverse=True,
                )
                self._last_fetch_all = rows[:limit]
            return

        # try_resume_pipeline_run_temporal (CAS into 'running', NO heartbeat guard).
        # Must precede the heartbeat-guarded resume below; distinguished by the absence
        # of a heartbeat_at clause in the SET (Temporal owns liveness).
        if (
            norm.startswith("update agentic_test_pipeline_runs set status = 'running'")
            and "heartbeat_at" not in norm
        ):
            human_input, run_id = params
            row = self._db["pipeline_runs"].get(run_id)
            if row and row["status"] == "waiting_for_input":
                row.update(status="running", human_prompt=None, human_input=human_input)
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # try_resume_pipeline_run (compare-and-swap into 'running', fresh-heartbeat).
        if norm.startswith("update agentic_test_pipeline_runs set status = 'running'"):
            human_input, heartbeat_at, run_id, cutoff = params
            row = self._db["pipeline_runs"].get(run_id)
            hb = row["heartbeat_at"] if row else None
            if row and row["status"] == "waiting_for_input" and hb is not None and hb >= cutoff:
                row.update(
                    status="running",
                    human_prompt=None,
                    human_input=human_input,
                    heartbeat_at=heartbeat_at,
                )
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # try_complete_pipeline_run (CAS to 'completed', WHERE status='running').
        if norm.startswith("update agentic_test_pipeline_runs set status = 'completed'"):
            step_results, finished_at, run_id = params
            row = self._db["pipeline_runs"].get(run_id)
            if row and row["status"] == "running":
                row.update(
                    status="completed",
                    step_results=_unwrap_json(step_results),
                    finished_at=finished_at,
                )
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # try_cancel_pipeline_run (CAS to 'cancelled', WHERE status active).
        if norm.startswith("update agentic_test_pipeline_runs set status = 'cancelled'"):
            finished_at, run_id = params
            row = self._db["pipeline_runs"].get(run_id)
            if row and row["status"] in ("running", "waiting_for_input"):
                row.update(status="cancelled", finished_at=finished_at)
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # try_fail_pipeline_run (CAS to 'failed', single row, WHERE status active).
        # Must precede try_expire: both SET status='failed' WHERE run_id, but this one
        # is gated on `status IN (...)` (an active run) rather than 'waiting_for_input'.
        if (
            norm.startswith("update agentic_test_pipeline_runs set status = 'failed'")
            and "where run_id" in norm
            and "status in (" in norm
        ):
            error, finished_at, run_id = params
            row = self._db["pipeline_runs"].get(run_id)
            if row and row["status"] in ("running", "waiting_for_input"):
                row.update(status="failed", error=error, finished_at=finished_at)
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # try_expire_pipeline_run (CAS to 'failed', single row, WHERE waiting_for_input).
        if (
            norm.startswith("update agentic_test_pipeline_runs set status = 'failed'")
            and "where run_id" in norm
        ):
            error, finished_at, run_id = params
            row = self._db["pipeline_runs"].get(run_id)
            if row and row["status"] == "waiting_for_input":
                row.update(status="failed", error=error, finished_at=finished_at)
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # reap_orphaned_pipeline_runs (bulk, staleness-filtered).
        if (
            norm.startswith("update agentic_test_pipeline_runs set status = 'failed'")
            and "where status in" in norm
        ):
            error, finished_at, cutoff = params
            n = 0
            for row in self._db["pipeline_runs"].values():
                if row["status"] not in ("running", "waiting_for_input"):
                    continue
                if row.get("temporal_owned"):
                    # Temporal owns liveness/recovery for these runs — never reaped.
                    continue
                hb = row["heartbeat_at"]
                if hb is None or hb < cutoff:
                    row.update(status="failed", error=error, finished_at=finished_at)
                    n += 1
            self.rowcount = n
            return

        # heartbeat_pipeline_run.
        if norm.startswith("update agentic_test_pipeline_runs set heartbeat_at"):
            heartbeat_at, run_id = params
            row = self._db["pipeline_runs"].get(run_id)
            if row and row["status"] in ("running", "waiting_for_input"):
                row["heartbeat_at"] = heartbeat_at
            return

        # update_pipeline_run: dynamic SET of arbitrary columns, WHERE run_id last.
        if norm.startswith("update agentic_test_pipeline_runs set"):
            set_clause = norm.split(" set ", 1)[1].split(" where run_id", 1)[0]
            cols = [c.split("=")[0].strip() for c in set_clause.split(",")]
            run_id = params[-1]
            row = self._db["pipeline_runs"].get(run_id)
            if row:
                for col, val in zip(cols, params[:-1]):
                    row[col] = _unwrap_json(val) if col == "step_results" else val
                self.rowcount = 1
            else:
                self.rowcount = 0
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


def install_fake_postgres(monkeypatch) -> dict[str, Any]:
    """Install a fake ``get_conn`` on both stores and return the backing db."""
    db = _default_db()
    ids = itertools.count(1)

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db, ids)

    import agentic_team_provisioning.assistant.store as store_mod
    import agentic_team_provisioning.infrastructure as infra_mod
    import agentic_team_provisioning.testing.store as testing_store_mod

    monkeypatch.setattr(store_mod, "get_conn", _fake_get_conn)
    monkeypatch.setattr(infra_mod, "get_conn", _fake_get_conn)
    monkeypatch.setattr(testing_store_mod, "get_conn", _fake_get_conn)
    return db


# Re-export so test files can import without depending on re
_SQL_RE = re.compile  # kept for potential future use
