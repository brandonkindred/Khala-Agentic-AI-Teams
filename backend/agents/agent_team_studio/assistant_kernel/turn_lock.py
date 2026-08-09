"""The turn-lock protocol: serializing a whole read-LLM-write conversation turn.

``agent_studio``'s conversation stores (``agent_studio.store.AgentStudioConversationStore``
in-memory, ``agent_studio.pg_store.PostgresAgentStudioConversationStore`` on
Postgres) each expose a ``turn(conversation_id)`` context manager that holds a
per-conversation lock across the whole read -> LLM call -> write sequence, so
a concurrent ``send_message`` on the same conversation blocks until the first
turn commits rather than racing it or losing an update. The in-memory store
implements the lock with a per-record ``threading.Lock`` and a manual
rollback-on-exception snapshot restore; the Postgres store implements the
same contract with ``SELECT ... FOR UPDATE`` and transaction rollback.
``agentic_team_provisioning``'s conversation routes have no equivalent lock at
all today.

This module extracts the store-agnostic pieces of that protocol:

* :class:`ConversationTurn` — the snapshot + bound-writer object every
  ``turn()`` implementation yields, generalized off a specific draft type
  (``AgentDefinition`` in ``agent_studio``) to any draft type ``D``.
* :class:`InMemoryTurnLocks` — a reusable keyed lock table implementing the
  acquire -> snapshot -> yield -> rollback-on-exception -> release dance, so
  a future in-memory store doesn't re-derive it from scratch.
* :class:`TurnStore` — a ``Protocol`` documenting the shape both existing
  stores already satisfy structurally, as a contract to code against rather
  than a base class either store must inherit.

Nothing here is wired into either existing store yet; that migration is a
follow-up.
"""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager, contextmanager
from typing import Callable, Generic, Hashable, Iterator, Protocol, TypeVar

D = TypeVar("D")


class ConversationTurn(Generic[D]):
    """One serialized authoring turn: history + draft read at turn start,
    plus buffered write ops applied within the turn's held lock.

    A turn-lock implementation yields one of these while holding its
    per-conversation lock (an in-memory ``threading.Lock`` or a Postgres row
    lock), so the whole read -> LLM -> write sequence of a service's
    message-handling method is serialized against a concurrent turn on the
    same conversation. ``history`` / ``draft`` are a snapshot from the start
    of the turn; ``append_message`` / ``set_draft`` perform the writes bound
    to the locked context.

    Invariants:
        * The bound write callables are only valid for the lifetime of the
          enclosing ``with store.turn(...)`` block that produced this object.
        * ``draft`` must be treated as read-only unless the enclosing
          ``turn()`` call was given a ``clone`` function (see
          :meth:`InMemoryTurnLocks.turn`) — mutating it in place rather than
          calling :meth:`set_draft` can corrupt the turn's rollback snapshot.
    """

    def __init__(
        self,
        *,
        history: list[tuple[str, str]],
        draft: D,
        on_message: Callable[[str, str], None],
        on_draft: Callable[[D], None],
    ) -> None:
        self.history = history
        self.draft = draft
        self._on_message = on_message
        self._on_draft = on_draft

    def append_message(self, role: str, content: str) -> None:
        """Record one message on the turn's locked context.

        Preconditions:
            * Called within the enclosing ``with store.turn(...)`` block.
        Postconditions:
            * The message is persisted once the turn commits; a store error
              propagates and rolls the whole turn back rather than partially
              applying.
        """
        self._on_message(role, content)

    def set_draft(self, draft: D) -> None:
        """Replace the working draft on the turn's locked context.

        Preconditions:
            * Called within the enclosing ``with store.turn(...)`` block.
        Postconditions:
            * The draft is persisted once the turn commits; a store error
              propagates and rolls the whole turn back rather than partially
              applying.
        """
        self._on_draft(draft)


