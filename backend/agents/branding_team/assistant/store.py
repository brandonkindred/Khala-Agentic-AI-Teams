"""In-memory store for branding conversation state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, List, Optional
from uuid import uuid4

from branding_team.models import BrandingMission, TeamOutput


def _default_mission() -> BrandingMission:
    """Minimal mission for new conversations (validates required fields)."""
    return BrandingMission(
        company_name="TBD",
        company_description="To be discussed.",
        target_audience="TBD",
    )


@dataclass
class _StoredMessage:
    role: str  # "user" | "assistant"
    content: str
    timestamp: str


@dataclass
class _ConversationRecord:
    conversation_id: str
    messages: List[_StoredMessage] = field(default_factory=list)
    mission: BrandingMission = field(default_factory=_default_mission)
    latest_output: Optional[TeamOutput] = None


class BrandingConversationStore:
    """Thread-safe in-memory store for chat conversations and mission state."""

    def __init__(self) -> None:
        self._conversations: Dict[str, _ConversationRecord] = {}
        self._lock = Lock()

    def create(
        self,
        conversation_id: Optional[str] = None,
        mission: Optional[BrandingMission] = None,
        latest_output: Optional[TeamOutput] = None,
    ) -> str:
        cid = conversation_id or str(uuid4())
        with self._lock:
            self._conversations[cid] = _ConversationRecord(
                conversation_id=cid,
                messages=[],
                mission=mission or _default_mission(),
                latest_output=latest_output,
            )
        return cid

    def get(
        self, conversation_id: str
    ) -> Optional[tuple[List[_StoredMessage], BrandingMission, Optional[TeamOutput]]]:
        with self._lock:
            rec = self._conversations.get(conversation_id)
            if rec is None:
                return None
            return (list(rec.messages), rec.mission, rec.latest_output)

    def append_message(self, conversation_id: str, role: str, content: str) -> bool:
        if role not in ("user", "assistant"):
            return False
        ts = datetime.now(tz=timezone.utc).isoformat()
        with self._lock:
            rec = self._conversations.get(conversation_id)
            if rec is None:
                return False
            rec.messages.append(_StoredMessage(role=role, content=content, timestamp=ts))
        return True

    def update_mission(self, conversation_id: str, mission: BrandingMission) -> bool:
        with self._lock:
            rec = self._conversations.get(conversation_id)
            if rec is None:
                return False
            rec.mission = mission
        return True

    def update_output(
        self, conversation_id: str, output: Optional[TeamOutput]
    ) -> bool:
        with self._lock:
            rec = self._conversations.get(conversation_id)
            if rec is None:
                return False
            rec.latest_output = output
        return True


_default_store: Optional[BrandingConversationStore] = None


def get_conversation_store() -> BrandingConversationStore:
    global _default_store
    if _default_store is None:
        _default_store = BrandingConversationStore()
    return _default_store
