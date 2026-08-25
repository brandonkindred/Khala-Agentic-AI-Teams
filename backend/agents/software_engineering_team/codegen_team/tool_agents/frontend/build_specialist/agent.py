"""Build Specialist tool agent for frontend-code-v2 (thin per-stack profile).

The shared build runner and base live in
:mod:`software_engineering_team.shared.tool_agent_build_specialist`; this module
binds the frontend build runner and the frontend single-issue prompt + parser.
"""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.codegen_team.stacks.frontend.profile import (
    parse_problem_solving_single_issue_template,
)
from software_engineering_team.codegen_team.stacks.frontend.prompts import (
    PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT,
)
from software_engineering_team.shared.tool_agent_build_specialist import (
    MAX_RELEVANT_CODE_CHARS,
    BuildSpecialistToolAgentBase,
    run_frontend_build_and_parse,
)

__all__ = ["BuildSpecialistAdapterAgent", "MAX_RELEVANT_CODE_CHARS"]


class BuildSpecialistAdapterAgent(BuildSpecialistToolAgentBase):
    """Identifies all build issues in review and fixes them one at a time.

    ``review`` runs the frontend build via the shared ``build_runner`` path;
    ``problem_solve`` and the other lifecycle methods are inherited from
    :class:`BuildSpecialistToolAgentBase`.
    """

    problem_solving_prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
    build_runner = staticmethod(run_frontend_build_and_parse)
    build_review_noun = "build issue(s)"
    _parse_single_issue = staticmethod(parse_problem_solving_single_issue_template)
