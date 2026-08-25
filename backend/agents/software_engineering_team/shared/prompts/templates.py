"""
Shared prompt builders for the code-v2 teams, and generalized builders for
prompt-scaffolding patterns used elsewhere in the software engineering team.

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

``build_json_output_prompt`` and ``build_document_rewrite_prompt`` (below the
code-v2 builders) generalize this to the JSON-object-output and
full-document-rewrite shapes used by prompt modules outside code-v2 (PRD agent,
tech_lead, qa, security, devops) — see those teams' sibling migration issues.
"""

from __future__ import annotations

from software_engineering_team.shared.coding_standards import (
    PRIORITY_FRAMEWORK as _PRIORITY_FRAMEWORK,
)
from software_engineering_team.shared.coding_standards import (
    REVIEW_PRIORITY_FRAMEWORK as _REVIEW_PRIORITY_FRAMEWORK,
)
from software_engineering_team.shared.prompt_utils import JSON_OUTPUT_INSTRUCTION
from software_engineering_team.shared.prompts.requirement_citation import (
    REQUIREMENT_CITATION_GUARDRAIL,
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


# ---------------------------------------------------------------------------
# Code review prompt
# ---------------------------------------------------------------------------


def build_code_review_prompt(*, project_kind: str, extra_verify_clause: str = "") -> str:
    """Assemble a team's REVIEW_PROMPT from the shared skeleton.

    Preconditions:
        ``project_kind`` is a bare noun (e.g. "backend"); ``extra_verify_clause``
        (if non-empty) ends with ", " so it reads inline before "correctness...".
    Postconditions:
        Returns the prompt preserving the ``{requirements}``,
        ``{acceptance_criteria}``, ``{architecture_context}``, ``{spec_content}``,
        ``{code}`` slots. Includes the Spec Compliance citation guardrail and an
        optional ``requirement_citation:`` field (prompt-only; not parsed here).
    """
    return (
        f"You are an expert Code Review Agent for a {project_kind} project.\n\n"
        + _REVIEW_PRIORITY_FRAMEWORK
        + "\n"
        + f"After checking these priorities, also verify: {extra_verify_clause}correctness against "
        + "requirements and acceptance criteria, testing coverage, and build/lint readiness.\n\n"
        + REQUIREMENT_CITATION_GUARDRAIL
        + "\n\n"
        + "**Requirements:**\n{requirements}\n\n"
        + "**Acceptance criteria:**\n{acceptance_criteria}\n\n"
        + "**Architecture context:**\n{architecture_context}\n\n"
        + "**Project specification excerpt:**\n{spec_content}\n\n"
        + "**Code to review:**\n{code}\n\n"
        + "**Output format (template – use exactly these section headers):**\n\n"
        + "## PASSED ##\ntrue\n## END PASSED ##\n"
        + "## ISSUES ##\n---\nsource: code_review\nseverity: critical|high|medium|low|info\n"
        + "description: what is wrong\nfile_path: which file\nrecommendation: how to fix it\n"
        + "requirement_citation: optional verbatim quote from Requirements/Acceptance "
        + "Criteria/Specification/Architecture\n---\n"
        + "## END ISSUES ##\n"
        + "## SUMMARY ##\noverall assessment\n## END SUMMARY ##\n\n"
        + '- Use "---" to separate each issue block. Omit ## ISSUES ## / ## END ISSUES ## if there are no issues.\n'
        + "- Do not use JSON. Use only the template above. No explanatory text before or after.\n"
    )


# ---------------------------------------------------------------------------
# Problem-solving prompt (multi-issue)
# ---------------------------------------------------------------------------


def build_problem_solving_prompt(
    *,
    project_kind: str,
    coding_standards: str,
    files_line: str,
    has_language_conventions: bool = True,
) -> str:
    """Assemble a team's PROBLEM_SOLVING_PROMPT from the shared skeleton.

    Preconditions:
        ``coding_standards`` starts and ends with a newline; ``files_line`` is the
        one-line intro before the FILE blocks (backend calls out "same as execution").
    Postconditions:
        Returns the prompt preserving the ``{issues}``/``{current_code}`` slots
        (and ``{language_conventions}`` iff ``has_language_conventions``).
    """
    lang_block = "{language_conventions}\n\n" if has_language_conventions else ""
    return (
        f"You are an expert Problem-Solving Specialist for a {project_kind} project.\n\n"
        "Given the issues found during review, produce fixes. Each fix should be a complete\n"
        "updated file that resolves the issue.\n\n"
        + _PRIORITY_FRAMEWORK
        + "\n"
        + coding_standards
        + "\n\n"
        + lang_block
        + "**Issues to resolve:**\n{issues}\n\n"
        + "**Current code:**\n{current_code}\n\n"
        + "**Output format (template – use exactly these markers):**\n\n"
        + f"{files_line}\n"
        + "## FILE path/to/file.ext ##\n<full updated file content>\n## FILE path/to/next.ext ##\n...\n"
        + "## FIXES_APPLIED ##\n---\nissue: summary of the issue\nfix: what was changed\n---\n"
        + "## END FIXES_APPLIED ##\n"
        + "## RESOLVED ##\ntrue\n## END RESOLVED ##\n"
        + "## SUMMARY ##\noverview of all fixes\n## END SUMMARY ##\n\n"
        + '- Use "## FILE <path> ##" for each file; "---" to separate each fix block.\n'
        + "- Do not use JSON. Use only the template above. No explanatory text before or after.\n"
    )


# ---------------------------------------------------------------------------
# QA tool-agent review prompt
# ---------------------------------------------------------------------------


def build_qa_review_prompt(*, second_test_kind: str, flakiness_examples: str) -> str:
    """Assemble a team's QA_TOOL_AGENT_REVIEW_PROMPT from the shared skeleton.

    Preconditions:
        ``second_test_kind`` / ``flakiness_examples`` are short noun phrases with
        no surrounding punctuation (e.g. "integration tests", "non-determinism,
        poor isolation").
    Postconditions:
        Returns the prompt preserving the ``{task_description}``, ``{code}`` slots.
    """
    return (
        "You are an expert QA/Testing specialist. Review the code from a testing and quality perspective only.\n\n"
        "Focus on:\n"
        f"1. Missing or weak unit tests, {second_test_kind}, or test coverage.\n"
        "2. Edge cases and error paths not covered.\n"
        f"3. Flaky or brittle test patterns (e.g. {flakiness_examples}).\n"
        "4. Assertions that are too weak or missing.\n"
        "5. Test data or mocks that don't reflect real behaviour.\n\n"
        "**Task context:**\n{task_description}\n\n"
        "**Code to review:**\n{code}\n\n"
        "**Output format (template – use exactly these section headers):**\n\n"
        "## PASSED ##\ntrue\n## END PASSED ##\n"
        "## ISSUES ##\n---\nsource: qa\nseverity: critical|high|medium|low|info\n"
        "description: what is wrong from a QA/testing perspective\nfile_path: which file\n"
        "recommendation: how to fix it\n---\n## END ISSUES ##\n"
        "## SUMMARY ##\nbrief QA assessment\n## END SUMMARY ##\n\n"
        '- Use "---" to separate each issue block. Use source: qa for every issue. '
        "Omit ## ISSUES ## / ## END ISSUES ## if there are no issues.\n"
        "- Do not use JSON. Use only the template above. No explanatory text before or after.\n"
    )


# ---------------------------------------------------------------------------
# Batch fix prompt
# ---------------------------------------------------------------------------


def build_batch_fix_prompt(*, role_title: str, coding_standards: str) -> str:
    """Assemble a team's BATCH_FIX_PROMPT from the shared skeleton.

    Preconditions:
        ``role_title`` is e.g. "Senior Backend Software Engineer"; ``coding_standards``
        starts and ends with a newline.
    Postconditions:
        Returns the prompt preserving the ``{language_conventions}``, ``{issue_count}``,
        ``{phase_name}``, ``{formatted_issues}``, ``{current_code}`` slots.
    """
    return (
        f"You are an expert {role_title} responsible for fixing all issues identified by the review team.\n\n"
        + coding_standards
        + "\n\n{language_conventions}\n\n"
        + "**You have been given {issue_count} issues from the {phase_name} phase.**\n\n"
        + "Your task is to address ALL of these issues in a single pass. Review each issue carefully, "
        + "understand the root causes, and implement comprehensive fixes.\n\n"
        + "## Issues to Fix\n\n{formatted_issues}\n\n"
        + "## Current Code\n\n{current_code}\n\n"
        + "## Instructions\n\n"
        + "1. Analyze all issues to understand their root causes\n"
        + "2. Identify any issues that can be fixed together with a single code change\n"
        + "3. Plan your fixes strategically to avoid introducing new problems\n"
        + "4. Implement ALL fixes - do not leave any issue unaddressed\n"
        + "5. Ensure your changes maintain code quality and don't break existing functionality\n\n"
        + "You decide how to organize the work internally. The key requirement is that ALL issues must be addressed.\n\n"
        + "**Output format (template – use exactly these markers):**\n\n"
        + "For each file you modify or create:\n"
        + "## FILE path/to/file.ext ##\n<full file content>\n## FILE path/to/next.ext ##\n<full file content>\n"
        + "## ISSUES_ADDRESSED ##\n---\nissue_index: 1\ndescription: brief description of what was fixed\n"
        + "---\nissue_index: 2\ndescription: brief description of what was fixed\n---\n"
        + "## END ISSUES_ADDRESSED ##\n"
        + "## SUMMARY ##\nOverview of all fixes applied\n## END SUMMARY ##\n\n"
        + '- Use "## FILE <path> ##" at the start of each file; the next "## FILE " or '
        + '"## ISSUES_ADDRESSED ##" ends the previous file.\n'
        + "- List each issue you addressed with its index (1-based) and a brief description.\n"
        + "- Do not use JSON. Use only the template above. No explanatory text before or after.\n"
    )


# ---------------------------------------------------------------------------
# Documentation self-review prompt
# ---------------------------------------------------------------------------


def build_documentation_self_review_prompt(
    *, project_kind_suffix: str, completeness_clause: str, accuracy_target: str
) -> str:
    """Assemble a team's DOCUMENTATION_SELF_REVIEW_PROMPT from the shared skeleton.

    Preconditions:
        ``project_kind_suffix`` is "" or " frontend" (inserted before "documentation."
        in the title); ``completeness_clause`` is "" or a parenthetical like
        " (props, usage, examples)"; ``accuracy_target`` is e.g. "code" or
        "component/function".
    Postconditions:
        Returns the prompt preserving the ``{iteration}``, ``{max_iterations}``,
        ``{task_description}``, ``{documentation}``, ``{code}`` slots.
    """
    return (
        "You are an expert Documentation Quality Specialist performing a self-review pass on"
        f"{project_kind_suffix} documentation.\n\n"
        "**Iteration:** {iteration} of {max_iterations}\n\n"
        "**Task Context:** {task_description}\n\n"
        "**Current Documentation:**\n\n{documentation}\n\n"
        "**Current Code:**\n\n{code}\n\n"
        "**Review criteria:**\n"
        "1. Clarity: Is the documentation easy to understand?\n"
        f"2. Completeness: Does it cover all important aspects{completeness_clause}?\n"
        f"3. Accuracy: Does it correctly describe the {accuracy_target} behavior?\n"
        "4. Structure: Is it well-organized with appropriate sections?\n"
        "5. Grammar and style: Is it professionally written?\n\n"
        "**Your task:**\n"
        "1. Review the documentation against the criteria above\n"
        "2. Identify specific improvements needed\n"
        "3. Apply those improvements and output the refined documentation\n\n"
        "**Output format (template – use exactly these markers):**\n\n"
        "## QUALITY_SCORE ##\n0.0-1.0 (your assessment of current documentation quality)\n"
        "## END QUALITY_SCORE ##\n"
        "## IMPROVEMENTS ##\n- List of specific improvements you are making\n- Each on its own line\n"
        "## END IMPROVEMENTS ##\n"
        "## FILE path/to/doc.md ##\n<full refined documentation content>\n"
        "## FILE path/to/next.md ##\n<content if multiple files>\n"
        "## SUMMARY ##\nBrief summary of refinements made in this iteration\n## END SUMMARY ##\n\n"
        "- Only output documentation files that you actually improved.\n"
        "- Do not use JSON. Use only the template above. No explanatory text before or after.\n"
    )


# ---------------------------------------------------------------------------
# Context block formatting helper (shared by the generalized builders below)
# ---------------------------------------------------------------------------


def format_context_block(label: str, slot: str) -> str:
    """Render one fenced context block: ``**{label}:**\\n---\\n{slot}\\n---\\n\\n``.

    Preconditions:
        ``label`` is a short noun phrase with no trailing colon; ``slot`` is
        either a ``.format()``-style token (e.g. ``"{spec_content}"``) or
        literal text to embed verbatim.
    Postconditions:
        Returns the fenced block ending in a blank line, ready to concatenate
        with other blocks. ``slot`` is embedded untouched (no substitution).
    """
    return f"**{label}:**\n---\n{slot}\n---\n\n"


# ---------------------------------------------------------------------------
# JSON output prompt (generalized — PRD agent, tech_lead, qa, security, devops)
# ---------------------------------------------------------------------------


def build_json_output_prompt(
    *,
    role_sentence: str,
    rules: str = "",
    context_blocks: str = "",
    json_schema: str,
    trailer: str = JSON_OUTPUT_INSTRUCTION,
) -> str:
    """Assemble a JSON-object-output prompt from role/rules/context/schema pieces.

    Generalizes the ``role sentence -> rules -> context -> JSON schema -> JSON
    trailer`` shape used outside code-v2 (e.g. devops_team's terse "Output
    JSON: - field: type" prompts, security_agent, tech_lead_agent's inline
    schemas, and the PRD agent's worked-example JSON prompts). Unlike
    ``build_planning_prompt``'s ``str.replace`` skeleton, this builder uses
    plain concatenation of caller-supplied pieces (matching
    ``build_execution_prompt``'s approach) so that a ``json_schema`` value
    containing doubled braces (``{{``/``}}``) for a later ``.format()`` call
    passes through untouched.

    Preconditions:
        ``role_sentence`` has no trailing newline. ``rules`` and
        ``context_blocks`` are either ``""`` or pre-formatted blocks ending in
        a blank line (e.g. built via ``format_context_block``). ``json_schema``
        is the schema description/worked example (no leading/trailing blank
        line required).
    Postconditions:
        Returns ``role_sentence``, then ``rules``/``context_blocks`` (each
        omitted entirely when ``""``, so no stray blank blocks appear), then
        an "Output format (JSON only):" header, ``json_schema``, and
        ``trailer``. Any ``{slot}`` tokens in the inputs survive untouched.
    """
    return (
        role_sentence
        + "\n\n"
        + rules
        + context_blocks
        + "**Output format (JSON only):**\n"
        + json_schema
        + "\n\n"
        + trailer
    )


# ---------------------------------------------------------------------------
# Document rewrite prompt (generalized — PRD agent's full-document rewrites)
# ---------------------------------------------------------------------------


def build_document_rewrite_prompt(
    *,
    role_sentence: str,
    rules: str = "",
    context_blocks: str = "",
    output_instruction: str = (
        "Respond with the FULL updated document as plain text (markdown format). "
        "Do not wrap in code fences. No explanatory text before or after."
    ),
) -> str:
    """Assemble a full-document-rewrite prompt from role/rules/context pieces.

    Generalizes the PRD agent's ``SPEC_UPDATE_PROMPT``/``SPEC_CLARIFICATION_PROMPT``
    shape: a plain-text/markdown document rewrite, not a JSON or
    ``## MARKER ##`` output. Uses the same concatenation approach as
    ``build_json_output_prompt`` for the same brace-survival reason.

    Preconditions:
        ``role_sentence`` has no trailing newline. ``rules`` and
        ``context_blocks`` are either ``""`` or pre-formatted blocks ending in
        a blank line (e.g. built via ``format_context_block``).
    Postconditions:
        Returns ``role_sentence``, then ``rules``/``context_blocks`` (each
        omitted entirely when ``""``), then ``output_instruction``. Any
        ``{slot}`` tokens in the inputs survive untouched.
    """
    return role_sentence + "\n\n" + rules + context_blocks + output_instruction


# ---------------------------------------------------------------------------
# Byte-identical constants (zero divergence between backend/frontend)
# ---------------------------------------------------------------------------

DOCUMENTATION_PROBLEM_SOLVE_PROMPT = """You are an expert Documentation Specialist fixing a specific documentation issue.

{language_conventions}

**Issue to fix:**
- Source: {source}
- Severity: {severity}
- Description: {description}
- File: {file_path}
- Recommendation: {recommendation}

**Current code:**
{current_code}

**Your task:** Fix ONLY this documentation issue. Do not change any code logic — only add or improve documentation.

**Output format (template – use exactly these markers):**
## FILE path/to/file.ext ##
<full file content with documentation fix>
## SUMMARY ##
what documentation you fixed
## END SUMMARY ##

- Output the complete file content with the documentation fix.
- Do not use JSON. Use only the template above. No explanatory text before or after.
"""

DELIVER_COMMIT_MSG_TEMPLATE = "feat({scope}): {summary}"

# File-output template instructions appended to codegen_team's
# FileGeneratorToolAgent prompts (auth, api_openapi, state_management,
# data_engineering, ...) -- byte-identical across both stacks, so it lives
# here once rather than as two independently maintained copies.
FILES_OUTPUT_TEMPLATE_INSTRUCTIONS = """
**Output format (template – use exactly these markers):**
For each file:
## FILE path/to/file.ext ##
<full file content>
## FILE path/to/next.ext ##
<content>
## SUMMARY ##
what you produced
## END SUMMARY ##
- Use "## FILE <path> ##" at the start of each file; the next "## FILE " or "## SUMMARY ##" ends the previous file.
- Do not put the exact line "## FILE " or "## SUMMARY ##" inside file content.
- Do not use JSON. Use only the template above. No explanatory text before or after.
"""
