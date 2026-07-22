"""Shared dict-backed fake for ``shared.postgres.get_conn`` used by user_profile tests.

Matching contract (read before changing store SQL):
    Dispatch matchers receive SQL normalized with
    ``" ".join(sql.split()).lower()`` and are tried in order (first True wins).
    This is deliberately simple and therefore *coupled to the exact SQL the
    store emits* — a wording, column-order, or clause change in a store query
    must be mirrored here or the cursor raises ``AssertionError("unexpected
    SQL")``. The matcher is intentionally NOT a SQL parser; that fragility is
    accepted because the Postgres-backed integration tests exercise the real
    SQL. Keep the two in sync: when you add/alter store SQL, update a handler
    here.

Boilerplate (``FakeCursor`` / ``FakeConn`` / normalize / install) lives in
``shared.postgres.fake`` (re-exported from ``shared.postgres.testing``); this
module owns only the user_profile SQL→handler dispatch table and the module
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
    return {"profiles": {}, "associations": {}}


def _dispatch() -> DispatchTable:
    """Return user_profile's ordered SQL→handler table.

    Preconditions:
        None.
    Postconditions:
        Returns a non-empty ``DispatchTable`` whose matcher order matches the
        historical ``_FakeCursor.execute`` if/return cascade (first match wins).
    """
    # Shared FakeCursor does not forward normalized SQL to handlers. The upsert
    # handler needs that text to parse the ON CONFLICT SET clause, so the
    # matcher stashes it here before the handler runs.
    upsert_norm: list[str] = [""]

    # -- user_profiles ----------------------------------------------------
    def match_select_profile(norm: str) -> bool:
        return norm.startswith("select user_id, display_name") and "from user_profiles" in norm

    def handle_select_profile(cur: FakeCursor, params: tuple) -> None:
        (user_id,) = params
        row = cur.db["profiles"].get(user_id)
        cur.set_one(dict(row) if row else None)

    def match_upsert_profile(norm: str) -> bool:
        if norm.startswith("insert into user_profiles") and "do update" in norm:
            upsert_norm[0] = norm
            return True
        return False

    def handle_upsert_profile(cur: FakeCursor, params: tuple) -> None:
        # upsert_profile: INSERT ... ON CONFLICT DO UPDATE ... RETURNING.
        cols = [
            "user_id",
            "display_name",
            "email",
            "bio",
            "profile_json",
            "created_at",
            "updated_at",
        ]
        incoming = dict(zip(cols, params))
        incoming["profile_json"] = unwrap_json(incoming["profile_json"])
        user_id = incoming["user_id"]
        existing = cur.db["profiles"].get(user_id)
        if existing is None:
            cur.db["profiles"][user_id] = dict(incoming)
        else:
            # On conflict, apply only the columns named in the SET clause.
            # Slice the clause text (between "do update set" and "returning")
            # and read each assignment's left-hand column — robust to the RHS
            # form (EXCLUDED.col, literal, expression) the store emits. A
            # `col = table.col || EXCLUDED.col` assignment is the store's
            # JSONB shallow merge for profile_json — mirror it as a dict
            # merge instead of a replacement.
            set_clause = upsert_norm[0].split("do update set", 1)[1].split("returning", 1)[0]
            for assignment in set_clause.split(","):
                col = assignment.split("=", 1)[0].strip()
                if col not in incoming:
                    continue
                if "||" in assignment:
                    existing[col] = {**existing[col], **incoming[col]}
                else:
                    existing[col] = incoming[col]
        cur.rowcount = 1
        cur.set_one(dict(cur.db["profiles"][user_id]))

    def match_insert_profile(norm: str) -> bool:
        return norm.startswith("insert into user_profiles")

    def handle_insert_profile(cur: FakeCursor, params: tuple) -> None:
        # get_profile ensure: INSERT ... ON CONFLICT DO NOTHING (3 params).
        # rowcount mirrors Postgres: 1 when the row is newly inserted, 0 when
        # the conflict target already existed (so get_profile knows whether it
        # can synthesize the just-inserted row or must re-read the winner's).
        user_id, created_at, updated_at = params
        if user_id in cur.db["profiles"]:
            cur.rowcount = 0
        else:
            cur.db["profiles"][user_id] = {
                "user_id": user_id,
                "display_name": "",
                "email": "",
                "bio": "",
                "profile_json": {},
                "created_at": created_at,
                "updated_at": updated_at,
            }
            cur.rowcount = 1

    # -- user_profile_associations ----------------------------------------
    def match_insert_association(norm: str) -> bool:
        return norm.startswith("insert into user_profile_associations")

    def handle_insert_association(cur: FakeCursor, params: tuple) -> None:
        assoc_id, user_id, atype, team, artifact_id, label, role, created_at = params
        key = (user_id, atype, artifact_id)
        existing = cur.db["associations"].get(key)
        if existing is not None:
            existing["label"] = label
            existing["role"] = role
            cur.set_one(dict(existing))
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
            cur.db["associations"][key] = row
            cur.set_one(dict(row))
        cur.rowcount = 1

    def match_select_associations(norm: str) -> bool:
        return (
            norm.startswith("select id, user_id, artifact_type")
            and "from user_profile_associations" in norm
        )

    def handle_select_associations(cur: FakeCursor, params: tuple) -> None:
        user_id = params[0]
        atype = params[1] if len(params) > 1 else None
        rows = [
            dict(r)
            for r in cur.db["associations"].values()
            if r["user_id"] == user_id and (atype is None or r["artifact_type"] == atype)
        ]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        cur.set_all(rows)

    def match_delete_association(norm: str) -> bool:
        return norm.startswith("delete from user_profile_associations")

    def handle_delete_association(cur: FakeCursor, params: tuple) -> None:
        assoc_id, user_id = params
        match = next(
            (
                k
                for k, r in cur.db["associations"].items()
                if r["id"] == assoc_id and r["user_id"] == user_id
            ),
            None,
        )
        if match is not None:
            del cur.db["associations"][match]
            cur.rowcount = 1
        else:
            cur.rowcount = 0

    return [
        (match_select_profile, handle_select_profile),
        (match_upsert_profile, handle_upsert_profile),
        (match_insert_profile, handle_insert_profile),
        (match_insert_association, handle_insert_association),
        (match_select_associations, handle_select_associations),
        (match_delete_association, handle_delete_association),
    ]


def install_fake_postgres(monkeypatch) -> dict[str, Any]:
    """Install a fake ``get_conn`` on ``user_profile.store`` and return the db.

    Preconditions:
        ``monkeypatch`` is a pytest ``MonkeyPatch`` (or compatible).
    Postconditions:
        ``user_profile.store.get_conn`` yields a shared ``FakeConn`` backed by
        the returned default db and user_profile's SQL dispatch table.
    """
    import user_profile.store as store_mod

    return _install_fake_postgres(
        monkeypatch,
        modules=[store_mod],
        dispatch=_dispatch(),
        db=_default_db(),
    )


__all__ = ["install_fake_postgres"]
