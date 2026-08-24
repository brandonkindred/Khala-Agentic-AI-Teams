"""In-memory store doubles for branding API tests.

Method-level replacements for ``BrandingStore``, ``BrandingConversationStore``,
and ``BrandingSessionStore`` so HTTP and orchestrator tests can exercise route
logic without live Postgres.

Example::

    from branding_team.tests._memory_stores import install_memory_stores

    def test_something(monkeypatch):
        bundle = install_memory_stores(monkeypatch)
        client = bundle.clients  # shared dict backing all three stores
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

import pytest

from branding_team.api.models import BrandingSession
from branding_team.api.state import _build_open_questions
from branding_team.assistant.store import (
    ConversationState,
    ConversationSummary,
    _default_mission,
    _StoredMessage,
)
from branding_team.models import (
    Brand,
    BrandingMission,
    BrandPhase,
    BrandStatus,
    BrandVersionSummary,
    Client,
    TeamOutput,
)
from branding_team.store import AttachConversationResult


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(tz=timezone.utc)


def _validate_pagination(limit: Optional[int], offset: int) -> None:
    """Enforce pagination preconditions with real raises (survives ``python -O``).

    Preconditions:
        ``limit`` is None or a positive int; ``offset`` is >= 0.
    """
    if limit is not None and limit <= 0:
        raise ValueError("limit must be None or a positive int")
    if offset < 0:
        raise ValueError("offset must be >= 0")


def _row_ts(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


@dataclass
class _ConversationRecord:
    conversation_id: str
    brand_id: Optional[str]
    mission: BrandingMission
    latest_output: Optional[TeamOutput]
    created_at: datetime
    updated_at: datetime


@dataclass
class MemoryStoreBundle:
    """Shared dict backing for the three in-memory store doubles."""

    clients: Dict[str, Client] = field(default_factory=dict)
    brands: Dict[str, Brand] = field(default_factory=dict)
    sessions: Dict[str, BrandingSession] = field(default_factory=dict)
    conversations: Dict[str, _ConversationRecord] = field(default_factory=dict)
    messages: Dict[str, List[_StoredMessage]] = field(default_factory=dict)


class MemoryBrandingStore:
    """In-memory ``BrandingStore`` for API unit tests."""

    def __init__(self, bundle: MemoryStoreBundle) -> None:
        self._bundle = bundle

    def get_client(self, client_id: str) -> Optional[Client]:
        return self._bundle.clients.get(client_id)

    def list_clients(self, limit: Optional[int] = None, offset: int = 0) -> List[Client]:
        _validate_pagination(limit, offset)
        rows = sorted(self._bundle.clients.values(), key=lambda c: (c.created_at, c.id))
        if limit is None:
            return rows[offset:]
        return rows[offset : offset + limit]

    def create_client(
        self,
        name: str,
        contact_info: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Client:
        client_id = f"client_{uuid4().hex[:12]}"
        now = _now_iso()
        client = Client(
            id=client_id,
            name=name,
            created_at=now,
            updated_at=now,
            contact_info=contact_info,
            notes=notes,
        )
        self._bundle.clients[client_id] = client
        return client

    def get_brand(self, client_id: str, brand_id: str) -> Optional[Brand]:
        brand = self._bundle.brands.get(brand_id)
        if brand is None or brand.client_id != client_id:
            return None
        return brand

    def brand_exists(self, brand_id: str) -> bool:
        return brand_id in self._bundle.brands

    def get_brand_by_id(self, brand_id: str) -> Optional[Tuple[str, Brand]]:
        brand = self._bundle.brands.get(brand_id)
        if brand is None:
            return None
        return brand.client_id, brand

    def get_brand_names(self, brand_ids: List[str]) -> Dict[str, Optional[str]]:
        unique_ids = list({bid for bid in brand_ids if bid})
        if not unique_ids:
            return {}
        return {
            bid: self._bundle.brands[bid].name for bid in unique_ids if bid in self._bundle.brands
        }

    def list_brands_for_client(
        self, client_id: str, limit: Optional[int] = None, offset: int = 0
    ) -> List[Brand]:
        _validate_pagination(limit, offset)
        rows = sorted(
            (b for b in self._bundle.brands.values() if b.client_id == client_id),
            key=lambda b: (b.created_at, b.id),
        )
        if limit is None:
            return rows[offset:]
        return rows[offset : offset + limit]

    def create_brand(
        self,
        client_id: str,
        mission: BrandingMission,
        name: Optional[str] = None,
    ) -> Optional[Brand]:
        """Insert a draft brand for an existing client.

        Preconditions:
            ``client_id`` is a non-empty string; ``mission`` is a validated
            ``BrandingMission``; ``name`` is optional (defaults to
            ``mission.company_name``).
        Postconditions:
            Returns ``None`` when no client exists for ``client_id``. Otherwise
            returns a draft ``Brand`` with a freshly minted ``brand_<hex>`` id.
            Profile association via ``record_association_safe`` is intentionally
            skipped — production coupling to ``user_profile`` is out of scope
            for these API unit doubles.
        """
        if client_id not in self._bundle.clients:
            return None
        brand_id = f"brand_{uuid4().hex[:12]}"
        now = _now_iso()
        brand = Brand(
            id=brand_id,
            client_id=client_id,
            name=name or mission.company_name,
            status=BrandStatus.draft,
            mission=mission,
            latest_output=None,
            version=0,
            history=[],
            created_at=now,
            updated_at=now,
        )
        self._bundle.brands[brand_id] = brand
        return brand

    def delete_brand(self, client_id: str, brand_id: str) -> bool:
        brand = self._bundle.brands.get(brand_id)
        if brand is None or brand.client_id != client_id:
            return False
        del self._bundle.brands[brand_id]
        return True

    def update_brand(
        self,
        client_id: str,
        brand_id: str,
        mission: Optional[BrandingMission] = None,
        status: Optional[BrandStatus] = None,
        name: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Optional[Brand]:
        brand = self.get_brand(client_id, brand_id)
        if brand is None:
            return None
        patch: dict = {"updated_at": _now_iso()}
        if mission is not None:
            patch["mission"] = mission
            patch["latest_output"] = None
            patch["current_phase"] = BrandPhase.STRATEGIC_CORE
        if status is not None:
            patch["status"] = status
        if name is not None:
            patch["name"] = name
        if conversation_id is not None:
            patch["conversation_id"] = conversation_id
        updated = brand.model_copy(update=patch)
        self._bundle.brands[brand_id] = updated
        return updated

    def attach_conversation(
        self,
        client_id: str,
        brand_id: str,
        conversation_id: str,
        mission: Optional[BrandingMission] = None,
    ) -> Tuple[AttachConversationResult, Optional[Brand]]:
        conv = self._bundle.conversations.get(conversation_id)
        if conv is None:
            return AttachConversationResult.CONVERSATION_NOT_FOUND, None
        if conv.brand_id and conv.brand_id != brand_id:
            return AttachConversationResult.ALREADY_ATTACHED, None
        brand = self.get_brand(client_id, brand_id)
        if brand is None:
            return AttachConversationResult.BRAND_NOT_FOUND, None

        now = _now_dt()
        conv.brand_id = brand_id
        if mission is not None:
            conv.mission = mission
        conv.updated_at = now
        updated_brand = brand.model_copy(
            update={"conversation_id": conversation_id, "updated_at": _now_iso()}
        )
        self._bundle.brands[brand_id] = updated_brand
        return AttachConversationResult.OK, updated_brand

    def append_brand_version(
        self,
        client_id: str,
        brand_id: str,
        output: TeamOutput,
    ) -> Optional[Brand]:
        brand = self.get_brand(client_id, brand_id)
        if brand is None:
            return None
        now = _now_iso()
        new_version = brand.version + 1
        history_entry = BrandVersionSummary(
            version=new_version,
            created_at=now,
            status=output.status.value,
        )
        updated = brand.model_copy(
            update={
                "latest_output": output,
                "current_phase": output.current_phase,
                "version": new_version,
                "history": [*brand.history, history_entry],
                "updated_at": now,
            }
        )
        self._bundle.brands[brand_id] = updated
        return updated


class MemoryConversationStore:
    """In-memory ``BrandingConversationStore`` subset used by API routes."""

    def __init__(self, bundle: MemoryStoreBundle) -> None:
        self._bundle = bundle

    def _messages_for(self, conversation_id: str) -> List[_StoredMessage]:
        return self._bundle.messages.setdefault(conversation_id, [])

    def create(
        self,
        conversation_id: Optional[str] = None,
        brand_id: Optional[str] = None,
        mission: Optional[BrandingMission] = None,
        latest_output: Optional[TeamOutput] = None,
    ) -> str:
        cid = conversation_id or str(uuid4())
        m = mission or _default_mission()
        now = _now_dt()
        self._bundle.conversations[cid] = _ConversationRecord(
            conversation_id=cid,
            brand_id=brand_id,
            mission=m,
            latest_output=latest_output,
            created_at=now,
            updated_at=now,
        )
        self._bundle.messages.setdefault(cid, [])
        return cid

    def get_state(self, conversation_id: str) -> Optional[ConversationState]:
        conv = self._bundle.conversations.get(conversation_id)
        if conv is None:
            return None
        return ConversationState(
            messages=list(self._messages_for(conversation_id)),
            mission=conv.mission,
            latest_output=conv.latest_output,
            brand_id=conv.brand_id,
        )

    def get(
        self, conversation_id: str
    ) -> Optional[tuple[List[_StoredMessage], BrandingMission, Optional[TeamOutput]]]:
        state = self.get_state(conversation_id)
        if state is None:
            return None
        return (state.messages, state.mission, state.latest_output)

    def append_message(self, conversation_id: str, role: str, content: str) -> bool:
        if role not in ("user", "assistant"):
            return False
        conv = self._bundle.conversations.get(conversation_id)
        if conv is None:
            return False
        ts = _now_dt()
        conv.updated_at = ts
        self._messages_for(conversation_id).append(
            _StoredMessage(role=role, content=content, timestamp=ts.isoformat())
        )
        return True

    def update_mission(self, conversation_id: str, mission: BrandingMission) -> bool:
        conv = self._bundle.conversations.get(conversation_id)
        if conv is None:
            return False
        conv.mission = mission
        conv.updated_at = _now_dt()
        return True

    def update_output(self, conversation_id: str, output: Optional[TeamOutput]) -> bool:
        conv = self._bundle.conversations.get(conversation_id)
        if conv is None:
            return False
        conv.latest_output = output
        conv.updated_at = _now_dt()
        return True

    def set_brand(self, conversation_id: str, brand_id: Optional[str]) -> bool:
        conv = self._bundle.conversations.get(conversation_id)
        if conv is None:
            return False
        conv.brand_id = brand_id
        conv.updated_at = _now_dt()
        return True

    def attach_and_update_mission(
        self, conversation_id: str, brand_id: Optional[str], mission: BrandingMission
    ) -> bool:
        conv = self._bundle.conversations.get(conversation_id)
        if conv is None:
            return False
        conv.brand_id = brand_id
        conv.mission = mission
        conv.updated_at = _now_dt()
        return True

    def get_by_brand_id(
        self, brand_id: str
    ) -> Optional[tuple[str, List[_StoredMessage], BrandingMission, Optional[TeamOutput]]]:
        candidates = [c for c in self._bundle.conversations.values() if c.brand_id == brand_id]
        conv = max(candidates, key=lambda c: c.updated_at, default=None)
        if conv is None:
            return None
        cid = conv.conversation_id
        return (cid, list(self._messages_for(cid)), conv.mission, conv.latest_output)

    def list_conversations(self, brand_id: Optional[str] = None) -> List[ConversationSummary]:
        convs = sorted(
            self._bundle.conversations.values(),
            key=lambda c: c.updated_at,
            reverse=True,
        )
        if brand_id is not None:
            convs = [c for c in convs if c.brand_id == brand_id]
        return [
            ConversationSummary(
                conversation_id=conv.conversation_id,
                brand_id=conv.brand_id,
                created_at=_row_ts(conv.created_at),
                updated_at=_row_ts(conv.updated_at),
                message_count=len(self._messages_for(conv.conversation_id)),
            )
            for conv in convs
        ]

    def get_conversation_brand_id(self, conversation_id: str) -> Optional[str]:
        conv = self._bundle.conversations.get(conversation_id)
        if conv is None or not conv.brand_id:
            return None
        return conv.brand_id


def _session_copy(session: BrandingSession) -> BrandingSession:
    """Return a detached ``BrandingSession`` (JSON round-trip), matching Postgres ``get``.

    Preconditions:
        ``session`` is a valid ``BrandingSession``.
    Postconditions:
        Returns a new model instance; mutating the result does not change ``session``.
    """
    return BrandingSession.model_validate(session.model_dump(mode="json"))


class MemorySessionStore:
    """In-memory ``BrandingSessionStore`` for interactive review routes."""

    def __init__(self, bundle: MemoryStoreBundle) -> None:
        self._bundle = bundle

    def create(
        self, mission: BrandingMission, latest_output: TeamOutput
    ) -> tuple[str, BrandingSession]:
        questions = _build_open_questions(mission)
        session_id = str(uuid4())
        session = BrandingSession(mission=mission, questions=questions, latest_output=latest_output)
        # Persist a detached copy so the returned object is not the stored row
        # (same isolation real Postgres JSON get/create provide).
        self._bundle.sessions[session_id] = _session_copy(session)
        return session_id, session

    def get(self, session_id: str) -> Optional[BrandingSession]:
        stored = self._bundle.sessions.get(session_id)
        if stored is None:
            return None
        return _session_copy(stored)

    def save(self, session_id: str, session: BrandingSession) -> None:
        if session_id in self._bundle.sessions:
            self._bundle.sessions[session_id] = _session_copy(session)


def install_memory_stores(monkeypatch: pytest.MonkeyPatch) -> MemoryStoreBundle:
    """Install in-memory stores into the API collaborator slots.

    Preconditions:
        ``monkeypatch`` is a live pytest MonkeyPatch.
    Postconditions:
        ``main.branding_store``, ``main.conversation_store``, and
        ``routes.sessions.session_store`` are bound to memory doubles
        sharing one ``MemoryStoreBundle``. When ``branding_team.tests.test_api``
        is already imported, ``test_api.branding_store`` is rebound to the
        same memory double. Returns that bundle.
    """
    from branding_team.api import main as main_mod
    from branding_team.api.routes import sessions as sessions_mod

    bundle = MemoryStoreBundle()
    branding = MemoryBrandingStore(bundle)
    conversations = MemoryConversationStore(bundle)
    sessions = MemorySessionStore(bundle)
    monkeypatch.setattr(main_mod, "branding_store", branding)
    monkeypatch.setattr(main_mod, "conversation_store", conversations)
    monkeypatch.setattr(sessions_mod, "session_store", sessions)
    if "branding_team.tests.test_api" in sys.modules:
        test_api_mod = sys.modules["branding_team.tests.test_api"]
        monkeypatch.setattr(test_api_mod, "branding_store", branding)
    return bundle
