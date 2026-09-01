"""Cross-store atomic operations for the branding team.

The branding team keeps three distinct persistence concerns, each owned by
its own Postgres-backed store: ``BrandingStore`` (clients + brands +
versioned history, ``store.py``), ``BrandingSessionStore`` (sessions), and
``BrandingConversationStore`` (chat conversations + messages + mission +
latest output, ``assistant/store.py``). Every store is the sole owner of its
own table's SQL.

Some operations need to update rows in two of those tables atomically (e.g.
linking a conversation to a brand). That coordination — opening the shared
transaction and deciding what each store does with it — lives here rather
than inside either store, so no single-table store class ends up owning
another store's cross-table orchestration. Each store still owns its own
table's writes: this module only sequences calls into the public,
cursor-aware methods (``BrandingConversationStore.attach_locked``,
``BrandingStore.patch_brand_locked``) that the stores expose for exactly
this purpose.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, Tuple

from .assistant.store import BrandingConversationStore, ConversationAttachResult
from .models import Brand, BrandingMission

if TYPE_CHECKING:
    from .store import AttachConversationResult, BrandingStore


class _AttachAbort(Exception):
    """Internal control-flow signal: abort the attach transaction with *result*."""

    def __init__(self, result: "AttachConversationResult") -> None:
        self.result = result


def attach_conversation_to_brand(
    store: "BrandingStore",
    conv_store: BrandingConversationStore,
    client_id: str,
    brand_id: str,
    conversation_id: str,
    mission: Optional[BrandingMission] = None,
) -> Tuple["AttachConversationResult", Optional[Brand]]:
    """Attach an existing conversation to *brand_id* and patch the brand, atomically.

    Locks the conversation row (``FOR UPDATE``) before checking whether it is
    already attached elsewhere, then updates both the conversation and the
    brand in the same transaction. This closes two races a check-then-write
    sequence across separate transactions would leave open: another request
    attaching the same conversation between the uniqueness check and the
    write, and the brand row disappearing after the conversation was already
    attached (which would otherwise leave the conversation pointing at a
    brand that never learns its id).

    *store* and *conv_store* are the two collaborating stores: this function
    borrows *store*'s ``_transaction()`` to open one connection/transaction
    shared by both writes, calls ``conv_store.attach_locked`` for the
    conversation row (``BrandingConversationStore`` remains the sole owner of
    ``branding_conversations`` writes), and calls *store*'s
    ``patch_brand_locked`` for the brand row (``BrandingStore`` remains the
    sole owner of ``branding_brands`` writes) — both are public,
    cursor-aware methods the stores expose for exactly this cross-store use.

    Preconditions:
        ``client_id``, ``brand_id``, ``conversation_id`` are non-empty
        strings; ``mission``, when provided, is a valid
        :class:`BrandingMission`.
    Postconditions:
        On :attr:`AttachConversationResult.OK`, the conversation row now has
        ``brand_id`` set to *brand_id*, the brand's ``conversation_id`` is set
        to *conversation_id*, and the updated :class:`Brand` is returned.

        When *mission* is provided, ``mission_json`` is overwritten with it.
        When *mission* is omitted (``None``), ``mission_json`` is left as
        whatever this same locked transaction just read — never a snapshot
        the caller took before acquiring the lock. This matters when the
        conversation may have gained a newer mission (e.g. via a concurrent
        ``POST /conversations/{id}/messages`` turn) between the caller's own
        read and this call: passing a stale pre-lock mission here would
        silently roll that edit back and could pair an old mission with
        output generated for a newer one. Callers that are themselves the
        source of truth for the mission (e.g. brand creation, where *mission*
        drove the just-created brand) should still pass it explicitly.

        Any other result leaves both rows unchanged (the transaction rolls
        back) and the paired value is ``None``.
    Raises:
        ValueError: if any of the id preconditions or the mission type
            precondition is violated.
        RuntimeError: if ``conv_store.attach_locked`` ever returns a
            :class:`ConversationAttachResult` member this function doesn't
            recognize — a fail-closed guard against a future member being
            added there without a matching branch here, rather than silently
            treating an unknown result as success.
    """
    from .store import AttachConversationResult

    if not client_id:
        raise ValueError("client_id must be a non-empty string")
    if not brand_id:
        raise ValueError("brand_id must be a non-empty string")
    if not conversation_id:
        raise ValueError("conversation_id must be a non-empty string")
    if mission is not None and not isinstance(mission, BrandingMission):
        raise ValueError("mission must be a BrandingMission")
    try:
        with store._transaction() as cur:
            conv_result = conv_store.attach_locked(cur, conversation_id, brand_id, mission)
            if conv_result is ConversationAttachResult.NOT_FOUND:
                raise _AttachAbort(AttachConversationResult.CONVERSATION_NOT_FOUND)
            if conv_result is ConversationAttachResult.ALREADY_ATTACHED:
                raise _AttachAbort(AttachConversationResult.ALREADY_ATTACHED)
            if conv_result is not ConversationAttachResult.OK:
                raise RuntimeError(f"unrecognized ConversationAttachResult: {conv_result!r}")

            patch = {
                "conversation_id": conversation_id,
                "updated_at": datetime.now(tz=timezone.utc).isoformat(),
            }
            brand = store.patch_brand_locked(cur, brand_id, client_id, patch)
            if brand is None:
                raise _AttachAbort(AttachConversationResult.BRAND_NOT_FOUND)
    except _AttachAbort as exc:
        return exc.result, None
    return AttachConversationResult.OK, brand
