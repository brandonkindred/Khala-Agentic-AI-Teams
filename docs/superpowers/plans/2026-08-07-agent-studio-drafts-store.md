# Agent Studio Drafts Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a user-scoped `agent_studio_drafts` persistence layer (in-memory + Postgres twins) with create/update/get/list-summaries/rename/delete, ready for HTTP routes later.

**Architecture:** Opaque `payload` dict stored as JSONB; twin stores mirroring the conversation store pattern; factory selects Postgres when `is_postgres_enabled()`. Extend the existing `agent_studio` `SCHEMA` (already registered by unified API lifespan).

**Tech Stack:** Python 3.10+, Pydantic, `shared.postgres` (`TeamSchema`, `get_conn`, `@timed_query`), pytest

## Global Constraints

- Work only in worktree `.worktrees/5700-agent-studio-drafts-store` on branch `feature/5700-agent-studio-drafts-store`
- Spec: `docs/superpowers/specs/2026-08-07-agent-studio-drafts-store-design.md`
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:` / `Invariants:` where relevant) on every new public function/method/module
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- No HTTP routes, Angular, or `AgentStudioService` wiring
- Wrong-user access ≡ not-found (`None` / `False`)
- List pagination: default limit 50, clamp to `[1, 100]`, `offset` clamped to `≥ 0`, order `updated_at DESC`

## File map

| File | Role |
|---|---|
| `agent_studio/models.py` | Add `AgentStudioDraftSummary`, `AgentStudioDraft` |
| `agent_studio/drafts_store.py` | In-memory store + shared validation/pagination helpers |
| `agent_studio/drafts_pg_store.py` | Postgres twin |
| `agent_studio/drafts_runtime.py` | `get_draft_store()` factory (process singleton) |
| `agent_studio/postgres/__init__.py` | Add `agent_studio_drafts` table + index |
| `agent_studio/tests/test_drafts_store.py` | Unit tests (tenancy + pagination + CRUD) |
| `agent_studio/tests/test_drafts_pg_store.py` | Live-PG tests (skip when `POSTGRES_HOST` unset) |
| `agent_studio/tests/test_drafts_runtime.py` | Factory selection tests |

Base package path: `backend/agents/agent_team_studio/agent_studio/`

---

### Task 1: Draft models

**Files:**
- Modify: `backend/agents/agent_team_studio/agent_studio/models.py`
- Test: `backend/agents/agent_team_studio/agent_studio/tests/test_drafts_models.py` (create)

**Interfaces:**
- Consumes: existing `pydantic.BaseModel`, `typing.Any`
- Produces:
  - `class AgentStudioDraftSummary(BaseModel)` with `draft_id: str`, `name: str`, `updated_at: str`
  - `class AgentStudioDraft(BaseModel)` with those fields plus `created_at: str`, `payload: dict[str, Any]`

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/agent_team_studio/agent_studio/tests/test_drafts_models.py`:

```python
"""Unit tests for Agent Studio draft Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_team_studio.agent_studio.models import AgentStudioDraft, AgentStudioDraftSummary


def test_summary_requires_core_fields() -> None:
    summary = AgentStudioDraftSummary(
        draft_id="d1", name="My draft", updated_at="2026-08-07T12:00:00+00:00"
    )
    assert summary.draft_id == "d1"
    assert summary.name == "My draft"


def test_draft_defaults_payload_to_empty_dict() -> None:
    draft = AgentStudioDraft(
        draft_id="d1",
        name="n",
        created_at="2026-08-07T12:00:00+00:00",
        updated_at="2026-08-07T12:00:00+00:00",
    )
    assert draft.payload == {}


def test_draft_accepts_opaque_payload() -> None:
    draft = AgentStudioDraft(
        draft_id="d1",
        name="n",
        created_at="2026-08-07T12:00:00+00:00",
        updated_at="2026-08-07T12:00:00+00:00",
        payload={"registryAgentId": "a1", "stage1AgentDraft": {"mode": "new"}},
    )
    assert draft.payload["registryAgentId"] == "a1"


def test_summary_rejects_missing_draft_id() -> None:
    with pytest.raises(ValidationError):
        AgentStudioDraftSummary(name="n", updated_at="2026-08-07T12:00:00+00:00")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd backend && python -m pytest agents/agent_team_studio/agent_studio/tests/test_drafts_models.py -v
```

Expected: FAIL with `ImportError` / `cannot import name 'AgentStudioDraftSummary'`

- [ ] **Step 3: Add the models**

Append to `backend/agents/agent_team_studio/agent_studio/models.py` (after `SaveAgentResponse`):

