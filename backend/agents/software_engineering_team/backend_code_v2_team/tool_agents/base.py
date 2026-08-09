"""Backend conventions profile for the code-v2 review tool agents.

Backend agents share the generalized
:class:`~software_engineering_team.shared.tool_agent_base.BaseReviewToolAgent` with the
frontend team. The only backend-specific bit is that their single-issue
problem-solving prompt carries a ``{language_conventions}`` slot (python vs java).
This intermediate supplies those conventions purely as data via
``conventions_by_language`` — the base injects them from the microtask's
``language`` — so the concrete agents stay declarative.
"""

from __future__ import annotations

from software_engineering_team.shared.tool_agent_base import BaseReviewToolAgent

from ..prompts import JAVA_CONVENTIONS, PYTHON_CONVENTIONS


class BackendReviewToolAgent(BaseReviewToolAgent):
    """``BaseReviewToolAgent`` profile that feeds python/java conventions to the fix prompt."""

    conventions_by_language = {"java": JAVA_CONVENTIONS, "_default": PYTHON_CONVENTIONS}
