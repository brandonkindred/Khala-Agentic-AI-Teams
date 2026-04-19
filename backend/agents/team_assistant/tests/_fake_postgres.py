"""Shared dict-backed fake for ``shared_postgres.get_conn`` used by team_assistant tests.

Approximates just enough of ``team_assistant_*`` table behaviour — in
particular, the ``team_key`` scoping — to exercise the store's ownership
checks without requiring a live Postgres.
"""

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
        "conversations": {},  # keyed by conversation_id -> row dict (incl. team_key)
        "messages": [],
        "artifacts": [],
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

        # INSERT conversation
        if norm.startswith("insert into team_assistant_conversations"):
            cid, team_key, job_id, ctx, created_at, updated_at = params
            self._db["conversations"][cid] = {
                "conversation_id": cid,
                "team_key": team_key,
                "job_id": job_id,
                "context_json": _unwrap_json(ctx),
                "created_at": created_at,
                "updated_at": updated_at,
            }
            self.rowcount = 1
            return

        # SELECT 1 FROM team_assistant_conversations WHERE conversation_id=%s AND team_key=%s
        if norm.startswith(
            "select 1 from team_assistant_conversations where conversation_id = %s and team_key"
        ):
            cid, team_key = params
            conv = self._db["conversations"].get(cid)
            self._last_fetch_one = (1,) if conv and conv["team_key"] == team_key else None
            return

        # SELECT context_json scoped by (cid, team_key)
        if norm.startswith(
            "select context_json from team_assistant_conversations where conversation_id = %s and team_key"
        ):
            cid, team_key = params
            conv = self._db["conversations"].get(cid)
            if conv and conv["team_key"] == team_key:
                self._last_fetch_one = {"context_json": conv["context_json"]}
            else:
                self._last_fetch_one = None
            return

        # SELECT messages ordered by id
        if norm.startswith(
            "select role, content, timestamp from team_assistant_conv_messages where conversation_id"
        ):
            (cid,) = params
            rows = [
                {"role": m["role"], "content": m["content"], "timestamp": m["timestamp"]}
                for m in self._db["messages"]
                if m["conversation_id"] == cid
            ]
            self._last_fetch_all = rows
            return

        # INSERT message
        if norm.startswith("insert into team_assistant_conv_messages"):
            cid, role, content, ts = params
            self._db["messages"].append(
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

        # UPDATE conversation updated_at
        if norm.startswith(
            "update team_assistant_conversations set updated_at = %s where conversation_id = %s and team_key"
        ):
            ts, cid, team_key = params
            conv = self._db["conversations"].get(cid)
            if conv and conv["team_key"] == team_key:
                conv["updated_at"] = ts
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # UPDATE conversation job_id (store.link_job)
        if norm.startswith(
            "update team_assistant_conversations set job_id = %s, updated_at = %s "
            "where conversation_id = %s and team_key"
        ):
            job_id, ts, cid, team_key = params
            conv = self._db["conversations"].get(cid)
            if conv and conv["team_key"] == team_key:
                conv["job_id"] = job_id
                conv["updated_at"] = ts
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # SELECT conversation_id by job_id
        if norm.startswith(
            "select conversation_id from team_assistant_conversations where job_id = %s and team_key"
        ):
            job_id, team_key = params
            for conv in self._db["conversations"].values():
                if conv.get("job_id") == job_id and conv["team_key"] == team_key:
                    self._last_fetch_one = (conv["conversation_id"],)
                    return
            self._last_fetch_one = None
            return

        # INSERT artifact ... RETURNING id
        if norm.startswith("insert into team_assistant_conv_artifacts"):
            cid, artifact_type, title, payload, ts = params
            art_id = next(self._ids)
            self._db["artifacts"].append(
                {
                    "id": art_id,
                    "conversation_id": cid,
                    "artifact_type": artifact_type,
                    "title": title,
                    "payload_json": _unwrap_json(payload),
                    "created_at": ts,
                }
            )
            self._last_fetch_one = (art_id,)
            self.rowcount = 1
            return

        # SELECT artifacts scoped by conversation's team_key via JOIN
        if (
            "from team_assistant_conv_artifacts a" in norm
            and "team_assistant_conversations c" in norm
        ):
            cid, team_key = params
            conv = self._db["conversations"].get(cid)
            if conv is None or conv["team_key"] != team_key:
                self._last_fetch_all = []
                return
            self._last_fetch_all = [
                {
                    "id": a["id"],
                    "artifact_type": a["artifact_type"],
                    "title": a["title"],
                    "payload_json": a["payload_json"],
                    "created_at": a["created_at"],
                }
                for a in self._db["artifacts"]
                if a["conversation_id"] == cid
            ]
            return

        # Everything else not exercised by these tests
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
    """Install a fake ``get_conn`` on ``team_assistant.store`` and return the db."""
    db = _default_db()
    ids = itertools.count(1)

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db, ids)

    import team_assistant.store as store_mod

    monkeypatch.setattr(store_mod, "get_conn", _fake_get_conn)
    return db


__all__ = ["install_fake_postgres", "datetime"]