```python
class AgentStudioDraftSummary(BaseModel):
    """Lightweight draft row for list endpoints (no payload).

    Invariants:
        * ``draft_id``, ``name``, and ``updated_at`` are always present on a
          persisted summary; the store owns id + timestamp assignment.
    """

    draft_id: str
    name: str
    updated_at: str = Field(..., description="ISO-8601 timestamp; server-managed.")


class AgentStudioDraft(BaseModel):
    """Full draft record: identity + opaque stage/handoff payload.

    The store persists ``payload`` verbatim and does not interpret stage fields
    (handoff ids, ``stage1AgentDraft``, etc.). Routes may validate shape later.

    Invariants:
        * ``payload`` is always a JSON object (``dict``), never a list/scalar.
    """

    draft_id: str
    name: str
    created_at: str = Field(..., description="ISO-8601 timestamp; server-managed.")
    updated_at: str = Field(..., description="ISO-8601 timestamp; server-managed.")
    payload: dict[str, Any] = Field(default_factory=dict)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd backend && python -m pytest agents/agent_team_studio/agent_studio/tests/test_drafts_models.py -v
```

Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add -f \
  backend/agents/agent_team_studio/agent_studio/models.py \
  backend/agents/agent_team_studio/agent_studio/tests/test_drafts_models.py
git commit -m "$(cat <<'EOF'
Add AgentStudioDraft and summary Pydantic models.

Opaque payload dict keeps the store from interpreting stage/handoff fields.

EOF
)"
```

---

### Task 2: In-memory drafts store

**Files:**
- Create: `backend/agents/agent_team_studio/agent_studio/drafts_store.py`
- Test: `backend/agents/agent_team_studio/agent_studio/tests/test_drafts_store.py` (create)

**Interfaces:**
- Consumes: `AgentStudioDraft`, `AgentStudioDraftSummary` from models
- Produces:
  - Helpers: `validate_user_id(user_id: str) -> str`, `clamp_pagination(limit: int, offset: int) -> tuple[int, int]`, `default_draft_name() -> str`, `iso_now() -> str`
  - `class AgentStudioDraftStore` with:
    - `create(user_id: str, *, name: str | None = None, payload: dict[str, Any] | None = None) -> AgentStudioDraft`
    - `update(user_id: str, draft_id: str, *, name: str | None = None, payload: dict[str, Any] | None = None) -> AgentStudioDraft | None`
    - `get(user_id: str, draft_id: str) -> AgentStudioDraft | None`
    - `list_summaries(user_id: str, *, limit: int = 50, offset: int = 0) -> list[AgentStudioDraftSummary]`
    - `rename(user_id: str, draft_id: str, name: str) -> AgentStudioDraftSummary | None`
    - `delete(user_id: str, draft_id: str) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/agent_team_studio/agent_studio/tests/test_drafts_store.py`:

```python
"""Unit tests for the in-memory Agent Studio drafts store."""

from __future__ import annotations

import time

import pytest

from agent_team_studio.agent_studio.drafts_store import AgentStudioDraftStore


@pytest.fixture()
def store() -> AgentStudioDraftStore:
    return AgentStudioDraftStore()


def test_create_get_round_trip(store: AgentStudioDraftStore) -> None:
    created = store.create("u1", name="Alpha", payload={"teamId": "t1"})
    loaded = store.get("u1", created.draft_id)
    assert loaded is not None
    assert loaded.draft_id == created.draft_id
    assert loaded.name == "Alpha"
    assert loaded.payload == {"teamId": "t1"}
    assert loaded.created_at == created.created_at
    assert loaded.updated_at == created.updated_at


def test_create_defaults_name_and_empty_payload(store: AgentStudioDraftStore) -> None:
    created = store.create("u1")
    assert created.name  # non-empty timestamp default
    assert created.payload == {}


def test_update_patches_owned_draft(store: AgentStudioDraftStore) -> None:
    created = store.create("u1", name="Old", payload={"a": 1})
    time.sleep(0.01)  # ensure updated_at can move forward on fast clocks
    updated = store.update("u1", created.draft_id, name="New", payload={"a": 2})
    assert updated is not None
    assert updated.name == "New"
    assert updated.payload == {"a": 2}
    assert updated.updated_at >= created.updated_at


def test_update_missing_returns_none(store: AgentStudioDraftStore) -> None:
    assert store.update("u1", "missing", name="x") is None


def test_tenancy_isolation(store: AgentStudioDraftStore) -> None:
    created = store.create("alice", name="Secret", payload={"x": 1})
    assert store.get("bob", created.draft_id) is None
    assert store.update("bob", created.draft_id, name="Hijack") is None
    assert store.rename("bob", created.draft_id, "Hijack") is None
    assert store.delete("bob", created.draft_id) is False
    assert store.list_summaries("bob") == []
    # Alice still owns it unchanged
    assert store.get("alice", created.draft_id) is not None
    assert store.get("alice", created.draft_id).name == "Secret"


