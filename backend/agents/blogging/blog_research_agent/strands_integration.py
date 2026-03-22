from __future__ import annotations

from typing import Any, Dict

from llm_service import LLMClient

from .agent import ResearchAgent
from .models import ResearchAgentOutput, ResearchBriefInput


def create_research_agent(llm_client: LLMClient) -> ResearchAgent:
    """
    Factory used by a Strands runtime (or any orchestrator) to construct the agent.

    The caller is responsible for providing an `LLMClient` implementation that
    adapts the host system's model API.

    Preconditions:
        - llm_client is not None.
    Postconditions:
        - Returns a ResearchAgent instance configured with the given llm_client.
    """
    assert llm_client is not None, "llm_client is required"
    return ResearchAgent(llm_client=llm_client)


def get_agent_spec() -> Dict[str, Any]:
    """
    Return a spec describing how to call this agent.

    A Strands host can use:
        - name: human-friendly identifier
        - description: short description of the agent
        - input_model / output_model: Pydantic models (ResearchBriefInput, ResearchAgentOutput)
        - handler_factory: callable(llm_client, payload) -> ResearchAgentOutput

    Preconditions:
        - None (no arguments).
    Postconditions:
        - Returns a dict with keys "name", "description", "input_model", "output_model", "handler_factory".
        - handler_factory(llm_client, payload) expects payload to validate as ResearchBriefInput
          and returns ResearchAgentOutput.
    """

    def handler_factory(llm_client: LLMClient, payload: Dict[str, Any]) -> ResearchAgentOutput:
        agent = create_research_agent(llm_client)
        brief = ResearchBriefInput.model_validate(payload)
        return agent.run(brief)

    return {
        "name": "research_agent",
        "description": "Performs web research based on a short content brief and returns structured references.",
        "input_model": ResearchBriefInput,
        "output_model": ResearchAgentOutput,
        "handler_factory": handler_factory,
    }

