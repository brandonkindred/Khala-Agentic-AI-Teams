"""
Shared utilities for agent prompt construction and LLM request logging.

Provides:
- build_problem_solving_header: Construct a problem-solving mode header from issue counts
- log_llm_prompt: Log metadata-only (agent, mode, task hint, prompt length) before LLM calls

Used by backend and frontend agents; other agents (QA, security, code review, tech lead)
can adopt these helpers for consistent problem-solving behavior and prompt observability.
"""

from __future__ import annotations

import logging
from typing import Dict


def build_problem_solving_header(
    issue_summaries: Dict[str, int],
    domain_hint: str,
    instructions: str | None = None,
    issue_descriptions: str | None = None,
) -> str:
    """
    Build a problem-solving mode header from issue counts.

    Args:
        issue_summaries: Dict mapping issue type to count, e.g.
            {"qa_issues": 2, "security_issues": 1, "code_review_issues": 3}
        domain_hint: Short label for the domain, e.g. "Backend" or "Frontend / Angular"
        instructions: Optional custom instruction block. If None, uses a generic block.
        issue_descriptions: Optional block of text describing each issue (e.g. one line
            per issue). When provided, it is inserted after the issue count line.

    Returns:
        A markdown-formatted header string to prepend to the prompt.
    """
    parts = [f"{label}: {count}" for label, count in issue_summaries.items() if count > 0]
    summary = ", ".join(parts) if parts else "issues"
    default_instructions = (
        "1. Identify the likely root cause using the issue details in this section.\n"
        "2. Propose minimal, targeted code edits. Do not change unrelated code.\n"
        "3. Keep passing tests and working features intact.\n"
        "4. Avoid broad rewrites or refactoring.\n"
        "5. Focus on resolving the provided issues before adding new features.\n"
        "6. For test failures: use the **Failing tests** and **Interpretation** sections to identify "
        "the exact tests and cause. If the failure shows **expected 200, got 401**, the request is "
        "unauthenticated—fix by making the test send the required auth header or token; do not "
        "disable auth in the app.\n"
        "7. Fix the code or tests indicated by the error (file and assertion). Do not change "
        "unrelated files or tests."
    )
    instr = instructions if instructions is not None else default_instructions
    description_block = ""
    if issue_descriptions and issue_descriptions.strip():
        description_block = f"\n{issue_descriptions.strip()}\n\n"
    return (
        f"**PROBLEM-SOLVING MODE ({domain_hint})**\n\n"
        "You are being asked to fix specific issues. The following issues were reported:\n"
        f"- {summary}\n"
        f"{description_block}"
        f"**Instructions:**\n{instr}\n\n"
        "---\n\n"
    )


def log_llm_prompt(
    log: logging.Logger,
    agent_label: str,
    mode: str,
    task_hint: str,
    prompt: str,
) -> None:
    """
    Log metadata for an LLM call. No prompt body is ever logged.

    Logs a single short line: agent, mode, task hint (truncated to 80 chars), and
    prompt length. Same format for both initial and problem_solving modes.

    Args:
        log: Logger instance
        agent_label: Agent name, e.g. "Backend" or "Frontend"
        mode: "initial" or "problem_solving"
        task_hint: Short task description (truncated to 80 chars in log)
        prompt: Full assembled prompt string (used only to compute length; never logged)
    """
    try:
        prompt_len = len(str(prompt)) if prompt is not None else 0
        hint = (task_hint or "")[:80]
        log.info(
            "LLM call: agent=%s mode=%s task=%s prompt_len=%d",
            agent_label,
            mode,
            hint,
            prompt_len,
        )
    except Exception as e:
        log.warning("Failed to log LLM prompt: %s", e)


# ---------------------------------------------------------------------------
# Shared JSON output instruction — append to any prompt that expects JSON.
# Replaces the per-prompt variants ("Respond with valid JSON only") with a
# consistent, more specific instruction that reduces JSON parse failures.
# ---------------------------------------------------------------------------
JSON_OUTPUT_INSTRUCTION = """
**CRITICAL — JSON output only:** Respond with exactly one JSON object and nothing else.
- Do NOT wrap in ```json``` code fences or markdown formatting
- Do NOT include any text, explanation, or commentary before or after the JSON
- Escape special characters in strings: newlines as \\n, tabs as \\t, literal double-quotes as \\"
- Ensure all strings, arrays, and objects are properly closed with matching delimiters"""
