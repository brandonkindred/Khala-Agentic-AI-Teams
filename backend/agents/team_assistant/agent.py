"""Generic team assistant agent — LLM-driven conversational intake."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, List, Tuple

logger = logging.getLogger(__name__)


def _parse_response(raw: str) -> tuple[str, dict[str, Any], list[str], dict[str, Any] | None]:
    """Parse LLM response JSON, with fallback for malformed output."""
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
        logger.warning("Failed to parse assistant LLM response as JSON, using raw text")
        return raw, {}, [], None


class TeamAssistantAgent:
    """Conversational agent that collects information for a team through dialogue."""

    def __init__(
        self,
        *,
        team_name: str,
        system_prompt: str,
        welcome_message: str,
        default_suggested_questions: list[str],
        required_fields: list[dict],
        llm: Any = None,
        llm_agent_key: str | None = None,
    ) -> None:
        self.team_name = team_name
        self.system_prompt = system_prompt
        self.welcome_message = welcome_message
        self.default_suggested_questions = list(default_suggested_questions)
        self.required_field_keys = [f["key"] for f in required_fields]

        if llm is not None:
            self._llm = llm
        else:
            from strands import Agent

            from llm_service import get_strands_model

            self._llm = Agent(
                model=get_strands_model(llm_agent_key or team_name),
                system_prompt=system_prompt,
            )

    def respond(
        self,
        history: List[Tuple[str, str]],
        context: dict[str, Any],
        user_message: str,
    ) -> Tuple[str, dict[str, Any], list[str], dict[str, Any] | None]:
        """Produce a reply, context updates, suggested questions, and optional artifact."""
        conversation_lines = []
        for role, content in history:
            prefix = "Assistant: " if role == "assistant" else "User: "
            conversation_lines.append(f"{prefix}{content}")
        conversation_history = (
            "\n".join(conversation_lines) if conversation_lines else "(New conversation)"
        )

        prompt = (
            f"## Accumulated Context\n{json.dumps(context, indent=2) if context else '{}'}\n\n"
            f"## Conversation History\n{conversation_history}\n\n"
            f"## Latest Message from User\n{user_message}\n\n"
            "Respond with the JSON object as specified in your instructions."
        )

        result = self._llm(prompt)
        raw = str(result).strip()
        return _parse_response(raw)

    def check_readiness(self, context: dict[str, Any]) -> tuple[bool, list[str]]:
        """Check if all required fields are present in the context.

        Returns (ready, missing_field_keys).
        """
        missing = [k for k in self.required_field_keys if not context.get(k)]
        return (len(missing) == 0, missing)
