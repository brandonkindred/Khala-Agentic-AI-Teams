"""
Requirements phase: RPO, RTO, SLAs, compliance, security, tech constraints.

Generates structured open questions with options (aligned with agency expectations).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from planning_v3_team.models import ClientContext, OpenQuestion, OpenQuestionOption

logger = logging.getLogger(__name__)

# Aligned with common agency/SLA expectations (see software_engineering_team.shared.sla_best_practices).
RPO_RTO_OPTIONS = [
    OpenQuestionOption(id="opt_none", label="None / standard backup", is_default=True),
    OpenQuestionOption(id="opt_moderate", label="Moderate (e.g. RTO 4h, RPO 1h)", is_default=False),
    OpenQuestionOption(id="opt_strict", label="Strict (e.g. RTO <1h, RPO <15min)", is_default=False),
]
DEPLOYMENT_OPTIONS = [
    OpenQuestionOption(id="opt_cloud", label="Cloud (AWS, GCP, Azure, etc.)", is_default=True),
    OpenQuestionOption(id="opt_onprem", label="On-premises", is_default=False),
    OpenQuestionOption(id="opt_hybrid", label="Hybrid (cloud + on-prem)", is_default=False),
]


def _default_requirements_questions() -> List[OpenQuestion]:
    """Default set of requirements questions when LLM is not used or fails."""
    return [
        OpenQuestion(
            id="req_rpo_rto",
            question_text="Any RTO/RPO or disaster-recovery mandates?",
            context="Recovery time and recovery point objectives.",
            category="business",
            priority="high",
            options=RPO_RTO_OPTIONS,
            source="planning_v3",
        ),
        OpenQuestion(
            id="req_deployment",
            question_text="Where will this be deployed?",
            context="Deployment model affects infrastructure and provider choices.",
            category="infrastructure",
            priority="high",
            options=DEPLOYMENT_OPTIONS,
            source="planning_v3",
        ),
    ]


REQUIREMENTS_PROMPT = """You are an expert product owner capturing requirements for a software engagement.

From the problem summary and opportunity below, generate 3-6 short clarification questions that a client PO would need to answer so that dev/UI/UX teams can align. Include:
- RTO/RPO or disaster recovery (if relevant)
- Deployment target (cloud/on-prem/hybrid)
- Compliance or security constraints (if any)
- Tech stack preferences (if any)

SLA defaults (for your reference): General apps often use RPO ≤ 15 min, RTO 1-2 hours; stricter for critical systems.

Input:
---
{input_text}
---

Respond with JSON only (no markdown):
{{
  "questions": [
    {{
      "id": "req_short_id",
      "question_text": "...",
      "context": "...",
      "category": "business|infrastructure|security|compliance|tech",
      "priority": "high|medium|low",
      "options": [
        {{ "id": "opt_1", "label": "...", "is_default": false }}
      ]
    }}
  ]
}}
"""


def run_requirements(
    context: Dict[str, Any],
    llm: Any,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run requirements phase: generate open questions (RPO/RTO, SLAs, compliance, etc.).

    Returns (context_update, artifacts). artifacts includes open_questions.
    """
    client_context = context.get("client_context")
    if isinstance(client_context, dict):
        client_context = ClientContext(**client_context)
    brief = context.get("initial_brief") or ""
    spec = context.get("spec_content") or ""
    problem = (client_context.problem_summary if client_context else "") or ""
    input_text = f"Brief: {brief[:2000]}\nSpec: {spec[:2000]}\nProblem: {problem}"

    open_questions: List[OpenQuestion] = []
    try:
        response = llm.complete_text(REQUIREMENTS_PROMPT.format(input_text=input_text), temperature=0.0)
        text = (response or "").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        data = json.loads(text)
        for q in data.get("questions", [])[:10]:
            opts = [OpenQuestionOption(id=o.get("id", ""), label=o.get("label", ""), is_default=o.get("is_default", False)) for o in q.get("options", [])]
            open_questions.append(
                OpenQuestion(
                    id=q.get("id", "q"),
                    question_text=q.get("question_text", ""),
                    context=q.get("context"),
                    category=q.get("category", "general"),
                    priority=q.get("priority", "medium"),
                    options=opts,
                    source="planning_v3",
                )
            )
    except (json.JSONDecodeError, TypeError, AttributeError) as e:
        logger.warning("Requirements LLM parse failed: %s", e)
        open_questions = _default_requirements_questions()

    context_update: Dict[str, Any] = {"open_questions": open_questions}
    artifacts: Dict[str, Any] = {"open_questions": [q.model_dump() for q in open_questions]}
    return context_update, artifacts