def test_rename_and_delete(store: AgentStudioDraftStore) -> None:
    created = store.create("u1", name="Old")
    renamed = store.rename("u1", created.draft_id, "Renamed")
    assert renamed is not None
    assert renamed.name == "Renamed"
    assert store.delete("u1", created.draft_id) is True
    assert store.get("u1", created.draft_id) is None
    assert store.delete("u1", created.draft_id) is False


def test_list_summaries_order_and_pagination(store: AgentStudioDraftStore) -> None:
    ids: list[str] = []
    for i in range(3):
        time.sleep(0.01)
        ids.append(store.create("u1", name=f"d{i}").draft_id)
    # Most recent first
    summaries = store.list_summaries("u1", limit=50, offset=0)
    assert [s.draft_id for s in summaries] == list(reversed(ids))
    page = store.list_summaries("u1", limit=1, offset=1)
    assert len(page) == 1
    assert page[0].draft_id == ids[1]


def test_list_summaries_clamps_limit_and_offset(store: AgentStudioDraftStore) -> None:
    for i in range(5):
        store.create("u1", name=f"n{i}")
    assert len(store.list_summaries("u1", limit=0)) == 1  # clamped to 1
    assert len(store.list_summaries("u1", limit=1000)) == 5  # clamped to 100, but only 5 exist
    assert store.list_summaries("u1", limit=50, offset=-5)  # negative offset → 0


