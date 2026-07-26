"""Shared dict-backed fake for ``shared.postgres.get_conn`` used by branding tests.

Matching contract (read before changing store SQL):
    Dispatch matchers receive SQL normalized with
    ``" ".join(sql.split()).lower()`` and are tried in order (first True wins).
    This is deliberately simple and therefore *coupled to the exact SQL the
    stores emit* — a wording, column-order, or clause change in a store query
    must be mirrored here or the cursor raises ``AssertionError("unexpected
    SQL")``. The matcher is intentionally NOT a SQL parser; that fragility is
    accepted because the authoritative correctness check is
    ``tests/test_store_real_postgres.py`` (the ``real_postgres`` marker), which
    runs the real queries against a live Postgres in the integration job. Keep
    the two in sync: when you add/alter store SQL, update a handler here and add
    real-Postgres coverage there.

Boilerplate (``FakeCursor`` / ``FakeConn`` / normalize / install) lives in
``shared.postgres.fake`` (re-exported from ``shared.postgres.testing``); this module owns only the branding SQL→handler
dispatch table and the module list passed to ``install_fake_postgres``.
"""

from __future__ import annotations

import sys
from collections import Counter
from typing import Any

from psycopg.errors import UniqueViolation

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
        "clients": {},
        "brands": {},
        "conversations": {},
        "conv_messages": [],
        "sessions": {},
    }


def _merge_brand(cur: FakeCursor, params: tuple, *, returning: bool) -> None:
    """Apply ``jsonb ||`` shallow merge for ``branding_brands``.

    Preconditions:
        ``params`` is ``(patch, brand_id, client_id)``.
    Postconditions:
        On ownership match, merges ``patch`` into the brand's data and sets
        ``rowcount`` to 1; if ``returning`` is True, ``fetchone`` yields the
        merged data row. On miss, ``rowcount`` is 0 and ``fetchone`` is None.
    """
    patch, brand_id, client_id = params
    patch = unwrap_json(patch)
    row = cur.db["brands"].get(brand_id)
    if row and row["client_id"] == client_id:
        row["data"] = {**row["data"], **patch}
        cur.rowcount = 1
        if returning:
            cur.set_one({"data": row["data"]})
    else:
        cur.rowcount = 0
        cur.set_one(None)


