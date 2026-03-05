"""
Base class for accessibility audit specialist agents.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AgentMessage(BaseModel):
    """Message passed between agents."""

    from_agent: str = Field(..., description="Sender agent code")
    to_agent: str = Field(..., description="Recipient agent code")
    message_type: str = Field(..., description="Type of message")
    audit_id: str = Field(default="")
    payload: Dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, description="Higher = more urgent")


class BaseSpecialistAgent(ABC):
    """
    Base class for all accessibility audit specialist agents.

    Each specialist owns a specific domain and produces specific outputs.
    Agents communicate via structured messages and share data through
    a common artifact store.
    """

    agent_code: str = ""
    agent_name: str = ""
    description: str = ""

    def __init__(self, llm_client: Optional[Any] = None):
        """Initialize the specialist agent."""
        self.llm_client = llm_client
        self._message_queue: List[AgentMessage] = []

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

    def send_message(self, message: AgentMessage) -> None:
        """Queue a message to another agent."""
        self._message_queue.append(message)

    def receive_messages(self) -> List[AgentMessage]:
        """Get and clear pending messages."""
        messages = self._message_queue[:]
        self._message_queue.clear()
        return messages

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(code={self.agent_code})"
