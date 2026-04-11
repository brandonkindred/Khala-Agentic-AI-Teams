"""
Ghost writer story elicitation agent.

Scans a ContentPlan for sections where a personal story would strengthen the post,
then conducts a multi-turn conversational interview with the author to surface
specific anecdotes, failures, and concrete moments. The gathered material is compiled
into first-person narrative snippets passed to the draft agent.

Architecture:
  - **Evaluator** (`_evaluate_sufficiency`): Assesses whether the conversation has enough
    material for a compelling story. Uses `chat_json_round` with native message history.
  - **Interviewer** (`_generate_follow_up`): Generates a single conversational follow-up
    question when the evaluator says "insufficient".
  - **Narrator** (`_compile_narrative`): Compiles a vivid first-person narrative from
    the raw conversation. Called when the evaluator says "sufficient" or at safety cap.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from shared.content_plan import ContentPlan

from llm_service.interface import (
    LLMClient,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    LLMTruncatedError,
)

from .models import StoryElicitationResult, StoryGap

logger = logging.getLogger(__name__)

EVENT_WAIT_TIMEOUT = 60  # seconds — safety net for event-based waiting
MAX_ROUNDS = 5  # hard cap for pre-draft interviews
MAX_ROUNDS_POST_DRAFT = 50  # hard safety cap for post-draft interviews

_JSON_RETRY_SUFFIX = "\n\nRespond with a single JSON object only (no markdown, no code fences)."

# ---------------------------------------------------------------------------
# Prompt: find story gaps in the content plan
# ---------------------------------------------------------------------------

_FIND_GAPS_SYSTEM = """\
You are a curious, enthusiastic ghost writer who loves hearing people's real stories.
You're reviewing a blog post outline to find the 1–3 spots where a personal story
from the author would make the piece come alive — the kind of moment that makes a
reader think "oh, they've actually been through this."

For each spot you find, write an opening question as if you're a friend who just
heard the topic come up and genuinely wants to hear the story. Your question should:
  - Sound natural and warm — like you're chatting over coffee, not conducting an interview
  - Ask about a specific kind of moment or experience (not a vague "tell me about your experience with X")
  - NOT mention the blog post, the content plan, section titles, or any internal structure
  - NOT ask for numbers, metrics, or frameworks — just ask for the story
  - Use phrases like "I'd love to hear about a time you..." or "Have you ever had one
    of those moments where..." or "What's the story behind..."

