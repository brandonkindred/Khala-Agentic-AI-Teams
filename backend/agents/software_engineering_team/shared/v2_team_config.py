"""
Team-config object for the code-v2 teams.

``backend_code_v2_team`` and ``frontend_code_v2_team`` are twin teams whose
orchestrators, phases, and tool-agent wiring are structurally identical and
diverge only on a fixed set of per-team knobs. The language default and
conventions-by-language map are already captured, as the single source of
truth, by :class:`~software_engineering_team.shared.stack_profile.StackProfile`;
review-body behavior is captured by :class:`~software_engineering_team.shared.v2_review.ReviewConfig`.
:class:`V2TeamConfig` composes a team's ``StackProfile`` rather than
duplicating its fields, and adds the two divergence axes neither existing
object covers: which ``ToolAgentKind`` members a team registers, and the
optional extra review clause a team's code-review fallback injects (e.g.
frontend's accessibility-verification note).

This module holds **only** the dataclass. It imports
:class:`~software_engineering_team.shared.stack_profile.StackProfile` (a
shared → shared import, not shared → team), but nothing from either team
package, so the ``shared → team → shared`` import cycle cannot form —
mirroring ``StackProfile``'s and ``ReviewConfig``'s identical constraint.
Each team will construct its own frozen instance when a later story wires
this object into the orchestrator; as of this change no team does so yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet

from software_engineering_team.shared.stack_profile import StackProfile


@dataclass(frozen=True)
class V2TeamConfig:
    """Per-team configuration capturing backend/frontend code-v2 divergence as data.

    Invariants:
        - ``stack_profile`` is the sole source of truth for this team's
          default language and conventions-by-language map (including the
          ``"_default"``-key invariant, enforced by
          ``StackProfile.__post_init__`` — not duplicated here).
        - The instance is immutable (``frozen=True``); every field is pure
          data.
    """

    stack_profile: StackProfile
    """This team's ``StackProfile`` — composed, not copied, so
    ``default_language`` / ``conventions_by_language`` have exactly one
    source of truth. Access via ``config.stack_profile.default_language``
    etc."""

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
