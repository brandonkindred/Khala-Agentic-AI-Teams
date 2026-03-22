"""
Discovery phase: problem statement, opportunity, personas, success criteria.

Uses LLM to synthesize from brief/spec and optional evidence.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from planning_v3_team.models import ClientContext

logger = logging.getLogger(__name__)

DISCOVERY_PROMPT = """You are an expert product owner doing discovery for a software engagement.

Given the following client brief and/or spec, extract and structure:

1. **Problem summary**: 2-4 sentences on the core problem.
2. **Opportunity statement**: Why now, what success looks like.
3. **Target users**: List of user segments or personas (short labels).
4. **Success criteria**: 3-7 measurable or observable criteria.

Keep each section concise. If information is missing, infer reasonable defaults and note them under "Assumptions".

Input:
---
{input_text}
---

Respond with JSON only (no markdown fences):
{{
  "problem_summary": "...",
  "opportunity_statement": "...",
  "target_users": ["...", "..."],
  "success_criteria": ["...", "..."],
  "assumptions": ["..."]
}}
"""


def run_discovery(
    context: Dict[str, Any],
    llm: Any,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run discovery phase using LLM to extract problem, opportunity, personas, success criteria.

    context should contain client_context, initial_brief, spec_content, and optionally evidence.
    Returns (context_update, artifacts).
    """
    client_context = context.get("client_context")
    if isinstance(client_context, dict):
        client_context = ClientContext(**client_context)
    brief = context.get("initial_brief") or ""
    spec = context.get("spec_content") or ""
    input_text = brief or spec or "No brief or spec provided."
    if brief and spec:
        input_text = f"Brief:\n{brief}\n\nSpec:\n{spec}"

    context_update: Dict[str, Any] = {}
    artifacts: Dict[str, Any] = {}

    try:
        response = llm.complete_text(
            DISCOVERY_PROMPT.format(input_text=input_text[:15000]),
            temperature=0.0,
        )
        text = (response or "").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, AttributeError) as e:
        logger.warning("Discovery LLM parse failed: %s", e)
        data = {
            "problem_summary": input_text[:500] if input_text else "See brief/spec.",
            "opportunity_statement": "",
            "target_users": [],
            "success_criteria": [],
            "assumptions": ["LLM extraction failed; using raw input."],
        }

    prev = client_context.model_dump() if hasattr(client_context, "model_dump") else (client_context or {})
    assumptions = list(prev.get("assumptions") or [])
    assumptions.extend(data.get("assumptions", []))

    merged = {
        **prev,
        "problem_summary": data.get("problem_summary"),
        "opportunity_statement": data.get("opportunity_statement"),
        "target_users": data.get("target_users", []),
        "success_criteria": data.get("success_criteria", []),
        "assumptions": assumptions,
    }
    updated_client = ClientContext(**merged)
    context_update["client_context"] = updated_client
    artifacts["discovery"] = data
    return context_update, artifacts
