"""Drift guard for architect_agents/agents/prompts.py string constants.

Pins each specialist's rendered system prompt to a distinctive substring
from its own persona heading, so a future edit that silently truncates or
corrupts a constant (e.g. during refactoring) fails loudly here.
"""

from agents.prompts import (
    API_DESIGN_PROMPT,
    APPLICATION_PROMPT,
    CLOUD_INFRA_PROMPT,
    DATA_PROMPT,
    DATA_STREAMING_PROMPT,
    DEVOPS_PROMPT,
    OBSERVABILITY_PROMPT,
    ORCHESTRATOR_PROMPT,
    SCRUTINEER_PROMPT,
    SECURITY_PROMPT,
)

EXPECTED_HEADINGS = {
    "ORCHESTRATOR_PROMPT": (ORCHESTRATOR_PROMPT, "Enterprise Architect Orchestrator"),
    "SECURITY_PROMPT": (SECURITY_PROMPT, "Security Architect"),
    "APPLICATION_PROMPT": (APPLICATION_PROMPT, "Application Architect"),
    "DATA_PROMPT": (DATA_PROMPT, "Data Architect"),
    "API_DESIGN_PROMPT": (API_DESIGN_PROMPT, "API Design Architect"),
    "CLOUD_INFRA_PROMPT": (CLOUD_INFRA_PROMPT, "Cloud Infrastructure Architect"),
    "DATA_STREAMING_PROMPT": (DATA_STREAMING_PROMPT, "Data Streaming Architect"),
    "DEVOPS_PROMPT": (DEVOPS_PROMPT, "DevOps Architect"),
    "OBSERVABILITY_PROMPT": (OBSERVABILITY_PROMPT, "Observability Architect"),
    "SCRUTINEER_PROMPT": (SCRUTINEER_PROMPT, "Architecture Scrutineer"),
}


def test_all_prompt_constants_are_non_empty_strings() -> None:
    for name, (prompt, _heading) in EXPECTED_HEADINGS.items():
        assert isinstance(prompt, str), f"{name} is not a str"
        assert prompt.strip(), f"{name} is empty"


def test_all_prompt_constants_contain_their_persona_heading() -> None:
    for name, (prompt, heading) in EXPECTED_HEADINGS.items():
        assert heading in prompt, f"{name} is missing expected heading {heading!r}"
