"""Shared file-context prefix builder for review-gate prompts.

QA and Security both render the code under review as a language line
followed by a fenced code block, positioned ahead of their gate-specific
role instructions in the assembled user prompt. This module owns that
single rendering so both gates stay byte-identical by construction
(a precondition for provider-side prompt caching to produce a hit across
gates), rather than by two hand-maintained copies happening to agree.
"""

from __future__ import annotations


def build_file_context_prefix(language: str, code: str) -> list[str]:
    """Render the file context (language + code under review) as prompt lines.

    Preconditions:
        - ``language`` and ``code`` describe the file under review.

    Postconditions:
        - Returns non-empty prompt lines: the language line, then the code
          fence around ``code``. Never raises or transforms the code.
    """
    return [
        f"**Language:** {language}",
        "**Code to review:**",
        "```",
        code,
        "```",
    ]
