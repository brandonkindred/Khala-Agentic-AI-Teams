"""Unit tests for ``startup_advisor.store`` (PR 2, Postgres backend).

These tests mock ``shared_postgres.get_conn`` with a tiny dict-backed
fake that approximates Postgres behaviour just enough to exercise the
store's SQL shape, parameter binding, and control flow. They run
without a live Postgres; the integration coverage against a real
``postgres:18`` service container lives in the ``test-shared-postgres``
CI job that PR 0 added.

The fake keeps three Python structures that mirror the three
``startup_advisor_*`` tables (conversations, messages, artifacts) and
routes each incoming SQL string to a small handler based on a keyword
match. That lets the tests verify the real store's read/write flow
(e.g. ``append_message`` SELECTs to check the conversation exists,
then INSERTs + UPDATEs) without stringly-typed SQL assertions.
"""

from __future__ import annotations

import itertools
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Minimal fake Postgres that speaks the subset of SQL the store issues
# ---------------------------------------------------------------------------


class _FakeCursor:
    """A tiny cursor that routes each SQL statement to a handler.

    Only the statements the store actually issues are implemented. The
    handlers mutate the shared ``_db`` dict passed in, and the cursor
    tracks ``rowcount`` / last ``fetchone`` / last ``fetchall`` so the
    store's control flow (SELECT-then-INSERT-or-UPDATE) works.
    """

    def __init__(self, db: dict[str, Any], ids: itertools.count) -> None:
        self._db = db
        self._ids = ids
        self.rowcount = 0
        self._last_fetch_one: tuple | dict | None = None
        self._last_fetch_all: list = []

    # Context-manager support for ``with conn.cursor() as cur:``
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    # -- SQL routing --------------------------------------------------------

    def execute(self, sql: str, params: tuple = ()) -> None:
        sql_l = " ".join(sql.split()).lower()

        # INSERT conversations
        if sql_l.startswith("insert into startup_advisor_conversations"):
            cid, ctx, created_at, updated_at = params
            # psycopg3 ``Json(dict)`` wraps a dict; our fake uses the raw dict.
            ctx_dict = ctx.obj if hasattr(ctx, "obj") else ctx
            self._db["conversations"][cid] = {
                "conversation_id": cid,
                "context_json": ctx_dict,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            self.rowcount = 1
            return

        # SELECT context_json FROM conversations WHERE conversation_id = %s
        if sql_l.startswith("select context_json from startup_advisor_conversations"):
            (cid,) = params
            conv = self._db["conversations"].get(cid)
            self._last_fetch_one = {"context_json": conv["context_json"]} if conv else None
            return

        # SELECT role, content, timestamp FROM conv_messages WHERE conversation_id ORDER BY id
        if sql_l.startswith("select role, content, timestamp from startup_advisor_conv_messages"):
            (cid,) = params
            msgs = [
                {"role": m["role"], "content": m["content"], "timestamp": m["timestamp"]}
                for m in self._db["messages"]
                if m["conversation_id"] == cid
            ]
            self._last_fetch_all = msgs
            return

        # SELECT 1 FROM conversations WHERE conversation_id = %s
        if sql_l.startswith("select 1 from startup_advisor_conversations"):
            (cid,) = params
            self._last_fetch_one = (1,) if cid in self._db["conversations"] else None
            return

        # INSERT conv_messages
        if sql_l.startswith("insert into startup_advisor_conv_messages"):
            cid, role, content, ts = params
            msg_id = next(self._ids)
            self._db["messages"].append(
                {
                    "id": msg_id,
                    "conversation_id": cid,
                    "role": role,
                    "content": content,
                    "timestamp": ts,
                }
            )
            self.rowcount = 1
            return

        # UPDATE startup_advisor_conversations SET updated_at = %s WHERE conversation_id = %s
        if sql_l.startswith("update startup_advisor_conversations set updated_at"):
            ts, cid = params
            conv = self._db["conversations"].get(cid)
            if conv is not None:
                conv["updated_at"] = ts
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # UPDATE conversations SET context_json + updated_at
        if sql_l.startswith("update startup_advisor_conversations set context_json"):
            ctx, ts, cid = params
            ctx_dict = ctx.obj if hasattr(ctx, "obj") else ctx
            conv = self._db["conversations"].get(cid)
            if conv is not None:
                conv["context_json"] = ctx_dict
                conv["updated_at"] = ts
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # INSERT conv_artifacts ... RETURNING id
        if sql_l.startswith("insert into startup_advisor_conv_artifacts"):
            cid, artifact_type, title, payload, ts = params
            payload_dict = payload.obj if hasattr(payload, "obj") else payload
            art_id = next(self._ids)
            self._db["artifacts"].append(
                {
                    "id": art_id,
                    "conversation_id": cid,
                    "artifact_type": artifact_type,
                    "title": title,
                    "payload_json": payload_dict,
                    "created_at": ts,
                }
            )
            self._last_fetch_one = (art_id,)
            self.rowcount = 1
            return

        # SELECT artifact fields FROM conv_artifacts WHERE conversation_id
        if sql_l.startswith(
            "select id, artifact_type, title, payload_json, created_at from startup_advisor_conv_artifacts"
        ):
            (cid,) = params
            arts = [dict(a) for a in self._db["artifacts"] if a["conversation_id"] == cid]
            self._last_fetch_all = arts
            return

        # list_conversations: GROUP BY aggregate
        if "select c.conversation_id" in sql_l and "from startup_advisor_conversations c" in sql_l:
            rows = []
            for conv in sorted(
                self._db["conversations"].values(),
                key=lambda c: c["updated_at"],
                reverse=True,
            ):
                count = sum(
                    1
                    for m in self._db["messages"]
                    if m["conversation_id"] == conv["conversation_id"]
                )
                rows.append(
                    {
                        "conversation_id": conv["conversation_id"],
                        "created_at": conv["created_at"],
                        "updated_at": conv["updated_at"],
                        "message_count": count,
                    }
                )
            self._last_fetch_all = rows
            return

        # SELECT conversation_id FROM conversations ORDER BY created_at ASC LIMIT 1
        if sql_l.startswith(
            "select conversation_id from startup_advisor_conversations order by created_at asc"
        ):
            convs = sorted(self._db["conversations"].values(), key=lambda c: c["created_at"])
            self._last_fetch_one = (convs[0]["conversation_id"],) if convs else None
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


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``get_conn`` that speaks the startup_advisor dialect.

    Yields the backing dict so tests can assert on the persisted state
    directly when the store's public API isn't enough.
    """
    db: dict[str, Any] = {
        "conversations": {},
        "messages": [],
        "artifacts": [],
    }
    ids = itertools.count(1)

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db, ids)

    # Patch the symbol on the ``startup_advisor.store`` module (the
    # store imports ``get_conn`` at module scope) so monkeypatch applies
    # to the exact reference the store uses.
    import startup_advisor.store as store_mod

    monkeypatch.setattr(store_mod, "get_conn", _fake_get_conn)
    yield db


# ---------------------------------------------------------------------------
# Fixtures for importing the store under test
# ---------------------------------------------------------------------------


@pytest.fixture
def store(fake_pg):
    from startup_advisor.store import StartupAdvisorConversationStore

    return StartupAdvisorConversationStore()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_create_inserts_row_with_uuid(store, fake_pg):
    cid = store.create()
    assert cid in fake_pg["conversations"]
    conv = fake_pg["conversations"][cid]
    assert conv["context_json"] == {}
    assert isinstance(conv["created_at"], datetime)
    assert conv["created_at"].tzinfo is timezone.utc


def test_create_accepts_explicit_id_and_context(store, fake_pg):
    cid = store.create(conversation_id="fixed-id", context={"a": 1})
    assert cid == "fixed-id"
    assert fake_pg["conversations"]["fixed-id"]["context_json"] == {"a": 1}


def test_get_returns_none_for_missing_conversation(store):
    assert store.get("does-not-exist") is None


def test_get_round_trips_messages_and_context(store):
    cid = store.create(context={"k": "v"})
    assert store.append_message(cid, "user", "hello") is True
    assert store.append_message(cid, "assistant", "hi back") is True

    result = store.get(cid)
    assert result is not None
    messages, context = result
    assert context == {"k": "v"}
    assert [m.role for m in messages] == ["user", "assistant"]
    assert [m.content for m in messages] == ["hello", "hi back"]
    assert all(isinstance(m.timestamp, str) for m in messages)


def test_append_message_rejects_unknown_role(store):
    cid = store.create()
    assert store.append_message(cid, "system", "oops") is False


def test_append_message_returns_false_for_missing_conversation(store):
    assert store.append_message("nope", "user", "hi") is False


def test_append_message_bumps_updated_at(store, fake_pg):
    cid = store.create()
    original_updated = fake_pg["conversations"][cid]["updated_at"]
    store.append_message(cid, "user", "msg")
    new_updated = fake_pg["conversations"][cid]["updated_at"]
    assert new_updated >= original_updated


def test_update_context_replaces_existing_context(store, fake_pg):
    cid = store.create(context={"initial": True})
    assert store.update_context(cid, {"replaced": True}) is True
    assert fake_pg["conversations"][cid]["context_json"] == {"replaced": True}


def test_update_context_returns_false_for_missing(store):
    assert store.update_context("does-not-exist", {"k": "v"}) is False


def test_add_artifact_returns_id_and_round_trips_payload(store):
    cid = store.create()
    art_id = store.add_artifact(cid, "plan", "My Plan", {"steps": ["a", "b"], "priority": 1})
    assert isinstance(art_id, int)
    arts = store.get_artifacts(cid)
    assert len(arts) == 1
    assert arts[0].artifact_id == art_id
    assert arts[0].artifact_type == "plan"
    assert arts[0].title == "My Plan"
    assert arts[0].payload == {"steps": ["a", "b"], "priority": 1}
    assert isinstance(arts[0].created_at, str)


def test_get_artifacts_returns_empty_for_unknown_conversation(store):
    assert store.get_artifacts("nope") == []


def test_list_conversations_orders_by_updated_at_desc(store):
    c1 = store.create()
    c2 = store.create()
    # Touch c1 after c2 so c1 should sort first.
    store.append_message(c1, "user", "later")

    listing = store.list_conversations()
    assert [c.conversation_id for c in listing[:2]] == [c1, c2]
    assert listing[0].message_count == 1
    assert listing[1].message_count == 0


def test_get_or_create_singleton_creates_when_empty(store, fake_pg):
    cid = store.get_or_create_singleton()
    assert cid in fake_pg["conversations"]


def test_get_or_create_singleton_returns_oldest_existing(store):
    first = store.create()
    _ = store.create()
    assert store.get_or_create_singleton() == first


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------


def test_get_conversation_store_is_lazy_and_cached(fake_pg, monkeypatch):
    import startup_advisor.store as store_mod

    # Reset the module-level cache so the test is hermetic.
    monkeypatch.setattr(store_mod, "_default_store", None)

    a = store_mod.get_conversation_store()
    b = store_mod.get_conversation_store()
    assert a is b
