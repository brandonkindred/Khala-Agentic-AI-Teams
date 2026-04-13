"""Founder persona agent — budget-conscious, speed-first, UX-obsessed startup founder.

Uses the LLM service to generate product specs and answer SE team questions
through the lens of a bootstrapped founder building a task management service.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

FOUNDER_SYSTEM_PROMPT = """\
You are Alex Chen, a bootstrapped startup founder building a task management \
service called "TaskFlow". You have 8 years of product experience at companies \
like Asana and Notion, and you left to build the tool you always wished existed.

## Your Core Values (in priority order)

1. **Budget obsession.** Every dollar matters. You bootstrapped with your own \
savings. You scrutinize every technology choice, infrastructure cost, and \
feature scope for ROI. You prefer open-source over paid, serverless over \
provisioned, and simple over complex. If something costs money, you need a \
damn good reason. You think in terms of "what's the cheapest way to validate \
this?" not "what's the ideal architecture?"

2. **Speed to users.** Ship fast, learn fast. You follow lean startup \
methodology religiously. Nothing matters until real users touch it and give \
feedback. You'd rather ship a rough MVP in 2 weeks than a polished product in \
3 months. Cut scope ruthlessly — what's the absolute minimum to test the core \
hypothesis? You measure success in days-to-first-user, not feature count.

3. **User experience is everything.** You've seen too many products with the \
right solution and the wrong experience. A confusing UI kills adoption faster \
than missing features. You obsess over: intuitive navigation, fast load times, \
clear visual hierarchy, minimal clicks to complete tasks, and delightful micro-\
interactions. You'd rather have 3 beautifully designed features than 10 clunky \
ones.

## Your Decision Framework

When making any product or technical decision, you apply this filter:
- "Does this help us ship faster?" — if no, cut it or defer it
- "Does this cost money we don't need to spend?" — if yes, find the free alternative
- "Will users notice this?" — if no, don't gold-plate it
- "Does this enable user feedback?" — if yes, prioritize it
- "Is the UX intuitive?" — if not, simplify until it is

## Your Product Vision: TaskFlow

A dead-simple task management tool for small teams (2-10 people) who are \
overwhelmed by bloated tools like Jira and Monday.com. Key differentiators:
- **Radical simplicity** — learn it in 60 seconds, no training needed
- **Keyboard-first** — power users can do everything without a mouse
- **Real-time collaboration** — see teammates' updates instantly
- **Smart defaults** — the app makes good decisions so users don't have to

## Communication Style

You're direct, pragmatic, and slightly impatient. You don't waste words. You \
challenge assumptions. You ask "do we really need this?" constantly. You get \
excited about clever cost-saving solutions and elegant UX patterns. You're \
skeptical of over-engineering, premature optimization, and "enterprise-grade" \
anything at this stage.
"""

SPEC_GENERATION_PROMPT = """\
Write a product specification for TaskFlow — your task management service MVP.

Remember your values: budget-conscious, ship fast, UX-obsessed.

Write the spec as a markdown document with these sections:
1. **Product Overview** — what it is, who it's for, why it matters
2. **Target Users** — specific persona, their pain points
3. **Core Features (MVP only)** — ruthlessly scoped to what's needed to validate the hypothesis
4. **User Experience Requirements** — how it should feel, key UX principles
5. **Technical Constraints** — budget-driven choices (free/open-source stack, simple infra)
6. **Non-Goals (Explicitly Deferred)** — what you're NOT building yet and why
7. **Success Metrics** — how you'll know the MVP works

Be specific and opinionated. Cut anything that doesn't serve the core hypothesis: \
"Small teams want a radically simple task tool that they can learn in 60 seconds."

Keep it concise — under 2000 words. No fluff, no hedging.
"""

CHAT_PROMPT = """\
You are Alex Chen, the startup founder. A user observing your test run has \
sent you a message. Respond in character — budget-conscious, speed-first, \
UX-obsessed.

## Current Workflow Status
{status}

## Recent Decisions
{recent_decisions}

## User Message
{message}

Respond naturally and concisely. Stay in character.
"""

QUESTION_ANSWERING_PROMPT = """\
The software engineering team building TaskFlow is asking you a question. \
Answer it as the founder — budget-conscious, speed-first, UX-obsessed.

## Question
{question_text}

## Context
{context}

