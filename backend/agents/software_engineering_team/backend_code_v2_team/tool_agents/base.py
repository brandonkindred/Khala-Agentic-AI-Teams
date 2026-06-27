"""Backend-specific base for the code-v2 review tool agents.

Backend agents share :class:`BaseReviewToolAgent` with the frontend team but
their single-issue problem-solving prompt carries a ``{language_conventions}``
slot (python vs java). This intermediate injects those conventions from the
microtask's ``language`` so the concrete agents stay purely declarative.
"""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.shared.tool_agent_base import BaseReviewToolAgent

from ..prompts import JAVA_CONVENTIONS, PYTHON_CONVENTIONS


class BackendReviewToolAgent(BaseReviewToolAgent):
    """``BaseReviewToolAgent`` that feeds python/java conventions to the fix prompt."""

    def _problem_solving_kwargs(self, inp) -> Dict[str, Any]:
        lang = (inp.language or "python").strip().lower()
        return {"language_conventions": JAVA_CONVENTIONS if lang == "java" else PYTHON_CONVENTIONS}
