# Insert Session Unique Violation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the branding fake Postgres `handle_insert_session` raise `psycopg.errors.UniqueViolation` on duplicate `session_id`, matching real `branding_sessions` primary-key semantics.

**Architecture:** One-line guard at the top of `handle_insert_session` before writing to `cur.db["sessions"]`. Raise the real `UniqueViolation` type (SQLSTATE `23505`) with a Postgres-shaped message so any future `except UniqueViolation` / `IntegrityError` handling sees the same type under the fake as under live Postgres. Prove the behavior with a unit test that inserts twice through `PostgresHelperMixin._execute`.

**Tech Stack:** Python 3.10, pytest, psycopg (`psycopg.errors.UniqueViolation`), branding fake in `_fake_postgres.py`.

**Spec:** `docs/superpowers/specs/2026-07-24-insert-session-unique-violation-design.md`

## Global Constraints

- Test-only change: touch `backend/agents/branding_team/tests/_fake_postgres.py` and `backend/agents/branding_team/tests/test_db.py` only.
- Raise `psycopg.errors.UniqueViolation`, not a local `FakeUniqueViolation`.
- Exception message must be exactly: `duplicate key value violates unique constraint "branding_sessions_pkey"`.
- On duplicate insert, leave the original row unchanged.
- Do not fix `handle_insert_client`, `handle_insert_brand`, or `handle_insert_conversation` in this plan (follow-ups).
- Never reference GitHub issue numbers in code, comments, or commit messages.
- Design by Contract: document Preconditions/Postconditions on any modified handler docstring.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/agents/branding_team/tests/_fake_postgres.py` | Dict-backed SQL dispatch; `handle_insert_session` must enforce PK uniqueness |
| `backend/agents/branding_team/tests/test_db.py` | Unit tests for `PostgresHelperMixin` via the fake; add duplicate-insert coverage |

No new files. No production store/API changes (`BrandingSessionStore.create` already mints a fresh UUID).

---

### Task 1: Failing duplicate-insert test

**Files:**
- Modify: `backend/agents/branding_team/tests/test_db.py`
- Test: `backend/agents/branding_team/tests/test_db.py::test_insert_session_raises_unique_violation_on_duplicate`

**Interfaces:**
- Consumes: `install_fake_postgres` fixture pattern (`fake_pg`), `_Probe` / `PostgresHelperMixin._execute`, INSERT SQL shape from `BrandingSessionStore.create`:
  `"INSERT INTO branding_sessions (session_id, session_json, updated_at) VALUES (%s, %s, %s)"`
- Produces: A failing test that expects `UniqueViolation` and an unchanged original row

- [ ] **Step 1: Append the failing test to `test_db.py`**

Add these imports at the top of `test_db.py` (keep existing imports; add only what is missing):

```python
from psycopg.errors import UniqueViolation
```

Append this test at the end of the file (after `test_execute_rowcount_reflects_matched_rows`):

```python
def test_insert_session_raises_unique_violation_on_duplicate(fake_pg: dict) -> None:
    """Duplicate branding_sessions INSERT raises UniqueViolation; original row kept."""
    probe = _Probe()
    now = datetime.now(tz=timezone.utc)
    original = {"mission": "v1"}
    duplicate = {"mission": "v2"}

    probe._execute(
        "INSERT INTO branding_sessions (session_id, session_json, updated_at) "
        "VALUES (%s, %s, %s)",
        ("sess_1", Json(original), now),
    )

    with pytest.raises(UniqueViolation, match='branding_sessions_pkey'):
        probe._execute(
            "INSERT INTO branding_sessions (session_id, session_json, updated_at) "
            "VALUES (%s, %s, %s)",
            ("sess_1", Json(duplicate), now),
        )

    row = fake_pg["sessions"]["sess_1"]
    assert row["session_json"] == original
    assert row["updated_at"] == now
```

- [ ] **Step 2: Run the new test and confirm it fails**

Run from `backend/`:

```bash
pytest agents/branding_team/tests/test_db.py::test_insert_session_raises_unique_violation_on_duplicate -v
```

Expected: FAIL — second insert succeeds (or no `UniqueViolation` raised), because `handle_insert_session` still overwrites silently.

- [ ] **Step 3: Commit the failing test**

```bash
git add backend/agents/branding_team/tests/test_db.py
git commit -m "$(cat <<'EOF'
Add failing test for duplicate branding session insert uniqueness.

EOF
)"
```

---

### Task 2: Raise UniqueViolation in `handle_insert_session`

**Files:**
- Modify: `backend/agents/branding_team/tests/_fake_postgres.py` (imports + `handle_insert_session` ~lines 504–510)
- Test: `backend/agents/branding_team/tests/test_db.py::test_insert_session_raises_unique_violation_on_duplicate`

**Interfaces:**
- Consumes: `cur.db["sessions"]` dict keyed by `session_id`; params `(session_id, session_json, updated_at)`
- Produces: `handle_insert_session` that raises `UniqueViolation` when `session_id` already exists; otherwise inserts as today

- [ ] **Step 1: Import `UniqueViolation`**

Near the top of `_fake_postgres.py`, after the existing imports, add:

```python
from psycopg.errors import UniqueViolation
```

- [ ] **Step 2: Guard `handle_insert_session` before writing**

Replace the current `handle_insert_session` body with:

```python
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
```

Do not change `match_insert_session`, `handle_select_session`, or `handle_update_session`.

- [ ] **Step 3: Run the new test and confirm it passes**

```bash
pytest agents/branding_team/tests/test_db.py::test_insert_session_raises_unique_violation_on_duplicate -v
```

Expected: PASS

- [ ] **Step 4: Run the full branding unit suite**

```bash
pytest agents/branding_team/tests/ -q --ignore=agents/branding_team/tests/test_store_real_postgres.py
```

Expected: all tests pass (0 failures). Skip the `real_postgres` marker suite unless a live Postgres is configured.

- [ ] **Step 5: Commit the fix**

```bash
git add backend/agents/branding_team/tests/_fake_postgres.py
git commit -m "$(cat <<'EOF'
Raise UniqueViolation on duplicate branding session insert in fake.

EOF
)"
```

---

## Self-Review

1. **Spec coverage:** UniqueViolation type + exact message → Task 2. Unchanged original row → Task 1 assertions. Unit test via `_execute` → Task 1. No production changes → Global Constraints. Follow-ups left out → Global Constraints.
2. **Placeholder scan:** No TBD/TODO; full test and handler code included.
3. **Type consistency:** `UniqueViolation` from `psycopg.errors` in both test and handler; params tuple shape matches store INSERT.