class InMemoryTurnLocks(Generic[D]):
    """A reusable per-key turn-lock table for in-memory conversation stores.

    Generalizes the acquire -> snapshot -> yield -> rollback-on-exception ->
    release dance that ``agent_studio.store.AgentStudioConversationStore.turn()``
    inlines against its own record structure, so a future in-memory store
    (e.g. for the Process Designer) can reuse the locking mechanics while
    supplying its own read/write callables.

    Invariants:
        * Each key maps to at most one ``threading.Lock``, created lazily on
          first use; membership in the lock table is itself guarded by
          ``_table_lock`` so concurrent first-uses of the same key can't
          create two distinct locks for it.
    """

    def __init__(self) -> None:
        self._locks: dict[Hashable, threading.Lock] = {}
        # Guards only membership in ``_locks`` (creation/removal), not the
        # per-key locks themselves — held briefly, never across a turn.
        self._table_lock = threading.Lock()

    def _lock_for(self, key: Hashable) -> threading.Lock:
        with self._table_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def turn(
        self,
        key: Hashable,
        *,
        read: Callable[[], tuple[list[tuple[str, str]], D]],
        on_message: Callable[[str, str], None],
        on_draft: Callable[[D], None],
        restore: Callable[[list[tuple[str, str]], D], None],
        clone: Callable[[D], D] | None = None,
    ) -> Iterator[ConversationTurn[D]]:
        """Serialize a whole turn for ``key``, rolling back on exception.

        Acquires ``key``'s lock and holds it for the duration of the ``with``
        block — including the caller's LLM round trip — so a second
        concurrent ``turn(key, ...)`` blocks until this one exits, then
        proceeds against fresh state. Different keys never contend.

        Preconditions:
            * ``read()`` returns the current ``(history, draft)`` snapshot
              for ``key``; it is called once, after the lock is acquired, so
              it observes state as of turn start (not any earlier caller-side
              snapshot).
            * ``on_message`` / ``on_draft`` perform the actual writes (e.g.
              against a dict, a record, or a transaction) and apply
              immediately when called — they are not buffered by this class.
            * ``restore(history, draft)`` resets the caller's backing state
              to exactly the given pre-turn snapshot; it must not raise.
            * If ``D`` is mutable and ``clone`` is omitted, the caller must
              treat the yielded ``turn.draft`` as read-only (only replace it
              wholesale via ``turn.set_draft``) — without ``clone``, the
              rollback snapshot is the *same object* ``read()`` returned, so
              an in-place mutation of ``turn.draft`` would silently also
              mutate the value ``restore`` rolls back to. Pass ``clone``
              (e.g. a deep-copy function for ``D``) to get a rollback
              snapshot that's independent of whatever the caller does to
              ``turn.draft``.
        Postconditions:
            * On clean exit, whatever ``on_message``/``on_draft`` calls were
              made during the block stand as committed.
            * On any exception inside the block, ``restore`` is invoked with
              the pre-turn snapshot before the exception propagates, so a
              partial write (e.g. the user message appended before the LLM
              call fails) is undone — the caller's state is restored to
              exactly its turn-start values (subject to the ``clone``
              precondition above), then the lock is released.
        """
        lock = self._lock_for(key)
        lock.acquire()
        try:
            history, draft = read()
            history_before = list(history)
            draft_before = clone(draft) if clone is not None else draft
            try:
                yield ConversationTurn(
                    history=history, draft=draft, on_message=on_message, on_draft=on_draft
                )
            except BaseException:
                restore(history_before, draft_before)
                raise
        finally:
            lock.release()

    def discard(self, key: Hashable) -> None:
        """Drop ``key``'s lock entry so a removed record's lock doesn't linger.

        Preconditions:
            * No turn for ``key`` is in flight (the caller removes the owning
              record, and thus calls this, only outside any ``with
              turn(key, ...)`` block for that same key).
        Postconditions:
            * A subsequent ``turn(key, ...)`` call mints a fresh lock for
              ``key``. Idempotent: discarding an unknown key is a no-op.
        """
        with self._table_lock:
            self._locks.pop(key, None)

    def __len__(self) -> int:
        """Number of keys with a live lock entry."""
        with self._table_lock:
            return len(self._locks)


class TurnStore(Protocol[D]):
    """The turn-lock contract both existing conversation stores already satisfy.

    Documents the shape shared by ``agent_studio.store.AgentStudioConversationStore``
    (in-memory ``threading.Lock``) and
    ``agent_studio.pg_store.PostgresAgentStudioConversationStore`` (Postgres
    ``SELECT ... FOR UPDATE``) — a structural contract to code against, not a
    base class either store must inherit. ``create`` / ``get`` are
    intentionally excluded: their signatures are store-specific (e.g. what a
    fresh draft is seeded from) and aren't part of the turn-lock protocol
    itself.
    """

    def turn(self, conversation_id: str) -> AbstractContextManager[ConversationTurn[D]]:
        """Serialize one turn for ``conversation_id``; see :class:`ConversationTurn`."""
        ...

    def append_message(self, conversation_id: str, role: str, content: str) -> None:
        """Append one message outside of a turn (e.g. an initial greeting)."""
        ...

    def discard(self, conversation_id: str) -> None:
        """Remove a conversation record; a no-op if it doesn't exist."""
        ...

    def __len__(self) -> int:
        """Number of live conversations."""
        ...


__all__ = ["ConversationTurn", "InMemoryTurnLocks", "TurnStore", "D"]
