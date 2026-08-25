"""Backend conventions profile for the code-v2 review tool agents.

Backend agents share the generalized
:class:`~software_engineering_team.shared.tool_agent_base.BaseReviewToolAgent` with the
frontend team. The only backend-specific bit is that their single-issue
problem-solving prompt carries a ``{language_conventions}`` slot (python vs java).
This intermediate supplies those conventions purely as data via
``conventions_by_language`` — sourced from the team's
:data:`~software_engineering_team.codegen_team.stacks.backend.profile.BACKEND_CONFIG`
``stack_profile`` (the single source of truth), so the concrete agents stay
declarative and conventions cannot drift from the profile.
"""

from __future__ import annotations

from software_engineering_team.codegen_team.stacks.backend.profile import BACKEND_CONFIG
from software_engineering_team.shared.tool_agent_base import BaseReviewToolAgent


class BackendReviewToolAgent(BaseReviewToolAgent):
    """``BaseReviewToolAgent`` profile that feeds python/java conventions to the fix prompt.

    ``conventions_by_language`` is sourced from the team's ``V2TeamConfig`` via
    ``BACKEND_CONFIG.stack_profile.conventions_by_language`` — the single source
    of truth — rather than duplicating the map as a hand-written class constant.
    """

    conventions_by_language = BACKEND_CONFIG.stack_profile.conventions_by_language
