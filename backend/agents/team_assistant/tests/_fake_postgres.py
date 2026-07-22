"""Shared dict-backed fake for ``shared.postgres.get_conn`` used by team_assistant tests.

Approximates just enough of ``team_assistant_*`` table behaviour — in
particular, the ``team_key`` scoping — to exercise the store's ownership
checks without requiring a live Postgres.

Matching contract (read before changing store SQL):
    Dispatch matchers receive SQL normalized with
    ``" ".join(sql.split()).lower()`` and are tried in order (first True wins).
    This is deliberately simple and therefore *coupled to the exact SQL the
    store emits* — a wording, column-order, or clause change in a store query
    must be mirrored here or the cursor raises ``AssertionError("unexpected
    SQL")``. The matcher is intentionally NOT a SQL parser.

Boilerplate (``FakeCursor`` / ``FakeConn`` / normalize / install) lives in
``shared.postgres.fake`` (re-exported from ``shared.postgres.testing``); this
module owns only the team_assistant SQL→handler dispatch table and the module
list passed to ``install_fake_postgres``.
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
        "conversations": {},  # keyed by conversation_id -> row dict (incl. team_key)
        "messages": [],
        "artifacts": [],
    }


def _dispatch() -> DispatchTable:
    """Return team_assistant's ordered SQL→handler table.

    Preconditions:
        None.
    Postconditions:
        Returns a non-empty ``DispatchTable`` whose matcher order matches the
        historical ``_FakeCursor.execute`` if/return cascade (first match wins).
    """

    def match_insert_conversation(norm: str) -> bool:
        return norm.startswith("insert into team_assistant_conversations")

    def handle_insert_conversation(cur: FakeCursor, params: tuple) -> None:
        cid, team_key, job_id, ctx, created_at, updated_at = params
        cur.db["conversations"][cid] = {
            "conversation_id": cid,
            "team_key": team_key,
            "job_id": job_id,
            "context_json": unwrap_json(ctx),
            "created_at": created_at,
            "updated_at": updated_at,
        }
        cur.rowcount = 1

    def match_exists_conversation(norm: str) -> bool:
        return norm.startswith(
            "select 1 from team_assistant_conversations where conversation_id = %s and team_key"
        )

    def handle_exists_conversation(cur: FakeCursor, params: tuple) -> None:
        cid, team_key = params
        conv = cur.db["conversations"].get(cid)
        cur.set_one((1,) if conv and conv["team_key"] == team_key else None)

    def match_select_context(norm: str) -> bool:
        return norm.startswith(
            "select context_json from team_assistant_conversations where conversation_id = %s and team_key"
        )

    def handle_select_context(cur: FakeCursor, params: tuple) -> None:
        cid, team_key = params
        conv = cur.db["conversations"].get(cid)
        if conv and conv["team_key"] == team_key:
            cur.set_one({"context_json": conv["context_json"]})
        else:
            cur.set_one(None)

    def match_select_messages(norm: str) -> bool:
        return norm.startswith(
            "select role, content, timestamp from team_assistant_conv_messages where conversation_id"
        )

    def handle_select_messages(cur: FakeCursor, params: tuple) -> None:
        (cid,) = params
        cur.set_all(
            [
                {"role": m["role"], "content": m["content"], "timestamp": m["timestamp"]}
                for m in cur.db["messages"]
                if m["conversation_id"] == cid
            ]
        )

    def match_insert_message(norm: str) -> bool:
        return norm.startswith("insert into team_assistant_conv_messages")

    def handle_insert_message(cur: FakeCursor, params: tuple) -> None:
        cid, role, content, ts = params
        cur.db["messages"].append(
            {
                "id": next(cur.ids),
                "conversation_id": cid,
                "role": role,
                "content": content,
                "timestamp": ts,
            }
        )
        cur.rowcount = 1

    def match_update_updated_at(norm: str) -> bool:
        return norm.startswith(
            "update team_assistant_conversations set updated_at = %s where conversation_id = %s and team_key"
        )

    def handle_update_updated_at(cur: FakeCursor, params: tuple) -> None:
        ts, cid, team_key = params
        conv = cur.db["conversations"].get(cid)
        if conv and conv["team_key"] == team_key:
            conv["updated_at"] = ts
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    def match_update_job_id(norm: str) -> bool:
        return norm.startswith(
            "update team_assistant_conversations set job_id = %s, updated_at = %s "
            "where conversation_id = %s and team_key"
        )

    def handle_update_job_id(cur: FakeCursor, params: tuple) -> None:
        job_id, ts, cid, team_key = params
        conv = cur.db["conversations"].get(cid)
        if conv and conv["team_key"] == team_key:
            conv["job_id"] = job_id
            conv["updated_at"] = ts
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    def match_select_by_job_id(norm: str) -> bool:
        return norm.startswith(
            "select conversation_id from team_assistant_conversations where job_id = %s and team_key"
        )

    def handle_select_by_job_id(cur: FakeCursor, params: tuple) -> None:
        job_id, team_key = params
        for conv in cur.db["conversations"].values():
            if conv.get("job_id") == job_id and conv["team_key"] == team_key:
                cur.set_one((conv["conversation_id"],))
                return
        cur.set_one(None)

    def match_insert_artifact(norm: str) -> bool:
        return norm.startswith("insert into team_assistant_conv_artifacts")

    def handle_insert_artifact(cur: FakeCursor, params: tuple) -> None:
        cid, artifact_type, title, payload, ts = params
        art_id = next(cur.ids)
        cur.db["artifacts"].append(
            {
                "id": art_id,
                "conversation_id": cid,
                "artifact_type": artifact_type,
                "title": title,
                "payload_json": unwrap_json(payload),
                "created_at": ts,
            }
        )
        cur.set_one((art_id,))
        cur.rowcount = 1

    def match_select_artifacts(norm: str) -> bool:
        return (
            "from team_assistant_conv_artifacts a" in norm
            and "team_assistant_conversations c" in norm
        )

    def handle_select_artifacts(cur: FakeCursor, params: tuple) -> None:
        cid, team_key = params
        conv = cur.db["conversations"].get(cid)
        if conv is None or conv["team_key"] != team_key:
            cur.set_all([])
            return
        cur.set_all(
            [
                {
                    "id": a["id"],
                    "artifact_type": a["artifact_type"],
                    "title": a["title"],
                    "payload_json": a["payload_json"],
                    "created_at": a["created_at"],
                }
                for a in cur.db["artifacts"]
                if a["conversation_id"] == cid
            ]
        )

    return [
        (match_insert_conversation, handle_insert_conversation),
        (match_exists_conversation, handle_exists_conversation),
        (match_select_context, handle_select_context),
        (match_select_messages, handle_select_messages),
        (match_insert_message, handle_insert_message),
        (match_update_updated_at, handle_update_updated_at),
        (match_update_job_id, handle_update_job_id),
        (match_select_by_job_id, handle_select_by_job_id),
        (match_insert_artifact, handle_insert_artifact),
        (match_select_artifacts, handle_select_artifacts),
    ]


def install_fake_postgres(monkeypatch) -> dict[str, Any]:
    """Install a fake ``get_conn`` on ``team_assistant.store`` and return the db.

    Preconditions:
        ``monkeypatch`` is a pytest ``MonkeyPatch`` (or compatible).
    Postconditions:
        ``team_assistant.store.get_conn`` yields a shared ``FakeConn`` backed by
        the returned default db and team_assistant's SQL dispatch table.
    """
    import team_assistant.store as store_mod

    return _install_fake_postgres(
        monkeypatch,
        modules=[store_mod],
        dispatch=_dispatch(),
        db=_default_db(),
    )


__all__ = ["install_fake_postgres"]