def test_preconditions(store: AgentStudioDraftStore) -> None:
    with pytest.raises(ValueError):
        store.create("")
    with pytest.raises(ValueError):
        store.create("u1", name="")
    with pytest.raises(ValueError):
        store.create("u1", payload=["not", "a", "dict"])  # type: ignore[arg-type]
    created = store.create("u1", name="ok")
    with pytest.raises(ValueError):
        store.rename("u1", created.draft_id, "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd backend && python -m pytest agents/agent_team_studio/agent_studio/tests/test_drafts_store.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_team_studio.agent_studio.drafts_store'`

- [ ] **Step 3: Implement `drafts_store.py`**

Create `backend/agents/agent_team_studio/agent_studio/drafts_store.py`:

```python
"""In-memory Agent Studio drafts store (local/dev when Postgres is unset).

User-scoped persistence of handoff state + partial Stage work as an opaque
``payload`` dict. Process-lifetime only — no LRU eviction (drafts are
user-owned durable-intent data; the Postgres twin is the multi-worker path).

Thread-safe via a single ``threading.Lock`` around the record map.

Invariants:
    * Every stored record is keyed by ``draft_id`` and carries a ``user_id``.
    * Ops for the wrong ``user_id`` behave as not-found.
    * ``len`` of internal map only changes via ``create`` / ``delete``.
"""

from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import AgentStudioDraft, AgentStudioDraftSummary

_LIST_DEFAULT_LIMIT = 50
_LIST_MAX_LIMIT = 100


def iso_now() -> str:
    """Return an aware UTC ISO-8601 timestamp string.

    Postconditions:
        * The string parses as an aware datetime; timezone is UTC.
    """
    return datetime.now(timezone.utc).isoformat()


def default_draft_name() -> str:
    """Timestamp label used when the caller omits ``name`` on create.

    Postconditions:
        * Returns a non-empty string.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def validate_user_id(user_id: str) -> str:
    """Reject empty / whitespace-only user ids.

    Preconditions:
        * ``user_id`` is a ``str``.
    Postconditions:
        * Returns the stripped ``user_id``.
    Raises:
        ValueError: when empty after strip.
    """
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id must be a non-empty string")
    return user_id.strip()


def validate_optional_name(name: str | None) -> str | None:
    """Validate an optional name; ``None`` means leave unchanged / use default.

    Raises:
        ValueError: when ``name`` is provided but empty/whitespace.
    """
    if name is None:
        return None
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    return name.strip()


def validate_optional_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate an optional opaque payload object.

    Raises:
        ValueError: when ``payload`` is not ``None`` and not a ``dict``.
    """
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    return payload


def clamp_pagination(limit: int, offset: int) -> tuple[int, int]:
    """Clamp list pagination to the UX-spec contract.

    Postconditions:
        * Returned ``limit`` is in ``[1, 100]`` (default intent 50 applied by callers
          before clamp when they pass the default).
        * Returned ``offset`` is ``>= 0``.
    """
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = _LIST_DEFAULT_LIMIT
    try:
        off = int(offset)
    except (TypeError, ValueError):
        off = 0
    lim = max(1, min(lim, _LIST_MAX_LIMIT))
    off = max(0, off)
    return lim, off


@dataclass
class _DraftRecord:
    draft_id: str
    user_id: str
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class AgentStudioDraftStore:
    """Process-local, user-scoped drafts store."""

    def __init__(self) -> None:
        self._records: dict[str, _DraftRecord] = {}
        self._lock = threading.Lock()

    def create(
        self,
        user_id: str,
        *,
        name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AgentStudioDraft:
        """Create a new draft owned by ``user_id``.

        Preconditions:
            * ``user_id`` non-empty; ``name`` if given non-empty; ``payload`` if given a dict.
        Postconditions:
            * Returns a new draft with a fresh ``draft_id``; ``get(user_id, id)`` resolves it.
        """
        uid = validate_user_id(user_id)
        resolved_name = validate_optional_name(name)
        if resolved_name is None:
            resolved_name = default_draft_name()
        resolved_payload = validate_optional_payload(payload)
        if resolved_payload is None:
            resolved_payload = {}
        now = iso_now()
        draft_id = str(uuid.uuid4())
        record = _DraftRecord(
            draft_id=draft_id,
            user_id=uid,
            name=resolved_name,
            payload=copy.deepcopy(resolved_payload),
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._records[draft_id] = record
        return self._to_draft(record)

    def update(
        self,
        user_id: str,
        draft_id: str,
        *,
        name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AgentStudioDraft | None:
        """Patch an owned draft; ``None`` if missing or wrong user.

        Preconditions:
            * ``user_id`` non-empty; optional ``name``/``payload`` validated when provided.
        """
        uid = validate_user_id(user_id)
        new_name = validate_optional_name(name)
        new_payload = validate_optional_payload(payload)
        with self._lock:
            record = self._records.get(draft_id)
            if record is None or record.user_id != uid:
                return None
            if new_name is not None:
                record.name = new_name
            if new_payload is not None:
                record.payload = copy.deepcopy(new_payload)
            record.updated_at = iso_now()
            return self._to_draft(record)

    def get(self, user_id: str, draft_id: str) -> AgentStudioDraft | None:
        """Return the full draft if owned by ``user_id``, else ``None``."""
        uid = validate_user_id(user_id)
        with self._lock:
            record = self._records.get(draft_id)
            if record is None or record.user_id != uid:
                return None
            return self._to_draft(record)

    def list_summaries(
        self, user_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[AgentStudioDraftSummary]:
        """List owned draft summaries, most-recent ``updated_at`` first."""
        uid = validate_user_id(user_id)
        lim, off = clamp_pagination(limit, offset)
        with self._lock:
            owned = [r for r in self._records.values() if r.user_id == uid]
            owned.sort(key=lambda r: r.updated_at, reverse=True)
            page = owned[off : off + lim]
            return [
                AgentStudioDraftSummary(
                    draft_id=r.draft_id, name=r.name, updated_at=r.updated_at
                )
                for r in page
            ]

    def rename(self, user_id: str, draft_id: str, name: str) -> AgentStudioDraftSummary | None:
        """Rename an owned draft; ``None`` if missing or wrong user."""
        uid = validate_user_id(user_id)
        new_name = validate_optional_name(name)
        assert new_name is not None  # rename requires a name
        with self._lock:
            record = self._records.get(draft_id)
            if record is None or record.user_id != uid:
                return None
            record.name = new_name
            record.updated_at = iso_now()
            return AgentStudioDraftSummary(
                draft_id=record.draft_id, name=record.name, updated_at=record.updated_at
            )

    def delete(self, user_id: str, draft_id: str) -> bool:
        """Delete an owned draft; ``False`` if missing or wrong user."""
        uid = validate_user_id(user_id)
        with self._lock:
            record = self._records.get(draft_id)
            if record is None or record.user_id != uid:
                return False
            del self._records[draft_id]
            return True

    @staticmethod
    def _to_draft(record: _DraftRecord) -> AgentStudioDraft:
        return AgentStudioDraft(
            draft_id=record.draft_id,
            name=record.name,
            created_at=record.created_at,
            updated_at=record.updated_at,
            payload=copy.deepcopy(record.payload),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd backend && python -m pytest agents/agent_team_studio/agent_studio/tests/test_drafts_store.py -v
```

Expected: PASS (all tests green)

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/agent_team_studio/agent_studio/drafts_store.py \
  backend/agents/agent_team_studio/agent_studio/tests/test_drafts_store.py
git commit -m "$(cat <<'EOF'
Add in-memory user-scoped Agent Studio drafts store.

Supports create/update/get/list/rename/delete with tenancy isolation and
paginated summaries for the local/dev path.

EOF
)"
```

---

### Task 3: SCHEMA + Postgres drafts store

**Files:**
- Modify: `backend/agents/agent_team_studio/agent_studio/postgres/__init__.py`
- Create: `backend/agents/agent_team_studio/agent_studio/drafts_pg_store.py`
- Test: `backend/agents/agent_team_studio/agent_studio/tests/test_drafts_pg_store.py` (create)

**Interfaces:**
- Consumes: helpers from `drafts_store`, models, `shared.postgres.get_conn`, `@timed_query`, `psycopg.types.json.Json`
- Produces: `class PostgresAgentStudioDraftStore` with the same six methods as `AgentStudioDraftStore`

- [ ] **Step 1: Extend SCHEMA**

Replace `backend/agents/agent_team_studio/agent_studio/postgres/__init__.py` contents with:

```python
"""Postgres schema for Agent Studio conversation + drafts stores.

Pure data module — importing it has no side effects. DDL runs when the unified
API lifespan calls ``shared.postgres.register_team_schemas(SCHEMA)``.

Backs (1) the durable authoring conversation store and (2) the user-scoped
``agent_studio_drafts`` table for save/resume of Studio handoff + stage work.
"""

