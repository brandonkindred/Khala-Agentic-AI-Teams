"""
Constraint-domain resolution analysis for the Product Requirements Analysis Agent.

A "constraint domain" (deployment, frontend, backend, database, auth) is resolved
progressively across up to four layers — from broad category down to specific
service. This module scans the spec text plus answered questions for keyword
indicators (defined in :mod:`constraint_domains`) and reports, per domain, the
deepest layer that has been resolved, then turns that into LLM hint text so SOP
Phase 1 asks only the *next* unresolved layer's question per domain.

Extracted verbatim from ``agent.py`` to keep the workflow module focused on
orchestration. Pure functions with no LLM or I/O.
"""

from __future__ import annotations

import re
from typing import Dict, List

from .constraint_domains import CONSTRAINT_DOMAINS_CONFIG
from .models import AnsweredQuestion

__all__ = [
    "CONSTRAINT_DOMAINS_CONFIG",
    "analyze_constraint_status",
    "generate_constraint_hints",
]


def _word_boundary_match(indicator: str, text: str) -> bool:
    """Check if indicator appears as a whole word/phrase in text.

    Uses regex word boundaries to avoid false positives like 'gin' in 'login'.

    Args:
        indicator: The word or phrase to search for.
        text: The text to search within.

    Returns:
        True if ``indicator`` occurs in ``text`` on word boundaries.

    Preconditions: ``indicator`` and ``text`` are strings.
    Postconditions: returns ``True`` iff ``indicator`` occurs in ``text`` on
        word boundaries; never raises.
    """
    pattern = r"\b" + re.escape(indicator) + r"\b"
    return bool(re.search(pattern, text))


def analyze_constraint_status(
    spec_content: str,
    answered_questions: List[AnsweredQuestion],
) -> Dict[str, int]:
    """Analyze which constraint domains are resolved and to what layer.

    Scans the spec content and answered questions to determine the current
    resolution level for each constraint domain.

    Args:
        spec_content: The current specification content.
        answered_questions: List of questions that have been answered.

    Returns:
        Dict mapping domain name to resolved layer (0 = unresolved, 1-4 = layer resolved).

    Preconditions: ``spec_content`` is a string; ``answered_questions`` is a list
        of :class:`AnsweredQuestion`.
    Postconditions: the returned dict has exactly one key per domain in
        ``CONSTRAINT_DOMAINS_CONFIG``, each value clamped to ``[0, max_layer]``.
    """
    status: Dict[str, int] = {domain: 0 for domain in CONSTRAINT_DOMAINS_CONFIG}

    spec_lower = spec_content.lower()

    # Also include answered questions in the analysis
    answers_text = ""
    for aq in answered_questions:
        answers_text += f" {aq.question_text} {aq.selected_answer} "
    answers_lower = answers_text.lower()

    combined_text = spec_lower + " " + answers_lower

    for domain, config in CONSTRAINT_DOMAINS_CONFIG.items():
        max_resolved = 0
        indicators = config.get("indicators", {})

        # Check each layer's indicators using word boundary matching
        for layer in range(1, config["max_layer"] + 1):
            layer_indicators = indicators.get(layer, [])
            for indicator, resolves_to in layer_indicators:
                if _word_boundary_match(indicator, combined_text):
                    max_resolved = max(max_resolved, resolves_to)

        status[domain] = min(max_resolved, config["max_layer"])

    return status


def generate_constraint_hints(constraint_status: Dict[str, int]) -> str:
    """Generate hints for the LLM about which constraint layers need questions.

    Args:
        constraint_status: Dict mapping domain to resolved layer.

    Returns:
        Formatted string with hints about which domains need attention.

    Preconditions: ``constraint_status`` maps domain keys to resolved layers.
    Postconditions: returns an empty string when there are no domains, otherwise a
        Markdown hint block; never raises.
    """
    hints = []

    for domain, resolved_layer in constraint_status.items():
        config = CONSTRAINT_DOMAINS_CONFIG.get(domain, {})
        max_layer = config.get("max_layer", 4)
        domain_name = config.get("name", domain)

        if resolved_layer >= max_layer:
            hints.append(
                f"- {domain_name}: FULLY RESOLVED (Layer {max_layer}/{max_layer}) - No questions needed"
            )
        elif resolved_layer == 0:
            hints.append(
                f"- {domain_name}: UNRESOLVED - Ask Layer 1 question (start from the beginning)"
            )
        else:
            next_layer = resolved_layer + 1
            hints.append(
                f"- {domain_name}: Resolved to Layer {resolved_layer}/{max_layer} - Ask Layer {next_layer} question"
            )

    if not hints:
        return ""

    return (
        """## CONSTRAINT STATUS (from previous answers)

Based on analysis of the specification and previous answers, here is the current constraint resolution status:

"""
        + "\n".join(hints)
        + """

Focus your questions on domains that are NOT fully resolved. Ask ONLY the next layer question for each domain.
"""
    )
