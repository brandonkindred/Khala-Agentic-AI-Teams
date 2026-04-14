"""Shared dict-backed fake for ``shared_postgres.get_conn`` used by branding tests."""

from __future__ import annotations

import itertools
from contextlib import contextmanager
from datetime import datetime
from typing import Any


def _unwrap_json(value: Any) -> Any:
    if hasattr(value, "obj"):
        return value.obj
    return value


def _default_db() -> dict[str, Any]:
    return {
        "clients": {},
        "brands": {},
        "conversations": {},
        "conv_messages": [],
        "sessions": {},
    }


class _FakeCursor:
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

        # -- clients ------------------------------------------------------
        if norm.startswith("insert into branding_clients"):
            client_id, data = params
            self._db["clients"][client_id] = {
                "id": client_id,
                "data": _unwrap_json(data),
            }
            self.rowcount = 1
            return

        if norm.startswith("select data from branding_clients where id"):
            (client_id,) = params
            row = self._db["clients"].get(client_id)
            self._last_fetch_one = {"data": row["data"]} if row else None
            return

        if norm.startswith("select data from branding_clients"):
            self._last_fetch_all = [{"data": c["data"]} for c in self._db["clients"].values()]
            return

        if norm.startswith("select 1 from branding_clients where id"):
            (client_id,) = params
            self._last_fetch_one = (1,) if client_id in self._db["clients"] else None
            return

        # -- brands -------------------------------------------------------
        if norm.startswith("insert into branding_brands"):
            brand_id, client_id, data = params
            self._db["brands"][brand_id] = {
                "id": brand_id,
                "client_id": client_id,
                "data": _unwrap_json(data),
            }
            self.rowcount = 1
            return

        if norm.startswith("select data from branding_brands where id = %s and client_id"):
            brand_id, client_id = params
            row = self._db["brands"].get(brand_id)
            if row and row["client_id"] == client_id:
                self._last_fetch_one = {"data": row["data"]}
            else:
                self._last_fetch_one = None
            return

        if norm.startswith("select data from branding_brands where client_id"):
            (client_id,) = params
            rows = [
                {"data": b["data"]}
                for b in self._db["brands"].values()
                if b["client_id"] == client_id
            ]
            self._last_fetch_all = rows
            return

        if norm.startswith("update branding_brands set data"):
            data, brand_id, client_id = params
            row = self._db["brands"].get(brand_id)
            if row and row["client_id"] == client_id:
                row["data"] = _unwrap_json(data)
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # -- conversations ------------------------------------------------
        if norm.startswith("insert into branding_conversations"):
            cid, brand_id, mission, latest_output, created_at, updated_at = params
            self._db["conversations"][cid] = {
                "conversation_id": cid,
                "brand_id": brand_id,
                "mission_json": _unwrap_json(mission),
                "latest_output_json": _unwrap_json(latest_output),
                "created_at": created_at,
                "updated_at": updated_at,
            }
            self.rowcount = 1
            return

        if norm.startswith(
            "select mission_json, latest_output_json from branding_conversations where conversation_id"
        ):
            (cid,) = params
            conv = self._db["conversations"].get(cid)
            if conv is None:
                self._last_fetch_one = None
            else:
                self._last_fetch_one = {
                    "mission_json": conv["mission_json"],
                    "latest_output_json": conv["latest_output_json"],
                }
            return

        if norm.startswith("select 1 from branding_conversations where conversation_id"):
            (cid,) = params
            self._last_fetch_one = (1,) if cid in self._db["conversations"] else None
            return

        if norm.startswith(
            "select role, content, timestamp from branding_conv_messages where conversation_id"
        ):
            (cid,) = params
            self._last_fetch_all = [
                {"role": m["role"], "content": m["content"], "timestamp": m["timestamp"]}
                for m in self._db["conv_messages"]
                if m["conversation_id"] == cid
            ]
            return

        if norm.startswith("insert into branding_conv_messages"):
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
            self.rowcount = 1
            return

        if norm.startswith("update branding_conversations set updated_at"):
            ts, cid = params
            conv = self._db["conversations"].get(cid)
            if conv:
                conv["updated_at"] = ts
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if norm.startswith("update branding_conversations set mission_json"):
            mission, ts, cid = params
            conv = self._db["conversations"].get(cid)
            if conv:
                conv["mission_json"] = _unwrap_json(mission)
                conv["updated_at"] = ts
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if norm.startswith("update branding_conversations set latest_output_json"):
            output, ts, cid = params
            conv = self._db["conversations"].get(cid)
            if conv:
                conv["latest_output_json"] = _unwrap_json(output)
                conv["updated_at"] = ts
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if norm.startswith("update branding_conversations set brand_id"):
            brand_id, ts, cid = params
            conv = self._db["conversations"].get(cid)
            if conv:
                conv["brand_id"] = brand_id
                conv["updated_at"] = ts
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        if norm.startswith(
            "select conversation_id, mission_json, latest_output_json from branding_conversations where brand_id"
        ):
            (brand_id,) = params
            match = next(
                (c for c in self._db["conversations"].values() if c["brand_id"] == brand_id),
                None,
            )
            if match:
                self._last_fetch_one = {
                    "conversation_id": match["conversation_id"],
                    "mission_json": match["mission_json"],
                    "latest_output_json": match["latest_output_json"],
                }
            else:
                self._last_fetch_one = None
            return

        if "from branding_conversations c" in norm and "order by c.updated_at desc" in norm:
            target_brand = params[0] if params else None
            convs = sorted(
                self._db["conversations"].values(),
                key=lambda c: c["updated_at"],
                reverse=True,
            )
            if target_brand is not None:
                convs = [c for c in convs if c["brand_id"] == target_brand]
            rows = []
            for c in convs:
                count = sum(
                    1
                    for m in self._db["conv_messages"]
                    if m["conversation_id"] == c["conversation_id"]
                )
                rows.append(
                    {
                        "conversation_id": c["conversation_id"],
                        "brand_id": c["brand_id"],
                        "created_at": c["created_at"],
                        "updated_at": c["updated_at"],
                        "message_count": count,
                    }
                )
            self._last_fetch_all = rows
            return

        if norm.startswith("select brand_id from branding_conversations where conversation_id"):
            (cid,) = params
            conv = self._db["conversations"].get(cid)
            self._last_fetch_one = (conv["brand_id"] if conv else None,) if conv else None
            return

        # -- sessions -----------------------------------------------------
        if norm.startswith("insert into branding_sessions"):
            session_id, session_json, updated_at = params
            self._db["sessions"][session_id] = {
                "session_id": session_id,
                "session_json": _unwrap_json(session_json),
                "updated_at": updated_at,
            }
            return

        if norm.startswith("select session_json from branding_sessions where session_id"):
            (session_id,) = params
            row = self._db["sessions"].get(session_id)
            self._last_fetch_one = {"session_json": row["session_json"]} if row else None
            return

        if norm.startswith("update branding_sessions set session_json"):
            session_json, ts, session_id = params
            row = self._db["sessions"].get(session_id)
            if row:
                row["session_json"] = _unwrap_json(session_json)
                row["updated_at"] = ts
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
    """Install a fake ``get_conn`` on the branding stores and return the db."""
    db = _default_db()
    ids = itertools.count(1)

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db, ids)

    import branding_team.assistant.store as assistant_store_mod
    import branding_team.store as store_mod

    monkeypatch.setattr(store_mod, "get_conn", _fake_get_conn)
    monkeypatch.setattr(assistant_store_mod, "get_conn", _fake_get_conn)

    # ``branding_team.api.main`` imports ``get_conn`` at module scope for the
    # BrandingSessionStore. Patch there too when already imported.
    import sys

    api_main = sys.modules.get("branding_team.api.main")
    if api_main is not None:
        monkeypatch.setattr(api_main, "get_conn", _fake_get_conn)
    return db


# Suppress unused-import warnings in downstream test files
__all__ = ["install_fake_postgres", "datetime"]
