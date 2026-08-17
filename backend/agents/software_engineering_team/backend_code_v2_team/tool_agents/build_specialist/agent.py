"""Build Specialist tool agent for backend-code-v2 (thin per-stack profile).

The shared build runner and base live in
:mod:`software_engineering_team.shared.tool_agent_build_specialist`; this module
binds the backend build runner, the python/java conventions profile
(:class:`BackendReviewToolAgent`), and the backend single-issue prompt + parser.
"""

from __future__ import annotations

import logging

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.shared.tool_agent_build_specialist import (
    MAX_RELEVANT_CODE_CHARS,
    BuildSpecialistToolAgentBase,
    run_backend_build_and_parse,
)

from ...models import ToolAgentInput, ToolAgentOutput, ToolAgentPhaseInput, ToolAgentPhaseOutput
from ...output_templates import parse_problem_solving_single_issue_template
from ...prompts import PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
from ..base import BackendReviewToolAgent

logger = logging.getLogger(__name__)

__all__ = ["BuildSpecialistAdapterAgent", "MAX_RELEVANT_CODE_CHARS"]


class BuildSpecialistAdapterAgent(BackendReviewToolAgent, BuildSpecialistToolAgentBase):
    """Identifies all build/test issues in review and fixes them one at a time.

    ``review`` runs the backend build via the shared ``build_runner`` path;
    ``execute``/``deliver`` are bespoke stubs for the backend build flow;
    ``problem_solve`` (with python/java conventions) is inherited.
    """

    problem_solving_prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
    build_runner = staticmethod(run_backend_build_and_parse)
    build_review_noun = "build/test issue(s)"
    _parse_single_issue = staticmethod(parse_problem_solving_single_issue_template)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Build Specialist: microtask %s (execute stub)", inp.microtask.id)
        return ToolAgentOutput(
            summary="Build Specialist execute — no changes applied.",
            recommendations=["Integrate with build verifier or build-fix flow for full support."],
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            summary="Build Specialist deliver — ensure build passes before merge."
        )
