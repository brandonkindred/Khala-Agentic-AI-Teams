"""Unit tests for ``PersonaStore`` CRUD + builtin seeding.

Same in-process Postgres-fake pattern as ``test_store.py`` — a small
dict-backed fake handles the SQL the store issues. Verifies CRUD round
trips, that builtins are NOT guarded against update/delete (per user
choice), and that seeding is idempotent on the slug.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest


class _FakePersonaCursor:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db
        self.rowcount = 0
        self._last_fetch_one: dict | None = None
        self._last_fetch_all: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, params: tuple | list = ()) -> None:
        sql_l = " ".join(sql.split()).lower()
        params = tuple(params)

        # SELECT list
        if (
            sql_l.startswith("select persona_id, name, description")
            and "where persona_id" not in sql_l
        ):
            rows = list(self._db["personas"].values())
            rows.sort(key=lambda r: (not r["is_builtin"], r["created_at"]))
            self._last_fetch_all = [dict(r) for r in rows]
            return

        # SELECT by id
        if (
            sql_l.startswith("select persona_id, name, description")
            and "where persona_id = %s" in sql_l
        ):
            (pid,) = params
            row = self._db["personas"].get(pid)
            self._last_fetch_one = dict(row) if row else None
            return

        # INSERT (used by create_persona AND seed_builtins via ON CONFLICT)
        if sql_l.startswith("insert into user_agent_founder_personas"):
            (
                persona_id,
                name,
                description,
                icon,
                system_prompt,
                spec_generation_prompt,
                is_builtin,
                created_at,
                updated_at,
            ) = params
            if persona_id in self._db["personas"] and "on conflict" in sql_l:
                # Idempotent seed: row already present.
                self.rowcount = 0
                self._last_fetch_one = None
                return
            row = {
                "persona_id": persona_id,
                "name": name,
                "description": description,
                "icon": icon,
                "system_prompt": system_prompt,
                "spec_generation_prompt": spec_generation_prompt,
                "is_builtin": is_builtin,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            self._db["personas"][persona_id] = row
            self.rowcount = 1
            self._last_fetch_one = dict(row)
            return

        # UPDATE persona ... RETURNING
        if sql_l.startswith("update user_agent_founder_personas set "):
            set_part = sql[sql.lower().index(" set ") + 5 : sql.lower().rindex(" where ")]
            cols = [c.strip().split(" ")[0] for c in set_part.split(",")]
            persona_id = params[-1]
            col_values = dict(zip(cols, params[:-1], strict=True))
            row = self._db["personas"].get(persona_id)
            if row is None:
                self.rowcount = 0
                self._last_fetch_one = None
                return
            row.update(col_values)
            self.rowcount = 1
            self._last_fetch_one = dict(row)
            return

        # DELETE persona
        if sql_l.startswith("delete from user_agent_founder_personas"):
            (persona_id,) = params
            if persona_id in self._db["personas"]:
                del self._db["personas"][persona_id]
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        raise AssertionError(f"unexpected SQL in persona fake cursor: {sql!r}")

    def fetchone(self):
        return self._last_fetch_one

    def fetchall(self):
        return self._last_fetch_all


class _FakeConn:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db

    def cursor(self, row_factory=None):  # noqa: ANN001
        return _FakePersonaCursor(self._db)


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch):
    db: dict[str, Any] = {"personas": {}}

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db)

    import agent_team_studio.user_agent_founder.store as store_mod

    monkeypatch.setattr(store_mod, "get_conn", _fake_get_conn)
    yield db


@pytest.fixture
def persona_store(fake_pg):
    from agent_team_studio.user_agent_founder.store import PersonaStore

    return PersonaStore()


def test_create_persona_returns_dataclass_with_uuid_id(persona_store, fake_pg):
    p = persona_store.create_persona(
        name="QA Bot",
        description="aggressive QA",
        icon="bug_report",
        system_prompt="be picky",
        spec_generation_prompt="write a spec",
    )
    assert p.name == "QA Bot"
    assert p.is_builtin is False
    assert re.fullmatch(r"[0-9a-f]{32}", p.persona_id)
    assert p.persona_id in fake_pg["personas"]


def test_get_persona_returns_none_for_unknown(persona_store):
    assert persona_store.get_persona("nope") is None


def test_get_persona_round_trips(persona_store):
    created = persona_store.create_persona(
        name="A",
        description="d",
        icon="i",
        system_prompt="s",
        spec_generation_prompt="g",
    )
    fetched = persona_store.get_persona(created.persona_id)
    assert fetched is not None
    assert fetched.name == "A"
    assert fetched.system_prompt == "s"


def test_list_personas_orders_builtins_first(persona_store):
    custom = persona_store.create_persona(
        name="Custom",
        description="d",
        icon="i",
        system_prompt="s",
        spec_generation_prompt="g",
    )
    builtin = persona_store.create_persona(
        persona_id="startup-founder",
        name="Startup Founder",
        description="d",
        icon="rocket_launch",
        system_prompt="s",
        spec_generation_prompt="g",
        is_builtin=True,
    )
    rows = persona_store.list_personas()
    assert rows[0].persona_id == builtin.persona_id
    assert rows[1].persona_id == custom.persona_id


def test_update_persona_changes_fields_and_bumps_updated_at(persona_store, fake_pg):
    p = persona_store.create_persona(
        name="A",
        description="d",
        icon="i",
        system_prompt="s",
        spec_generation_prompt="g",
    )
    original = fake_pg["personas"][p.persona_id]["updated_at"]
    updated = persona_store.update_persona(p.persona_id, name="B", description="d2")
    assert updated is not None
    assert updated.name == "B"
    assert updated.description == "d2"
    assert fake_pg["personas"][p.persona_id]["updated_at"] >= original


def test_update_persona_returns_none_when_missing(persona_store):
    assert persona_store.update_persona("ghost", name="x") is None


def test_update_persona_with_no_changes_returns_current_row(persona_store):
    p = persona_store.create_persona(
        name="A",
        description="d",
        icon="i",
        system_prompt="s",
        spec_generation_prompt="g",
    )
    same = persona_store.update_persona(p.persona_id)
    assert same is not None
    assert same.name == "A"


def test_update_persona_works_on_builtin(persona_store):
    """Per user choice: builtins are editable like any other persona."""
    persona_store.create_persona(
        persona_id="startup-founder",
        name="Startup Founder",
        description="d",
        icon="rocket_launch",
        system_prompt="s",
        spec_generation_prompt="g",
        is_builtin=True,
    )
    updated = persona_store.update_persona("startup-founder", description="customized")
    assert updated is not None
    assert updated.description == "customized"
    assert updated.is_builtin is True  # flag preserved


def test_delete_persona_removes_row(persona_store, fake_pg):
    p = persona_store.create_persona(
        name="A",
        description="d",
        icon="i",
        system_prompt="s",
        spec_generation_prompt="g",
    )
    assert persona_store.delete_persona(p.persona_id) is True
    assert p.persona_id not in fake_pg["personas"]


def test_delete_persona_works_on_builtin(persona_store, fake_pg):
    """Per user choice: builtins are deletable."""
    persona_store.create_persona(
        persona_id="startup-founder",
        name="Startup Founder",
        description="d",
        icon="rocket_launch",
        system_prompt="s",
        spec_generation_prompt="g",
        is_builtin=True,
    )
    assert persona_store.delete_persona("startup-founder") is True
    assert "startup-founder" not in fake_pg["personas"]


def test_delete_persona_returns_false_when_missing(persona_store):
    assert persona_store.delete_persona("ghost") is False


def test_seed_builtins_inserts_when_missing(persona_store, fake_pg):
    assert persona_store.seed_builtins() is True
    row = fake_pg["personas"]["startup-founder"]
    assert row["is_builtin"] is True
    assert row["name"] == "Startup Founder"


def test_seed_builtins_is_idempotent(persona_store, fake_pg):
    assert persona_store.seed_builtins() is True
    # Hand-mutate to verify the second call doesn't overwrite.
    fake_pg["personas"]["startup-founder"]["description"] = "user edited"
    assert persona_store.seed_builtins() is False
    assert fake_pg["personas"]["startup-founder"]["description"] == "user edited"


def test_seed_builtins_recreates_after_delete(persona_store):
    """Documents the intended side effect: delete a builtin → next seed re-inserts."""
    persona_store.seed_builtins()
    persona_store.delete_persona("startup-founder")
    assert persona_store.seed_builtins() is True
    assert persona_store.get_persona("startup-founder") is not None


def test_get_persona_store_is_lazy_and_cached(fake_pg, monkeypatch):
    import agent_team_studio.user_agent_founder.store as store_mod

    monkeypatch.setattr(store_mod, "_default_persona_store", None)
    a = store_mod.get_persona_store()
    b = store_mod.get_persona_store()
    assert a is b


def test_create_persona_round_trips_explicit_id_and_timestamps(persona_store):
    p = persona_store.create_persona(
        persona_id="explicit-id",
        name="Custom",
        description="d",
        icon="i",
        system_prompt="s",
        spec_generation_prompt="g",
    )
    assert p.persona_id == "explicit-id"
    assert isinstance(p.created_at, str)
    # ISO formatting marker.
    assert "T" in p.created_at
    # Unwrapped from datetime?
    parsed = datetime.fromisoformat(p.created_at)
    assert parsed.tzinfo is timezone.utc
