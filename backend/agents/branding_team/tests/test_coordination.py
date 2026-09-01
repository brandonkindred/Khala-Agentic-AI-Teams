"""Pure unit tests for ``coordination.attach_conversation_to_brand``.

These exercise the coordinator's own logic in isolation — precondition
validation, the fail-closed guard against an unrecognized
``ConversationAttachResult``, and how it wires the two stores together —
against small stand-in stores, not live Postgres. End-to-end behavior
against real tables (races closed, actual row contents) is covered by
``test_store.py``'s ``test_attach_conversation_*`` tests, which exercise
this same function indirectly through ``BrandingStore.attach_conversation``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

import pytest

from branding_team.assistant.store import ConversationAttachResult
from branding_team.coordination import attach_conversation_to_brand
from branding_team.store import AttachConversationResult
from branding_team.tests.conftest import make_mission


class _StubConversationStore:
    """Cursor-aware ``attach_locked`` stand-in returning a fixed result."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[tuple] = []

    def attach_locked(self, cur: Any, conversation_id: str, brand_id: str, mission: Any) -> Any:
        self.calls.append((cur, conversation_id, brand_id, mission))
        return self._result


class _StubBrandingStore:
    """``_transaction``/``patch_brand_locked`` stand-in for ``BrandingStore``."""

    def __init__(self, patched_brand: Any = "patched-brand") -> None:
        self._patched_brand = patched_brand
        self.patch_calls: list[tuple] = []

    @contextmanager
    def _transaction(self) -> Iterator[str]:
        yield "cursor"

    def patch_brand_locked(
        self, cur: Any, brand_id: str, client_id: str, patch: dict
    ) -> Optional[Any]:
        self.patch_calls.append((cur, brand_id, client_id, patch))
        return self._patched_brand


# ---------------------------------------------------------------------------
# Preconditions are enforced, not just documented
# ---------------------------------------------------------------------------


def test_rejects_empty_client_id() -> None:
    with pytest.raises(ValueError, match="client_id"):
        attach_conversation_to_brand(
            _StubBrandingStore(),
            _StubConversationStore(ConversationAttachResult.OK),
            "",
            "brand_x",
            "conv_x",
        )


def test_rejects_empty_brand_id() -> None:
    with pytest.raises(ValueError, match="brand_id"):
        attach_conversation_to_brand(
            _StubBrandingStore(),
            _StubConversationStore(ConversationAttachResult.OK),
            "client_x",
            "",
            "conv_x",
        )


def test_rejects_empty_conversation_id() -> None:
    with pytest.raises(ValueError, match="conversation_id"):
        attach_conversation_to_brand(
            _StubBrandingStore(),
            _StubConversationStore(ConversationAttachResult.OK),
            "client_x",
            "brand_x",
            "",
        )


def test_rejects_bad_mission_type() -> None:
    with pytest.raises(ValueError, match="mission"):
        attach_conversation_to_brand(
            _StubBrandingStore(),
            _StubConversationStore(ConversationAttachResult.OK),
            "client_x",
            "brand_x",
            "conv_x",
            mission={"company_name": "Acme"},
        )


def test_preconditions_are_checked_before_opening_a_transaction() -> None:
    """A precondition violation must not touch either store at all."""
    conv_store = _StubConversationStore(ConversationAttachResult.OK)
    store = _StubBrandingStore()
    with pytest.raises(ValueError):
        attach_conversation_to_brand(store, conv_store, "", "brand_x", "conv_x")
    assert conv_store.calls == []
    assert store.patch_calls == []


# ---------------------------------------------------------------------------
# Result wiring: each ConversationAttachResult maps to the right outcome
# ---------------------------------------------------------------------------


def test_conversation_not_found_short_circuits_before_patching_the_brand() -> None:
    conv_store = _StubConversationStore(ConversationAttachResult.NOT_FOUND)
    store = _StubBrandingStore()
    result, brand = attach_conversation_to_brand(store, conv_store, "client_x", "brand_x", "conv_x")
    assert result is AttachConversationResult.CONVERSATION_NOT_FOUND
    assert brand is None
    assert store.patch_calls == []


def test_already_attached_short_circuits_before_patching_the_brand() -> None:
    conv_store = _StubConversationStore(ConversationAttachResult.ALREADY_ATTACHED)
    store = _StubBrandingStore()
    result, brand = attach_conversation_to_brand(store, conv_store, "client_x", "brand_x", "conv_x")
    assert result is AttachConversationResult.ALREADY_ATTACHED
    assert brand is None
    assert store.patch_calls == []


def test_brand_not_found_when_patch_brand_locked_returns_none() -> None:
    conv_store = _StubConversationStore(ConversationAttachResult.OK)
    store = _StubBrandingStore(patched_brand=None)
    result, brand = attach_conversation_to_brand(store, conv_store, "client_x", "brand_x", "conv_x")
    assert result is AttachConversationResult.BRAND_NOT_FOUND
    assert brand is None


def test_ok_delegates_to_both_stores_cursor_aware_methods() -> None:
    conv_store = _StubConversationStore(ConversationAttachResult.OK)
    store = _StubBrandingStore(patched_brand="the-updated-brand")
    mission = make_mission(company_name="Acme")

    result, brand = attach_conversation_to_brand(
        store, conv_store, "client_x", "brand_x", "conv_x", mission
    )

    assert result is AttachConversationResult.OK
    assert brand == "the-updated-brand"
    assert conv_store.calls == [("cursor", "conv_x", "brand_x", mission)]
    assert len(store.patch_calls) == 1
    cur, brand_id, client_id, patch = store.patch_calls[0]
    assert (cur, brand_id, client_id) == ("cursor", "brand_x", "client_x")
    assert patch["conversation_id"] == "conv_x"
    assert "updated_at" in patch


def test_fails_closed_on_an_unrecognized_conversation_attach_result() -> None:
    """A ``ConversationAttachResult`` member this function doesn't recognize

    must raise rather than be silently treated as success — guards against
    ``assistant.store`` gaining a new member without a matching branch here.
    """
    conv_store = _StubConversationStore("not-a-real-result")
    store = _StubBrandingStore()
    with pytest.raises(RuntimeError, match="unrecognized ConversationAttachResult"):
        attach_conversation_to_brand(store, conv_store, "client_x", "brand_x", "conv_x")
    assert store.patch_calls == []
