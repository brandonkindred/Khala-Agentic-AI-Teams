"""Shared dict-backed fake for ``shared.postgres.get_conn``.

Approximates the Postgres behaviour the agentic_team_provisioning stores
rely on — only as much as tests need. Tests monkey-patch the module-level
``get_conn`` on ``agent_team_studio.agentic_team_provisioning.assistant.store``,
``agent_team_studio.agentic_team_provisioning.infrastructure``, and
``agent_team_studio.agentic_team_provisioning.testing.store`` to redirect to this fake.

Matching contract (read before changing store SQL):
    Dispatch matchers receive SQL normalized with
    ``" ".join(sql.split()).lower()`` and are tried in order (first True wins).
    This is deliberately simple and therefore *coupled to the exact SQL the
    stores emit* — a wording, column-order, or clause change in a store query
    must be mirrored here or the cursor raises ``AssertionError("unexpected
    SQL")``. The matcher is intentionally NOT a SQL parser; that fragility is
    accepted so drift surfaces as a hard test failure, not a silent pass.
    When you change a store query, update the matching handler here.

Boilerplate (``FakeCursor`` / ``FakeConn`` / normalize / install) lives in
``shared.postgres.fake`` (re-exported from ``shared.postgres.testing``); this
module owns only the agentic_team_provisioning SQL→handler dispatch table and
the module list passed to ``install_fake_postgres``.
"""

from __future__ import annotations

from typing import Any

from shared.postgres.fake import (
    DispatchTable,
    FakeCursor,
    unwrap_json,
)
from shared.postgres.fake import (
    install_fake_postgres as _install_fake_postgres,
)


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


