"""
Project context and constraints discovery for the Product Requirements Analysis Agent.

A standalone pair of helpers, independent of the SOP Phase 1/Phase 2 engine: one
formulates open-ended context/constraint questions (project context, deployment,
tenets, mandates) with an LLM call and a fixed fallback list on failure; the other
folds answered questions back into the spec as a prepended "Project context and
constraints" section.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List

from .llm_io import call_llm_json
from .models import AnsweredQuestion, OpenQuestion
from .prompts import CONTEXT_CONSTRAINTS_QUESTIONS_PROMPT
from .question_data import _context_discovery_fallback_questions
from .question_processing import parse_open_question
from .spec_writing import format_answered_questions

logger = logging.getLogger(__name__)


def run_context_constraints_discovery(
    model: Any, spec_content: str, repo_path: Path
) -> List[OpenQuestion]:
    """Formulate context/constraint questions (project context, deployment, tenets, mandates).

    Uses LLM with CONTEXT_CONSTRAINTS_QUESTIONS_PROMPT; on empty or invalid response
    returns a fixed fallback list.

    Preconditions: ``model`` is a Strands ``Model``.
    Postconditions: returns a non-empty question list — the LLM's when valid, else the
        fixed fallback; never raises.
    """
    spec_excerpt = spec_content or ""
    prompt = CONTEXT_CONSTRAINTS_QUESTIONS_PROMPT.format(spec_excerpt=spec_excerpt)
    try:
        parsed = call_llm_json(model, prompt)
        questions_data = parsed.get("open_questions") if isinstance(parsed, dict) else None
        if not questions_data or not isinstance(questions_data, list):
            return _context_discovery_fallback_questions()
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
        return out if out else _context_discovery_fallback_questions()
    except Exception as e:
        logger.warning(
            "Context constraints discovery LLM failed, using fallback: %s",
            str(e),
        )
        return _context_discovery_fallback_questions()


def inject_context_answers_into_spec(
    current_spec: str,
    answered_questions: List[AnsweredQuestion],
    repo_path: Path,
) -> str:
    """Build '## Project context and constraints' section from Q&A and prepend to current_spec.

    Preconditions: ``answered_questions`` is a list of :class:`AnsweredQuestion`.
    Postconditions: returns ``current_spec`` unchanged for an empty answer list;
        otherwise the spec with a prepended context section.
    """
    if not answered_questions:
        return current_spec
    section = "## Project context and constraints\n\n"
    section += format_answered_questions(answered_questions)
    section += "\n\n---\n\n"
    return section + current_spec
