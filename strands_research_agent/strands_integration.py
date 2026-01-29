from __future__ import annotations

from typing import Any, Callable, Dict

from .agent import ResearchAgent
from .llm import LLMClient, OllamaLLMClient
from .models import ResearchBriefInput, ResearchAgentOutput


def create_research_agent(llm_client: LLMClient) -> ResearchAgent:
    """
    Factory used by a Strands runtime (or any orchestrator) to construct the agent.

    The caller is responsible for providing an `LLMClient` implementation that
    adapts the host system's model API.
    """
    llm_client = OllamaLLMClient(  # uses http://127.0.0.1:11434 by default
        model="llama3.1",          # or any other Ollama model name you have
    )
    return ResearchAgent(llm_client=llm_client)


def get_agent_spec() -> Dict[str, Any]:
    """
    Return a simple spec describing how to call this agent.

    A Strands host can import this function and introspect:
    - `name`: human-friendly identifier
    - `input_model` / `output_model`: Pydantic models
    - `handler`: callable accepting an input model instance and returning an output model
    """

    def handler(llm_client: LLMClient, payload: Dict[str, Any]) -> ResearchAgentOutput:
        agent = create_research_agent(llm_client)
        brief = ResearchBriefInput.model_validate(payload)
        return agent.run(brief)

    return {
        "name": "research_agent",
        "description": "Performs web research based on a short content brief and returns structured references.",
        "input_model": ResearchBriefInput,
        "output_model": ResearchAgentOutput,
        "handler_factory": handler,
    }

