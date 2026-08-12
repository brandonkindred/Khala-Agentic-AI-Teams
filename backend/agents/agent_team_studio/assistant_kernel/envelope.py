"""The canonical design-assistant chat message envelope.

Reconciles the two DTOs that grew independently in ``agent_studio.models``
(frozen, no timestamp) and ``agentic_team_provisioning.models`` (mutable,
timestamp required): this envelope is frozen like the former and carries an
optional timestamp like the latter, so either caller can adopt it without a
shape it can't represent. Both ``agent_studio.models`` and
``agentic_team_provisioning.models`` now import and re-export this class
directly, in place of their former local definitions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ConversationMessage(BaseModel):
    """One turn in a design-assistant conversation.

    Invariants:
        * Frozen — a stored message is never mutated after creation, so a
          store snapshot can safely share instances with internal state.
    """

    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant"]
    content: str
    timestamp: str | None = None
