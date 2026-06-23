"""Shared Postgres-availability guard for the SE observability stores.

``se_events`` / ``trace_store`` / ``learnings_store`` each open every operation
with the same two-step guard: import ``shared_postgres`` (whose optional
dependencies may be absent) and check ``is_postgres_enabled()``.
:func:`postgres_available` collapses that into one import-safe predicate so each
store operation degrades to its documented no-op (``False`` / ``[]`` / ``0`` /
zeroed summary) without repeating the boilerplate.

Invariant:
    - Never raises — an unimportable ``shared_postgres`` or a disabled Postgres
      both yield ``False``.
"""

from __future__ import annotations


def postgres_available() -> bool:
    """Return True iff ``shared_postgres`` imports and reports Postgres enabled.

    Postconditions:
        - ``False`` when ``shared_postgres`` cannot be imported (its optional
          dependencies are absent) or ``is_postgres_enabled()`` is falsy; never
          raises. When it returns ``True``, a subsequent
          ``from shared_postgres import ...`` is guaranteed to succeed.
    """
    try:
        from shared_postgres import is_postgres_enabled
    except Exception:
        return False
    return bool(is_postgres_enabled())


__all__ = ["postgres_available"]
