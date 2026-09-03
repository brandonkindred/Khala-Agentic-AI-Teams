"""Shared system-prompt assembly for Strands Agent list-form system prompts.

``build_system_prompt_with_content`` now lives in ``llm_service`` (the
team-independent infrastructure package every team already depends on for
``CacheBreakpoint``/``LLMClient``), since the normalize-and-prepend logic it
implements is generic Strands plumbing with no software-engineering-team
behavior. Re-exported here for backward compatibility with this team's
existing call sites — ``shared.persona_agent_base.run_structured_persona``
and ``code_review_agent.via_reasoning.run_agent_via_reasoning`` — which both
previously had their own copy of this same function before it was
consolidated first here, then hoisted to ``llm_service``.

See ``llm_service.system_prompt_assembly`` for the implementation and its
contract.
"""

from __future__ import annotations

from llm_service import build_system_prompt_with_content

__all__ = ["build_system_prompt_with_content"]
