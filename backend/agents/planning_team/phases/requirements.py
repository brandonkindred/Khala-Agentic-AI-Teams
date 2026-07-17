"""
Requirements phase: RPO, RTO, SLAs, compliance, security, tech constraints.

Thin backward-compatible adapter over
``planning_team.agents.requirements.RequirementsAgent``: maps the ``context`` dict to the
agent's typed Input, injects the ``llm`` tool, and maps the typed Output back to the
``(context_update, artifacts)`` tuple. The real elicitation logic (map-reduce, prompt
split, dedup, defaults) lives in the agent package.
"""

from __future__ import annotations

from typing import Any, Dict


def run_requirements(
    context: Dict[str, Any],
    llm: Any,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run requirements phase: generate open questions (RPO/RTO, SLAs, compliance, etc.).

    The whole brief+spec is digested via section-aware map-reduce (see
    ``planning_team.spec_digest``); no input is truncated.

    Returns (context_update, artifacts). artifacts includes open_questions.
    """
    from planning_team.agents.requirements import RequirementsAgent, RequirementsInput

    out = RequirementsAgent().run(
        RequirementsInput(
            client_context=context.get("client_context"),
            initial_brief=context.get("initial_brief"),
            spec_content=context.get("spec_content"),
        ),
        llm,
    )
    context_update: Dict[str, Any] = {"open_questions": out.open_questions}
    artifacts: Dict[str, Any] = {"open_questions": [q.model_dump() for q in out.open_questions]}
    return context_update, artifacts