def _dispatch() -> DispatchTable:
    """Return agentic_team_provisioning's ordered SQL→handler table.

    Preconditions:
        None.
    Postconditions:
        Returns a non-empty ``DispatchTable`` whose matcher order matches the
        historical ``_FakeCursor.execute`` if/return cascade (first match wins).
    """

    # -- teams ------------------------------------------------------------
    def match_insert_team(norm: str) -> bool:
        return norm.startswith("insert into agentic_teams")

    def handle_insert_team(cur: FakeCursor, params: tuple) -> None:
        team_id, name, description, created_at, updated_at = params
        cur.db["teams"][team_id] = {
            "team_id": team_id,
            "name": name,
            "description": description,
            "created_at": created_at,
            "updated_at": updated_at,
        }
        cur.rowcount = 1

    def match_update_team_updated_at(norm: str) -> bool:
        return norm.startswith("update agentic_teams set updated_at")

    def handle_update_team_updated_at(cur: FakeCursor, params: tuple) -> None:
        ts, team_id = params
        row = cur.db["teams"].get(team_id)
        if row is not None:
            row["updated_at"] = ts
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    def match_select_team_by_id(norm: str) -> bool:
        return norm.startswith(
            "select team_id, name, description, created_at, updated_at from agentic_teams where team_id"
        )

    def handle_select_team_by_id(cur: FakeCursor, params: tuple) -> None:
        (team_id,) = params
        cur.set_one(cur.db["teams"].get(team_id))

    # Team-row lock probe used by merge_generated_agents (FOR UPDATE is a no-op
    # in the fake — it only checks existence). Distinct prefix from the full
    # column select above ("select team_id from" vs "select team_id, ...").
    def match_select_team_id_lock(norm: str) -> bool:
        return norm.startswith("select team_id from agentic_teams where team_id")

    def handle_select_team_id_lock(cur: FakeCursor, params: tuple) -> None:
        (team_id,) = params
        cur.set_one((team_id,) if team_id in cur.db["teams"] else None)

    # Team-row deletion used by the create_team rollback path when provisioning fails.
    def match_delete_team(norm: str) -> bool:
        return norm.startswith("delete from agentic_teams where team_id")

    def handle_delete_team(cur: FakeCursor, params: tuple) -> None:
        """``params`` is ``(team_id,)``; ``cur.rowcount`` reflects whether the row existed."""
        (team_id,) = params
        removed = cur.db["teams"].pop(team_id, None)
        cur.rowcount = 1 if removed else 0

    def match_list_teams(norm: str) -> bool:
        return "from agentic_teams t" in norm and "order by t.created_at desc" in norm

    def handle_list_teams(cur: FakeCursor, params: tuple) -> None:
        rows = []
        for t in sorted(
            cur.db["teams"].values(),
            key=lambda r: r["created_at"],
            reverse=True,
        ):
            process_count = sum(
                1 for p in cur.db["processes"].values() if p["team_id"] == t["team_id"]
            )
            rows.append({**t, "process_count": process_count})
        cur.set_all(rows)

    # -- processes --------------------------------------------------------
    def match_upsert_process(norm: str) -> bool:
        return norm.startswith("insert into agentic_processes") and "on conflict" in norm

    def handle_upsert_process(cur: FakeCursor, params: tuple) -> None:
        process_id, team_id, data_json, created_at, updated_at = params
        data = unwrap_json(data_json)
        existing = cur.db["processes"].get(process_id)
        if existing:
            existing["data_json"] = data
            existing["updated_at"] = updated_at
        else:
            cur.db["processes"][process_id] = {
                "process_id": process_id,
                "team_id": team_id,
                "data_json": data,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        cur.rowcount = 1

    def match_select_process_data(norm: str) -> bool:
        return norm.startswith("select data_json from agentic_processes where process_id")

    def handle_select_process_data(cur: FakeCursor, params: tuple) -> None:
        (process_id,) = params
        row = cur.db["processes"].get(process_id)
        cur.set_one({"data_json": row["data_json"]} if row else None)

    def match_select_process_team_id(norm: str) -> bool:
        return norm.startswith("select team_id from agentic_processes where process_id")

    def handle_select_process_team_id(cur: FakeCursor, params: tuple) -> None:
        (process_id,) = params
        row = cur.db["processes"].get(process_id)
        cur.set_one((row["team_id"],) if row else None)

    def match_select_processes_by_team(norm: str) -> bool:
        return norm.startswith("select data_json from agentic_processes where team_id")

    def handle_select_processes_by_team(cur: FakeCursor, params: tuple) -> None:
        (team_id,) = params
        rows = [
            {"data_json": p["data_json"]}
            for p in sorted(
                (p for p in cur.db["processes"].values() if p["team_id"] == team_id),
                key=lambda p: p["created_at"],
            )
        ]
        cur.set_all(rows)

    # -- team_agents ------------------------------------------------------
    # Full-roster prune used by _write_team_agents: delete rows whose agent_name
    # is not in the kept set. Must precede the single-row delete (it also matches
    # "agent_name in norm") and the team-wide delete.
    def match_prune_team_agents(norm: str) -> bool:
        return norm.startswith("delete from agentic_team_agents where team_id") and "<> all" in norm

    def handle_prune_team_agents(cur: FakeCursor, params: tuple) -> None:
        team_id, names = params
        keep = set(names)
        removed = 0
        for k in list(cur.db["team_agents"].keys()):
            if k[0] == team_id and k[1] not in keep:
                del cur.db["team_agents"][k]
                removed += 1
        cur.rowcount = removed

    # Single-row targeted delete (RETURNING the deleted row) — must precede the
    # team-wide delete handler, whose prefix this also matches.
    def match_delete_team_agent(norm: str) -> bool:
        return (
            norm.startswith("delete from agentic_team_agents where team_id")
            and "agent_name" in norm
        )

    def handle_delete_team_agent(cur: FakeCursor, params: tuple) -> None:
        team_id, agent_name = params
        removed = cur.db["team_agents"].pop((team_id, agent_name), None)
        cur.rowcount = 1 if removed else 0
        cur.set_one({"data_json": removed["data_json"]} if removed else None)

    def match_delete_team_agents(norm: str) -> bool:
        return norm.startswith("delete from agentic_team_agents where team_id")

    def handle_delete_team_agents(cur: FakeCursor, params: tuple) -> None:
        (team_id,) = params
        removed = 0
        for k in list(cur.db["team_agents"].keys()):
            if k[0] == team_id:
                del cur.db["team_agents"][k]
                removed += 1
        cur.rowcount = removed

    # Single-row upsert (ON CONFLICT) — preserves the existing created_at, like
    # real Postgres. Must precede the plain INSERT handler.
    def match_upsert_team_agent(norm: str) -> bool:
        return norm.startswith("insert into agentic_team_agents") and "on conflict" in norm

    def handle_upsert_team_agent(cur: FakeCursor, params: tuple) -> None:
        team_id, agent_name, data_json, created_at, updated_at = params
        data = unwrap_json(data_json)
        existing = cur.db["team_agents"].get((team_id, agent_name))
        if existing:
            existing["data_json"] = data
            existing["updated_at"] = updated_at
        else:
            cur.db["team_agents"][(team_id, agent_name)] = {
                "team_id": team_id,
                "agent_name": agent_name,
                "data_json": data,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        cur.rowcount = 1

    def match_insert_team_agent(norm: str) -> bool:
        # Exclude ON CONFLICT upserts so this stays mutually exclusive with
        # match_upsert_team_agent even if dispatch order changes.
        return norm.startswith("insert into agentic_team_agents") and "on conflict" not in norm

    def handle_insert_team_agent(cur: FakeCursor, params: tuple) -> None:
        team_id, agent_name, data_json, created_at, updated_at = params
        data = unwrap_json(data_json)
        cur.db["team_agents"][(team_id, agent_name)] = {
            "team_id": team_id,
            "agent_name": agent_name,
            "data_json": data,
            "created_at": created_at,
            "updated_at": updated_at,
        }
        cur.rowcount = 1

    # Single-row read by name (under the team lock) — must precede the team-wide
    # list select below, whose prefix this also matches. Keyed on the WHERE
    # "and agent_name" so the list query's "ORDER BY agent_name" doesn't match.
    def match_select_team_agent(norm: str) -> bool:
        return (
            norm.startswith("select data_json from agentic_team_agents where team_id")
            and "and agent_name" in norm
        )

    def handle_select_team_agent(cur: FakeCursor, params: tuple) -> None:
        team_id, agent_name = params
        row = cur.db["team_agents"].get((team_id, agent_name))
        cur.set_one({"data_json": row["data_json"]} if row else None)

    def match_select_team_agents(norm: str) -> bool:
        return norm.startswith("select data_json from agentic_team_agents where team_id")

    def handle_select_team_agents(cur: FakeCursor, params: tuple) -> None:
        (team_id,) = params
        rows = [
            {"data_json": r["data_json"]}
            for r in sorted(
                (r for (tid, _), r in cur.db["team_agents"].items() if tid == team_id),
                key=lambda r: r["agent_name"],
            )
        ]
        cur.set_all(rows)

    # -- conversations ----------------------------------------------------
    def match_insert_conversation(norm: str) -> bool:
        return norm.startswith("insert into agentic_conversations")

    def handle_insert_conversation(cur: FakeCursor, params: tuple) -> None:
        conversation_id, team_id, created_at, updated_at = params
        cur.db["conversations"][conversation_id] = {
            "conversation_id": conversation_id,
            "team_id": team_id,
            "process_id": None,
            "created_at": created_at,
            "updated_at": updated_at,
        }
        cur.rowcount = 1

    def match_select_conv_team_id(norm: str) -> bool:
        return norm.startswith("select team_id from agentic_conversations where conversation_id")

    def handle_select_conv_team_id(cur: FakeCursor, params: tuple) -> None:
        (cid,) = params
        row = cur.db["conversations"].get(cid)
        cur.set_one((row["team_id"],) if row else None)

    def match_select_conv_process_id(norm: str) -> bool:
        return norm.startswith("select process_id from agentic_conversations where conversation_id")

    def handle_select_conv_process_id(cur: FakeCursor, params: tuple) -> None:
        (cid,) = params
        row = cur.db["conversations"].get(cid)
        cur.set_one((row["process_id"],) if row else None)

    def match_update_conv_process_id(norm: str) -> bool:
        return norm.startswith("update agentic_conversations set process_id")

    def handle_update_conv_process_id(cur: FakeCursor, params: tuple) -> None:
        process_id, ts, cid = params
        row = cur.db["conversations"].get(cid)
        if row:
            row["process_id"] = process_id
            row["updated_at"] = ts
        cur.rowcount = 1 if row else 0

    def match_update_conv_updated_at(norm: str) -> bool:
        return norm.startswith("update agentic_conversations set updated_at")

    def handle_update_conv_updated_at(cur: FakeCursor, params: tuple) -> None:
        ts, cid = params
        row = cur.db["conversations"].get(cid)
        if row:
            row["updated_at"] = ts
        cur.rowcount = 1 if row else 0

    def match_insert_conv_message(norm: str) -> bool:
        return norm.startswith("insert into agentic_conv_messages")

    def handle_insert_conv_message(cur: FakeCursor, params: tuple) -> None:
        cid, role, content, ts = params
        cur.db["conv_messages"].append(
            {
                "id": next(cur.ids),
                "conversation_id": cid,
                "role": role,
                "content": content,
                "timestamp": ts,
            }
        )
        cur.rowcount = 1

    def match_select_conv_messages(norm: str) -> bool:
        return norm.startswith(
            "select role, content, timestamp from agentic_conv_messages where conversation_id"
        )

    def handle_select_conv_messages(cur: FakeCursor, params: tuple) -> None:
        (cid,) = params
        msgs = [
            {"role": m["role"], "content": m["content"], "timestamp": m["timestamp"]}
            for m in cur.db["conv_messages"]
            if m["conversation_id"] == cid
        ]
        cur.set_all(msgs)

    def match_list_conversations(norm: str) -> bool:
        return "from agentic_conversations c where c.team_id" in norm

    def handle_list_conversations(cur: FakeCursor, params: tuple) -> None:
        (team_id,) = params
        rows = []
        for c in sorted(
            (c for c in cur.db["conversations"].values() if c["team_id"] == team_id),
            key=lambda c: c["created_at"],
            reverse=True,
        ):
            count = sum(
                1 for m in cur.db["conv_messages"] if m["conversation_id"] == c["conversation_id"]
            )
            rows.append({**c, "message_count": count})
        cur.set_all(rows)

    # -- env provisions ---------------------------------------------------
    def match_claim_env_provision(norm: str) -> bool:
        return "with prev as" in norm and "agentic_env_provisions" in norm

    def handle_claim_env_provision(cur: FakeCursor, params: tuple) -> None:
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
        prev_row = cur.db["env_provisions"].get((select_team_id, select_stable_key))
        prev_status = prev_row["status"] if prev_row else None
        # INSERT ... ON CONFLICT DO UPDATE WHERE status='failed' RETURNING status
        if prev_row is None:
            cur.db["env_provisions"][(insert_team_id, insert_stable_key)] = {
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
        cur.set_one({"prev_status": prev_status, "new_status": new_status})

    def match_update_env_provision_status(norm: str) -> bool:
        return norm.startswith("update agentic_env_provisions set status")

    def handle_update_env_provision_status(cur: FakeCursor, params: tuple) -> None:
        status, error_message, ts, team_id, stable_key = params
        row = cur.db["env_provisions"].get((team_id, stable_key))
        if row:
            row["status"] = status
            row["error_message"] = error_message
            row["updated_at"] = ts
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    def match_list_env_provisions(norm: str) -> bool:
        return norm.startswith(
            "select stable_key, process_id, step_id, agent_name, provisioning_agent_id,"
        )

    def handle_list_env_provisions(cur: FakeCursor, params: tuple) -> None:
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
                (r for (tid, _), r in cur.db["env_provisions"].items() if tid == team_id),
                key=lambda r: r["updated_at"],
                reverse=True,
            )
        ]
        cur.set_all(rows)

    # -- form_data --------------------------------------------------------
    def match_insert_form_data(norm: str) -> bool:
        return norm.startswith("insert into agentic_form_data")

    def handle_insert_form_data(cur: FakeCursor, params: tuple) -> None:
        record_id, team_id, form_key, data_json, created_at, updated_at = params
        data = unwrap_json(data_json)
        cur.db["form_data"][record_id] = {
            "record_id": record_id,
            "team_id": team_id,
            "form_key": form_key,
            "data_json": data,
            "created_at": created_at,
            "updated_at": updated_at,
        }
        cur.rowcount = 1

    def match_select_form_data_by_key(norm: str) -> bool:
        return (
            norm.startswith(
                "select record_id, form_key, data_json, created_at, updated_at from agentic_form_data"
            )
            and "form_key" in norm
            and "team_id = %s and form_key = %s" in norm
        )

    def handle_select_form_data_by_key(cur: FakeCursor, params: tuple) -> None:
        team_id, form_key = params
        rows = [
            r
            for r in sorted(cur.db["form_data"].values(), key=lambda r: r["created_at"])
            if r["team_id"] == team_id and r["form_key"] == form_key
        ]
        cur.set_all(rows)

    def match_select_form_data_by_id(norm: str) -> bool:
        return (
            norm.startswith(
                "select record_id, form_key, data_json, created_at, updated_at from agentic_form_data"
            )
            and "team_id = %s and record_id = %s" in norm
        )

    def handle_select_form_data_by_id(cur: FakeCursor, params: tuple) -> None:
        team_id, record_id = params
        row = cur.db["form_data"].get(record_id)
        cur.set_one(row if row and row["team_id"] == team_id else None)

    def match_update_form_data(norm: str) -> bool:
        return norm.startswith("update agentic_form_data set data_json")

    def handle_update_form_data(cur: FakeCursor, params: tuple) -> None:
        data_json, ts, team_id, record_id, form_key = params
        data = unwrap_json(data_json)
        row = cur.db["form_data"].get(record_id)
        if row and row["team_id"] == team_id and row["form_key"] == form_key:
            row["data_json"] = data
            row["updated_at"] = ts
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    def match_delete_form_data(norm: str) -> bool:
        return norm.startswith("delete from agentic_form_data where team_id = %s and record_id")

    def handle_delete_form_data(cur: FakeCursor, params: tuple) -> None:
        team_id, record_id, form_key = params
        row = cur.db["form_data"].get(record_id)
        if row and row["team_id"] == team_id and row["form_key"] == form_key:
            del cur.db["form_data"][record_id]
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    def match_list_form_keys(norm: str) -> bool:
        return norm.startswith("select distinct form_key from agentic_form_data where team_id")

    def handle_list_form_keys(cur: FakeCursor, params: tuple) -> None:
        (team_id,) = params
        keys = sorted(
            {r["form_key"] for r in cur.db["form_data"].values() if r["team_id"] == team_id}
        )
        cur.set_all([(k,) for k in keys])

    # -- pipeline runs ----------------------------------------------------
    # Advisory lock (reap): a no-op that always "acquires" in the single-process
    # fake. Anchored to the actual lock-function call prefix (not a substring
    # match) so an unrelated query merely mentioning "advisory" can't misroute here.
    def match_advisory(norm: str) -> bool:
        return norm.startswith("select pg_try_advisory") or norm.startswith("select pg_advisory")

    def handle_advisory(cur: FakeCursor, params: tuple) -> None:
        cur.set_one((True,))

    def match_insert_pipeline_run(norm: str) -> bool:
        return norm.startswith("insert into agentic_test_pipeline_runs")

    def handle_insert_pipeline_run(cur: FakeCursor, params: tuple) -> None:
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
        cur.db["pipeline_runs"][run_id] = {
            "run_id": run_id,
            "team_id": team_id,
            "process_id": process_id,
            "status": status,
            "current_step_id": None,
            "initial_input": initial_input,
            "step_results": unwrap_json(step_results),
            "human_prompt": None,
            "human_input": None,
            "error": None,
            "started_at": started_at,
            "finished_at": None,
            "heartbeat_at": heartbeat_at,
            "temporal_owned": temporal_owned,
        }
        cur.rowcount = 1

    # is_pipeline_run_temporal_owned (single-column read).
    def match_select_temporal_owned(norm: str) -> bool:
        return norm.startswith("select temporal_owned from agentic_test_pipeline_runs")

    def handle_select_temporal_owned(cur: FakeCursor, params: tuple) -> None:
        (run_id,) = params
        row = cur.db["pipeline_runs"].get(run_id)
        cur.set_one({"temporal_owned": row.get("temporal_owned", False)} if row else None)

    # get_pipeline_status (lightweight status + pending answer read).
    def match_select_pipeline_status(norm: str) -> bool:
        return norm.startswith("select status, human_input from agentic_test_pipeline_runs")

    def handle_select_pipeline_status(cur: FakeCursor, params: tuple) -> None:
        (run_id,) = params
        row = cur.db["pipeline_runs"].get(run_id)
        cur.set_one({"status": row["status"], "human_input": row["human_input"]} if row else None)

    # advance_pipeline_step (cursor UPDATE gated on status='running'). Must precede
    # the generic update handler, which would otherwise write unconditionally.
    def match_advance_pipeline_step(norm: str) -> bool:
        return norm.startswith("update agentic_test_pipeline_runs set current_step_id")

    def handle_advance_pipeline_step(cur: FakeCursor, params: tuple) -> None:
        step_id, run_id = params
        row = cur.db["pipeline_runs"].get(run_id)
        if row and row["status"] == "running":
            row["current_step_id"] = step_id
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    # get_pipeline_run (WHERE run_id) vs list_pipeline_runs (WHERE team_id) share
    # the same column list; split into two mutually exclusive matchers so handlers
    # need only params, regardless of which is registered first.
    def match_get_pipeline_run(norm: str) -> bool:
        return (
            norm.startswith("select run_id, team_id, process_id, status, current_step_id")
            and "where run_id" in norm
        )

    def handle_get_pipeline_run(cur: FakeCursor, params: tuple) -> None:
        (run_id,) = params
        cur.set_one(cur.db["pipeline_runs"].get(run_id))

    def match_list_pipeline_runs(norm: str) -> bool:
        return (
            norm.startswith("select run_id, team_id, process_id, status, current_step_id")
            and "where run_id" not in norm
        )

    def handle_list_pipeline_runs(cur: FakeCursor, params: tuple) -> None:
        team_id, limit = params
        rows = sorted(
            (r for r in cur.db["pipeline_runs"].values() if r["team_id"] == team_id),
            key=lambda r: r["started_at"],
            reverse=True,
        )
        cur.set_all(rows[:limit])

    # try_resume_pipeline_run_temporal (CAS into 'running', NO heartbeat guard).
    # Mutually exclusive with the heartbeat-guarded resume below regardless of
    # registration order; distinguished by the absence of a heartbeat_at clause
    # in the SET (Temporal owns liveness).
    def match_resume_pipeline_temporal(norm: str) -> bool:
        return (
            norm.startswith("update agentic_test_pipeline_runs set status = 'running'")
            and "heartbeat_at" not in norm
        )

    def handle_resume_pipeline_temporal(cur: FakeCursor, params: tuple) -> None:
        human_input, run_id = params
        row = cur.db["pipeline_runs"].get(run_id)
        if row and row["status"] == "waiting_for_input":
            row.update(status="running", human_prompt=None, human_input=human_input)
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    # try_resume_pipeline_run (compare-and-swap into 'running', fresh-heartbeat).
    def match_resume_pipeline(norm: str) -> bool:
        return (
            norm.startswith("update agentic_test_pipeline_runs set status = 'running'")
            and "heartbeat_at" in norm
        )

    def handle_resume_pipeline(cur: FakeCursor, params: tuple) -> None:
        human_input, heartbeat_at, run_id, cutoff = params
        row = cur.db["pipeline_runs"].get(run_id)
        hb = row["heartbeat_at"] if row else None
        if row and row["status"] == "waiting_for_input" and hb is not None and hb >= cutoff:
            row.update(
                status="running",
                human_prompt=None,
                human_input=human_input,
                heartbeat_at=heartbeat_at,
            )
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    # try_complete_pipeline_run (CAS to 'completed', WHERE status='running').
    def match_complete_pipeline(norm: str) -> bool:
        return norm.startswith("update agentic_test_pipeline_runs set status = 'completed'")

    def handle_complete_pipeline(cur: FakeCursor, params: tuple) -> None:
        step_results, finished_at, run_id = params
        row = cur.db["pipeline_runs"].get(run_id)
        if row and row["status"] == "running":
            row.update(
                status="completed",
                step_results=unwrap_json(step_results),
                finished_at=finished_at,
            )
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    # try_cancel_pipeline_run (CAS to 'cancelled', WHERE status active).
    def match_cancel_pipeline(norm: str) -> bool:
        return norm.startswith("update agentic_test_pipeline_runs set status = 'cancelled'")

    def handle_cancel_pipeline(cur: FakeCursor, params: tuple) -> None:
        finished_at, run_id = params
        row = cur.db["pipeline_runs"].get(run_id)
        if row and row["status"] in ("running", "waiting_for_input"):
            row.update(status="cancelled", finished_at=finished_at)
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    # try_fail_pipeline_run (CAS to 'failed', single row, WHERE status active).
    # Both this and try_expire below SET status='failed' WHERE run_id, but this one
    # is gated on `status IN (...)` (an active run) rather than 'waiting_for_input';
    # try_expire excludes that clause so the two are mutually exclusive regardless
    # of registration order.
    def match_fail_pipeline_active(norm: str) -> bool:
        return (
            norm.startswith("update agentic_test_pipeline_runs set status = 'failed'")
            and "where run_id" in norm
            and "status in (" in norm
        )

    def handle_fail_pipeline_active(cur: FakeCursor, params: tuple) -> None:
        error, finished_at, run_id = params
        row = cur.db["pipeline_runs"].get(run_id)
        if row and row["status"] in ("running", "waiting_for_input"):
            row.update(status="failed", error=error, finished_at=finished_at)
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    # try_expire_pipeline_run (CAS to 'failed', single row, WHERE waiting_for_input).
    def match_expire_pipeline(norm: str) -> bool:
        return (
            norm.startswith("update agentic_test_pipeline_runs set status = 'failed'")
            and "where run_id" in norm
            and "status in (" not in norm
        )

    def handle_expire_pipeline(cur: FakeCursor, params: tuple) -> None:
        error, finished_at, run_id = params
        row = cur.db["pipeline_runs"].get(run_id)
        if row and row["status"] == "waiting_for_input":
            row.update(status="failed", error=error, finished_at=finished_at)
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    # reap_orphaned_pipeline_runs (bulk, staleness-filtered).
    def match_reap_pipeline(norm: str) -> bool:
        return (
            norm.startswith("update agentic_test_pipeline_runs set status = 'failed'")
            and "where status in" in norm
        )

    def handle_reap_pipeline(cur: FakeCursor, params: tuple) -> None:
        error, finished_at, cutoff = params
        n = 0
        for row in cur.db["pipeline_runs"].values():
            if row["status"] not in ("running", "waiting_for_input"):
                continue
            if row.get("temporal_owned"):
                # Temporal owns liveness/recovery for these runs — never reaped.
                continue
            hb = row["heartbeat_at"]
            if hb is None or hb < cutoff:
                row.update(status="failed", error=error, finished_at=finished_at)
                n += 1
        cur.rowcount = n

    # heartbeat_pipeline_run.
    def match_heartbeat_pipeline(norm: str) -> bool:
        return norm.startswith("update agentic_test_pipeline_runs set heartbeat_at")

    def handle_heartbeat_pipeline(cur: FakeCursor, params: tuple) -> None:
        heartbeat_at, run_id = params
        row = cur.db["pipeline_runs"].get(run_id)
        if row and row["status"] in ("running", "waiting_for_input"):
            row["heartbeat_at"] = heartbeat_at

    # update_pipeline_run: dynamic SET of arbitrary columns, WHERE run_id last.
    # Column names live only in the SET clause text; read them from
    # ``cur.last_norm`` (set by ``FakeCursor.execute`` for this invocation).
    def match_update_pipeline_generic(norm: str) -> bool:
        return norm.startswith("update agentic_test_pipeline_runs set")

    def handle_update_pipeline_generic(cur: FakeCursor, params: tuple) -> None:
        norm = cur.last_norm or ""
        set_clause = norm.split(" set ", 1)[1].split(" where run_id", 1)[0]
        cols = [c.split("=")[0].strip() for c in set_clause.split(",")]
        run_id = params[-1]
        row = cur.db["pipeline_runs"].get(run_id)
        if row:
            for col, val in zip(cols, params[:-1]):
                row[col] = unwrap_json(val) if col == "step_results" else val
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    return [
        (match_insert_team, handle_insert_team),
        (match_update_team_updated_at, handle_update_team_updated_at),
        (match_select_team_by_id, handle_select_team_by_id),
        (match_select_team_id_lock, handle_select_team_id_lock),
        (match_delete_team, handle_delete_team),
        (match_list_teams, handle_list_teams),
        (match_upsert_process, handle_upsert_process),
        (match_select_process_data, handle_select_process_data),
        (match_select_process_team_id, handle_select_process_team_id),
        (match_select_processes_by_team, handle_select_processes_by_team),
        (match_prune_team_agents, handle_prune_team_agents),
        (match_delete_team_agent, handle_delete_team_agent),
        (match_delete_team_agents, handle_delete_team_agents),
        (match_upsert_team_agent, handle_upsert_team_agent),
        (match_insert_team_agent, handle_insert_team_agent),
        (match_select_team_agent, handle_select_team_agent),
        (match_select_team_agents, handle_select_team_agents),
        (match_insert_conversation, handle_insert_conversation),
        (match_select_conv_team_id, handle_select_conv_team_id),
        (match_select_conv_process_id, handle_select_conv_process_id),
        (match_update_conv_process_id, handle_update_conv_process_id),
        (match_update_conv_updated_at, handle_update_conv_updated_at),
        (match_insert_conv_message, handle_insert_conv_message),
        (match_select_conv_messages, handle_select_conv_messages),
        (match_list_conversations, handle_list_conversations),
        (match_claim_env_provision, handle_claim_env_provision),
        (match_update_env_provision_status, handle_update_env_provision_status),
        (match_list_env_provisions, handle_list_env_provisions),
        (match_insert_form_data, handle_insert_form_data),
        (match_select_form_data_by_key, handle_select_form_data_by_key),
        (match_select_form_data_by_id, handle_select_form_data_by_id),
        (match_update_form_data, handle_update_form_data),
        (match_delete_form_data, handle_delete_form_data),
        (match_list_form_keys, handle_list_form_keys),
        (match_advisory, handle_advisory),
        (match_insert_pipeline_run, handle_insert_pipeline_run),
        (match_select_temporal_owned, handle_select_temporal_owned),
        (match_select_pipeline_status, handle_select_pipeline_status),
        (match_advance_pipeline_step, handle_advance_pipeline_step),
        (match_get_pipeline_run, handle_get_pipeline_run),
        (match_list_pipeline_runs, handle_list_pipeline_runs),
        (match_resume_pipeline_temporal, handle_resume_pipeline_temporal),
        (match_resume_pipeline, handle_resume_pipeline),
        (match_complete_pipeline, handle_complete_pipeline),
        (match_cancel_pipeline, handle_cancel_pipeline),
        (match_fail_pipeline_active, handle_fail_pipeline_active),
        (match_expire_pipeline, handle_expire_pipeline),
        (match_reap_pipeline, handle_reap_pipeline),
        (match_heartbeat_pipeline, handle_heartbeat_pipeline),
        (match_update_pipeline_generic, handle_update_pipeline_generic),
    ]


def install_fake_postgres(monkeypatch) -> dict[str, Any]:
    """Install a fake ``get_conn`` on ATP stores and return the backing db.

    Preconditions:
        ``monkeypatch`` is a pytest ``MonkeyPatch`` (or compatible).
    Postconditions:
        ``assistant.store``, ``infrastructure``, and ``testing.store`` each
        have ``get_conn`` patched to yield a shared ``FakeConn`` backed by the
        returned default db and this team's SQL dispatch table.
    """
    import agent_team_studio.agentic_team_provisioning.assistant.store as store_mod
    import agent_team_studio.agentic_team_provisioning.infrastructure as infra_mod
    import agent_team_studio.agentic_team_provisioning.testing.store as testing_store_mod

    return _install_fake_postgres(
        monkeypatch,
        modules=[store_mod, infra_mod, testing_store_mod],
        dispatch=_dispatch(),
        db=_default_db(),
    )


__all__ = ["install_fake_postgres"]
