"""Spec Clarification Agent: drives chat to resolve open questions and assumptions."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from llm_service import LLMClient

from .models import SpecClarificationOutput
from .prompts import ASK_NEXT_PROMPT, PROCESS_ANSWER_PROMPT

logger = logging.getLogger(__name__)


class SpecClarificationAgent:
    """
    Drives a clarification chat: asks questions from open_questions,
    processes user answers, and accumulates resolved_questions.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def process_answer(
        self,
        question: str,
        user_message: str,
        resolved_questions: List[Dict[str, Any]],
        open_questions: List[str],
        assumptions: List[str],
    ) -> SpecClarificationOutput:
        """
        Process the user's answer to a question. Extract summary and category via LLM,
        append to resolved_questions, remove question from open_questions, and return next state.
        """
        prompt = PROCESS_ANSWER_PROMPT.format(
            question=question,
            user_message=user_message[:2000] + ("..." if len(user_message) > 2000 else ""),
        )
        data: Dict[str, Any] = self.llm.complete_json(prompt, temperature=0.1) or {}
        answer_summary = data.get("answer_summary") or user_message[:500]
        category = data.get("category") or "other"

        new_resolved = resolved_questions + [
            {"question": question, "answer": answer_summary, "category": category}
        ]
        remaining = [q for q in open_questions if q != question]
        if not remaining:
            return SpecClarificationOutput(
                assistant_message="Thanks. Clarification is complete. I've captured your answers for the planning team.",
                open_questions=[],
                assumptions=assumptions,
                resolved_questions=new_resolved,
                confidence_score=0.9,
                done_clarifying=True,
            )

        prompt_next = ASK_NEXT_PROMPT.format(
            questions="\n".join(f"- {q}" for q in remaining),
            assumptions="\n".join(f"- {a}" for a in assumptions) if assumptions else "None",
        )
        data_next = self.llm.complete_json(prompt_next, temperature=0.2) or {}
        return SpecClarificationOutput(
            assistant_message=data_next.get("assistant_message")
            or f"Next question: {remaining[0]}",
            open_questions=remaining,
            assumptions=assumptions,
            resolved_questions=new_resolved,
            confidence_score=0.2 + 0.15 * len(new_resolved),
            done_clarifying=data_next.get("done_clarifying", False),
        )

    def ask_next(
        self,
        open_questions: List[str],
        assumptions: List[str],
    ) -> SpecClarificationOutput:
        """Ask the next question when no user message is being processed (e.g. session create)."""
        if not open_questions:
            return SpecClarificationOutput(
                assistant_message="No open questions. The spec is ready for planning.",
                open_questions=[],
                assumptions=assumptions,
                resolved_questions=[],
                confidence_score=0.9,
                done_clarifying=True,
            )
        prompt = ASK_NEXT_PROMPT.format(
            questions="\n".join(f"- {q}" for q in open_questions),
            assumptions="\n".join(f"- {a}" for a in assumptions) if assumptions else "None",
        )
        data = self.llm.complete_json(prompt, temperature=0.2) or {}
        return SpecClarificationOutput(
            assistant_message=data.get("assistant_message")
            or f"First question: {open_questions[0]}",
            open_questions=open_questions,
            assumptions=assumptions,
            resolved_questions=[],
            confidence_score=0.2,
            done_clarifying=data.get("done_clarifying", False),
        )
