"""Regression guard for the JSON-output-instruction migration.

Every SE-team prompt that expects a JSON reply must carry the shared
``JSON_OUTPUT_INSTRUCTION`` (or use ``build_json_output_prompt``, whose
default trailer is that same constant) instead of a hand-rolled "respond
with valid JSON only" variant -- the shared instruction adds explicit
no-fence and escaping rules that reduce JSON parse failures. This file
pins that every migrated prompt still carries the instruction verbatim, so
a future edit can't silently reintroduce a weaker ad hoc trailer.
"""

from __future__ import annotations

from software_engineering_team.accessibility_agent.prompts import ACCESSIBILITY_PROMPT
from software_engineering_team.architect_agents.architecture_expert.prompts import (
    ARCHITECTURE_PROMPT,
)
from software_engineering_team.devops_team.cicd_pipeline_agent.prompts import (
    CICD_PIPELINE_PROMPT,
)
from software_engineering_team.devops_team.deployment_strategy_agent.prompts import (
    DEPLOYMENT_STRATEGY_PROMPT,
)
from software_engineering_team.devops_team.doc_runbook_agent.prompts import DOC_RUNBOOK_PROMPT
from software_engineering_team.devops_team.iac_agent.prompts import IAC_AGENT_PROMPT
from software_engineering_team.devops_team.task_clarifier.prompts import (
    DEVOPS_TASK_CLARIFIER_PROMPT,
)
from software_engineering_team.codegen_team.tool_agents.frontend.accessibility.agent import (
    ACCESSIBILITY_REVIEW_PROMPT,
)
from software_engineering_team.codegen_team.tool_agents.frontend.architecture.agent import (
    FRONTEND_ARCHITECT_PROMPT,
)
from software_engineering_team.codegen_team.tool_agents.frontend.branding_theme.agent import (
    DESIGN_SYSTEM_PLAN_PROMPT,
)
from software_engineering_team.codegen_team.tool_agents.frontend.performance.agent import (
    PERFORMANCE_REVIEW_PROMPT,
)
from software_engineering_team.codegen_team.tool_agents.frontend.ui_design.agent import (
    UI_DESIGNER_PLAN_PROMPT,
)
from software_engineering_team.codegen_team.tool_agents.frontend.ux_usability.agent import (
    UX_DESIGNER_PLAN_PROMPT,
    UX_ENGINEER_REVIEW_PROMPT,
)
from software_engineering_team.integration_team.prompts import INTEGRATION_PROMPT
from software_engineering_team.problem_solver_agent.prompts import PROBLEM_SOLVER_PROMPT
from software_engineering_team.shared.prompt_utils import JSON_OUTPUT_INSTRUCTION
from software_engineering_team.technical_writers.dbc_comments_agent.prompts import (
    DBC_COMMENTS_PROMPT,
)
from software_engineering_team.technical_writers.documentation_agent.prompts import (
    DOCUMENTATION_CONTRIBUTORS_PROMPT,
    DOCUMENTATION_README_PROMPT,
)

# Prompts where JSON_OUTPUT_INSTRUCTION is the trailing content -- direct
# string-concatenation migration (Pattern 1, end-of-string sites) and the
# devops_team prompts built via build_json_output_prompt with no trailer
# override, so the builder's default (JSON_OUTPUT_INSTRUCTION) applies
# (Pattern 2).
ENDS_WITH_INSTRUCTION = [
    DOCUMENTATION_README_PROMPT,
    DOCUMENTATION_CONTRIBUTORS_PROMPT,
    DBC_COMMENTS_PROMPT,
    ACCESSIBILITY_PROMPT,
    INTEGRATION_PROMPT,
    ARCHITECTURE_PROMPT,
    PROBLEM_SOLVER_PROMPT,
    IAC_AGENT_PROMPT,
    CICD_PIPELINE_PROMPT,
    DOC_RUNBOOK_PROMPT,
    DEPLOYMENT_STRATEGY_PROMPT,
    DEVOPS_TASK_CLARIFIER_PROMPT,
]

# Prompts where JSON_OUTPUT_INSTRUCTION is spliced in mid-string, followed by
# a "**Task:** {task_description}" block that gets `.format()`-ed later
# (Pattern 1, mid-string sites) -- can only assert containment, not a suffix.
CONTAINS_INSTRUCTION = [
    FRONTEND_ARCHITECT_PROMPT,
    UX_DESIGNER_PLAN_PROMPT,
    UX_ENGINEER_REVIEW_PROMPT,
    PERFORMANCE_REVIEW_PROMPT,
    UI_DESIGNER_PLAN_PROMPT,
    DESIGN_SYSTEM_PLAN_PROMPT,
    ACCESSIBILITY_REVIEW_PROMPT,
]


def test_end_of_string_prompts_end_with_shared_json_instruction() -> None:
    """Every migrated end-of-string prompt's final content is the shared,
    stronger JSON-output instruction -- not a hand-rolled one-liner."""
    for prompt in ENDS_WITH_INSTRUCTION:
        assert prompt.endswith(JSON_OUTPUT_INSTRUCTION)


def test_mid_string_prompts_contain_shared_json_instruction() -> None:
    """Every migrated mid-string (``.format()``-templated) prompt still
    carries the shared instruction verbatim, ahead of its task/spec slots."""
    for prompt in CONTAINS_INSTRUCTION:
        assert JSON_OUTPUT_INSTRUCTION in prompt


def test_no_prompt_retains_the_old_weak_json_only_phrasing() -> None:
    """None of the migrated prompts regress back to an ad hoc "valid JSON
    only" / "Return JSON only" variant -- the shared instruction must be the
    sole JSON-output directive in each of these prompts."""
    old_phrasings = (
        "valid JSON only",
        "Respond with JSON only",
        "Return JSON only",
    )
    for prompt in ENDS_WITH_INSTRUCTION + CONTAINS_INSTRUCTION:
        for phrasing in old_phrasings:
            assert phrasing not in prompt


def test_problem_solver_prompt_keeps_its_key_list_schema() -> None:
    """The problem-solver prompt's directive wording changed (no longer
    claims to itself be the JSON-only instruction), but the required-keys
    schema it lists must survive the migration unchanged."""
    for key in (
        "plan",
        "execution_steps",
        "review_checks",
        "testing_strategy",
        "fix_recommendation",
    ):
        assert f"- {key}" in PROBLEM_SOLVER_PROMPT
