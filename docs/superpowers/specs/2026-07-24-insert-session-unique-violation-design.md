# Design: Raise UniqueViolation on duplicate branding session insert

**Issue:** #2263  
**Branch / worktree:** `fix/2263-insert-session-unique`  
**Date:** 2026-07-24

## Problem

`handle_insert_session` in
`backend/agents/branding_team/tests/_fake_postgres.py` silently overwrites an
existing row when the same ``session_id`` is inserted again.

The real ``branding_sessions`` table declares ``session_id TEXT PRIMARY KEY``,
so a duplicate insert against live Postgres raises a unique-violation
(``psycopg.errors.UniqueViolation``, SQLSTATE ``23505``). The fake's overwrite
behavior can mask duplicate-insert bugs in code under test.

## Goal

Make the fake match Postgres primary-key semantics for session inserts: a
second insert with an existing ``session_id`` must raise
``psycopg.errors.UniqueViolation`` and leave the original row unchanged.

## Non-goals

- No production store or API changes (``BrandingSessionStore`` already
  generates a fresh UUID per ``create``).
- No shared helper in ``shared.postgres.fake`` in this change.
- No fixes for the analogous silent-overwrite paths on
  ``handle_insert_client``, ``handle_insert_brand``, or
  ``handle_insert_conversation`` (tracked as follow-up issues).

## Design

### Exception type

Raise the real ``psycopg.errors.UniqueViolation`` with message:

```text
duplicate key value violates unique constraint "branding_sessions_pkey"
```

Prefer the live exception type over a local ``FakeUniqueViolation`` so any
future ``except UniqueViolation`` / ``IntegrityError`` handling in branding
code sees the same type under the fake as under Postgres.

### Handler change

In ``handle_insert_session``, before writing:

1. If ``session_id in cur.db["sessions"]``, raise ``UniqueViolation`` with the
   message above.
2. Otherwise insert the row as today (``session_id``, unwrapped
   ``session_json``, ``updated_at``).

Select and update session handlers are unchanged.

### Files touched

| File | Change |
|---|---|
| `backend/agents/branding_team/tests/_fake_postgres.py` | Import ``UniqueViolation``; guard in ``handle_insert_session`` |
| `backend/agents/branding_team/tests/test_db.py` | Add duplicate-insert unit test |

### Testing

Add a unit test that:

1. Inserts a ``branding_sessions`` row via the fake (through
   ``PostgresHelperMixin._execute`` or equivalent).
2. Attempts a second insert with the same ``session_id``.
3. Asserts ``pytest.raises(UniqueViolation)``.
4. Asserts the original row's ``session_json`` / ``updated_at`` are unchanged.

Verify with:

```bash
pytest agents/branding_team/tests/ -q
```

### Follow-ups

Open separate GitHub issues for the same silent-overwrite bug on:

- ``handle_insert_client`` (``branding_clients`` PK)
- ``handle_insert_brand`` (``branding_brands`` PK)
- ``handle_insert_conversation`` (``branding_conversations`` PK)

## Success criteria

1. Duplicate session insert via the fake raises ``UniqueViolation``.
2. Existing session row is not overwritten on the failed insert.
3. Existing branding unit tests remain green.
4. No production code changes for this fix.
