"""
Ghost writer story elicitation agent.

Scans a ContentPlan for sections where a personal story would strengthen the post,
then conducts a multi-turn conversational interview with the author to surface
specific anecdotes, failures, and concrete moments. The gathered material is compiled
into first-person narrative snippets passed to the draft agent.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from shared.content_plan import ContentPlan

from llm_service.interface import LLMClient

from .models import StoryElicitationResult, StoryGap

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2  # seconds between job-store polls
MAX_ROUNDS = 5  # maximum interview turns per story gap

_FIND_GAPS_SYSTEM = """\
You are an expert ghost writer interviewer who specializes in finding places in a
blog post outline where a personal anecdote, failure story, or concrete lived
experience from the author would dramatically strengthen the piece.

You receive a content plan (outline + narrative flow) and identify the 1–3 sections
where adding a real story from the author's experience would:
  1. Make the teaching more credible ("I learned this the hard way")
  2. Create an emotional hook that connects to the reader
  3. Provide concrete evidence for a claim that otherwise reads as generic advice

For each gap you identify, write an opening interview question that:
  - Asks about a specific event or moment (not a general question)
  - Is conversational and non-intimidating
  - Opens with "Tell me about..." or "Walk me through the time when..." or similar

Return a JSON array of objects. Each object has:
  - "section_title": exact title of the section
  - "section_context": one sentence explaining what the section argues and why a story helps here
  - "seed_question": the opening interview question

Return [] if no personal story opportunities exist (e.g. purely technical reference post).
Return at most 3 gaps — prioritise the highest-impact ones.
"""

_EVALUATE_SYSTEM = """\
You are an expert ghost writer evaluating whether an author's response contains
enough raw material to write a compelling first-person anecdote for their blog post.

A response is "sufficient" if it contains at least:
  - A specific event or moment (not just a general statement)
  - Some indication of what happened or what was learned
  - Enough detail that you could write 2–4 sentences of vivid first-person narrative

A response is "insufficient" if it is:
  - Vague ("yeah I've done that kind of thing")
  - Only confirms the experience exists without details
  - Missing the outcome or what was learned