Return a JSON array of objects. Each object has:
  - "section_title": exact title of the section (for internal tracking only — the author won't see this)
  - "section_context": one sentence explaining what the section covers and why a story fits
  - "seed_question": your friendly opening question

Return [] if no personal story opportunities exist (e.g. purely technical reference post).
Return at most 3 — pick the highest-impact spots.
"""

# ---------------------------------------------------------------------------
# Prompt: evaluate whether the conversation has enough material
# ---------------------------------------------------------------------------

_EVALUATE_SUFFICIENCY_SYSTEM = """\
You are a ghost writer assessing whether an author has shared enough material for
you to write a compelling first-person story for their blog post.

Evaluate the conversation and determine ONE of three outcomes:

1. **sufficient** — you have what you need:
  - A specific moment or situation (not just "I've done that kind of thing")
  - What actually happened — the key actions, decisions, or turning points
  - Why it mattered — the outcome, lesson, or how things changed
  - Enough texture to make a reader feel like they were there

2. **no_experience** — the author clearly doesn't have a story for this:
  - They said "skip", "no experience", "pass", "I haven't done that", or similar
  - Respect this immediately

3. **insufficient** — the author is sharing but there's more to uncover:
  - They're being vague or general ("yeah I've dealt with that")
  - Missing the interesting parts — what went wrong, how they figured it out, what surprised them
  - You don't know the context yet (side project? client work? day job?)
  - No clear ending — what happened as a result?

Also identify the **story context** when you can tell:
  - "personal" — a side project, hobby, something done for fun or learning
  - "client" — work done for a client or customer
  - "employer" — work done as an employee at a company
  - null if unclear yet

When insufficient, describe what's missing in 1-2 sentences so the interviewer
knows what to ask about next.

Respond in JSON:
{
  "sufficient": true/false,
  "no_experience": true/false,
  "story_context": "personal" | "client" | "employer" | null,
  "missing": "If insufficient: what specific detail or element is lacking. Otherwise: null."
}
"""

# ---------------------------------------------------------------------------
# Prompt: generate a follow-up question as a curious friend
# ---------------------------------------------------------------------------

_INTERVIEWER_SYSTEM = """\
You are a curious friend chatting with someone who's telling you a story. The
evaluator has told you what's still missing from the story. Your job: ask ONE
natural follow-up question that will draw out the missing detail.

Rules:
  - Sound like a friend, not an interviewer — "Wait, what happened next?" not
    "Can you elaborate on the outcome?"
  - If the story context is known, adapt:
    - personal/fun: ask what sparked the idea, what was surprising, what they learned
    - client: ask about the client's reaction, constraints, the handoff
    - employer: ask about team dynamics, what they had to convince people of, stakeholders
  - If context is unknown, naturally ask: "Was this something you built on your own,
    or were you working with a team / for a client?"
  - Don't ask for numbers or metrics directly
  - One question only — keep it short and conversational
"""

# ---------------------------------------------------------------------------
# Prompt: compile the final narrative from conversation
# ---------------------------------------------------------------------------

_NARRATOR_SYSTEM = """\
You are a skilled ghost writer who turns casual conversation into compelling
first-person narratives. Write like a great storyteller, not a report generator.

Given the full conversation between the ghost writer and the author, compile a
2–5 sentence first-person narrative as if the author wrote it.

Guidelines:
  - Open with the specific moment or situation
  - Include what they actually did and why
  - Close with how it turned out — the outcome, lesson, or surprise
  - Use 'I' voice throughout
  - Include real details and numbers ONLY if the author provided them
  - Do NOT invent anything — every fact must come from the conversation
  - Make the reader feel like they were there
"""

# ---------------------------------------------------------------------------
# No-experience phrase detection
# ---------------------------------------------------------------------------

_NO_EXPERIENCE_PHRASES = frozenset(
    {
        "skip",
        "no experience",
        "no relevant experience",
        "n/a",
        "none",
        "pass",
        "i don't have",
        "i haven't",
        "i have no",
        "i can't think of",
        "nothing comes to mind",
        "not applicable",
        "i don't have a story",
        "no story",
    }
)


def _is_no_experience(message: str) -> bool:
    """Return True if the user's message indicates they have no relevant experience."""
    text = message.strip().lower().rstrip(".!?")
    if text in _NO_EXPERIENCE_PHRASES:
        return True
    return any(phrase in text for phrase in _NO_EXPERIENCE_PHRASES)


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------


class GhostWriterElicitationAgent:
    """
    Identifies story gaps in a content plan and conducts conversational interviews
    to elicit personal anecdotes from the author.

    Uses three specialised LLM roles:
      - Evaluator: assesses story sufficiency via ``chat_json_round``
      - Interviewer: generates conversational follow-up questions
      - Narrator: compiles vivid first-person narratives
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm_client = llm_client

    # ------------------------------------------------------------------
    # Gap finding
    # ------------------------------------------------------------------

    def find_story_gaps(self, content_plan: ContentPlan) -> List[StoryGap]:
        """
        Analyse the content plan and return sections where a personal story would help.
        Returns at most 3 gaps.

        First checks the planning agent's ``story_opportunity`` fields — if the planner
        already identified story opportunities, those are converted directly to gaps
        without an extra LLM call. Falls back to LLM gap-finding only when the plan
        has no story_opportunity fields populated.
        """
        plan_gaps = self._extract_gaps_from_plan(content_plan)
        if plan_gaps:
            logger.info(
                "Ghost writer: using %s story gap(s) from planning agent's story_opportunity fields",
                len(plan_gaps),
            )
            return plan_gaps[:3]

        return self._find_gaps_via_llm(content_plan)

    def _extract_gaps_from_plan(self, content_plan: ContentPlan) -> List[StoryGap]:
        """Convert planning agent's story_opportunity fields to StoryGap objects."""
        sections_with_opps = []
        for sec in sorted(content_plan.sections, key=lambda s: s.order):
            opp = getattr(sec, "story_opportunity", None)
            if opp:
                sections_with_opps.append((sec, opp))

        if not sections_with_opps:
            return []

        # Batch-generate friendly seed questions in one LLM call
        opportunities = [opp for _, opp in sections_with_opps]
        seeds = self._generate_friendly_seeds(opportunities)

        gaps = []
        for (sec, opp), seed in zip(sections_with_opps, seeds):
            gaps.append(
                StoryGap(
                    section_title=sec.title,
                    section_context=f"{sec.coverage_description} — Story needed: {opp}",
                    seed_question=seed,
                )
            )
        return gaps

    def _generate_friendly_seeds(self, opportunities: List[str]) -> List[str]:
        """Generate warm opening questions for multiple story opportunities in one LLM call."""
        if not opportunities:
            return []

        numbered = "\n".join(f"{i + 1}. {opp}" for i, opp in enumerate(opportunities))
        prompt = (
            f"Here are {len(opportunities)} story opportunities for a blog post:\n{numbered}\n\n"
            "For each one, write a warm, casual opening question — like you're a friend "
            "who genuinely wants to hear the story. Do NOT mention the blog post, section "
            "titles, or any internal structure. Return a JSON array of strings (one per opportunity)."
        )
        system = (
            "You are a friendly ghost writer. Write conversational questions. "
            "Return a JSON array of strings, nothing else."
        )

        try:
            data = self.llm_client.complete_json(prompt, system_prompt=system)
            # complete_json may return {"text": "[...]"} or a list directly
            if isinstance(data, dict):
                for key in ("questions", "seeds", "text"):
                    if key in data and isinstance(data[key], list):
                        data = data[key]
                        break
            if isinstance(data, list) and len(data) == len(opportunities):
                cleaned = [str(s).strip().strip('"') for s in data]
                # Treat empty/whitespace-only items as failures — fall through to fallback
                if all(cleaned):
                    return cleaned
        except (LLMJsonParseError, LLMTruncatedError) as e:
            logger.warning("Ghost writer batch seed generation parse error: %s", e)
        except (LLMTemporaryError, LLMRateLimitError) as e:
            logger.warning("Ghost writer batch seed generation transient error: %s", e)
        except LLMPermanentError as e:
            logger.warning("Ghost writer batch seed generation permanent error: %s", e)

        # Fallback: generate generic friendly seeds without LLM
        return [
            f"I'd love to hear about a time you dealt with {opp.lower().rstrip('.')}. What comes to mind?"
            for opp in opportunities
        ]

    def _find_gaps_via_llm(self, content_plan: ContentPlan) -> List[StoryGap]:
        """Fallback: use LLM to identify story gaps when plan lacks story_opportunity fields."""
        outline_text = self._plan_to_text(content_plan)
        prompt = f"Content plan:\n\n{outline_text}\n\nIdentify story gaps."

        for attempt in range(2):
            try:
                working_prompt = prompt if attempt == 0 else prompt + _JSON_RETRY_SUFFIX
                response = self.llm_client.complete(working_prompt, system_prompt=_FIND_GAPS_SYSTEM)
                raw = (response or "").strip()
                start = raw.find("[")
                end = raw.rfind("]") + 1
                if start == -1 or end == 0:
                    logger.warning("Ghost writer: no JSON array in gap-finding response")
                    return []
                data = json.loads(raw[start:end])
                gaps = []
                for item in data[:3]:
                    ctx = item.get("section_context", "")
                    seed = (item.get("seed_question") or "").strip()
                    if not seed:
                        seed = f"I'd love to hear about a time you dealt with {ctx.lower().rstrip('.')}. What comes to mind?"
                    gaps.append(
                        StoryGap(
                            section_title=item.get("section_title", ""),
                            section_context=ctx,
                            seed_question=seed,
                        )
                    )
                logger.info("Ghost writer: found %s story gap(s) via LLM", len(gaps))
                return gaps
            except LLMJsonParseError as e:
                if attempt == 0:
                    logger.warning("Ghost writer gap-finding JSON parse failed, retrying: %s", e)
                    continue
                logger.warning("Ghost writer gap-finding JSON parse failed after retry: %s", e)
                return []
            except (LLMTemporaryError, LLMRateLimitError) as e:
                logger.warning("Ghost writer gap-finding transient error: %s", e)
                if attempt == 0:
                    time.sleep(2.0)
                    continue
                return []
            except LLMPermanentError as e:
                logger.warning("Ghost writer gap-finding permanent error: %s", e)
                return []
        return []

    # ------------------------------------------------------------------
    # Interview loop
    # ------------------------------------------------------------------

    def conduct_interview(
        self,
        gap: StoryGap,
        job_id: str,
        gap_index: int,
        job_updater: Optional[Callable[..., None]] = None,
        max_rounds: int = MAX_ROUNDS,
    ) -> StoryElicitationResult:
        """
        Conduct a multi-turn interview for a single story gap.

        Uses the event bus to wait for user responses instead of polling.
        Posts questions to the job store, waits for each user response,
        evaluates sufficiency, and compiles a first-person narrative when ready.

        The interview ends when one of these happens:
        1. The evaluator says **sufficient** → narrator compiles narrative.
        2. The evaluator (or direct phrase detection) identifies **no_experience**.
        3. The user explicitly skips via the UI (gap index advanced).
        4. The job is cancelled/failed.
        5. Safety cap (*max_rounds*) is reached → narrator compiles from history.

        The pipeline must have already posted the seed question and set
        ``waiting_for_story_input=True`` before calling this method.
        """
        from shared.blog_job_store import (
            add_story_agent_message,
            get_blog_job,
            is_waiting_for_story_input,
        )
        from shared.job_event_bus import subscribe, unsubscribe

        conversation: List[Dict[str, str]] = [{"role": "agent", "content": gap.seed_question}]
        detected_context: Optional[str] = None

        sub = subscribe(job_id)
        try:
            for round_num in range(max_rounds):
                # ── Wait for the user to respond (event-driven) ─────────
                while is_waiting_for_story_input(job_id):
                    # Liveness signal for the event-bus reaper: this consumer
                    # may wait on human input for much longer than the idle
                    # TTL, but is not actually abandoned.
                    sub.touch()
                    job_data = get_blog_job(job_id)
                    if job_data and job_data.get("status") in ("failed", "cancelled"):
                        return StoryElicitationResult(
                            gap=gap, narrative=None, skipped=True, rounds_used=round_num
                        )
                    if job_data and job_data.get("current_story_gap_index", 0) > gap_index:
                        return StoryElicitationResult(
                            gap=gap, narrative=None, skipped=True, rounds_used=round_num
                        )
                    sub.notify.wait(timeout=EVENT_WAIT_TIMEOUT)
                    sub.notify.clear()

                # Check if user skipped via UI
                job_data = get_blog_job(job_id)
                if job_data and job_data.get("current_story_gap_index", 0) > gap_index:
                    return StoryElicitationResult(
                        gap=gap, narrative=None, skipped=True, rounds_used=round_num + 1
                    )

                # Get user's last message
                history = (job_data or {}).get("story_chat_history", [])
                gap_round = (job_data or {}).get("current_gap_round", 0)
                user_messages = [
                    m
                    for m in history
                    if m.get("role") == "user" and m.get("gap_round", gap_round) == gap_round
                ]
                last_user_msg = user_messages[-1]["content"] if user_messages else ""

                # ── Quick check: did user say "skip" / "no experience"? ──
                if _is_no_experience(last_user_msg):
                    logger.info(
                        "Ghost writer: user indicated no experience for '%s'", gap.section_title
                    )
                    return StoryElicitationResult(
                        gap=gap, narrative=None, skipped=True, rounds_used=round_num + 1
                    )

                conversation.append({"role": "user", "content": last_user_msg})

                # ── Evaluate with dedicated evaluator ────────────────────
                evaluation = self._evaluate_sufficiency(gap, conversation)

                # Track story context as it's detected
                if evaluation.get("story_context"):
                    detected_context = evaluation["story_context"]

                # Outcome 1: no_experience flagged by evaluator
                if evaluation.get("no_experience"):
                    logger.info("Ghost writer: evaluator detected no-experience for '%s'", gap.section_title)
                    return StoryElicitationResult(
                        gap=gap, narrative=None, skipped=True, rounds_used=round_num + 1
                    )

                # Outcome 2: sufficient material — compile via narrator
                if evaluation.get("sufficient"):
                    logger.info(
                        "Ghost writer: sufficient story collected for '%s' after %s round(s)",
                        gap.section_title,
                        round_num + 1,
                    )
                    narrative = self._compile_narrative(gap, conversation, detected_context)
                    return StoryElicitationResult(
                        gap=gap,
                        narrative=narrative,
                        skipped=False,
                        rounds_used=round_num + 1,
                        story_context=detected_context,
                    )

                # Outcome 3: insufficient — generate follow-up via interviewer
                follow_up = self._generate_follow_up(gap, conversation, evaluation)
                if not follow_up:
                    # Interviewer couldn't generate a question — compile from what we have
                    narrative = self._compile_narrative(gap, conversation, detected_context)
                    if narrative:
                        return StoryElicitationResult(
                            gap=gap,
                            narrative=narrative,
                            skipped=False,
                            rounds_used=round_num + 1,
                            story_context=detected_context,
                        )
                    break

                conversation.append({"role": "agent", "content": follow_up})
                add_story_agent_message(job_id, follow_up, gap_index)
        finally:
            unsubscribe(job_id, sub)

        # Safety cap reached — compile whatever we have
        logger.info(
            "Ghost writer: round cap reached for '%s', compiling from history", gap.section_title
        )
        narrative = self._compile_narrative(gap, conversation, detected_context)
        return StoryElicitationResult(
            gap=gap, narrative=narrative, skipped=False, rounds_used=max_rounds, story_context=detected_context
        )

    # ------------------------------------------------------------------
    # Evaluator: assess whether conversation has enough material
    # ------------------------------------------------------------------

    def _evaluate_sufficiency(
        self,
        gap: StoryGap,
        conversation: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Use the LLM evaluator to assess whether the conversation has enough material.

        Uses ``chat_json_round`` with native message history for proper role attribution,
        and ``think=True`` for chain-of-thought reasoning.
        """
        messages: list[Dict[str, Any]] = [
            {"role": "system", "content": _EVALUATE_SUFFICIENCY_SYSTEM},
            {
                "role": "system",
                "content": f"Section: {gap.section_title}\nContext: {gap.section_context}",
            },
        ]
        for msg in conversation:
            role = "assistant" if msg["role"] == "agent" else "user"
            messages.append({"role": role, "content": msg["content"]})
        messages.append({
            "role": "user",
            "content": "Evaluate the conversation above. Respond with the JSON object only.",
        })

        default = {"sufficient": False, "no_experience": False, "story_context": None, "missing": None}

        for attempt in range(2):
            try:
                result = self.llm_client.chat_json_round(messages, temperature=0.2, think=True)
                if isinstance(result, dict):
                    return result
                return default
            except LLMJsonParseError as e:
                if attempt == 0:
                    logger.warning("Ghost writer evaluator JSON parse failed, retrying: %s", e)
                    messages[-1]["content"] += _JSON_RETRY_SUFFIX
                    continue
                logger.warning("Ghost writer evaluator JSON parse failed after retry: %s", e)
                return default
            except LLMTruncatedError as e:
                # Try to parse partial content
                if e.partial_content:
                    try:
                        start = e.partial_content.find("{")
                        end = e.partial_content.rfind("}") + 1
                        if start != -1 and end > start:
                            return json.loads(e.partial_content[start:end])
                    except (json.JSONDecodeError, ValueError):
                        pass
                logger.warning("Ghost writer evaluator truncated: %s", e)
                return default
            except (LLMTemporaryError, LLMRateLimitError) as e:
                if attempt == 0:
                    logger.warning("Ghost writer evaluator transient error, retrying: %s", e)
                    time.sleep(2.0)
                    continue
                logger.warning("Ghost writer evaluator transient error after retry: %s", e)
                return default
            except LLMPermanentError as e:
                logger.warning("Ghost writer evaluator permanent error: %s", e)
                return default
        return default

    # ------------------------------------------------------------------
    # Interviewer: generate a conversational follow-up question
    # ------------------------------------------------------------------

    def _generate_follow_up(
        self,
        gap: StoryGap,
        conversation: List[Dict[str, str]],
        evaluation: Dict[str, Any],
    ) -> Optional[str]:
        """Generate a single conversational follow-up question.

        Uses the evaluator's ``missing`` and ``story_context`` fields to know what to ask.
        """
        missing = evaluation.get("missing") or "more detail about what happened"
        story_context = evaluation.get("story_context")

        # Build a short context for the interviewer
        recent_exchange = ""
        for msg in conversation[-4:]:  # last 2 exchanges
            role = "Ghost writer" if msg["role"] == "agent" else "Author"
            recent_exchange += f"{role}: {msg['content']}\n"

        context_hint = ""
        if story_context:
            context_hint = f"\nThe story context is: {story_context}."

        prompt = (
            f"The evaluator says the story is missing: {missing}{context_hint}\n\n"
            f"Recent conversation:\n{recent_exchange}\n"
            "Write ONE follow-up question."
        )

        try:
            result = self.llm_client.complete(prompt, system_prompt=_INTERVIEWER_SYSTEM)
            return (result or "").strip() or None
        except (LLMTemporaryError, LLMRateLimitError, LLMJsonParseError, LLMTruncatedError) as e:
            logger.warning("Ghost writer interviewer failed: %s", e)
            return None
        except LLMPermanentError as e:
            logger.warning("Ghost writer interviewer permanent error: %s", e)
            return None

    # ------------------------------------------------------------------
    # Narrator: compile a vivid first-person narrative
    # ------------------------------------------------------------------

    def _compile_narrative(
        self,
        gap: StoryGap,
        conversation: List[Dict[str, str]],
        story_context: Optional[str] = None,
    ) -> Optional[str]:
        """Compile the final narrative from the full conversation using a dedicated narrator."""
        user_content = " ".join(
            m["content"] for m in conversation if m["role"] == "user" and m.get("content")
        )
        if not user_content.strip():
            return None

        tone_hint = ""
        if story_context == "personal":
            tone_hint = (
                "This was a personal or side project — keep the tone lighter and enthusiastic. "
                "Highlight curiosity, experimentation, and what made it fun or interesting.\n\n"
            )
        elif story_context == "client":
            tone_hint = (
                "This was client work — convey professional credibility while keeping it human. "
                "Highlight the constraints, the client relationship, and the delivered result.\n\n"
            )
        elif story_context == "employer":
            tone_hint = (
                "This happened at the author's company — emphasize the team dynamic, "
                "organizational context, and the author's specific contribution.\n\n"
            )

        # Build conversation transcript for the narrator
        transcript = ""
        for msg in conversation:
            role = "Ghost writer" if msg["role"] == "agent" else "Author"
            transcript += f"{role}: {msg['content']}\n"

        prompt = (
            f"Section context: {gap.section_context}\n\n"
            f"Conversation transcript:\n{transcript}\n\n"
            f"{tone_hint}"
            "Compile the narrative now."
        )

        for attempt in range(2):
            try:
                return self.llm_client.complete(prompt, system_prompt=_NARRATOR_SYSTEM, think=True)
            except (LLMTemporaryError, LLMRateLimitError) as e:
                if attempt == 0:
                    logger.warning("Ghost writer narrator transient error, retrying: %s", e)
                    time.sleep(2.0)
                    continue
                logger.warning("Ghost writer narrator transient error after retry: %s", e)
                return None
            except (LLMJsonParseError, LLMTruncatedError) as e:
                logger.warning("Ghost writer narrator parse/truncation error: %s", e)
                return None
            except LLMPermanentError as e:
                logger.warning("Ghost writer narrator permanent error: %s", e)
                return None
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
