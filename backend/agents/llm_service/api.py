"""Public LLM entrypoints: ``generate_text`` and ``generate_structured``.

Thin, opinionated wrappers over the existing :func:`llm_service.get_client`
plumbing. The two functions make the contract explicit at every call site:

- :func:`generate_text` — free-form string output. Never JSON-parsed.
  Use for prose, Markdown, code, or any prompt that does not have a strict
  response schema.
- :func:`generate_structured` — Pydantic-typed structured output. Internally
  enforces JSON mode (provider default) and applies the
  :func:`llm_service.complete_validated` self-correction guard.

Legacy methods (``complete``, ``complete_text``, ``complete_json``,
``chat_json_round``) remain fully supported. This module is purely additive;
new code is strongly encouraged to use these entrypoints instead.

See :doc:`/backend/agents/llm_service/FEATURE_SPEC_structured_output_contract.md`.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from .factory import get_client
from .structured import complete_validated

T = TypeVar("T", bound=BaseModel)


def generate_text(
    prompt: str,
    *,
    system_prompt: str | None = None,
    temperature: float = 0.7,
    agent_key: str | None = None,
    think: bool = False,
) -> str:
    """Generate free-form text. The output is never JSON-parsed.

    Use this for prose, Markdown, code, long-form answers, or any prompt whose
    response does not have a strict schema.

    Args:
        prompt: The user prompt.
        system_prompt: Optional system prompt.
        temperature: Sampling temperature (default 0.7 for text generation).
        agent_key: Per-agent config selector forwarded to ``get_client``.
        think: Enable chain-of-thought / reasoning mode.

    Returns:
        Raw string response, stripped of leading/trailing whitespace.
    """
    client = get_client(agent_key=agent_key)
    return str(
        client.complete(
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            think=think,
        )
    ).strip()


def generate_structured(
    prompt: str,
    *,
    schema: type[T],
    system_prompt: str | None = None,
    temperature: float = 0.0,
    agent_key: str | None = None,
    correction_attempts: int = 1,
) -> T:
    """Generate a typed structured response validated against ``schema``.

    Internally delegates to :func:`llm_service.complete_validated`, which
    provides one schema-grounded self-correction retry by default. JSON mode
    is enforced implicitly inside the provider's ``complete_json``.

    Args:
        prompt: The user prompt. It should instruct the model to emit JSON.
        schema: Pydantic ``BaseModel`` subclass the response must satisfy.
        system_prompt: Optional system prompt.
        temperature: Sampling temperature (default 0.0 for deterministic
            structured output).
        agent_key: Per-agent config selector forwarded to ``get_client``.
        correction_attempts: Max corrective follow-up calls on parse /
            validation failure (default 1; 0 opts out).

    Returns:
        An instance of ``schema`` validated against the final successful reply.

    Raises:
        LLMJsonParseError: Every attempt returned unparseable output.
        LLMSchemaValidationError: Every attempt returned parseable JSON that
            failed Pydantic validation.
    """
    client = get_client(agent_key=agent_key)
    return complete_validated(
        client,
        prompt,
        schema=schema,
        system_prompt=system_prompt,
        temperature=temperature,
        correction_attempts=correction_attempts,
    )


__all__ = ["generate_structured", "generate_text"]