from __future__ import annotations

from shared.postgres import TeamSchema

SCHEMA: TeamSchema = TeamSchema(
    team="agent_studio",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS agent_studio_conversations (
            conversation_id  TEXT PRIMARY KEY,
            mode             TEXT NOT NULL,
            source_agent_id  TEXT,
            definition_json  JSONB NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS agent_studio_conv_messages (
            id               BIGSERIAL PRIMARY KEY,
            conversation_id  TEXT NOT NULL
                REFERENCES agent_studio_conversations(conversation_id) ON DELETE CASCADE,
            role             TEXT NOT NULL,
            content          TEXT NOT NULL,
            timestamp        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_agent_studio_conv_messages_conv
            ON agent_studio_conv_messages(conversation_id, id)""",
        """CREATE TABLE IF NOT EXISTS agent_studio_drafts (
            draft_id     TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL,
            name         TEXT NOT NULL,
            payload_json JSONB NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_agent_studio_drafts_user_updated
            ON agent_studio_drafts (user_id, updated_at DESC)""",
    ],
    table_names=[
        # Messages first: FK-dependent child truncated before its parent.
        "agent_studio_conv_messages",
        "agent_studio_conversations",
        "agent_studio_drafts",
    ],
)
```

- [ ] **Step 2: Write the failing live-PG tests**

Create `backend/agents/agent_team_studio/agent_studio/tests/test_drafts_pg_store.py`:

```python
"""Live-Postgres tests for the durable Agent Studio drafts store.

Skipped when ``POSTGRES_HOST`` is unset.
"""

from __future__ import annotations

import time

import pytest

from shared.postgres import is_postgres_enabled

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(), reason="POSTGRES_HOST not set; skipping live-Postgres draft store tests"
)


@pytest.fixture()
def store():
    from agent_team_studio.agent_studio.drafts_pg_store import PostgresAgentStudioDraftStore
    from agent_team_studio.agent_studio.postgres import SCHEMA
    from shared.postgres import register_team_schemas
    from shared.postgres.testing import truncate_team_tables

    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)
    return PostgresAgentStudioDraftStore()


def test_create_get_round_trip(store) -> None:
    created = store.create("u1", name="Alpha", payload={"teamId": "t1"})
    loaded = store.get("u1", created.draft_id)
    assert loaded is not None
    assert loaded.name == "Alpha"
    assert loaded.payload == {"teamId": "t1"}


def test_tenancy_isolation(store) -> None:
    created = store.create("alice", name="Secret", payload={"x": 1})
    assert store.get("bob", created.draft_id) is None
    assert store.update("bob", created.draft_id, name="Hijack") is None
    assert store.rename("bob", created.draft_id, "Hijack") is None
    assert store.delete("bob", created.draft_id) is False
    assert store.list_summaries("bob") == []
    assert store.get("alice", created.draft_id).name == "Secret"


def test_list_summaries_order_and_pagination(store) -> None:
    ids: list[str] = []
    for i in range(3):
        time.sleep(0.01)
        ids.append(store.create("u1", name=f"d{i}").draft_id)
    summaries = store.list_summaries("u1", limit=50, offset=0)
    assert [s.draft_id for s in summaries] == list(reversed(ids))
    page = store.list_summaries("u1", limit=1, offset=1)
    assert len(page) == 1
    assert page[0].draft_id == ids[1]


def test_update_rename_delete(store) -> None:
    created = store.create("u1", name="Old", payload={"a": 1})
    updated = store.update("u1", created.draft_id, payload={"a": 2})
    assert updated is not None and updated.payload == {"a": 2}
    renamed = store.rename("u1", created.draft_id, "Renamed")
    assert renamed is not None and renamed.name == "Renamed"
    assert store.delete("u1", created.draft_id) is True
    assert store.get("u1", created.draft_id) is None


def test_list_clamps_limit(store) -> None:
    for i in range(3):
        store.create("u1", name=f"n{i}")
    assert len(store.list_summaries("u1", limit=0)) == 1
    assert len(store.list_summaries("u1", limit=1000)) == 3
```

- [ ] **Step 3: Run tests to verify they fail (when Postgres is on) or skip**

Run:

```bash
cd backend && python -m pytest agents/agent_team_studio/agent_studio/tests/test_drafts_pg_store.py -v
```

Expected without `POSTGRES_HOST`: all SKIPPED.  
Expected with `POSTGRES_HOST`: FAIL with `ModuleNotFoundError` for `drafts_pg_store`.

- [ ] **Step 4: Implement `drafts_pg_store.py`**

Create `backend/agents/agent_team_studio/agent_studio/drafts_pg_store.py`:

```python
"""Postgres-backed Agent Studio drafts store.

Durable, cross-worker twin of
:class:`~agent_team_studio.agent_studio.drafts_store.AgentStudioDraftStore`.
Same public surface so callers (and follow-on HTTP routes) are backend-agnostic.

DDL lives in ``agent_team_studio.agent_studio.postgres`` and is registered from the
unified API lifespan. Import this module only when Postgres is enabled.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared.postgres import get_conn
from shared.postgres.metrics import timed_query

from .drafts_store import (
    clamp_pagination,
    default_draft_name,
    iso_now,
    validate_optional_name,
    validate_optional_payload,
    validate_user_id,
)
from .models import AgentStudioDraft, AgentStudioDraftSummary

logger = logging.getLogger(__name__)

_STORE = "agent_studio_drafts"
_TABLE = "agent_studio_drafts"


def _iso(value: Any) -> str:
    """Normalize a DB timestamptz / datetime to an ISO-8601 string."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row_to_draft(row: dict[str, Any]) -> AgentStudioDraft:
    payload = row["payload_json"]
    if not isinstance(payload, dict):
        payload = dict(payload) if payload is not None else {}
    return AgentStudioDraft(
        draft_id=row["draft_id"],
        name=row["name"],
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
        payload=payload,
    )


class PostgresAgentStudioDraftStore:
    """Postgres-backed user-scoped drafts store."""

    @timed_query(store=_STORE, op="create")
    def create(
        self,
        user_id: str,
        *,
        name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AgentStudioDraft:
        """Insert a new draft row owned by ``user_id``."""
        uid = validate_user_id(user_id)
        resolved_name = validate_optional_name(name) or default_draft_name()
        resolved_payload = validate_optional_payload(payload)
        if resolved_payload is None:
            resolved_payload = {}
        draft_id = str(uuid4())
        now = iso_now()
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO {_TABLE} "
                "(draft_id, user_id, name, payload_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s::timestamptz, %s::timestamptz) "
                "RETURNING draft_id, name, payload_json, created_at, updated_at",
                (draft_id, uid, resolved_name, Json(resolved_payload), now, now),
            )
            row = cur.fetchone()
        assert row is not None
        return _row_to_draft(row)

    @timed_query(store=_STORE, op="update")
    def update(
        self,
        user_id: str,
        draft_id: str,
        *,
        name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AgentStudioDraft | None:
        """Patch an owned draft; ``None`` if missing or wrong user."""
        uid = validate_user_id(user_id)
        new_name = validate_optional_name(name)
        new_payload = validate_optional_payload(payload)
        now = iso_now()
        sets: list[str] = ["updated_at = %s::timestamptz"]
        params: list[Any] = [now]
        if new_name is not None:
            sets.append("name = %s")
            params.append(new_name)
        if new_payload is not None:
            sets.append("payload_json = %s")
            params.append(Json(new_payload))
        params.extend([draft_id, uid])
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"UPDATE {_TABLE} SET {', '.join(sets)} "
                "WHERE draft_id = %s AND user_id = %s "
                "RETURNING draft_id, name, payload_json, created_at, updated_at",
                params,
            )
            row = cur.fetchone()
        return _row_to_draft(row) if row else None

    @timed_query(store=_STORE, op="get")
    def get(self, user_id: str, draft_id: str) -> AgentStudioDraft | None:
        """Load a full draft if owned by ``user_id``."""
        uid = validate_user_id(user_id)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT draft_id, name, payload_json, created_at, updated_at "
                f"FROM {_TABLE} WHERE draft_id = %s AND user_id = %s",
                (draft_id, uid),
            )
            row = cur.fetchone()
        return _row_to_draft(row) if row else None

    @timed_query(store=_STORE, op="list_summaries")
    def list_summaries(
        self, user_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[AgentStudioDraftSummary]:
        """List owned summaries, most-recent ``updated_at`` first."""
        uid = validate_user_id(user_id)
        lim, off = clamp_pagination(limit, offset)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT draft_id, name, updated_at FROM {_TABLE} "
                "WHERE user_id = %s ORDER BY updated_at DESC "
                "LIMIT %s OFFSET %s",
                (uid, lim, off),
            )
            rows = cur.fetchall()
        return [
            AgentStudioDraftSummary(
                draft_id=r["draft_id"], name=r["name"], updated_at=_iso(r["updated_at"])
            )
            for r in rows
        ]

    @timed_query(store=_STORE, op="rename")
    def rename(self, user_id: str, draft_id: str, name: str) -> AgentStudioDraftSummary | None:
        """Rename an owned draft; ``None`` if missing or wrong user."""
        uid = validate_user_id(user_id)
        new_name = validate_optional_name(name)
        assert new_name is not None
        now = iso_now()
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"UPDATE {_TABLE} SET name = %s, updated_at = %s::timestamptz "
                "WHERE draft_id = %s AND user_id = %s "
                "RETURNING draft_id, name, updated_at",
                (new_name, now, draft_id, uid),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return AgentStudioDraftSummary(
            draft_id=row["draft_id"], name=row["name"], updated_at=_iso(row["updated_at"])
        )

    @timed_query(store=_STORE, op="delete")
    def delete(self, user_id: str, draft_id: str) -> bool:
        """Delete an owned draft; ``False`` if missing or wrong user."""
        uid = validate_user_id(user_id)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {_TABLE} WHERE draft_id = %s AND user_id = %s",
                (draft_id, uid),
            )
            return cur.rowcount > 0
```

- [ ] **Step 5: Run tests**

Run:

```bash
cd backend && python -m pytest \
  agents/agent_team_studio/agent_studio/tests/test_drafts_pg_store.py \
  agents/agent_team_studio/agent_studio/tests/test_pg_store.py -v
```

Expected: drafts PG tests PASS when Postgres is configured (or SKIP); existing conversation PG tests still PASS/SKIP unchanged.

Also re-run in-memory suite to confirm no regressions:

```bash
cd backend && python -m pytest agents/agent_team_studio/agent_studio/tests/test_drafts_store.py -v
```

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/agent_team_studio/agent_studio/postgres/__init__.py \
  backend/agents/agent_team_studio/agent_studio/drafts_pg_store.py \
  backend/agents/agent_team_studio/agent_studio/tests/test_drafts_pg_store.py
git commit -m "$(cat <<'EOF'
Add Postgres agent_studio_drafts schema and store.

Extends the agent_studio TeamSchema and mirrors the in-memory drafts API
with user_id predicates on every operation.

EOF
)"
```

---

### Task 4: Drafts store factory

**Files:**
- Create: `backend/agents/agent_team_studio/agent_studio/drafts_runtime.py`
- Test: `backend/agents/agent_team_studio/agent_studio/tests/test_drafts_runtime.py` (create)

**Interfaces:**
- Consumes: `is_postgres_enabled`, both store classes
- Produces: `get_draft_store() -> AgentStudioDraftStore | PostgresAgentStudioDraftStore` (process singleton)

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/agent_team_studio/agent_studio/tests/test_drafts_runtime.py`
(mirrors `tests/test_runtime.py`):

```python
"""Factory tests for get_draft_store()."""

from __future__ import annotations

import pytest

from agent_team_studio.agent_studio.drafts_store import AgentStudioDraftStore


def test_get_draft_store_returns_stable_singleton() -> None:
    from agent_team_studio.agent_studio.drafts_runtime import get_draft_store

    store = get_draft_store()
    assert get_draft_store() is store


def test_build_draft_store_in_memory_when_postgres_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shared.postgres
    from agent_team_studio.agent_studio import drafts_runtime

    monkeypatch.setattr(shared.postgres, "is_postgres_enabled", lambda: False)
    store = drafts_runtime._build_draft_store()
    assert isinstance(store, AgentStudioDraftStore)


def test_build_draft_store_uses_postgres_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_team_studio.agent_studio.drafts_pg_store as pg
    import shared.postgres
    from agent_team_studio.agent_studio import drafts_runtime

    class _StubStore:
        """Stand-in so the selection does not construct a real Postgres store."""

    monkeypatch.setattr(shared.postgres, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(pg, "PostgresAgentStudioDraftStore", _StubStore)

    store = drafts_runtime._build_draft_store()
    assert isinstance(store, _StubStore)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd backend && python -m pytest agents/agent_team_studio/agent_studio/tests/test_drafts_runtime.py -v
```

Expected: FAIL with `ModuleNotFoundError` for `drafts_runtime`

- [ ] **Step 3: Implement `drafts_runtime.py`**

Create `backend/agents/agent_team_studio/agent_studio/drafts_runtime.py`:

```python
"""Process-wide Agent Studio drafts store singleton.

Store selection (Postgres when configured, else in-memory) is bound once at
import time — the same contract as ``agent_studio.runtime`` for conversations.
"""

from __future__ import annotations

import logging
from typing import Union

from agent_team_studio.agent_studio.drafts_store import AgentStudioDraftStore

logger = logging.getLogger(__name__)

DraftStore = Union[AgentStudioDraftStore, "PostgresAgentStudioDraftStore"]


def _build_draft_store() -> DraftStore:
    """Select Postgres drafts store when enabled, else in-memory.

    Postconditions:
        * Returns a drafts store instance; Postgres-backed iff
          ``is_postgres_enabled()`` and psycopg is importable.
    """
    try:
        from shared.postgres import is_postgres_enabled

        if is_postgres_enabled():
            from agent_team_studio.agent_studio.drafts_pg_store import (
                PostgresAgentStudioDraftStore,
            )

            return PostgresAgentStudioDraftStore()
    except ImportError:  # pragma: no cover - missing optional dep
        logger.warning(
            "Postgres Agent Studio drafts store unavailable (missing dependency); "
            "using in-memory store",
            exc_info=True,
        )
    return AgentStudioDraftStore()


_store = _build_draft_store()


def get_draft_store() -> DraftStore:
    """Return the process-wide drafts store singleton.

    Postconditions:
        * Returns the same instance on every call within a process.
    """
    return _store
```

- [ ] **Step 4: Run all drafts tests**

Run:

```bash
cd backend && python -m pytest \
  agents/agent_team_studio/agent_studio/tests/test_drafts_models.py \
  agents/agent_team_studio/agent_studio/tests/test_drafts_store.py \
  agents/agent_team_studio/agent_studio/tests/test_drafts_pg_store.py \
  agents/agent_team_studio/agent_studio/tests/test_drafts_runtime.py \
  agents/agent_team_studio/agent_studio/tests/test_models.py \
  -v --cov=agents/agent_team_studio/agent_studio/drafts_store \
  --cov=agents/agent_team_studio/agent_studio/drafts_pg_store \
  --cov=agents/agent_team_studio/agent_studio/drafts_runtime \
  --cov-report=term-missing
```

Expected: all non-skipped tests PASS; line coverage ≥ 90% on the three new modules.

- [ ] **Step 5: Lint**

Run:

```bash
cd backend && ruff check agents/agent_team_studio/agent_studio/drafts_store.py \
  agents/agent_team_studio/agent_studio/drafts_pg_store.py \
  agents/agent_team_studio/agent_studio/drafts_runtime.py \
  agents/agent_team_studio/agent_studio/models.py \
  agents/agent_team_studio/agent_studio/postgres/__init__.py \
  agents/agent_team_studio/agent_studio/tests/test_drafts_*.py
ruff format agents/agent_team_studio/agent_studio/drafts_store.py \
  agents/agent_team_studio/agent_studio/drafts_pg_store.py \
  agents/agent_team_studio/agent_studio/drafts_runtime.py \
  agents/agent_team_studio/agent_studio/models.py \
  agents/agent_team_studio/agent_studio/postgres/__init__.py \
  agents/agent_team_studio/agent_studio/tests/test_drafts_*.py
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/agent_team_studio/agent_studio/drafts_runtime.py \
  backend/agents/agent_team_studio/agent_studio/tests/test_drafts_runtime.py
git commit -m "$(cat <<'EOF'
Add get_draft_store factory for Agent Studio drafts.

Selects Postgres when configured and falls back to the in-memory store for
local/dev, matching the conversation runtime pattern.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Opaque payload blob | Task 1–2 |
| Dual backends (in-memory + PG) | Task 2–3 |
| Create / update / get / list / rename / delete | Task 2–3 |
| User scoping / tenancy isolation | Task 2–3 tests |
| Pagination default 50 / max 100 / offset / DESC | Task 2–3 |
| SCHEMA via existing team registration | Task 3 |
| Factory, not service wiring | Task 4 |
| Unit tests tenancy + pagination | Task 2 |
| Live PG tests | Task 3 |
| No HTTP / UI | (non-goal, untouched) |
