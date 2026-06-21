"""Dict-backed fake for ``shared_postgres.get_conn`` used by user_profile tests.

Handles exactly the SQL emitted by ``user_profile.store`` so the unit tests
run without a real Postgres (matching the default, non-integration suite).
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any


def _unwrap_json(value: Any) -> Any:
    """Unwrap a ``psycopg.types.json.Json`` wrapper to its plain object."""
    if hasattr(value, "obj"):
        return value.obj
    return value


def _default_db() -> dict[str, Any]:
    return {"profiles": {}, "associations": {}}


class _FakeCursor:
    def __init__(self, db: dict[str, Any], dict_rows: bool) -> None:
        self._db = db
        self._dict = dict_rows
        self.rowcount = 0
        self._one: Any = None
        self._all: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, params: Any = ()) -> None:  # noqa: C901
        norm = " ".join(sql.split()).lower()
        params = tuple(params)

        # -- user_profiles ------------------------------------------------
        if norm.startswith("select user_id, display_name") and "from user_profiles" in norm:
            (user_id,) = params
            row = self._db["profiles"].get(user_id)
            self._one = dict(row) if row else None
            return

        if norm.startswith("insert into user_profiles") and "do update" in norm:
            # upsert_profile: INSERT ... ON CONFLICT DO UPDATE ... RETURNING.
            cols = ["user_id", "display_name", "email", "bio", "profile_json", "created_at", "updated_at"]
            incoming = dict(zip(cols, params))
            incoming["profile_json"] = _unwrap_json(incoming["profile_json"])
            user_id = incoming["user_id"]
            existing = self._db["profiles"].get(user_id)
            if existing is None:
                self._db["profiles"][user_id] = dict(incoming)
            else:
                # On conflict, apply only the columns named in the SET clause
                # (each as ``col = excluded.col``), advancing them from EXCLUDED.
                for col in re.findall(r"(\w+)\s*=\s*excluded\.\w+", norm):
                    existing[col] = incoming[col]
            self.rowcount = 1
            self._one = dict(self._db["profiles"][user_id])
            return

        if norm.startswith("insert into user_profiles"):
            # get_profile ensure: INSERT ... ON CONFLICT DO NOTHING (3 params).
            user_id, created_at, updated_at = params
            self._db["profiles"].setdefault(
                user_id,
                {
                    "user_id": user_id,
                    "display_name": "",
                    "email": "",
                    "bio": "",
                    "profile_json": {},
                    "created_at": created_at,
                    "updated_at": updated_at,
                },
            )
            self.rowcount = 1
            return

        # -- user_profile_associations ------------------------------------
        if norm.startswith("insert into user_profile_associations"):
            assoc_id, user_id, atype, team, artifact_id, label, role, created_at = params
            key = (user_id, atype, artifact_id)
            existing = self._db["associations"].get(key)
            if existing is not None:
                existing["label"] = label
                existing["role"] = role
                self._one = dict(existing)
            else:
                row = {
                    "id": assoc_id,
                    "user_id": user_id,
                    "artifact_type": atype,
                    "team": team,
                    "artifact_id": artifact_id,
                    "label": label,
                    "role": role,
                    "created_at": created_at,
                }
                self._db["associations"][key] = row
                self._one = dict(row)
            self.rowcount = 1
            return

        if norm.startswith("select id, user_id, artifact_type") and "from user_profile_associations" in norm:
            user_id = params[0]
            atype = params[1] if len(params) > 1 else None
            rows = [
                dict(r)
                for r in self._db["associations"].values()
                if r["user_id"] == user_id and (atype is None or r["artifact_type"] == atype)
            ]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            self._all = rows
            return

        if norm.startswith("delete from user_profile_associations"):
            assoc_id, user_id = params
            match = next(
                (k for k, r in self._db["associations"].items() if r["id"] == assoc_id and r["user_id"] == user_id),
                None,
            )
            if match is not None:
                del self._db["associations"][match]
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        raise AssertionError(f"unexpected SQL in fake cursor: {sql!r}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _FakeConn:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db

    def cursor(self, row_factory=None):  # noqa: ANN001
        return _FakeCursor(self._db, dict_rows=row_factory is not None)


def install_fake_postgres(monkeypatch) -> dict[str, Any]:
    """Patch ``user_profile.store.get_conn`` with a dict-backed fake.

    Returns the underlying db dict so tests can assert on persisted state.
    """
    db = _default_db()

    @contextmanager
    def _fake_get_conn(database=None):  # noqa: ANN001
        yield _FakeConn(db)

    import user_profile.store as store_mod

    monkeypatch.setattr(store_mod, "get_conn", _fake_get_conn)
    return db


__all__ = ["install_fake_postgres"]
