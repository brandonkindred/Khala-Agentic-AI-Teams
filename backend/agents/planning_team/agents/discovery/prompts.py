"""Prompts for the Discovery agent — code-level System/User split (§5).

``AGENT_ANATOMY.md`` §5 requires separating System (identity/constraints) from
User (the task payload). The Planning LLM runtime (``LLMClient.complete_text``)
supports only a single prompt string — it has no ``system_prompt`` parameter and
hardcodes ``system_prompt=None`` (see ``llm_service.interface``). §5 explicitly
allows this case: *"Avoid stuffing everything into a single undifferentiated
string unless the runtime only supports that — then document the equivalent
split in code."*

So the split lives here as two constants, ``SYSTEM_PROMPT`` and
``build_user_prompt(...)``, re-joined by :func:`build_prompt` into the exact
single string the runtime consumes. ``tests/test_prompts.py`` asserts that join
is byte-identical to the pre-split literal — behaviour is strictly preserved.
"""

from __future__ import annotations

# System turn: identity + extraction constraints. No trailing newline — the
# ``\n\n`` in build_prompt supplies the paragraph break before the User turn.
SYSTEM_PROMPT = (
    "You are an expert product owner doing discovery for a software engagement.\n"
    "\n"
    "Given the following client brief and/or spec, extract and structure:\n"
    "\n"
    "1. **Problem summary**: 2-4 sentences on the core problem.\n"
    "2. **Opportunity statement**: Why now, what success looks like.\n"
    "3. **Target users**: List of user segments or personas (short labels).\n"
    "4. **Success criteria**: 3-7 measurable or observable criteria.\n"
    "5. **Technology constraints**: Technologies the brief/spec explicitly requires or mandates\n"
    "   (languages, frameworks, databases, platforms, cloud/hosting). Include ONLY what is\n"
    "   explicitly stated — leave this empty if the input does not name a required technology.\n"
    "   Do NOT guess or infer a default stack here.\n"
    "\n"
    "Keep each section concise. If information is missing, infer reasonable defaults and note "
    'them under "Assumptions". (This does not apply to "Technology constraints", which must '
    "stay empty unless a technology is explicitly required.)"
)


def build_user_prompt(input_text: str) -> str:
    """Render the User turn: the brief/spec payload + the JSON output shape.

    Preconditions:
        - ``input_text`` is the section of brief/spec material to analyse.
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
        "Respond with JSON only (no markdown fences):\n"
        "{\n"
        '  "problem_summary": "...",\n'
        '  "opportunity_statement": "...",\n'
        '  "target_users": ["...", "..."],\n'
        '  "success_criteria": ["...", "..."],\n'
        '  "tech_constraints": ["..."],\n'
        '  "assumptions": ["..."]\n'
        "}\n"
    )


def build_prompt(input_text: str) -> str:
    """Re-join System + User into the single string ``complete_text`` consumes.

    Postconditions:
        - Byte-identical to the pre-split ``DISCOVERY_PROMPT.format(input_text=...)``
          (guarded by ``tests/test_prompts.py``).
    """
    return f"{SYSTEM_PROMPT}\n\n{build_user_prompt(input_text)}"
