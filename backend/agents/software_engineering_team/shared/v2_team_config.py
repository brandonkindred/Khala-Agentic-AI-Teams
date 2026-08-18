"""
Team-config object for the code-v2 teams.

``backend_code_v2_team`` and ``frontend_code_v2_team`` are twin teams whose
orchestrators, phases, and tool-agent wiring are structurally identical and
diverge only on a fixed set of per-team knobs. Two of those divergence axes
already have a dedicated frozen-dataclass object: :class:`~software_engineering_team.shared.stack_profile.StackProfile`
(language default + repo/tooling detection) and :class:`~software_engineering_team.shared.v2_review.ReviewConfig`
(review-body behavior). :class:`V2TeamConfig` captures the remaining
divergence the base orchestrator (a later story) will need: which
``ToolAgentKind`` members a team registers, and the optional extra review
clause a team's code-review fallback injects (e.g. frontend's
accessibility-verification note) — plus, for the same single seam, the
language default and conventions map ``StackProfile`` already holds.

This module holds **only** the dataclass — it imports nothing from either
team, so the ``shared → team → shared`` import cycle cannot form, mirroring
``StackProfile``'s and ``ReviewConfig``'s identical constraint. Each team
will construct its own frozen instance when a later story wires this object
into the orchestrator; as of this change no team does so yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet


@dataclass(frozen=True)
class V2TeamConfig:
    """Per-team configuration capturing backend/frontend code-v2 divergence as data.

    Invariants:
        - ``conventions_by_language`` always contains a ``"_default"`` key
          (mirrors ``StackProfile.conventions_by_language``'s identical
          invariant; both are populated from the same per-team dict today).
        - The instance is immutable (``frozen=True``); every field is pure
          data.
    """

    default_language: str
    """Fallback language/stack for this team when detection yields nothing
    (e.g. ``"python"``, ``"typescript"``)."""

    tool_agent_kinds: FrozenSet[str]
    """This team's ``ToolAgentKind`` member values, as plain strings — not
    the enum type itself, since this module must not import either team's
    ``ToolAgentKind`` (mirrors ``shared.v2_models.Microtask.tool_agent``'s
    identical rationale: both team enums are ``(str, Enum)`` subclasses, so
    their values serialize losslessly into a plain ``str``/``FrozenSet[str]``)."""

    extra_review_clause: str
    """Extra guidance appended to code-review task requirements (e.g.
    frontend's accessibility-verification note, restoring what its retired
    ``REVIEW_PROMPT`` used to state explicitly). ``""`` means no extra
    clause (e.g. backend, whose code has no UI to check accessibility on)."""

    conventions_by_language: Dict[str, str]
    """Map of language name (plus a required ``"_default"`` key) to a
    conventions string, mirroring ``StackProfile.conventions_by_language``."""

    def __post_init__(self) -> None:
        """Enforce the ``conventions_by_language`` invariant at construction.

        Preconditions: none.
        Postconditions: raises ``ValueError`` if ``conventions_by_language``
        lacks a ``"_default"`` key.
        """
        if "_default" not in self.conventions_by_language:
            raise ValueError("conventions_by_language must contain a '_default' key")
