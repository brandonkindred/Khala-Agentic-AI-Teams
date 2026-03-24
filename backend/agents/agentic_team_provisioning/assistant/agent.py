"""Process Designer Assistant — LLM-powered agent that helps users define team processes via chat."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Optional

from agentic_team_provisioning.models import (
    ProcessDefinition,
    ProcessOutput,
    ProcessStatus,
    ProcessStep,
    ProcessStepAgent,
    ProcessTrigger,
    StepType,
    TriggerType,
)
from llm_service import get_client

logger = logging.getLogger(__name__)

_TRIGGER_MAP = {v.value: v for v in TriggerType}
_STEP_TYPE_MAP = {v.value: v for v in StepType}

SYSTEM_PROMPT = """\
You are a Process Designer assistant. Your job is to help the user define \
business or technical processes for an agentic team through conversation.

A process has:
- A **trigger** (message, event, schedule, or manual) that starts it.
- A series of **steps**, each with a name, description, type, assigned agents, \
  and connections to subsequent steps.
- A clear **output** describing the deliverable and where it goes when the process completes.

Step types: action, decision, parallel_split, parallel_join, wait, subprocess.

Guidelines:
1. Ask clarifying questions to fully understand the process.
2. Strive for completeness — every process must have a clear start (trigger) and \
   a clear end (output/deliverable).
3. After gathering enough information, produce or update the process definition.
4. When you update the process, include a JSON block wrapped in ```process ... ``` \
   fences. The JSON must conform to this schema:

```example
{
  "name": "Process Name",
  "description": "Short description",
  "trigger": {"trigger_type": "message", "description": "When a customer submits a ticket"},
  "steps": [
    {
      "step_id": "step_1",
      "name": "Triage",
      "description": "Classify the incoming ticket",
      "step_type": "action",
      "agents": [{"agent_name": "Triage Agent", "role": "Classifies tickets by urgency"}],
      "next_steps": ["step_2"]
    },
    {
      "step_id": "step_2",
      "name": "Route",
      "description": "Route to appropriate handler",
      "step_type": "decision",
      "agents": [{"agent_name": "Router Agent", "role": "Decides which team handles the ticket"}],
      "next_steps": ["step_3a", "step_3b"],
      "condition": "Based on ticket category"
    }
  ],
  "output": {"description": "Resolved ticket with summary", "destination": "Customer notification + ticket archive"}
}
```

5. Always include the FULL process JSON when updating — not partial diffs.
6. You may also include a ```suggestions ... ``` block with a JSON array of \
   follow-up question strings.
7. Keep your conversational replies concise and friendly.
"""


def _build_messages(
    conversation_history: list[tuple[str, str]],
    current_process: Optional[ProcessDefinition],
    user_message: str,
) -> list[dict]:
    """Build the LLM message list."""
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Inject current process state if we have one
    if current_process and current_process.name:
        process_json = current_process.model_dump(mode="json")
        messages.append(
            {
                "role": "system",
                "content": (
                    "Current process definition (the user has built so far):\n"
                    f"```json\n{json.dumps(process_json, indent=2)}\n```\n"
                    "Continue refining this process based on the user's input."
                ),
            }
        )

    # Prior conversation turns
    for role, content in conversation_history:
        messages.append({"role": role, "content": content})

    # Latest user message
    messages.append({"role": "user", "content": user_message})
    return messages


def _parse_process_json(text: str) -> Optional[dict]:
    """Extract the ```process ... ``` JSON block from assistant response."""
    pattern = r"```process\s*\n?(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return None
    raw = match.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse process JSON from assistant response")
        return None


def _parse_suggestions(text: str) -> list[str]:
    """Extract the ```suggestions ... ``` JSON block."""
    pattern = r"```suggestions\s*\n?(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(1).strip())
        if isinstance(data, list):
            return [str(s) for s in data]
    except json.JSONDecodeError:
        pass
    return []


def _strip_code_blocks(text: str) -> str:
    """Remove ```process``` and ```suggestions``` blocks from the visible reply."""
    text = re.sub(r"```process\s*\n?.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"```suggestions\s*\n?.*?```", "", text, flags=re.DOTALL)
    return text.strip()


def _dict_to_process(data: dict, existing_id: Optional[str] = None) -> ProcessDefinition:
    """Convert a raw dict from the LLM into a ProcessDefinition."""
    process_id = existing_id or str(uuid.uuid4())

    trigger_data = data.get("trigger", {})
    trigger = ProcessTrigger(
        trigger_type=_TRIGGER_MAP.get(trigger_data.get("trigger_type", ""), TriggerType.MESSAGE),
        description=trigger_data.get("description", ""),
    )

    steps: list[ProcessStep] = []
    for s in data.get("steps", []):
        agents = [
            ProcessStepAgent(agent_name=a.get("agent_name", ""), role=a.get("role", ""))
            for a in s.get("agents", [])
        ]
        steps.append(
            ProcessStep(
                step_id=s.get("step_id", f"step_{len(steps) + 1}"),
                name=s.get("name", ""),
                description=s.get("description", ""),
                step_type=_STEP_TYPE_MAP.get(s.get("step_type", ""), StepType.ACTION),
                agents=agents,
                next_steps=s.get("next_steps", []),
                condition=s.get("condition"),
            )
        )

    output_data = data.get("output", {})
    output = ProcessOutput(
        description=output_data.get("description", ""),
        destination=output_data.get("destination", ""),
    )

    return ProcessDefinition(
        process_id=process_id,
        name=data.get("name", ""),
        description=data.get("description", ""),
        trigger=trigger,
        steps=steps,
        output=output,
        status=ProcessStatus.DRAFT,
    )


class ProcessDesignerAgent:
    """Conversational agent that helps users design agentic team processes."""

    def respond(
        self,
        conversation_history: list[tuple[str, str]],
        current_process: Optional[ProcessDefinition],
        user_message: str,
    ) -> tuple[str, Optional[ProcessDefinition], list[str]]:
        """Send user message to LLM and return (reply, updated_process, suggestions).

        Parameters
        ----------
        conversation_history:
            List of (role, content) pairs for prior turns (excluding the new message).
        current_process:
            The process being designed so far, or None.
        user_message:
            The latest message from the user.

        Returns
        -------
        tuple of (reply_text, updated_process_or_None, suggested_questions)
        """
        messages = _build_messages(conversation_history, current_process, user_message)

        client = get_client(agent_key="agentic_team_provisioning")
        response = client.complete(messages)

        raw_text: str = response if isinstance(response, str) else response.get("content", "")

        # Parse structured blocks
        process_data = _parse_process_json(raw_text)
        suggestions = _parse_suggestions(raw_text)
        reply_text = _strip_code_blocks(raw_text)

        # Build/update process definition
        updated_process: Optional[ProcessDefinition] = None
        if process_data:
            existing_id = current_process.process_id if current_process else None
            updated_process = _dict_to_process(process_data, existing_id)

        # Default suggestions when none provided
        if not suggestions:
            if not current_process and not updated_process:
                suggestions = [
                    "What triggers this process?",
                    "What are the main steps involved?",
                    "What is the final deliverable?",
                ]

        return reply_text, updated_process, suggestions
