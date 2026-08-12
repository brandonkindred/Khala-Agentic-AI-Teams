"""Shared conversation kernel for design-assistant chat flows.

``AgentDesignerAgent`` (``agent_team_studio.agent_studio.assistant``) and
``ProcessDesignerAgent`` (``agent_team_studio.agentic_team_provisioning.assistant.agent``)
independently reimplement the same three primitives for their LLM chat
conversations: a message envelope DTO, fenced-JSON-in-prose extraction, and
(on the Agent Studio side only) a turn-lock protocol that serializes
concurrent turns against one conversation. This package is the shared,
importable home for those primitives:

* :mod:`.envelope` — the canonical ``ConversationMessage`` DTO.
* :mod:`.fenced_json` — fenced-block extraction, stripping, and keyed-list
  merge helpers.
* :mod:`.turn_lock` — the generic ``ConversationTurn`` snapshot type, an
  in-memory keyed turn lock, and the ``TurnStore`` protocol.

``agent_studio`` (the Agent designer) imports these primitives directly — its
local fenced-JSON parsers, states-merge overlay, turn-lock, and message DTO are
gone. ``agentic_team_provisioning`` (the Process Designer) now imports the
fenced-JSON helpers and the message DTO too; it has no turn-lock of its own to
migrate onto :mod:`.turn_lock` (its conversation routes never had one), and
wiring one up remains a follow-up, not part of either migration.
"""

from __future__ import annotations

from .envelope import ConversationMessage
from .fenced_json import merge_list_by_key, parse_fenced_json, strip_fenced_blocks
from .turn_lock import ConversationTurn, InMemoryTurnLocks, TurnStore

__all__ = [
    "ConversationMessage",
    "parse_fenced_json",
    "strip_fenced_blocks",
    "merge_list_by_key",
    "ConversationTurn",
    "InMemoryTurnLocks",
    "TurnStore",
]
