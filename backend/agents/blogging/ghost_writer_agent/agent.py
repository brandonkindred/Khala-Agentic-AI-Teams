"""
Ghost writer story elicitation agent.

Scans a ContentPlan for sections where a personal story would strengthen the post,
then conducts a multi-turn conversational interview with the author to surface
specific anecdotes, failures, and concrete moments. The gathered material is compiled
into first-person narrative snippets passed to the draft agent.

Architecture:
  - **Evaluator** (`_evaluate_sufficiency`): Assesses whether the conversation has enough
    material for a compelling story. Delegates JSON extraction/retry to
    ``run_json_gate()`` and falls back to a default-dict result on exhausted
    parse retries, unexpected errors, and transient LLM transport errors (so the
    story-phase wrapper cannot silently abandon an in-progress interview).
  - **Interviewer** (`_generate_follow_up`): Generates a single conversational follow-up
    question when the evaluator says "insufficient".
  - **Narrator** (`_compile_narrative`): Compiles a vivid first-person narrative from
    the raw conversation. Called when the evaluator says "sufficient" or at safety cap.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

from agents.blogging.shared.agent_base import _BlogAgentBase
from agents.blogging.shared.content_plan import ContentPlan
from agents.blogging.shared.json_retry import run_json_gate
from strands import Agent
from temporalio.exceptions import CancelledError

from llm_service import LLMRateLimitError, LLMTemporaryError, extract_json_from_response

from .models import StoryElicitationResult, StoryGap

logger = logging.getLogger(__name__)

EVENT_WAIT_TIMEOUT = 60  # seconds — safety net for event-based waiting
MAX_ROUNDS = 5  # hard cap for pre-draft interviews
MAX_ROUNDS_POST_DRAFT = 50  # hard safety cap for post-draft interviews

_JSON_RETRY_SUFFIX = "\n\nRespond with valid JSON only (no markdown, no code fences)."

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

# Exact whole-message refusals (normalized). Short tokens and formerly ambiguous
# stems belong here so they never match as substrings inside ordinary prose.
_NO_EXPERIENCE_EXACT = frozenset(
    {
        "skip",
        "none",
        "pass",
        "n/a",
        "i haven't",
        "i have no",
        "i can't think of",
        "nothing comes to mind",
        "i haven't done that",
    }
)

# Leading command tokens: "skip this one", "pass on this question", "n/a for now".
# Anchored at start so incidental mid-sentence uses ("please skip ahead") stay False.
# "none" stays exact-only — "none of my colleagues…" is ordinary prose, not a skip command.
_NO_EXPERIENCE_COMMAND_PREFIX_RE = re.compile(r"^(?:skip|pass|n/a)\b")

# Specific refusal phrases matched with word-boundary regex. Ambiguous stems
# like "i have no" / "i haven't" / "i can't think of" / "nothing comes to mind"
# are exact-only (see ``_NO_EXPERIENCE_EXACT``) so mid-sentence non-refusals do
# not prematurely end the interview; the LLM evaluator still has a no_experience
# path for softer refusals.
_NO_EXPERIENCE_PHRASES = frozenset(
    {
        "no experience",
        "no relevant experience",
        "not applicable",
        "i don't have a story",
        "no story",
        "i have no story",
        "i can't think of a story",
        "i can't think of any",
    }
)

_NO_EXPERIENCE_PHRASE_RE = tuple(
    re.compile(r"\b" + re.escape(phrase) + r"\b") for phrase in _NO_EXPERIENCE_PHRASES
)

# "I have no [direct/personal/…] experience(s)" — bounded so "I have no idea" stays False.
_NO_EXPERIENCE_QUALIFIED_RE = re.compile(
    r"\bi have no (?:direct |personal |relevant |real |prior )?experiences?\b"
)

# "I don't have [a/any/direct/…] story/experience" — specific enough to avoid
# treating missing incidental details ("I don't have the exact dates") as a
# refusal to answer the interview question.
_NO_EXPERIENCE_DONT_HAVE_RE = re.compile(
    r"\bi don't have (?:(?:a|any) )?"
    r"(?:(?:direct|personal|relevant|real|prior) )?(?:story|experiences?)\b"
)


def _is_no_experience(message: str) -> bool:
    """Return True if the user's message indicates they have no relevant experience.

    Preconditions:
        - ``message`` is a string (may be empty or whitespace-only).
    Postconditions:
        - Returns True for exact whole-message refusals (``_NO_EXPERIENCE_EXACT``),
          leading skip/pass/n/a command prefixes, qualified ``i have no … experience``
          and ``i don't have … story/experience`` patterns, or word-boundary
          matches against ``_NO_EXPERIENCE_PHRASES``; otherwise False.
        - Does not mutate ``message``.
    """
    text = message.strip().lower().rstrip(".!?")
    if not text:
        return False
    if text in _NO_EXPERIENCE_EXACT:
        return True
    if _NO_EXPERIENCE_COMMAND_PREFIX_RE.match(text):
        return True
    if _NO_EXPERIENCE_QUALIFIED_RE.search(
        text
    ) or _NO_EXPERIENCE_DONT_HAVE_RE.search(text):
        return True
    return any(pattern.search(text) for pattern in _NO_EXPERIENCE_PHRASE_RE)


def _notify_job_updater(
    job_updater: Optional[Callable[..., None]],
    **kwargs: Any,
) -> None:
    """Best-effort progress callback for interview milestones.

    Preconditions:
        - ``kwargs`` are acceptable to the pipeline ``job_updater`` (typically
          ``status_text`` / ``phase`` / ``progress``).
    Postconditions:
        - If ``job_updater`` is None, no side effects.
        - Otherwise ``job_updater(**kwargs)`` is invoked. ``CancelledError``
          propagates; other exceptions are logged and swallowed so progress
          failures cannot abort the interview.
    """
    if job_updater is None:
        return
    try:
        job_updater(**kwargs)
    except CancelledError:
        raise
    except Exception as e:
        logger.warning("Ghost writer job_updater failed (non-fatal): %s", e)


def _empty_list_fallback(_exc: Exception) -> list:
    """
    Shared JSON-retry fallback for gap finding.

    Preconditions:
        - ``_exc`` is the failure that exhausted retries or was unexpected.
    Postconditions:
        - Returns a new empty list (never ``None``).
    """
    return []


def _default_sufficiency_fallback(_exc: Exception) -> Dict[str, Any]:
    """
    Shared JSON-retry fallback for the sufficiency evaluator.

    Preconditions:
        - ``_exc`` is the failure that exhausted retries or was unexpected.
    Postconditions:
        - Returns a fresh default result dictionary with ``sufficient``/
          ``no_experience`` false and ``story_context``/``missing`` null
          (never ``None``). Plain ``dict``, not ``collections.defaultdict``.
    """
    return {
        "sufficient": False,
        "no_experience": False,
        "story_context": None,
        "missing": None,
    }


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------


class GhostWriterElicitationAgent(_BlogAgentBase):
    """
    Identifies story gaps in a content plan and conducts conversational interviews
    to elicit personal anecdotes from the author.

    Uses three specialised LLM roles:
      - Evaluator: assesses story sufficiency via ``run_json_gate``
      - Interviewer: generates conversational follow-up questions
      - Narrator: compiles vivid first-person narratives (``_compile_narrative``
        retries via its own plain-text loop, not ``run_json_gate`` —
        left unchanged when the evaluator was migrated)

    Preconditions:
        - llm_client is not None.
    """

    def __init__(self, llm_client: Any) -> None:
        """
        Preconditions:
            - llm_client is not None.
        """
        super().__init__(llm_client)

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
            agent = Agent(model=self._model, system_prompt=system)
            result = agent(prompt + "\n\nRespond with valid JSON only, no markdown fences.")
            data = extract_json_from_response(str(result).strip())
            if isinstance(data, dict):
                for key in ("questions", "seeds", "text"):
                    if key in data and isinstance(data[key], list):
                        data = data[key]
                        break
            if isinstance(data, list) and len(data) == len(opportunities) and all(isinstance(s, str) for s in data):
                cleaned = [s.strip().strip('"') for s in data]
                # Treat empty/whitespace-only items as failures — fall through to fallback
                if all(cleaned):
                    return cleaned
        except Exception as e:
            logger.warning("Ghost writer batch seed generation error: %s", e)

        # Fallback: generate generic friendly seeds without LLM
        return [
            f"I'd love to hear about a time you dealt with {opp.lower().rstrip('.')}. What comes to mind?"
            for opp in opportunities
        ]

    def _find_gaps_via_llm(self, content_plan: ContentPlan) -> List[StoryGap]:
        """
        Fallback: use LLM to identify story gaps when plan lacks story_opportunity fields.

        Preconditions:
            - ``content_plan`` is a populated ``ContentPlan``.
        Postconditions:
            - Returns at most 3 ``StoryGap`` objects.
            - Uses ``run_json_gate`` with ``max_attempts=2``.
            - On parse exhaustion, unexpected helper errors, or transient LLM
              transport errors, returns ``[]`` via ``_empty_list_fallback``.
            - Non-object array items are skipped; missing/blank ``seed_question``
              values get a generic seed. Field values are coerced to ``str``.
        """
        outline_text = self._plan_to_text(content_plan)
        prompt = f"Content plan:\n\n{outline_text}\n\nIdentify story gaps."

        # Transient LLM errors re-raise from the helper; map them to the same empty
        # fallback here so planning_stage's broad except cannot abandon elicitation
        # mid-flight without clearing interactive story state.
        try:
            data = run_json_gate(
                self._model,
                _FIND_GAPS_SYSTEM,
                prompt,
                max_attempts=2,
                strict_json_suffix=_JSON_RETRY_SUFFIX,
                fallback_builder=_empty_list_fallback,
                logger=logger,
            )
        except (LLMRateLimitError, LLMTemporaryError) as e:
            logger.warning("Ghost writer gap-finding transient LLM error, falling back: %s", e)
            return _empty_list_fallback(e)
        if not isinstance(data, list):
            logger.warning("Ghost writer: no JSON array in gap-finding response")
            return []
        gaps = []
        for item in data[:3]:
            if not isinstance(item, dict):
                logger.warning("Ghost writer: skipping non-object gap item: %r", item)
                continue
            ctx = str(item.get("section_context") or "")
            seed = str(item.get("seed_question") or "").strip()
            if not seed:
                seed = f"I'd love to hear about a time you dealt with {ctx.lower().rstrip('.')}. What comes to mind?"
            gaps.append(
                StoryGap(
                    section_title=str(item.get("section_title") or ""),
                    section_context=ctx,
                    seed_question=seed,
                )
            )
        logger.info("Ghost writer: found %s story gap(s) via LLM", len(gaps))
        return gaps

    # ------------------------------------------------------------------
    # Interview loop
    # ------------------------------------------------------------------

    def conduct_interview(  # pragma: no cover - event-bus + job-store driven interactive interview loop; exercised end-to-end by integration tests that simulate the UI replying via /story-response. Unit-testing it requires a full mock job store + event bus + multi-round LLM evaluator, which would be a fragile and low-signal harness.
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
        Posts follow-up questions to the job store, waits for each user response,
        evaluates sufficiency, and compiles a first-person narrative when ready.

        The interview ends when one of these happens:
        1. The evaluator says **sufficient** → narrator compiles narrative.
        2. The evaluator (or direct phrase detection) identifies **no_experience**.
        3. The user explicitly skips via the UI (gap index advanced).
        4. The job is cancelled/failed.
        5. Safety cap (*max_rounds*) is reached → narrator compiles from history.

        Preconditions:
            - ``gap.seed_question`` is the opening turn used for the local conversation
              history (this method does not re-read a seed from the job store).
        Postconditions:
            - Returns a ``StoryElicitationResult`` for this gap.
        Notes:
            - Callers should post the same seed to the job store and set
              ``waiting_for_story_input=True`` for UI consistency so the wait loop
              can receive replies; those steps are not enforced here. If the wait
              flag is already false, the wait loop is skipped and the last stored
              user message (if any) is used.
            - ``job_updater`` is an optional progress callback invoked at wait /
              evaluate / follow-up / compile milestones (failures are non-fatal).
            - ``max_rounds`` is the hard cap on interview rounds.
        """
        from agents.blogging.shared.blog_job_store import (
            add_story_agent_message,
            get_blog_job,
            is_waiting_for_story_input,
        )
        from agents.blogging.shared.job_event_bus import subscribe, unsubscribe

        conversation: List[Dict[str, str]] = [{"role": "agent", "content": gap.seed_question}]
        detected_context: Optional[str] = None
        section = gap.section_title

        sub = subscribe(job_id)
        try:
            for round_num in range(max_rounds):
                # ── Wait for the user to respond (event-driven) ─────────
                _notify_job_updater(
                    job_updater,
                    phase="story_elicitation",
                    status_text=f"Waiting for your response about: {section}",
                )
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
                _notify_job_updater(
                    job_updater,
                    phase="story_elicitation",
                    status_text=f"Evaluating your story for: {section}",
                )
                evaluation = self._evaluate_sufficiency(gap, conversation)

                # Track story context as it's detected
                if evaluation.get("story_context"):
                    detected_context = evaluation["story_context"]

                # Outcome 1: no_experience flagged by evaluator
                if evaluation.get("no_experience"):
                    logger.info(
                        "Ghost writer: evaluator detected no-experience for '%s'", gap.section_title
                    )
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
                    _notify_job_updater(
                        job_updater,
                        phase="story_elicitation",
                        status_text=f"Compiling your story for: {section}",
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
                    _notify_job_updater(
                        job_updater,
                        phase="story_elicitation",
                        status_text=f"Compiling your story for: {section}",
                    )
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
                _notify_job_updater(
                    job_updater,
                    phase="story_elicitation",
                    status_text=f"Asking a follow-up about: {section}",
                )
        finally:
            unsubscribe(job_id, sub)

        # Safety cap reached — compile whatever we have
        logger.info(
            "Ghost writer: round cap reached for '%s', compiling from history", gap.section_title
        )
        _notify_job_updater(
            job_updater,
            phase="story_elicitation",
            status_text=f"Compiling your story for: {section}",
        )
        narrative = self._compile_narrative(gap, conversation, detected_context)
        return StoryElicitationResult(
            gap=gap,
            narrative=narrative,
            skipped=False,
            rounds_used=max_rounds,
            story_context=detected_context,
        )

    # ------------------------------------------------------------------
    # Evaluator: assess whether conversation has enough material
    # ------------------------------------------------------------------

    def _evaluate_sufficiency(
        self,
        gap: StoryGap,
        conversation: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """
        Use the LLM evaluator to assess whether the conversation has enough material.

        Preconditions:
            - ``gap`` identifies the section under discussion.
            - ``conversation`` is a list of ``{"role", "content"}`` turns.
        Postconditions:
            - Returns a dict with keys ``sufficient``, ``no_experience``,
              ``story_context``, and ``missing``.
            - Uses ``run_json_gate``; on parse exhaustion, unexpected
              helper errors, transient LLM transport errors, or a non-dict
              payload, returns ``_default_sufficiency_fallback`` so the
              interview loop can continue safely.
        """
        system = (
            _EVALUATE_SUFFICIENCY_SYSTEM
            + f"\n\nSection: {gap.section_title}\nContext: {gap.section_context}"
        )

        conv_text = ""
        for msg in conversation:
            role = "Ghost writer" if msg["role"] == "agent" else "Author"
            conv_text += f"{role}: {msg['content']}\n"

        prompt = (
            conv_text
            + "\nEvaluate the conversation above. Respond with the JSON object only, no markdown fences."
        )

        # Same rationale as ``_find_gaps_via_llm``: keep soft fallbacks at this site so
        # transient errors do not escape into planning_stage's skip-on-Exception path.
        try:
            data = run_json_gate(
                self._model,
                system,
                prompt,
                max_attempts=2,
                strict_json_suffix=_JSON_RETRY_SUFFIX,
                fallback_builder=_default_sufficiency_fallback,
                logger=logger,
            )
        except (LLMRateLimitError, LLMTemporaryError) as e:
            logger.warning("Ghost writer evaluator transient LLM error, falling back: %s", e)
            return _default_sufficiency_fallback(e)
        if isinstance(data, dict):
            return data
        return _default_sufficiency_fallback(ValueError("non-dict sufficiency payload"))

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

        Args:
            gap: The story gap being explored.
            conversation: The interview conversation history as role/content dicts.
            evaluation: The sufficiency evaluation containing ``missing`` and ``story_context``.

        Returns:
            A follow-up question string, or ``None`` if the LLM call fails.
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
            agent = Agent(model=self._model, system_prompt=_INTERVIEWER_SYSTEM)
            result = agent(prompt)
            raw = str(result).strip()
            return raw or None
        except Exception as e:  # pragma: no cover - LLM-failure fallback in interviewer; covered by integration tests with a flaky model.
            logger.warning("Ghost writer interviewer failed: %s", e)
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
        """
        Compile the final first-person narrative from the full conversation.

        Preconditions:
            - ``gap`` provides section context for the narrator prompt.
            - ``conversation`` is a list of ``{"role", "content"}`` turns.
            - ``story_context`` is optional (``personal``, ``client``, ``employer``,
              or unrecognized / omitted for no tone hint).
        Postconditions:
            - Returns the compiled narrative string, or ``None`` when there is no
              non-empty user content, the narrator returns blank text, or the
              narrator fails after one retry.
            - On a narrator exception, waits ``time.sleep(2.0)`` once before the
              retry; after the second failure, returns ``None``.
        """
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

        agent = Agent(model=self._model, system_prompt=_NARRATOR_SYSTEM)
        for attempt in range(2):
            try:
                result = agent(prompt)
                return str(result).strip() or None
            except Exception as e:  # pragma: no cover - LLM-failure retry/exit branch in narrator; covered by integration tests with a flaky model.
                if attempt == 0:
                    logger.warning("Ghost writer narrator error, retrying: %s", e)
                    time.sleep(2.0)
                    continue
                logger.warning("Ghost writer narrator error after retry: %s", e)
                return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _plan_to_text(plan: ContentPlan) -> str:
        """
        Render a ContentPlan as a plain-text outline for LLM prompts.

        Preconditions:
            - ``plan`` is a populated ``ContentPlan``.
        Postconditions:
            - Returns a multi-line string with the overarching topic, the
              narrative flow (if present), and each section's title and
              coverage description (if present).
        """
        lines = [f"Topic/thesis: {plan.overarching_topic}"]
        if plan.narrative_flow:
            lines.append(f"Narrative flow: {plan.narrative_flow}")
        for section in plan.sections:
            lines.append(f"\nSection: {section.title}")
            if section.coverage_description:
                lines.append(f"  Coverage: {section.coverage_description}")
        return "\n".join(lines)
