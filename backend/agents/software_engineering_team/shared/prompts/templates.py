"""
Shared prompt builders for the code-v2 teams.

The planning / execution / single-issue problem-solving prompts were ~90% the
same between the backend and frontend teams; the stack-specific parts are the
tool-agent domain list, the language field/output, the coding standards, the
file-path rules, and whether a ``{language_conventions}`` slot is present.

These builders assemble the one canonical skeleton and substitute the
stack-specific pieces, **preserving the downstream ``.format()`` slots** exactly
(``{microtask_description}``, ``{requirements}``, ``{source}``, … and — only for
backend — ``{language_conventions}``). Following the ``build_review_prompt``
precedent in ``shared/security_service.py``, the planning builder substitutes
via ``str.replace`` so a value that itself contains braces (frontend's literal
``{detected_language}`` LANGUAGE block) survives untouched.

Each team's ``prompts.py`` calls these builders to define its module-level
``PLANNING_PROMPT`` / ``EXECUTION_PROMPT`` / ``PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT``
constants, so every existing importer keeps working with byte-identical output.
"""

from __future__ import annotations

from software_engineering_team.shared.coding_standards import (
    PRIORITY_FRAMEWORK as _PRIORITY_FRAMEWORK,
)

# ---------------------------------------------------------------------------
# Planning prompt
# ---------------------------------------------------------------------------

_PLANNING_TEMPLATE = """You are an expert Planning Agent for a {team_kind} development team.

**Context:** You receive a single **task** (assigned to the {team_kind} team from the Tech Lead's plan). Your job is to produce **subtasks** (microtasks) that together implement this task. Each subtask should be small enough that a single specialist tool-agent (or a general code-generation step) can handle it. The task's acceptance criteria and detailed description define what "done" means; your subtasks must collectively satisfy them.

**Available tool-agent domains you can assign microtasks to:**
{tool_agent_domains}

**Input you receive:**
- Task description and requirements
- Optional project spec, architecture, existing code context
- {language_input_line}

**Output format (template – use exactly these section headers):**

## MICROTASKS ##
---
id: mt-<short-kebab>
title: short title
description: what to do (2-4 sentences)
tool_agent: <domain from list above>
depends_on: mt-other-id|mt-another-id
---
## END MICROTASKS ##
## LANGUAGE ##
{language_output}
## END LANGUAGE ##
## SUMMARY ##
1-2 sentence overview of the plan
## END SUMMARY ##

{planning_rules}
"""


def build_planning_prompt(
    *,
    team_kind: str,
    tool_agent_domains: str,
    language_input_line: str,
    language_output: str,
    planning_rules: str,
) -> str:
    """Assemble a team's PLANNING_PROMPT from the shared skeleton.

    Preconditions:
        All arguments are strings. ``tool_agent_domains`` / ``planning_rules``
        are pre-formatted blocks without trailing newlines; ``language_output``
        may itself contain braces (frontend's literal ``{detected_language}``).
    Postconditions:
        Returns the skeleton with each ``{token}`` replaced by its value; no
        ``.format()`` slots remain (planning prompts are used as static text).
        Substitution is via ``str.replace`` so brace-bearing values survive.
    """
    result = _PLANNING_TEMPLATE
    for token, value in (
        ("{team_kind}", team_kind),
        ("{tool_agent_domains}", tool_agent_domains),
        ("{language_input_line}", language_input_line),
        ("{language_output}", language_output),
        ("{planning_rules}", planning_rules),
    ):
        result = result.replace(token, value)
    return result


# ---------------------------------------------------------------------------
# Execution prompt
# ---------------------------------------------------------------------------


