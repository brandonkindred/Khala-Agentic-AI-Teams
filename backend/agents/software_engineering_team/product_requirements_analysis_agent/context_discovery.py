"""
Project context and constraints discovery for the Product Requirements Analysis Agent.

Two helpers live here:

- :func:`inject_context_answers_into_spec` — used by the SOP Phase 1 engine to
  prepend answered context/constraint Q&A into the live spec.
- :func:`run_context_constraints_discovery` — optional lighter-weight question
  generation (LLM + fixed fallback), exposed on
  :class:`ProductRequirementsAnalysisAgent` for alternate/manual flows. The
  default ``run_workflow`` path uses SOP Phase 1/2 instead of this helper, so
  the overlapping question topics in ``SOP_PHASE1_QUESTIONS`` are intentional:
  this remains a standalone entry point rather than a second workflow stage.
"""

from __future__ import annotations

import logging
from typing import List

from strands.models.model import Model

from .llm_io import call_llm_json
from .models import AnsweredQuestion, OpenQuestion
from .prompts import CONTEXT_CONSTRAINTS_QUESTIONS_PROMPT
from .question_data import context_discovery_fallback_questions
from .question_processing import parse_open_question
from .spec_writing import format_answered_questions

logger = logging.getLogger(__name__)


def run_context_constraints_discovery(model: Model, spec_content: str) -> List[OpenQuestion]:
    """Formulate context/constraint questions (project context, deployment, tenets, mandates).

    Uses LLM with CONTEXT_CONSTRAINTS_QUESTIONS_PROMPT; on empty or invalid response
    returns a fixed fallback list. Questions whose parsed ``source`` is
    ``'spec_review'`` are rewritten to ``'context_discovery'`` because this
    helper shares :func:`parse_open_question` with the spec-review path (whose
    default source is ``spec_review``).

    Preconditions: ``model`` is a Strands ``Model``.
    Postconditions: returns a non-empty question list — the LLM's parsed questions
        when at least one item parses, otherwise the fixed fallback. Every returned
        question has ``source != 'spec_review'`` (rewritten to ``context_discovery``
        when needed). Prompt formatting, LLM, and parse failures are caught and
        also yield the fallback, so this function never raises to callers. This
        helper does not apply organizational filtering; an all-invalid parse batch
        falls back rather than returning ``[]``.
    """
    try:
        spec_excerpt = spec_content or ""
        prompt = CONTEXT_CONSTRAINTS_QUESTIONS_PROMPT.format(spec_excerpt=spec_excerpt)
        parsed = call_llm_json(model, prompt)
        questions_data = parsed.get("open_questions") if isinstance(parsed, dict) else None
        if not questions_data or not isinstance(questions_data, list):
            return context_discovery_fallback_questions()
        out: List[OpenQuestion] = []
        for i, q_data in enumerate(questions_data):
            try:
                q = parse_open_question(q_data, i)
            except Exception as exc:
                logger.warning(
                    "Skipping malformed context-discovery question at index %d: %s",
                    i,
                    exc,
                )
                continue
            if q.source == "spec_review":
                q = q.model_copy(update={"source": "context_discovery"})
            out.append(q)
        return out if out else context_discovery_fallback_questions()
    except Exception as e:
        logger.warning(
            "Context constraints discovery LLM failed, using fallback: %s",
            str(e),
        )
        return context_discovery_fallback_questions()


def inject_context_answers_into_spec(
    current_spec: str,
    answered_questions: List[AnsweredQuestion],
) -> str:
    """Build '## Project context and constraints' section from Q&A and prepend to current_spec.

    Preconditions: ``answered_questions`` is a list of :class:`AnsweredQuestion`.
    Postconditions: when ``answered_questions`` is empty, returns ``current_spec``
        unchanged (no section is prepended); otherwise returns the spec with a
        prepended context section built from the answers.
    """
    if not answered_questions:
        return current_spec
    section = "## Project context and constraints\n\n"
    section += format_answered_questions(answered_questions)
    section += "\n\n---\n\n"
    return section + current_spec
