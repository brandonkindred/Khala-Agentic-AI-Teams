"""Shared helper for assembling the project_overview dict.

Both the SE orchestrator and the planning adapter build the same
``project_overview`` structure from PRD content and client context.  This module
provides a single-source-of-truth implementation so the logic is defined once
and tested in isolation.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def build_project_overview(
    prd_content: Optional[str] = None,
    client_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Build a project overview dict from PRD content and client context.

    Design-by-Contract
    ------------------
    Preconditions:
        - *prd_content*, when provided, is a non-empty string containing the
          product requirements document body.
        - *client_context*, when provided, is a dict that may contain the keys
          ``problem_summary`` (str) and ``opportunity_statement`` (str).

    Postconditions:
        - Returns a dict with exactly two keys:
          ``features_and_functionality_doc`` (str) and ``goals`` (str).
        - ``features_and_functionality_doc`` is the concatenation of the PRD
          content and any problem_summary / opportunity_statement sections.
          Each contributed section is prefixed with a markdown header
          (``## Problem summary`` or ``## Opportunity``), and all parts are
          joined by double newlines.  Empty string when no inputs are supplied.
        - ``goals`` is the combination of problem_summary and
          opportunity_statement separated by a newline, stripped of leading/
          trailing whitespace.  Empty string when neither field is present.

    Invariants:
        - The function is pure — no side-effects, no I/O.
        - The returned dict shape is always the same regardless of input
          combination.

    Parameters
    ----------
    prd_content:
        Optional PRD document body.
    client_context:
        Optional dict with project context fields.  Recognised keys:
        ``problem_summary``, ``opportunity_statement``.

    Returns
    -------
    Dict[str, str]
        ``{"features_and_functionality_doc": str, "goals": str}``
    """
    features_parts: list[str] = []

    if prd_content:
        features_parts.append(prd_content)

    if client_context:
        if client_context.get("problem_summary"):
            features_parts.append(
                "## Problem summary\n" + (client_context["problem_summary"] or "")
            )
        if client_context.get("opportunity_statement"):
            features_parts.append(
                "## Opportunity\n" + (client_context["opportunity_statement"] or "")
            )

    features_doc = "\n\n".join(features_parts) if features_parts else ""

    goals = ""
    if client_context and (
        client_context.get("problem_summary") or client_context.get("opportunity_statement")
    ):
        goals = (
            (client_context.get("problem_summary") or "")
            + "\n"
            + (client_context.get("opportunity_statement") or "")
        )

    return {
        "features_and_functionality_doc": features_doc,
        "goals": goals.strip(),
    }
