"""
Prompt templates for the Agent Provisioning Team.

All prompts that create or refine AI agents are prefixed with the canonical
specification from AGENT_ANATOMY.md and reference design_assets/ diagrams.
"""

from __future__ import annotations

from typing import Any

from .anatomy_assets import get_anatomy_prompt_preamble

# --- Bodies (no anatomy prefix; use format_* helpers below) ---

_ONBOARDING_SUMMARY_BODY = """Generate a concise onboarding summary for an AI agent.

Agent ID: {agent_id}
Access Tier: {access_tier}
Tools Provisioned: {tool_names}

The summary should:
1. Welcome the agent to the environment
2. List the available tools briefly
3. Mention the access tier and its implications
4. Point to key environment variables

Keep the summary under 3 sentences.
"""

_TOOL_GETTING_STARTED_BODY = """Generate getting-started documentation for a tool.

Tool: {tool_name}
Description: {description}
Connection Details: {connection_details}
Permissions: {permissions}

Provide clear, actionable steps for an AI agent to start using this tool.
Include any environment variables they should use.
Keep it concise and technical.
"""

_ENVIRONMENT_OVERVIEW_BODY = """Generate an environment overview document.

Container: {container_name}
Workspace: {workspace_path}
Tools Available:
{tools_list}

Environment Variables:
{env_vars}

Create a brief technical overview suitable for an AI agent to understand
their execution environment and available resources.
"""

_AI_AGENT_CREATE_BODY = """Design a new AI agent implementation plan that complies with the canonical anatomy
in the preamble (Input/Output, Tools, Memory tiers, Prompt roles, Security Guardrails, Subagents).

Produce structured output with these sections:
1. **Purpose** — one paragraph.
2. **Input / Output** — request and response shapes (fields and validation).
3. **Tools** — standalone vs browser-style; no undeclared side effects.
4. **Memory** — short-term, mid-term (if used), long-term strategy.
5. **Prompts** — what belongs in System vs User vs Assistant.
6. **Security guardrails** — validation, redaction, policy hooks (not prompt-only).
7. **Subagents** — list with INPUT/OUTPUT contracts if delegation applies; note recursion.

Constraints and context from the requester:
{requirements}
"""

_AI_AGENT_REFINE_BODY = """Refine an existing AI agent definition so it fully complies with the canonical anatomy
in the preamble. Preserve intent; close gaps (missing guardrails, unclear I/O, undocumented tools or memory).

Current definition / code excerpt:
{current_definition}

Refinement goals:
{refinement_goals}

Return an updated specification using the same section headings as in the create-agent prompt.
"""


def format_onboarding_summary_prompt(**kwargs: Any) -> str:
    return f"{get_anatomy_prompt_preamble()}\n\n---\n\n{_ONBOARDING_SUMMARY_BODY.format(**kwargs)}"


def format_tool_getting_started_prompt(**kwargs: Any) -> str:
    return (
        f"{get_anatomy_prompt_preamble()}\n\n---\n\n{_TOOL_GETTING_STARTED_BODY.format(**kwargs)}"
    )


def format_environment_overview_prompt(**kwargs: Any) -> str:
    return (
        f"{get_anatomy_prompt_preamble()}\n\n---\n\n{_ENVIRONMENT_OVERVIEW_BODY.format(**kwargs)}"
    )


def format_ai_agent_create_prompt(requirements: str) -> str:
    return f"{get_anatomy_prompt_preamble()}\n\n---\n\n{_AI_AGENT_CREATE_BODY.format(requirements=requirements)}"


def format_ai_agent_refine_prompt(current_definition: str, refinement_goals: str) -> str:
    body = _AI_AGENT_REFINE_BODY.format(
        current_definition=current_definition,
        refinement_goals=refinement_goals,
    )
    return f"{get_anatomy_prompt_preamble()}\n\n---\n\n{body}"


# Backward-compatible names: full prompt string with .format() placeholders (anatomy applied at access time).
def onboarding_summary_prompt() -> str:
    """Template factory: returns prompt with anatomy + body; use .format(agent_id=..., ...)."""
    return f"{get_anatomy_prompt_preamble()}\n\n---\n\n{_ONBOARDING_SUMMARY_BODY}"


def tool_getting_started_prompt() -> str:
    return f"{get_anatomy_prompt_preamble()}\n\n---\n\n{_TOOL_GETTING_STARTED_BODY}"


def environment_overview_prompt() -> str:
    return f"{get_anatomy_prompt_preamble()}\n\n---\n\n{_ENVIRONMENT_OVERVIEW_BODY}"


def ai_agent_create_prompt() -> str:
    return f"{get_anatomy_prompt_preamble()}\n\n---\n\n{_AI_AGENT_CREATE_BODY}"


def ai_agent_refine_prompt() -> str:
    return f"{get_anatomy_prompt_preamble()}\n\n---\n\n{_AI_AGENT_REFINE_BODY}"
