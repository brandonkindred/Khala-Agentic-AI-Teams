# Branding Fake Session Unique-Violation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `handle_insert_session` raise `psycopg.errors.UniqueViolation` on duplicate `session_id`, matching the real `branding_sessions` primary key.

**Architecture:** Guard the existing dict-backed insert handler before write; leave select/update and production store code untouched. Prove the contract with one unit test through `PostgresHelperMixin._execute`.

**Tech Stack:** Python 3.10+, pytest, psycopg (`UniqueViolation`), branding team fake Postgres harness.

**Spec:** `docs/superpowers/specs/2026-07-24-insert-session-unique-violation-design.md`

## Global Constraints

- Raise real `psycopg.errors.UniqueViolation`, not a local `FakeUniqueViolation`.
- Exception message must include `branding_sessions_pkey`.
- Sessions-only: do not change `handle_insert_client`, `handle_insert_brand`, or `handle_insert_conversation` in this plan.
- No production store/API changes.
- Work in worktree `.worktrees/fix-2263-insert-session-unique` on branch `fix/2263-insert-session-unique`.
- Never reference GitHub issue numbers in code, comments, or commit messages (PR body only).

## File Structure

| File | Responsibility |
|---|---|
| `backend/agents/branding_team/tests/_fake_postgres.py` | Branding SQL→handler dispatch; own the duplicate-session guard |
| `backend/agents/branding_team/tests/test_db.py` | Unit coverage for fake Postgres behavior via `_Probe` / `_execute` |

---

### Task 1: Duplicate session insert raises UniqueViolation

**Files:**
- Modify: `backend/agents/branding_team/tests/test_db.py` (append new test; import `UniqueViolation`)
- Modify: `backend/agents/branding_team/tests/_fake_postgres.py` (`handle_insert_session` ~lines 504–510; add `UniqueViolation` import)
- Test: `backend/agents/branding_team/tests/test_db.py::test_duplicate_session_insert_raises_unique_violation`

**Interfaces:**
- Consumes: `install_fake_postgres` / `fake_pg` fixture; `PostgresHelperMixin._execute`; store SQL shape from `BrandingSessionStore.create`
- Produces: `handle_insert_session` raises `UniqueViolation` when `session_id in cur.db["sessions"]`; otherwise writes the row unchanged from today

- [ ] **Step 1: Write the failing test**

In `backend/agents/branding_team/tests/test_db.py`, add the import next to the existing `psycopg.types.json.Json` import:

```python
from psycopg.errors import UniqueViolation
from psycopg.types.json import Json
```

Append this test at the end of the file:

```python
def test_duplicate_session_insert_raises_unique_violation(fake_pg: dict) -> None:
    """Duplicate branding_sessions insert raises UniqueViolation and keeps the row."""
    probe = _Probe()
    now = datetime.now(tz=timezone.utc)
    original = {"mission": {"company_name": "Acme"}, "questions": []}
    probe._execute(
        "INSERT INTO branding_sessions (session_id, session_json, updated_at) "
        "VALUES (%s, %s, %s)",
        ("sess_1", Json(original), now),
    )

    with pytest.raises(UniqueViolation, match="branding_sessions_pkey"):
        probe._execute(
            "INSERT INTO branding_sessions (session_id, session_json, updated_at) "
            "VALUES (%s, %s, %s)",
            (
                "sess_1",
                Json({"mission": {"company_name": "Overwrite"}, "questions": []}),
                now,
            ),
        )

    assert fake_pg["sessions"]["sess_1"]["session_json"] == original
    assert fake_pg["sessions"]["sess_1"]["updated_at"] is now
```

- [ ] **Step 2: Run test to verify it fails**

From `backend/`:

```bash
LLM_PROVIDER=dummy .venv/bin/pytest \
  agents/branding_team/tests/test_db.py::test_duplicate_session_insert_raises_unique_violation -v
```

Expected: FAIL — either `Failed: DID NOT RAISE <class 'psycopg.errors.UniqueViolation'>` (current silent overwrite) or assertion failure on `session_json` if the raise somehow succeeds without the guard. Do not proceed until the failure is the missing raise / overwrite.

- [ ] **Step 3: Implement the guard in `handle_insert_session`**

In `backend/agents/branding_team/tests/_fake_postgres.py`, add the import with the other top-level imports (after `from typing import Any`):

```python
from psycopg.errors import UniqueViolation
```

