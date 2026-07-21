"""Requirements agent: generate structured open questions (RPO/RTO, deployment, …).

The brief+spec is digested via section-aware map-reduce; questions are deduped by id
and by text, and a deterministic default set is substituted when the LLM yields none.
The LLM is the agent's only declared tool (§3), injected into :meth:`RequirementsAgent.run`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from planning_team.agents.requirements.models import RequirementsInput, RequirementsOutput
from planning_team.agents.requirements.prompts import build_prompt
from planning_team.models import OpenQuestion, OpenQuestionOption
from planning_team.phases._util import (
    DEFAULT_MATERIAL_FALLBACK,
    as_client_context,
    assemble_material,
)
from planning_team.spec_digest import map_reduce
from shared.llm_recovery import extract_json_object

logger = logging.getLogger(__name__)

# Aligned with common agency/SLA expectations (see software_engineering_team.shared.sla_best_practices).
RPO_RTO_OPTIONS = [
    OpenQuestionOption(id="opt_none", label="None / standard backup", is_default=True),
    OpenQuestionOption(id="opt_moderate", label="Moderate (e.g. RTO 4h, RPO 1h)", is_default=False),
    OpenQuestionOption(
        id="opt_strict", label="Strict (e.g. RTO <1h, RPO <15min)", is_default=False
    ),
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
            source="planning",
        ),
        OpenQuestion(
            id="req_deployment",
            question_text="Where will this be deployed?",
            context="Deployment model affects infrastructure and provider choices.",
            category="infrastructure",
            priority="high",
            options=DEPLOYMENT_OPTIONS,
            source="planning",
        ),
    ]


def _requirements_map_factory(
    problem: str,
) -> Callable[[str, Any, int, int], Optional[Dict[str, Any]]]:
    """Build the map step. ``problem`` (small) is prepended to every section so each
    section's question generation stays problem-aware.

    Contract: the returned callable expects an ``llm_service.LLMClient`` exposing
    ``complete_text(prompt, *, objective, temperature, think)`` (see
    ``llm_service.interface``); the ``think``/``objective`` kwargs are part of that
    interface and validated in tests via ``create_autospec(LLMClient)``.
    """

    def _map(section: str, llm: Any, idx: int, total: int) -> Optional[Dict[str, Any]]:
        input_text = f"Problem: {problem}\nBrief/Spec section:\n{section}"
        response = llm.complete_text(
            build_prompt(input_text),
            temperature=0.0,
            think=True,
            objective=f"derive requirements/open questions (section {idx + 1}/{total})",
        )
        return extract_json_object(response, required_keys=("questions",))

    return _map


def _requirements_reduce(parts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce step: merge per-section questions, deduped by id and by question text."""
    questions: List[Dict[str, Any]] = []
    seen_ids: set = set()
    seen_text: set = set()
    for p in parts:
        raw = p.get("questions")
        if not isinstance(raw, list):
            continue
        for q in raw:
            if not isinstance(q, dict):
                continue  # skip malformed entries (e.g. bare strings) instead of crashing
            qid = (q.get("id") or "").strip()
            qtext = (q.get("question_text") or "").strip().lower()
            if (qid and qid in seen_ids) or (qtext and qtext in seen_text):
                continue
            if not qid:
                # Synthesize a unique id so id-less questions don't all collapse to the
                # build loop's default "q" (which would yield duplicate OpenQuestion ids).
                qid = f"q_{len(questions) + 1}"
                q = {**q, "id": qid}  # copy, don't mutate the caller's dict
            seen_ids.add(qid)
            if qtext:
                seen_text.add(qtext)
            questions.append(q)
    return {"questions": questions[:10]}  # keep existing cap


class RequirementsAgent:
    """Stateless agent that elicits structured open questions from brief/spec material.

    Invariants:
        - Holds no mutable state; a single instance is safe to reuse across runs.
    """

    def run(self, input_data: RequirementsInput, llm: Any) -> RequirementsOutput:
        """Generate open questions (RPO/RTO, SLAs, compliance, deployment, tech).

        Preconditions:
            - ``llm`` is an ``llm_service.LLMClient`` (see ``_requirements_map_factory``).
        Postconditions:
            - Returns a ``RequirementsOutput`` whose ``open_questions`` is non-empty
              (falls back to the default question set when the LLM yields none).
        """
        context = {
            "client_context": input_data.client_context,
            "initial_brief": input_data.initial_brief,
            "spec_content": input_data.spec_content,
        }
        client_context = as_client_context(context.get("client_context"))
        problem = (client_context.problem_summary if client_context else "") or ""
        material = assemble_material(context, default=problem or DEFAULT_MATERIAL_FALLBACK)

        data = map_reduce(
            material,
            llm,
            content_description="client brief and specification",
            map_fn=_requirements_map_factory(problem),
            reduce_fn=_requirements_reduce,
            fallback={"questions": []},
        )

        open_questions: List[OpenQuestion] = []
        for q in data.get("questions", []):
            raw_opts = q.get("options")
            opts = [
                OpenQuestionOption(
                    id=o.get("id", ""),
                    label=o.get("label", ""),
                    is_default=o.get("is_default", False),
                )
                for o in (raw_opts if isinstance(raw_opts, list) else [])
                if isinstance(o, dict)
            ]
            open_questions.append(
                OpenQuestion(
                    id=q.get("id", "q"),
                    question_text=q.get("question_text", ""),
                    context=q.get("context"),
                    category=q.get("category", "general"),
                    priority=q.get("priority", "medium"),
                    options=opts,
                    source="planning",
                )
            )

        # Empty result (map-reduce fallback, or no questions produced) ⇒ default questions.
        if not open_questions:
            open_questions = _default_requirements_questions()

        return RequirementsOutput(open_questions=open_questions)