def build_execution_prompt(
    *,
    engineer_intro: str,
    coding_standards: str,
    has_language_conventions: bool,
    file_noun: str,
    path_rules: str,
) -> str:
    """Assemble a team's EXECUTION_PROMPT from the shared skeleton.

    Preconditions:
        ``engineer_intro`` has no trailing newline; ``coding_standards`` starts
        and ends with a newline (as the team constants do); ``path_rules`` is a
        block without a trailing newline.
    Postconditions:
        Returns the prompt preserving the ``{microtask_description}``,
        ``{requirements}``, ``{existing_code}``, ``{architecture_context}`` slots
        (and ``{language_conventions}`` iff ``has_language_conventions``).
    """
    lang_block = "{language_conventions}\n\n" if has_language_conventions else ""
    return (
        engineer_intro
        + "\n\n"
        + _PRIORITY_FRAMEWORK
        + "\n"
        + coding_standards
        + "\n\n"
        + lang_block
        + "**Your task:**\n"
        + "Implement the microtask described below. Produce complete, runnable "
        + file_noun
        + ".\n\n"
        + "**Microtask:**\n{microtask_description}\n\n"
        + "**Requirements:**\n{requirements}\n\n"
        + "**Existing codebase (if any):**\n{existing_code}\n\n"
        + "**Architecture context (if any):**\n{architecture_context}\n\n"
        + "**File path rules:**\n"
        + path_rules
        + "\n\n"
        + "**Output format (template – use exactly these markers):**\n\n"
        + "For each file, write:\n"
        + "## FILE path/to/file.ext ##\n"
        + "<full file content>\n"
        + "## FILE path/to/next.ext ##\n"
        + "<full file content>\n"
        + "## SUMMARY ##\n"
        + "what you implemented\n"
        + "## END SUMMARY ##\n\n"
        + '- Use "## FILE <path> ##" at the start of each file; the next "## FILE " or "## SUMMARY ##" ends the previous file.\n'
        + '- Do not put the exact line "## FILE " or "## SUMMARY ##" inside file content (use a comment placeholder if needed).\n'
        + "- All imports must be valid; all referenced modules must be included.\n"
        + "- Do not use JSON. Use only the template above. No explanatory text before or after.\n"
    )


# ---------------------------------------------------------------------------
# Single-issue problem-solving prompt
# ---------------------------------------------------------------------------


def build_problem_solving_single_issue_prompt(
    *,
    coding_standards: str,
    has_language_conventions: bool,
    file_output_block: str,
) -> str:
    """Assemble a team's PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT from the shared skeleton.

    Preconditions:
        ``coding_standards`` starts and ends with a newline; ``file_output_block``
        is the ``## FILE`` output section (backend allows a second file), ending
        with a trailing newline.
    Postconditions:
        Returns the prompt preserving the ``{source}``, ``{severity}``,
        ``{description}``, ``{file_path}``, ``{recommendation}``, ``{current_code}``
        slots (and ``{language_conventions}`` iff ``has_language_conventions``).
    """
    lang_block = "{language_conventions}\n\n" if has_language_conventions else ""
    return (
        "You are an expert Problem-Solving Specialist. Fix exactly ONE issue.\n\n"
        + coding_standards
        + "\n\n"
        + lang_block
        + "**Single issue to fix:**\n"
        + "- Source: {source}\n"
        + "- Severity: {severity}\n"
        + "- Description: {description}\n"
        + "- File: {file_path}\n"
        + "- Recommendation: {recommendation}\n\n"
        + "**Relevant code (only the file(s) involved):**\n{current_code}\n\n"
        + "**Your steps:**\n"
        + "1. Identify the root cause of this issue.\n"
        + "2. Implement the fix by outputting the complete updated file(s).\n\n"
        + "**Output format (template – use exactly these markers):**\n\n"
        + "## ROOT_CAUSE ##\n"
        + "One or two sentences: why this issue occurs.\n"
        + "## END ROOT_CAUSE ##\n"
        + file_output_block
        + "## RESOLVED ##\n"
        + "true\n"
        + "## END RESOLVED ##\n"
        + "## SUMMARY ##\n"
        + "one sentence: what you changed\n"
        + "## END SUMMARY ##\n\n"
        + '- Output only the file(s) you change. Use "## FILE <path> ##" for each.\n'
        + "- Do not use JSON. Use only the template above. No explanatory text before or after.\n"
    )