Given the conversation so far and the section context, respond in JSON:
{
  "sufficient": true/false,
  "follow_up": "If not sufficient: a single specific follow-up question to extract more detail. If sufficient: null.",
  "narrative": "If sufficient: a 2–5 sentence first-person narrative (as if Brandon wrote it) compiled from what they shared. If not sufficient: null."
}
"""


class GhostWriterElicitationAgent:
    """
    Identifies story gaps in a content plan and conducts conversational interviews
    to elicit personal anecdotes from the author.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm_client = llm_client

    def find_story_gaps(self, content_plan: ContentPlan) -> List[StoryGap]:
        """
        Analyse the content plan and return sections where a personal story would help.
        Returns at most 3 gaps.
        """
        outline_text = self._plan_to_text(content_plan)
        prompt = f"Content plan:\n\n{outline_text}\n\nIdentify story gaps."

        try:
            response = self.llm_client.chat(
                messages=[{"role": "user", "content": prompt}],
                system=_FIND_GAPS_SYSTEM,
            )
            raw = response.strip()
            # Extract JSON array
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start == -1 or end == 0:
                logger.warning("Ghost writer: no JSON array in gap-finding response")
                return []
            data = json.loads(raw[start:end])
            gaps = []
            for item in data[:3]:
                gaps.append(
                    StoryGap(
                        section_title=item.get("section_title", ""),
                        section_context=item.get("section_context", ""),
                        seed_question=item.get("seed_question", ""),
                    )
                )
            logger.info("Ghost writer: found %s story gap(s)", len(gaps))
            return gaps
        except Exception as e:
            logger.warning("Ghost writer find_story_gaps failed: %s", e)
            return []

    def conduct_interview(
        self,
        gap: StoryGap,
        job_id: str,
        gap_index: int,
        job_updater: Optional[Callable[..., None]] = None,
    ) -> StoryElicitationResult:
        """
        Conduct a multi-turn interview for a single story gap.

        Posts questions to the job store, waits for user responses, evaluates
        sufficiency, and compiles a first-person narrative when ready.

        The pipeline must have already posted the seed question and set
        waiting_for_story_input=True before calling this method.
        """
        # Import job store functions here to avoid circular imports at module load
        from shared.blog_job_store import (
            add_story_agent_message,
            get_blog_job,
            is_waiting_for_story_input,
        )

        conversation: List[Dict[str, str]] = [
            {"role": "agent", "content": gap.seed_question}
        ]

        for round_num in range(MAX_ROUNDS):
            # Wait for user response
            while is_waiting_for_story_input(job_id):
                job_data = get_blog_job(job_id)
                if job_data and job_data.get("status") in ("failed", "cancelled"):
                    return StoryElicitationResult(gap=gap, narrative=None, skipped=True, rounds_used=round_num)
                # Check if user skipped (gap index advanced)
                if job_data and job_data.get("current_story_gap_index", 0) > gap_index:
                    return StoryElicitationResult(gap=gap, narrative=None, skipped=True, rounds_used=round_num)
                time.sleep(POLL_INTERVAL)

            # Check if user skipped this gap
            job_data = get_blog_job(job_id)
            if job_data and job_data.get("current_story_gap_index", 0) > gap_index:
                return StoryElicitationResult(gap=gap, narrative=None, skipped=True, rounds_used=round_num + 1)

            # Get user's last message from chat history
            history = (job_data or {}).get("story_chat_history", [])
            user_messages = [m for m in history if m.get("role") == "user" and m.get("gap_index", gap_index) == gap_index or m.get("role") == "user"]
            last_user_msg = user_messages[-1]["content"] if user_messages else ""

            conversation.append({"role": "user", "content": last_user_msg})

            # Evaluate sufficiency
            evaluation = self._evaluate_response(gap, conversation)

            if evaluation.get("sufficient"):
                narrative = evaluation.get("narrative")
                logger.info("Ghost writer: sufficient story collected for '%s' after %s round(s)", gap.section_title, round_num + 1)
                return StoryElicitationResult(
                    gap=gap,
                    narrative=narrative,
                    skipped=False,
                    rounds_used=round_num + 1,
                )

            if round_num >= MAX_ROUNDS - 1:
                # Max rounds reached — compile whatever we have
                narrative = evaluation.get("narrative") or self._compile_from_history(gap, conversation)
                logger.info("Ghost writer: max rounds reached for '%s', compiling from history", gap.section_title)
                return StoryElicitationResult(
                    gap=gap,
                    narrative=narrative,
                    skipped=False,
                    rounds_used=round_num + 1,
                )

            # Ask follow-up question
            follow_up = evaluation.get("follow_up")
            if not follow_up:
                break

            conversation.append({"role": "agent", "content": follow_up})
            add_story_agent_message(job_id, follow_up, gap_index)
            # waiting_for_story_input is set to True inside add_story_agent_message

        return StoryElicitationResult(gap=gap, narrative=None, skipped=False, rounds_used=MAX_ROUNDS)

    def _evaluate_response(
        self,
        gap: StoryGap,
        conversation: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Use the LLM to evaluate whether the conversation has enough material."""
        context_block = (
            f"Section: {gap.section_title}\n"
            f"Context: {gap.section_context}\n\n"
            "Conversation so far:\n"
        )
        for msg in conversation:
            role = "Ghost writer" if msg["role"] == "agent" else "Author"
            context_block += f"{role}: {msg['content']}\n"

        try:
            response = self.llm_client.chat(
                messages=[{"role": "user", "content": context_block}],
                system=_EVALUATE_SYSTEM,
            )
            raw = response.strip()
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start == -1 or end == 0:
                return {"sufficient": False, "follow_up": None, "narrative": None}
            return json.loads(raw[start:end])
        except Exception as e:
            logger.warning("Ghost writer evaluate_response failed: %s", e)
            return {"sufficient": False, "follow_up": None, "narrative": None}

    def _compile_from_history(
        self,
        gap: StoryGap,
        conversation: List[Dict[str, str]],
    ) -> Optional[str]:
        """Compile a narrative from the conversation even if evaluation didn't produce one."""
        user_content = " ".join(
            m["content"] for m in conversation if m["role"] == "user" and m.get("content")
        )
        if not user_content.strip():
            return None
        prompt = (
            f"Section context: {gap.section_context}\n\n"
            f"Author's raw responses: {user_content}\n\n"
            "Write a 2–5 sentence first-person narrative (as if the author wrote it) "
            "that captures the key moment or experience they described. "
            "Be concrete and specific. Use 'I' voice. Do not invent details."
        )
        try:
            return self.llm_client.chat(
                messages=[{"role": "user", "content": prompt}],
                system="You are a skilled ghost writer who turns author notes into vivid first-person prose.",
            )
        except Exception as e:
            logger.warning("Ghost writer compile_from_history failed: %s", e)
            return None

    @staticmethod
    def _plan_to_text(plan: ContentPlan) -> str:
        lines = [f"Topic/thesis: {plan.overarching_topic}"]
        if plan.narrative_flow:
            lines.append(f"Narrative flow: {plan.narrative_flow}")
        for section in plan.sections:
            lines.append(f"\nSection: {section.title}")
            if section.coverage_description:
                lines.append(f"  Coverage: {section.coverage_description}")
        return "\n".join(lines)
