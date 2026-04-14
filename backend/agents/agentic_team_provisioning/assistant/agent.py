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
from llm_service import get_strands_model

logger = logging.getLogger(__name__)

_TRIGGER_MAP = {v.value: v for v in TriggerType}
_STEP_TYPE_MAP = {v.value: v for v in StepType}

SYSTEM_PROMPT = """\
You are a Process Designer assistant. You help users create and refine **agentic \
teams** — teams of AI agents that execute business or technical processes.

## Required agentic team architecture

Every team you design MUST follow this architecture:

### API layer (5 categories, handled by the platform)
The platform exposes: **User Requests / Chat**, **Questions for User**, \
**Job Status**, **Assets** (file system), and **Form Information** (database). \
You do not design these — they exist automatically.

### Orchestrator Agent
The central coordinator inside the team. It receives user requests, manages \
**Job Tracking** and **Question Tracking**, delegates work to named agents, \
and executes processes. The platform acts as the orchestrator.

### Agents pool / Roster (Agent 1 … Agent N)
Each team maintains a **roster** — a named pool of agents. The roster is \
validated to ensure the team is fully staffed: every skill, capability, tool, \
and expertise area needed by the team's processes must be covered by at least \
one rostered agent. Each agent has:
- **agent_name** — stable, unique within the team; used for provisioning.
- **role** — primary role on the team.
- **skills** — specific skills (e.g. "data analysis", "copywriting").
- **capabilities** — functional capabilities (e.g. "code generation", "web search").
- **tools** — tools or integrations the agent can use (e.g. "Git", "Slack API").
- **expertise** — domain expertise areas (e.g. "customer support", "HIPAA compliance").

Each named agent is provisioned by the **Agent Provisioning** team: they \
receive a sandboxed environment per the canonical agent anatomy (Input/Output, \
Tools, Memory tiers, Prompts, Security Guardrails, Subagents). Use clear, \
stable names — they participate in provisioning.

**You MUST provide all six fields** for every agent. The roster is validated \
automatically; agents missing skills/capabilities/tools/expertise will be \
flagged as incomplete.

### Processes pool (Process 1 … Process N)
Each team defines one or more processes. A process has:
- A **trigger** (message, event, schedule, or manual) that starts it.
- A series of **steps**, each assigned to agents **from the team's agents pool**.
- A clear **output** (deliverable + destination).

Step types: action, decision, parallel_split, parallel_join, wait, subprocess.

### Infrastructure (automatic)
- **File System** for assets.
- **Postgres** for form data.
- **Job Service** for job lifecycle.

## Your responsibilities

1. Ask clarifying questions to fully understand the team's purpose and processes.
2. First define the **agents pool** — the full roster of named agents the team needs.
3. Then define **processes** that reference those agents by name.
4. Every agent mentioned in a process step MUST appear in the agents pool.
5. Every process must have a trigger, at least one step, and an output.

## Output format

When you produce or update the team definition, include TWO JSON blocks:

### ```agents``` block — the full team roster
```agents-example
[
  {
    "agent_name": "Triage Agent",
    "role": "Classifies tickets by urgency",
    "skills": ["text classification", "priority assessment"],
    "capabilities": ["NLP analysis", "rule-based routing"],
    "tools": ["Ticket API", "Label service"],
    "expertise": ["customer support", "SLA management"]
  },
  {
    "agent_name": "Router Agent",
    "role": "Decides which team handles the ticket",
    "skills": ["intent detection", "team matching"],
    "capabilities": ["decision making", "workload balancing"],
    "tools": ["Team directory API", "Queue manager"],
    "expertise": ["operations", "resource allocation"]
  },
  {
    "agent_name": "Resolution Agent",
    "role": "Resolves the ticket and writes summary",
    "skills": ["problem solving", "technical writing"],
    "capabilities": ["knowledge base search", "solution generation"],
    "tools": ["Knowledge base", "Ticket API"],
    "expertise": ["technical support", "documentation"]
  }
]
```

### ```process``` block — a complete process definition
```process-example
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

6. Always include the FULL JSON when updating — not partial diffs.
7. Always include the ```agents``` block when you update the process (even if \
   agents haven't changed) so the roster stays in sync.
8. You may also include a ```suggestions ... ``` block with a JSON array of \
   follow-up question strings.
9. Keep your conversational replies concise and friendly.
"""