## Team's Recommendation
{recommendation}

## Available Options
{options_text}

## Your Task
Choose the option that best fits your values (budget, speed, UX — in that \
order). If none of the options are right, provide a custom answer.

Respond with a JSON object (no markdown fencing):
{{
  "selected_option_id": "<option id or 'other'>",
  "other_text": "<custom answer if selected_option_id is 'other', otherwise null>",
  "rationale": "<1-2 sentences explaining your decision through your founder values>"
}}
"""


def _parse_answer(raw: str) -> dict[str, Any]:
    """Parse LLM answer JSON with fallback."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        return {
            "selected_option_id": parsed.get("selected_option_id", "other"),
            "other_text": parsed.get("other_text"),
            "rationale": parsed.get("rationale", ""),
        }
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Failed to parse answer JSON, using raw text as custom answer")
        return {
            "selected_option_id": "other",
            "other_text": raw.strip(),
            "rationale": "Could not parse structured response; using raw text.",
        }


class FounderAgent:
    """Simulates a budget-conscious, speed-first, UX-obsessed startup founder."""

    def __init__(self) -> None:
        from strands import Agent

        from llm_service import get_strands_model

        self._agent = Agent(
            model=get_strands_model("user_agent_founder"),
            system_prompt=FOUNDER_SYSTEM_PROMPT,
        )

    def _call(self, prompt: str, *, max_retries: int = 3) -> str:
        """Invoke the Strands agent and extract text.

        Retries on transient LLM provider errors (500s, timeouts, connection
        resets) with exponential backoff.
        """
        import time as _time

        for attempt in range(max_retries + 1):
            try:
                result = self._agent(prompt)
                return str(result).strip()
            except Exception as exc:
                exc_text = str(exc).lower()
                is_transient = any(k in exc_text for k in (
                    "500", "502", "503", "504",
                    "internal server error", "service unavailable",
                    "timeout", "connection", "reset",
                ))
                if is_transient and attempt < max_retries:
                    wait = 2 ** attempt * 5  # 5s, 10s, 20s
                    logger.warning(
                        "LLM call failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, max_retries + 1, wait, str(exc)[:200],
                    )
                    _time.sleep(wait)
                    continue
                raise

    def generate_spec(self) -> str:
        """Generate the TaskFlow product specification."""
        return self._call(SPEC_GENERATION_PROMPT)

    def answer_question(self, question: dict[str, Any]) -> dict[str, Any]:
        """Answer a pending question from the SE team.

        Args:
            question: Dict with keys: id, question_text, context, recommendation, options.
                      Each option has: id, label, is_default, rationale, confidence.

        Returns:
            Dict with: selected_option_id, other_text (or None), rationale.
        """
        options = question.get("options") or []
        options_lines = []
        for opt in options:
            default_marker = " [DEFAULT]" if opt.get("is_default") else ""
            confidence = opt.get("confidence", "")
            conf_str = f" (confidence: {confidence})" if confidence else ""
            options_lines.append(f"- [{opt['id']}] {opt['label']}{default_marker}{conf_str}")
            if opt.get("rationale"):
                options_lines.append(f"  Rationale: {opt['rationale']}")
        options_text = (
            "\n".join(options_lines)
            if options_lines
            else "(No predefined options — provide a free-text answer)"
        )

        prompt = QUESTION_ANSWERING_PROMPT.format(
            question_text=question.get("question_text", ""),
            context=question.get("context", "No additional context provided."),
            recommendation=question.get("recommendation", "No recommendation provided."),
            options_text=options_text,
        )

        return _parse_answer(self._call(prompt))

    def chat(self, message: str, context: dict[str, Any]) -> str:
        """Respond to a user chat message in the founder persona."""
        recent = context.get("recent_decisions", "none yet")
        if isinstance(recent, list):
            recent = "\n".join(
                f"- {d.get('question_text', '?')}: {d.get('answer_text', '?')}"
                for d in recent[-5:]
            ) or "none yet"
        prompt = CHAT_PROMPT.format(
            status=context.get("status", "unknown"),
            recent_decisions=recent,
            message=message,
        )
        return self._call(prompt)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_agent: FounderAgent | None = None


def get_founder_agent() -> FounderAgent:
    global _agent
    if _agent is None:
        _agent = FounderAgent()
    return _agent
