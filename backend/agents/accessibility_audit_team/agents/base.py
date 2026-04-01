"""
Base class for accessibility audit specialist agents.
"""

import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..models import (
    Finding,
    FindingState,
    IssueType,
    Scope,
    Severity,
    Surface,
    WCAGMapping,
)

logger = logging.getLogger(__name__)


class AgentMessage(BaseModel):
    """Message passed between agents."""

    from_agent: str = Field(..., description="Sender agent code")
    to_agent: str = Field(..., description="Recipient agent code")
    message_type: str = Field(..., description="Type of message")
    audit_id: str = Field(default="")
    payload: Dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, description="Higher = more urgent")


class MessageBus:
    """
    Simple in-memory message bus for inter-agent communication.

    Agents send messages addressed to a target agent code; the bus
    holds them until the target agent calls receive().
    """

    def __init__(self):
        self._queues: Dict[str, List[AgentMessage]] = {}

    def send(self, message: AgentMessage) -> None:
        """Deliver a message to the target agent's queue."""
        target = message.to_agent
        if target not in self._queues:
            self._queues[target] = []
        self._queues[target].append(message)

    def receive(self, agent_code: str) -> List[AgentMessage]:
        """Drain and return all pending messages for *agent_code*."""
        return self._queues.pop(agent_code, [])

    def pending_count(self, agent_code: str) -> int:
        """Number of messages waiting for *agent_code*."""
        return len(self._queues.get(agent_code, []))


class BaseSpecialistAgent(ABC):
    """
    Base class for all accessibility audit specialist agents.

    Each specialist owns a specific domain and produces specific outputs.
    Agents communicate via structured messages routed through a shared
    MessageBus and share data through a common artifact store.
    """

    agent_code: str = ""
    agent_name: str = ""
    description: str = ""

    def __init__(
        self,
        llm_client: Optional[Any] = None,
        message_bus: Optional[MessageBus] = None,
    ):
        """Initialize the specialist agent."""
        self.llm_client = llm_client
        self._message_bus = message_bus
        self._message_queue: List[AgentMessage] = []

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a task within this agent's domain.

        Args:
            context: Task context including audit_id, phase, and relevant data

        Returns:
            Result dictionary with agent outputs
        """
        pass

    async def safe_process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Wrapper around process() with structured error handling.

        Returns {"success": False, "error": ...} on exception instead of
        propagating.
        """
        try:
            return await self.process(context)
        except Exception as e:
            logger.exception("%s failed during processing: %s", self.agent_code, e)
            return {"success": False, "error": str(e), "agent": self.agent_code}

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def send_message(self, message: AgentMessage) -> None:
        """Send a message — routed via the shared bus when available."""
        if self._message_bus is not None:
            self._message_bus.send(message)
        else:
            self._message_queue.append(message)

    def receive_messages(self) -> List[AgentMessage]:
        """Get and clear pending messages for this agent."""
        if self._message_bus is not None:
            return self._message_bus.receive(self.agent_code)
        messages = self._message_queue[:]
        self._message_queue.clear()
        return messages

    # ------------------------------------------------------------------
    # Finding factory
    # ------------------------------------------------------------------

    def create_finding(
        self,
        audit_id: str,
        target: str,
        surface: Surface,
        issue_type: IssueType,
        severity: Severity,
        title: str,
        summary: str,
        expected: str,
        actual: str,
        user_impact: str,
        wcag_scs: List[str],
        scope: Scope = Scope.LOCALIZED,
        confidence: float = 0.7,
    ) -> Finding:
        """Create a draft finding with standard defaults."""
        return Finding(
            id=f"finding_{uuid.uuid4().hex[:8]}",
            state=FindingState.DRAFT,
            surface=surface,
            target=target,
            issue_type=issue_type,
            severity=severity,
            scope=scope,
            confidence=confidence,
            title=title,
            summary=summary,
            repro_steps=[],
            expected=expected,
            actual=actual,
            user_impact=user_impact,
            wcag_mappings=[
                WCAGMapping(sc=sc, name="", confidence=0.8, rationale="")
                for sc in wcag_scs
            ],
            created_by=self.agent_code,
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(code={self.agent_code})"