Replace `handle_insert_session` with:

```python
    def handle_insert_session(cur: FakeCursor, params: tuple) -> None:
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

Do not change `match_insert_session`, select, or update handlers.

- [ ] **Step 4: Run the new test and the branding suite**

```bash
LLM_PROVIDER=dummy .venv/bin/pytest \
  agents/branding_team/tests/test_db.py::test_duplicate_session_insert_raises_unique_violation -v
```

Expected: PASS

```bash
LLM_PROVIDER=dummy .venv/bin/pytest agents/branding_team/tests/ -q
```

Expected: all previously green tests still pass (plus the new one); skips for real-postgres / API markers unchanged.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/branding_team/tests/_fake_postgres.py \
  backend/agents/branding_team/tests/test_db.py
git commit -m "$(cat <<'EOF'
Raise UniqueViolation on duplicate branding session insert in fake Postgres.

EOF
)"
```

---

### Task 2: Open follow-up issues for other silent-overwrite inserts

**Files:**
- None in-repo (GitHub issues only)

**Interfaces:**
- Consumes: same root cause as Task 1; handlers `handle_insert_client`, `handle_insert_brand`, `handle_insert_conversation` in `_fake_postgres.py`
- Produces: three open GitHub issues linking back to the pattern fixed for sessions

- [ ] **Step 1: Create three issues with `gh`**

From the worktree root (needs network):

```bash
gh issue create --title "[low] handle_insert_client silently overwrites an existing client with the same id" --body "$(cat <<'EOF'
## Context

Follow-up from the branding fake Postgres unique-violation fix for sessions.

## Problem

`handle_insert_client` in `backend/agents/branding_team/tests/_fake_postgres.py` silently overwrites when `client_id` already exists in `cur.db["clients"]`. The real `branding_clients` table has a primary key on `id`, so a duplicate insert should raise `psycopg.errors.UniqueViolation`.

## Suggested fix

Mirror the session handler: if the id is already present, raise `UniqueViolation` with a message naming `branding_clients_pkey`, and leave the existing row unchanged. Add a unit test in `test_db.py`.

## Non-goals

No production store changes unless a real double-insert path is found.
EOF
)"

gh issue create --title "[low] handle_insert_brand silently overwrites an existing brand with the same id" --body "$(cat <<'EOF'
## Context

Follow-up from the branding fake Postgres unique-violation fix for sessions.

## Problem

`handle_insert_brand` in `backend/agents/branding_team/tests/_fake_postgres.py` silently overwrites when `brand_id` already exists in `cur.db["brands"]`. The real `branding_brands` table has a primary key on `id`, so a duplicate insert should raise `psycopg.errors.UniqueViolation`.

## Suggested fix

If the id is already present, raise `UniqueViolation` with a message naming `branding_brands_pkey`, and leave the existing row unchanged. Add a unit test in `test_db.py`.

## Non-goals

No production store changes unless a real double-insert path is found.
EOF
)"

gh issue create --title "[low] handle_insert_conversation silently overwrites an existing conversation with the same id" --body "$(cat <<'EOF'
## Context

Follow-up from the branding fake Postgres unique-violation fix for sessions.

## Problem

`handle_insert_conversation` in `backend/agents/branding_team/tests/_fake_postgres.py` silently overwrites when `conversation_id` already exists in `cur.db["conversations"]`. The real `branding_conversations` table has a primary key on `conversation_id`, so a duplicate insert should raise `psycopg.errors.UniqueViolation`.

## Suggested fix

If the id is already present, raise `UniqueViolation` with a message naming `branding_conversations_pkey`, and leave the existing row unchanged. Add a unit test in `test_db.py`.

## Non-goals

No production store changes unless a real double-insert path is found.
EOF
)"
```

- [ ] **Step 2: Record the issue URLs**

Paste the three URLs into the PR description when opening the PR for Task 1 (under a “Follow-ups” heading). Do not put issue numbers in code or commit messages.

---

## Self-review checklist (plan author)

1. **Spec coverage:** UniqueViolation type + pkey message → Task 1 Step 3; unit test + unchanged row → Task 1 Steps 1–4; follow-up issues → Task 2; non-goals (no production / no other handlers) → Global Constraints.
2. **Placeholders:** None — full test and handler code included.
3. **Type consistency:** `UniqueViolation` from `psycopg.errors`; params `(session_id, session_json, updated_at)` match store SQL and existing handler.
