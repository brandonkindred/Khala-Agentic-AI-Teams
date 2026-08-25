"""
State Management tool agent for frontend-code-v2: store setup, actions/reducers
or signals, selectors.

Real implementation (mirrors the backend stack's file-generator tool agents).
Uses template-based output (not JSON) so parsing works across model providers.
"""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.codegen_team.stacks.frontend.profile import (
    parse_files_and_summary_template,
)
from software_engineering_team.codegen_team.stacks.frontend.prompts import (
    FILES_OUTPUT_TEMPLATE_INSTRUCTIONS,
)
from software_engineering_team.shared.tool_agent_static import FileGeneratorToolAgent

STATE_MANAGEMENT_PROMPT = (
    """You are an expert Frontend State Management specialist.

Given a microtask about application/component state, produce the required
files for the detected state approach (NgRx store/actions/reducers/selectors
or Angular signals for Angular; Redux Toolkit slice, Zustand store, or
Context+useReducer for React; Pinia store for Vue) — whichever the existing
code context already uses, or a sensible default for the framework when none
is established yet.

**Microtask:** {description}
**Language/stack:** {language}
**Existing code context:** {existing_code}
"""
    + FILES_OUTPUT_TEMPLATE_INSTRUCTIONS
)


class StateManagementToolAgent(FileGeneratorToolAgent):
    """Produces state-management store/actions/selectors code."""

    log_label = "StateManagement"
    generation_prompt = STATE_MANAGEMENT_PROMPT
    _parse_files_and_summary = staticmethod(parse_files_and_summary_template)

    plan_recommendations = ["Include state shape and data flow in the microtask plan."]
    plan_summary = "State Management planning input provided."
    review_recommendations = [
        "Check for state mutated outside the store's own actions/reducers/updaters."
    ]
    review_summary = "State Management review completed."
    problem_solve_recommendations = ["Fix state shape and update paths as needed."]
    problem_solve_summary = "State Management problem-solving input provided."
    deliver_summary = "State Management deliver phase completed."