def _dispatch() -> DispatchTable:
    """Return branding's ordered SQL→handler table.

    Preconditions:
        None.
    Postconditions:
        Returns a non-empty ``DispatchTable`` whose matcher order matches the
        historical ``_FakeCursor.execute`` if/return cascade (first match wins).
    """

    # -- clients ----------------------------------------------------------
    def match_insert_client(norm: str) -> bool:
        return norm.startswith("insert into branding_clients")

    def handle_insert_client(cur: FakeCursor, params: tuple) -> None:
        """Emulate INSERT into branding_clients (id PRIMARY KEY).

        Preconditions:
            ``params`` is ``(client_id, data)``.
        Postconditions:
            On success, ``cur.db["clients"][client_id]`` holds the new row and
            ``cur.rowcount`` is 1.
            If ``client_id`` already exists, raises ``UniqueViolation`` and
            leaves the existing row unchanged (matches Postgres PK semantics).
        """
        client_id, data = params
        if client_id in cur.db["clients"]:
            raise UniqueViolation(
                'duplicate key value violates unique constraint "branding_clients_pkey"'
            )
        cur.db["clients"][client_id] = {
            "id": client_id,
            "data": unwrap_json(data),
        }
        cur.rowcount = 1

    def match_select_client_by_id(norm: str) -> bool:
        return norm.startswith("select data from branding_clients where id")

    def handle_select_client_by_id(cur: FakeCursor, params: tuple) -> None:
        (client_id,) = params
        row = cur.db["clients"].get(client_id)
        cur.set_one({"data": row["data"]} if row else None)

    def match_select_clients_limit(norm: str) -> bool:
        return norm.startswith("select data from branding_clients") and "limit" in norm

    def handle_select_clients_limit(cur: FakeCursor, params: tuple) -> None:
        rows = [{"data": c["data"]} for c in cur.db["clients"].values()]
        limit, offset = params[0], params[1]
        cur.set_all(rows[offset : offset + limit])

    def match_select_clients(norm: str) -> bool:
        return norm.startswith("select data from branding_clients")

    def handle_select_clients(cur: FakeCursor, params: tuple) -> None:
        cur.set_all([{"data": c["data"]} for c in cur.db["clients"].values()])

    def match_exists_client(norm: str) -> bool:
        return norm.startswith("select 1 from branding_clients where id")

    def handle_exists_client(cur: FakeCursor, params: tuple) -> None:
        (client_id,) = params
        # dict_row shape — callers only check presence via ``is not None``.
        cur.set_one({"1": 1} if client_id in cur.db["clients"] else None)

    # -- brands -----------------------------------------------------------
    def match_insert_brand(norm: str) -> bool:
        return norm.startswith("insert into branding_brands")

    def handle_insert_brand(cur: FakeCursor, params: tuple) -> None:
        """Emulate INSERT into branding_brands (id PRIMARY KEY).

        Preconditions:
            ``params`` is ``(brand_id, client_id, data)``.
        Postconditions:
            On success, ``cur.db["brands"][brand_id]`` holds the new row and
            ``cur.rowcount`` is 1.
            If ``brand_id`` already exists, raises ``UniqueViolation`` and
            leaves the existing row unchanged (matches Postgres PK semantics).
        """
        brand_id, client_id, data = params
        if brand_id in cur.db["brands"]:
            raise UniqueViolation(
                'duplicate key value violates unique constraint "branding_brands_pkey"'
            )
        cur.db["brands"][brand_id] = {
            "id": brand_id,
            "client_id": client_id,
            "data": unwrap_json(data),
        }
        cur.rowcount = 1

    def match_select_brand_data_owned(norm: str) -> bool:
        return norm.startswith("select data from branding_brands where id = %s and client_id")

    def handle_select_brand_data_owned(cur: FakeCursor, params: tuple) -> None:
        brand_id, client_id = params
        row = cur.db["brands"].get(brand_id)
        if row and row["client_id"] == client_id:
            cur.set_one({"data": row["data"]})
        else:
            cur.set_one(None)

    def match_select_brand_version(norm: str) -> bool:
        return norm.startswith("select data->>'version'") and "from branding_brands" in norm

    def handle_select_brand_version(cur: FakeCursor, params: tuple) -> None:
        brand_id, client_id = params
        row = cur.db["brands"].get(brand_id)
        if row and row["client_id"] == client_id:
            data = row["data"]
            version = data.get("version", 0)
            cur.set_one(
                {
                    "version": None if version is None else str(version),
                    "history": data.get("history", []),
                }
            )
        else:
            cur.set_one(None)

    def match_exists_brand(norm: str) -> bool:
        return norm.startswith("select 1 from branding_brands where id")

    def handle_exists_brand(cur: FakeCursor, params: tuple) -> None:
        (brand_id,) = params
        # dict_row shape — callers only check presence via ``is not None``.
        cur.set_one({"1": 1} if brand_id in cur.db["brands"] else None)

    def match_select_brand_client_data(norm: str) -> bool:
        return norm.startswith("select client_id, data from branding_brands where id")

    def handle_select_brand_client_data(cur: FakeCursor, params: tuple) -> None:
        (brand_id,) = params
        row = cur.db["brands"].get(brand_id)
        cur.set_one({"client_id": row["client_id"], "data": row["data"]} if row else None)

    def match_select_brands_any(norm: str) -> bool:
        return norm.startswith("select id, data from branding_brands where id = any")

    def handle_select_brands_any(cur: FakeCursor, params: tuple) -> None:
        (wanted,) = params
        wanted_set = set(wanted)
        cur.set_all(
            [
                {"id": b["id"], "data": b["data"]}
                for b in cur.db["brands"].values()
                if b["id"] in wanted_set
            ]
        )

    def match_select_brands_by_client_limit(norm: str) -> bool:
        return (
            norm.startswith("select data from branding_brands where client_id") and "limit" in norm
        )

    def handle_select_brands_by_client_limit(cur: FakeCursor, params: tuple) -> None:
        client_id = params[0]
        rows = [
            {"data": b["data"]} for b in cur.db["brands"].values() if b["client_id"] == client_id
        ]
        limit, offset = params[1], params[2]
        cur.set_all(rows[offset : offset + limit])

    def match_select_brands_by_client(norm: str) -> bool:
        return norm.startswith("select data from branding_brands where client_id")

    def handle_select_brands_by_client(cur: FakeCursor, params: tuple) -> None:
        client_id = params[0]
        cur.set_all(
            [{"data": b["data"]} for b in cur.db["brands"].values() if b["client_id"] == client_id]
        )

    def match_update_brand_merge_returning(norm: str) -> bool:
        return (
            norm.startswith("update branding_brands set data = data ||")
            and "returning data" in norm
        )

    def handle_update_brand_merge_returning(cur: FakeCursor, params: tuple) -> None:
        _merge_brand(cur, params, returning=True)

    def match_update_brand_merge(norm: str) -> bool:
        return norm.startswith("update branding_brands set data = data ||")

    def handle_update_brand_merge(cur: FakeCursor, params: tuple) -> None:
        _merge_brand(cur, params, returning=False)

    # -- conversations ----------------------------------------------------
    def match_insert_conversation(norm: str) -> bool:
        return norm.startswith("insert into branding_conversations")

    def handle_insert_conversation(cur: FakeCursor, params: tuple) -> None:
        """Emulate INSERT into branding_conversations (conversation_id PRIMARY KEY).

        Preconditions:
            ``params`` is ``(conversation_id, brand_id, mission, latest_output,
            created_at, updated_at)``.
        Postconditions:
            On success, ``cur.db["conversations"][conversation_id]`` holds the
            new row and ``cur.rowcount`` is 1.
            If ``conversation_id`` already exists, raises ``UniqueViolation`` and
            leaves the existing row unchanged (matches Postgres PK semantics).
        """
        cid, brand_id, mission, latest_output, created_at, updated_at = params
        if cid in cur.db["conversations"]:
            raise UniqueViolation(
                'duplicate key value violates unique constraint "branding_conversations_pkey"'
            )
        cur.db["conversations"][cid] = {
            "conversation_id": cid,
            "brand_id": brand_id,
            "mission_json": unwrap_json(mission),
            "latest_output_json": unwrap_json(latest_output),
            "created_at": created_at,
            "updated_at": updated_at,
        }
        cur.rowcount = 1

    def match_join_conv_by_id(norm: str) -> bool:
        return (
            "from branding_conversations c" in norm
            and "left join branding_conv_messages" in norm
            and "where c.conversation_id" in norm
            and "order by m.id" in norm
        )

    def handle_join_conv_by_id(cur: FakeCursor, params: tuple) -> None:
        """Emulate get_state LEFT JOIN (conversation + messages by conversation_id).

        Params: ``(conversation_id,)``.
        Returns dict rows with brand/mission/output plus message fields
        (null-role placeholder when the conversation has no messages).
        """
        (cid,) = params
        conv = cur.db["conversations"].get(cid)
        if conv is None:
            cur.set_all([])
            return
        msgs = [m for m in cur.db["conv_messages"] if m["conversation_id"] == cid]
        msgs.sort(key=lambda m: m["id"])
        base = {
            "brand_id": conv["brand_id"],
            "mission_json": conv["mission_json"],
            "latest_output_json": conv["latest_output_json"],
        }
        if not msgs:
            cur.set_all([{**base, "role": None, "content": None, "timestamp": None}])
        else:
            cur.set_all(
                [
                    {
                        **base,
                        "role": m["role"],
                        "content": m["content"],
                        "timestamp": m["timestamp"],
                    }
                    for m in msgs
                ]
            )

    def match_join_conv_by_brand(norm: str) -> bool:
        return (
            "from branding_conversations c" in norm
            and "left join branding_conv_messages" in norm
            and "where c.brand_id" in norm
            and "order by m.id" in norm
        )

    def handle_join_conv_by_brand(cur: FakeCursor, params: tuple) -> None:
        """Emulate get_by_brand_id LEFT JOIN (conversation + messages by brand_id).

        Params: ``(brand_id,)``.
        Returns dict rows with conversation_id/mission/output plus message fields
        (null-role placeholder when empty); empty list when no conversation matches.
        """
        (brand_id,) = params
        conv = next(
            (c for c in cur.db["conversations"].values() if c["brand_id"] == brand_id),
            None,
        )
        if conv is None:
            cur.set_all([])
            return
        cid = conv["conversation_id"]
        msgs = [m for m in cur.db["conv_messages"] if m["conversation_id"] == cid]
        msgs.sort(key=lambda m: m["id"])
        base = {
            "conversation_id": cid,
            "mission_json": conv["mission_json"],
            "latest_output_json": conv["latest_output_json"],
        }
        if not msgs:
            cur.set_all([{**base, "role": None, "content": None, "timestamp": None}])
        else:
            cur.set_all(
                [
                    {
                        **base,
                        "role": m["role"],
                        "content": m["content"],
                        "timestamp": m["timestamp"],
                    }
                    for m in msgs
                ]
            )

    def match_select_mission_output(norm: str) -> bool:
        return norm.startswith(
            "select mission_json, latest_output_json from branding_conversations where conversation_id"
        )

    def handle_select_mission_output(cur: FakeCursor, params: tuple) -> None:
        (cid,) = params
        conv = cur.db["conversations"].get(cid)
        if conv is None:
            cur.set_one(None)
        else:
            cur.set_one(
                {
                    "mission_json": conv["mission_json"],
                    "latest_output_json": conv["latest_output_json"],
                }
            )

    def match_exists_conversation(norm: str) -> bool:
        return norm.startswith("select 1 from branding_conversations where conversation_id")

    def handle_exists_conversation(cur: FakeCursor, params: tuple) -> None:
        (cid,) = params
        # dict_row shape — callers only check presence via ``is not None``.
        cur.set_one({"1": 1} if cid in cur.db["conversations"] else None)

    def match_cte_insert_message(norm: str) -> bool:
        return norm.startswith("with conv as") and "insert into branding_conv_messages" in norm

    def handle_cte_insert_message(cur: FakeCursor, params: tuple) -> None:
        """Emulate append_message CTE (bump updated_at + insert message).

        Params: ``(updated_at, conversation_id, role, content, message_timestamp)``.
        Returns ``{'id': new_message_id}`` on success, or None/rowcount 0 if the
        conversation does not exist.
        """
        ts, cid, role, content, ts2 = params
        conv = cur.db["conversations"].get(cid)
        if conv is None:
            cur.set_one(None)
            cur.rowcount = 0
            return
        conv["updated_at"] = ts
        new_id = next(cur.ids)
        cur.db["conv_messages"].append(
            {
                "id": new_id,
                "conversation_id": cid,
                "role": role,
                "content": content,
                "timestamp": ts2,
            }
        )
        cur.set_one({"id": new_id})
        cur.rowcount = 1

    def match_update_conv_mission(norm: str) -> bool:
        return norm.startswith("update branding_conversations set mission_json")

    def handle_update_conv_mission(cur: FakeCursor, params: tuple) -> None:
        """Emulate update_mission SET mission_json + updated_at.

        Params: ``(mission_json, updated_at, conversation_id)``.
        Sets ``rowcount`` to 1 on match, else 0.
        """
        mission, ts, cid = params
        conv = cur.db["conversations"].get(cid)
        if conv:
            conv["mission_json"] = unwrap_json(mission)
            conv["updated_at"] = ts
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    def match_update_conv_output(norm: str) -> bool:
        return norm.startswith("update branding_conversations set latest_output_json")

    def handle_update_conv_output(cur: FakeCursor, params: tuple) -> None:
        """Emulate update_output SET latest_output_json + updated_at.

        Params: ``(latest_output_json, updated_at, conversation_id)``.
        Sets ``rowcount`` to 1 on match, else 0.
        """
        output, ts, cid = params
        conv = cur.db["conversations"].get(cid)
        if conv:
            conv["latest_output_json"] = unwrap_json(output)
            conv["updated_at"] = ts
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    def match_update_conv_brand_and_mission(norm: str) -> bool:
        return norm.startswith("update branding_conversations set brand_id = %s, mission_json")

    def handle_update_conv_brand_and_mission(cur: FakeCursor, params: tuple) -> None:
        """Emulate attach_and_update_mission SET brand_id + mission_json + updated_at.

        Params: ``(brand_id, mission_json, updated_at, conversation_id)``.
        Sets ``rowcount`` to 1 on match, else 0.
        """
        brand_id, mission, ts, cid = params
        conv = cur.db["conversations"].get(cid)
        if conv:
            conv["brand_id"] = brand_id
            conv["mission_json"] = unwrap_json(mission)
            conv["updated_at"] = ts
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    def match_update_conv_brand(norm: str) -> bool:
        return norm.startswith("update branding_conversations set brand_id")

    def handle_update_conv_brand(cur: FakeCursor, params: tuple) -> None:
        """Emulate set_brand SET brand_id + updated_at.

        Params: ``(brand_id, updated_at, conversation_id)``.
        Sets ``rowcount`` to 1 on match, else 0.
        """
        brand_id, ts, cid = params
        conv = cur.db["conversations"].get(cid)
        if conv:
            conv["brand_id"] = brand_id
            conv["updated_at"] = ts
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    def match_list_conversations(norm: str) -> bool:
        return (
            norm.startswith(
                "select c.conversation_id, c.brand_id, c.created_at, c.updated_at,"
                " count(m.id) as message_count"
            )
            and "from branding_conversations c" in norm
            and "order by c.updated_at desc" in norm
        )

    def handle_list_conversations(cur: FakeCursor, params: tuple) -> None:
        """Emulate list_conversations GROUP BY summary query.

        Params: ``(brand_id,)`` when filtered, else ``()``.
        Returns summary dict rows ordered by ``updated_at`` descending, each
        with ``message_count`` from ``conv_messages``.
        """
        target_brand = params[0] if params else None
        convs = sorted(
            cur.db["conversations"].values(),
            key=lambda c: c["updated_at"],
            reverse=True,
        )
        if target_brand is not None:
            convs = [c for c in convs if c["brand_id"] == target_brand]
        msg_counts = Counter(m["conversation_id"] for m in cur.db["conv_messages"])
        rows = [
            {
                "conversation_id": c["conversation_id"],
                "brand_id": c["brand_id"],
                "created_at": c["created_at"],
                "updated_at": c["updated_at"],
                "message_count": msg_counts[c["conversation_id"]],
            }
            for c in convs
        ]
        cur.set_all(rows)

    def match_select_conv_brand_id(norm: str) -> bool:
        return norm.startswith("select brand_id from branding_conversations where conversation_id")

    def handle_select_conv_brand_id(cur: FakeCursor, params: tuple) -> None:
        """Emulate get_conversation_brand_id SELECT.

        Params: ``(conversation_id,)``.
        Returns ``{'brand_id': ...}`` when the conversation exists, else None.
        """
        (cid,) = params
        conv = cur.db["conversations"].get(cid)
        # Match ``dict_row``: a row dict when the conversation exists, else None.
        if conv is None:
            cur.set_one(None)
        else:
            cur.set_one({"brand_id": conv["brand_id"]})

    # -- sessions ---------------------------------------------------------
    def match_insert_session(norm: str) -> bool:
        return norm.startswith("insert into branding_sessions")

    def handle_insert_session(cur: FakeCursor, params: tuple) -> None:
        """Emulate INSERT into branding_sessions (session_id PRIMARY KEY).

        Preconditions:
            ``params`` is ``(session_id, session_json, updated_at)``.
        Postconditions:
            On success, ``cur.db["sessions"][session_id]`` holds the new row.
            If ``session_id`` already exists, raises ``UniqueViolation`` and
            leaves the existing row unchanged (matches Postgres PK semantics).
        """
        session_id, session_json, updated_at = params
        if session_id in cur.db["sessions"]:
            raise UniqueViolation(
                'duplicate key value violates unique constraint "branding_sessions_pkey"'
            )
        cur.db["sessions"][session_id] = {
            "session_id": session_id,
            "session_json": unwrap_json(session_json),
            "updated_at": updated_at,
        }

    def match_select_session(norm: str) -> bool:
        return norm.startswith("select session_json from branding_sessions where session_id")

    def handle_select_session(cur: FakeCursor, params: tuple) -> None:
        (session_id,) = params
        row = cur.db["sessions"].get(session_id)
        cur.set_one({"session_json": row["session_json"]} if row else None)

    def match_update_session(norm: str) -> bool:
        return norm.startswith("update branding_sessions set session_json")

    def handle_update_session(cur: FakeCursor, params: tuple) -> None:
        session_json, ts, session_id = params
        row = cur.db["sessions"].get(session_id)
        if row:
            row["session_json"] = unwrap_json(session_json)
            row["updated_at"] = ts

    return [
        (match_insert_client, handle_insert_client),
        (match_select_client_by_id, handle_select_client_by_id),
        (match_select_clients_limit, handle_select_clients_limit),
        (match_select_clients, handle_select_clients),
        (match_exists_client, handle_exists_client),
        (match_insert_brand, handle_insert_brand),
        (match_select_brand_data_owned, handle_select_brand_data_owned),
        (match_select_brand_version, handle_select_brand_version),
        (match_exists_brand, handle_exists_brand),
        (match_select_brand_client_data, handle_select_brand_client_data),
        (match_select_brands_any, handle_select_brands_any),
        (match_select_brands_by_client_limit, handle_select_brands_by_client_limit),
        (match_select_brands_by_client, handle_select_brands_by_client),
        (match_update_brand_merge_returning, handle_update_brand_merge_returning),
        (match_update_brand_merge, handle_update_brand_merge),
        (match_insert_conversation, handle_insert_conversation),
        (match_join_conv_by_id, handle_join_conv_by_id),
        (match_join_conv_by_brand, handle_join_conv_by_brand),
        (match_select_mission_output, handle_select_mission_output),
        (match_exists_conversation, handle_exists_conversation),
        (match_cte_insert_message, handle_cte_insert_message),
        (match_update_conv_mission, handle_update_conv_mission),
        (match_update_conv_output, handle_update_conv_output),
        (match_update_conv_brand_and_mission, handle_update_conv_brand_and_mission),
        (match_update_conv_brand, handle_update_conv_brand),
        (match_list_conversations, handle_list_conversations),
        (match_select_conv_brand_id, handle_select_conv_brand_id),
        (match_insert_session, handle_insert_session),
        (match_select_session, handle_select_session),
        (match_update_session, handle_update_session),
    ]


def install_fake_postgres(monkeypatch) -> dict[str, Any]:
    """Install a fake ``get_conn`` on the branding stores and return the db.

    Preconditions:
        ``monkeypatch`` is a pytest ``MonkeyPatch`` (or compatible).
        All store classes reach Postgres only through ``PostgresHelperMixin``
        in ``branding_team._db`` (their modules are not patched here).
    Postconditions:
        ``branding_team._db.get_conn`` is patched to yield a shared ``FakeConn``
        backed by the returned default db and branding's SQL dispatch table.
        ``api.state`` is patched only if it still exposes ``get_conn``.
    """
    import branding_team._db as db_mod

    # BrandingStore, BrandingConversationStore, and BrandingSessionStore all use
    # PostgresHelperMixin from branding_team._db, so patching _db.get_conn covers them.
    modules: list[Any] = [db_mod]

    api_state = sys.modules.get("branding_team.api.state")
    if api_state is not None and hasattr(api_state, "get_conn"):
        modules.append(api_state)

    return _install_fake_postgres(
        monkeypatch,
        modules=modules,
        dispatch=_dispatch(),
        db=_default_db(),
    )


__all__ = ["install_fake_postgres"]