def _build_messages(
    conversation_history: list[tuple[str, str]],
    current_process: Optional[ProcessDefinition],
    current_agents: Optional[list[dict]],
    user_message: str,
) -> list[dict]:
    """Build the LLM message list."""
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    state_parts: list[str] = []
    if current_agents:
        state_parts.append(
            f"Current team agents pool:\n```json\n{json.dumps(current_agents, indent=2)}\n```"
        )
    if current_process and current_process.name:
        process_json = current_process.model_dump(mode="json")
        state_parts.append(
            "Current process definition (the user has built so far):\n"
            f"```json\n{json.dumps(process_json, indent=2)}\n```"
        )
    if state_parts:
        messages.append(
            {
                "role": "system",
                "content": "\n\n".join(state_parts)
                + "\n\nContinue refining based on the user's input.",
            }
        )

    for role, content in conversation_history:
        messages.append({"role": role, "content": content})

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


def _parse_agents_json(text: str) -> Optional[list]:
    """Extract the ```agents ... ``` JSON block (array of agent dicts)."""
    pattern = r"```agents\s*\n?(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return None
    raw = match.group(1).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        logger.warning("Failed to parse agents JSON from assistant response")
    return None


def _strip_code_blocks(text: str) -> str:
    """Remove ```process```, ```agents```, and ```suggestions``` blocks from the visible reply."""
    text = re.sub(r"```process\s*\n?.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"```agents\s*\n?.*?```", "", text, flags=re.DOTALL)
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
    """Conversational agent that helps users design agentic team processes.

    The LLM is instructed to produce both an **agents** roster and a **process**
    definition so teams comply with the canonical agentic team architecture
    (Orchestrator → Agents pool + Processes pool).
    """

    def respond(
        self,
        conversation_history: list[tuple[str, str]],
        current_process: Optional[ProcessDefinition],
        user_message: str,
        current_agents: Optional[list[dict]] = None,
    ) -> tuple[str, Optional[ProcessDefinition], list[str], Optional[list[dict]]]:
        """Send user message to LLM and return structured outputs.

        Returns
        -------
        tuple of (reply_text, updated_process_or_None, suggested_questions,
                  agents_roster_or_None)

        ``agents_roster`` is a list of dicts with ``agent_name`` and ``role``
        when the LLM included an ``agents`` block; None otherwise.
        """
        messages = _build_messages(
            conversation_history, current_process, current_agents, user_message
        )

        # Extract system prompt and format conversation as a single prompt string.
        # The LLM client's complete() expects (prompt: str, system_prompt: str),
        # not a raw messages list.
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        system_prompt = "\n\n".join(system_parts) if system_parts else None

        conversation_parts: list[str] = []
        for m in messages:
            if m["role"] == "system":
                continue
            prefix = "User" if m["role"] == "user" else "Assistant"
            conversation_parts.append(f"{prefix}: {m['content']}")
        prompt = "\n\n".join(conversation_parts)

        from strands import Agent

        agent = Agent(
            model=get_strands_model("agentic_team_provisioning"),
            system_prompt=system_prompt,
        )
        result = agent(prompt)
        raw_text = str(result).strip()

        # Parse structured blocks
        process_data = _parse_process_json(raw_text)
        agents_data = _parse_agents_json(raw_text)
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
                    "What is the team's purpose?",
                    "What agents should be on this team?",
                    "What processes will they run?",
                ]

        return reply_text, updated_process, suggestions, agents_data
