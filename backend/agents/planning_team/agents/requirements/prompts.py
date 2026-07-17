"""Prompts for the Requirements agent — code-level System/User split (§5).

Same rationale as ``planning_team.agents.discovery.prompts``: the
``LLMClient.complete_text`` runtime supports only a single prompt string (no
``system_prompt`` parameter), so ``AGENT_ANATOMY.md`` §5's System/User split is
documented here as ``SYSTEM_PROMPT`` + ``build_user_prompt(...)`` and re-joined
by :func:`build_prompt` into the exact string the runtime consumes.
``tests/test_prompts.py`` guards the join byte-for-byte.
"""

from __future__ import annotations

# System turn: identity + what clarification questions to elicit. No trailing
# newline — build_prompt's ``\n\n`` supplies the break before the User turn.
SYSTEM_PROMPT = (
    "You are an expert product owner capturing requirements for a software engagement.\n"
    "\n"
    "From the problem summary and opportunity below, generate 3-6 short clarification questions "
    "that a client PO would need to answer so that dev/UI/UX teams can align. Include:\n"
    "- RTO/RPO or disaster recovery (if relevant)\n"
    "- Deployment target (cloud/on-prem/hybrid)\n"
    "- Compliance or security constraints (if any)\n"
    "- Tech stack preferences (if any)\n"
    "\n"
    "SLA defaults (for your reference): General apps often use RPO ≤ 15 min, RTO 1-2 hours; "
    "stricter for critical systems."
)


def build_user_prompt(input_text: str) -> str:
    """Render the User turn: the problem+brief payload and the JSON output shape.

    Preconditions:
        - ``input_text`` is the assembled ``Problem: ...`` + brief/spec section material.
    Postconditions:
        - Returns the ``Input:``/``---`` payload block followed by the JSON-shape
          instruction, ending with a trailing newline (matching the legacy literal).
    """
    return (
        "Input:\n"
        "---\n"
        f"{input_text}\n"
        "---\n"
        "\n"
        "Respond with JSON only (no markdown):\n"
        "{\n"
        '  "questions": [\n'
        "    {\n"
        '      "id": "req_short_id",\n'
        '      "question_text": "...",\n'
        '      "context": "...",\n'
        '      "category": "business|infrastructure|security|compliance|tech",\n'
        '      "priority": "high|medium|low",\n'
        '      "options": [\n'
        '        { "id": "opt_1", "label": "...", "is_default": false }\n'
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n"
    )


def build_prompt(input_text: str) -> str:
    """Re-join System + User into the single string ``complete_text`` consumes.

    Postconditions:
        - Byte-identical to the pre-split ``REQUIREMENTS_PROMPT.format(input_text=...)``
          (guarded by ``tests/test_prompts.py``).
    """
    return f"{SYSTEM_PROMPT}\n\n{build_user_prompt(input_text)}"
