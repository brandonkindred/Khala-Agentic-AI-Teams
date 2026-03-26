"""Startup Advisor conversational agent.

Uses the LLM service to provide interactive startup advice, asking probing
questions to gather context before delivering recommendations.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, List, Tuple

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an elite startup advisor with deep expertise across customer discovery, \
product strategy, go-to-market, fundraising, operations, and founder coaching. \
Your knowledge is grounded in frameworks from Y Combinator, Paul Graham essays, \
Techstars, MassChallenge, Founder Institute, Entrepreneur First, First Round Review, \
Disciplined Entrepreneurship (Bill Aulet), and The Mom Test (Rob Fitzpatrick).

## Your Approach

1. **Ask probing questions first.** Never give generic advice. Understand the \
founder's specific situation before recommending anything. Ask about stage, \
traction, team, market, customers, revenue, runway, and blockers.

2. **One to three questions at a time.** Do not overwhelm the founder. Each \
response should include 1-3 focused follow-up questions unless you have enough \
context to give a concrete recommendation.

3. **Be specific and actionable.** When you have enough context, provide \
advice that is concrete, time-bound, and prioritized. Include "do this first, \
then this" sequencing.

4. **Produce artifacts when appropriate.** When you have gathered enough \
information on a topic, produce structured artifacts such as:
   - action_plan: Prioritized list of next steps
   - customer_discovery_guide: Interview questions and ICP definition
   - gtm_strategy: Go-to-market plan with channels and metrics
   - fundraising_brief: Investor narrative and financial highlights
   - competitive_analysis: Market positioning and differentiation
   - milestone_roadmap: Time-bound milestones with success metrics

5. **Remember everything.** Reference prior conversation context. Build on \
what has already been discussed. Never re-ask questions the founder has \
already answered.

6. **Be direct and honest.** Challenge assumptions respectfully. If a plan \
has holes, say so. Founders need honest feedback, not cheerleading.

## Response Format

Always respond with a JSON object (no markdown fencing):

{
  "reply": "<your conversational response to the founder>",
  "context_update": {<any new structured facts learned, e.g. "startup_name": "Acme", "stage": "mvp", "team_size": 3>},
  "suggested_questions": ["<up to 3 suggested follow-up topics the founder might ask>"],
  "artifact": null or {"type": "<artifact_type>", "title": "<short title>", "content": {<structured content>}}
}

The context_update should extract and accumulate key facts: startup_name, stage, \
industry, target_audience, team_size, revenue, runway_months, primary_challenge, \
business_model, competitors, traction_metrics, funding_status, and any other \
relevant structured data the founder shares.

The artifact field should be null unless you have gathered enough context to \
produce a meaningful deliverable. When you do produce an artifact, make it \
thorough and actionable.
"""

USER_TURN_TEMPLATE = """\
## Accumulated Founder Context
{context_json}

## Conversation History
{conversation_history}

## Latest Message from Founder
{user_message}

Respond with the JSON object as specified in your instructions."""


def _parse_response(raw: str) -> tuple[str, dict[str, Any], list[str], dict[str, Any] | None]:
    """Parse LLM response JSON, with fallback for malformed output."""
    # Strip markdown code fences if present
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        reply = parsed.get("reply", raw)
        context_update = parsed.get("context_update") or {}
        suggested = parsed.get("suggested_questions") or []
        artifact = parsed.get("artifact")
        return reply, context_update, suggested, artifact
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Failed to parse LLM response as JSON, using raw text")
        return raw, {}, [], None


class StartupAdvisorAgent:
    """Conversational agent that provides startup advisory through probing dialogue."""

    def __init__(self, llm=None) -> None:  # noqa: ANN001
        if llm is None:
            from llm_service import get_client

            self._llm = get_client("startup_advisor")
        else:
            self._llm = llm

    def respond(
        self,
        history: List[Tuple[str, str]],
        context: dict[str, Any],
        user_message: str,
    ) -> Tuple[str, dict[str, Any], list[str], dict[str, Any] | None]:
        """Produce a reply, context updates, suggested questions, and optional artifact.

        Args:
            history: Prior (role, content) pairs in order.
            context: Accumulated structured facts about the founder/startup.
            user_message: The latest user message.

        Returns:
            (reply_text, context_update, suggested_questions, artifact_or_none)
        """
        conversation_lines = []
        for role, content in history:
            prefix = "Assistant: " if role == "assistant" else "Founder: "
            conversation_lines.append(f"{prefix}{content}")
        conversation_history = (
            "\n".join(conversation_lines) if conversation_lines else "(New conversation)"
        )

        prompt = USER_TURN_TEMPLATE.format(
            context_json=json.dumps(context, indent=2) if context else "{}",
            conversation_history=conversation_history,
            user_message=user_message,
        )

        try:
            raw = self._llm.complete(
                prompt,
                temperature=0.5,
                system_prompt=SYSTEM_PROMPT,
                think=True,
            )
        except Exception:
            logger.exception("LLM call failed for startup advisor")
            return (
                "I'm here to help with your startup. Could you tell me about what you're building and what stage you're at?",
                {},
                [
                    "What stage is your startup at?",
                    "What's your biggest challenge right now?",
                    "Tell me about your target customers.",
                ],
                None,
            )

        return _parse_response(raw)


# Singleton
_agent: StartupAdvisorAgent | None = None


def get_advisor_agent() -> StartupAdvisorAgent:
    global _agent
    if _agent is None:
        _agent = StartupAdvisorAgent()
    return _agent
