"""
Discovery phase: problem statement, opportunity, personas, success criteria.

Uses LLM to synthesize from brief/spec and optional evidence.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

from planning_v3_team.models import ClientContext
from planning_v3_team.spec_digest import map_reduce, parse_json_response

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


def _discovery_map(section: str, llm: Any, idx: int, total: int) -> Optional[Dict[str, Any]]:
    """Map step: extract discovery facts from one section of the brief+spec."""
    response = llm.complete_text(
        DISCOVERY_PROMPT.format(input_text=section),
        temperature=0.0,
        think=True,
        objective=f"extract discovery facts (section {idx + 1}/{total})",
    )
    return parse_json_response(response)


def _discovery_reduce(parts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce step: merge per-section discovery dicts.

    Scalars take the first non-empty value; lists are an order-preserving union with
    case-insensitive dedupe so a multi-section spec contributes all its personas /
    criteria / assumptions without duplicates.
    """
    if len(parts) == 1:
        return dict(parts[0])

    def first_str(key: str) -> str:
        return next((p.get(key) for p in parts if (p.get(key) or "").strip()), "")

    def union(key: str) -> list:
        seen: set = set()
        out: list = []
        for p in parts:
            for v in p.get(key) or []:
                k = v.strip().lower() if isinstance(v, str) else v
                if k not in seen:
                    seen.add(k)
                    out.append(v)
        return out

    return {
        "problem_summary": first_str("problem_summary"),
        "opportunity_statement": first_str("opportunity_statement"),
        "target_users": union("target_users"),
        "success_criteria": union("success_criteria"),
        "assumptions": union("assumptions"),
    }


def run_discovery(
    context: Dict[str, Any],
    llm: Any,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run discovery phase using LLM to extract problem, opportunity, personas, success criteria.

    The whole brief+spec is digested via section-aware map-reduce (see
    ``planning_v3_team.spec_digest``); no input is truncated.

    context should contain client_context, initial_brief, spec_content, and optionally evidence.
    Returns (context_update, artifacts).
    """
    client_context = context.get("client_context")
    if isinstance(client_context, dict):
        client_context = ClientContext(**client_context)
    brief = context.get("initial_brief") or ""
    spec = context.get("spec_content") or ""
    if brief and spec:
        material = f"Brief:\n{brief}\n\nSpec:\n{spec}"
    else:
        material = brief or spec or "No brief or spec provided."

    context_update: Dict[str, Any] = {}
    artifacts: Dict[str, Any] = {}

    fallback = {
        "problem_summary": material,
        "opportunity_statement": "",
        "target_users": [],
        "success_criteria": [],
        "assumptions": ["LLM extraction failed; using raw input."],
    }
    data = map_reduce(
        material,
        llm,
        content_description="client brief and specification",
        map_fn=_discovery_map,
        reduce_fn=_discovery_reduce,
        fallback=fallback,
    )

    prev = (
        client_context.model_dump()
        if hasattr(client_context, "model_dump")
        else (client_context or {})
    )
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
