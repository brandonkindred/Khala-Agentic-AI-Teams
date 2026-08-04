"""
Shared single-shot LLM review call: model resolution + one validated call.

Single-shot review agents (a security/QA/lint-style check that sends one
prompt and expects one JSON reply — as opposed to the multi-chunk
orchestration in ``shared/llm_review.py``) each hand-roll the same two steps:
resolve an ``LLMClient`` for their ``agent_key`` when the caller didn't
already inject one, then call it and turn the JSON reply into a result. That
resolution step is duplicated by hand at nearly every such agent's call site
(``llm_client if llm_client is not None else get_client(agent_key)``), and the
JSON-handling step diverges: some agents validate the reply against a Pydantic
schema via ``llm_service.structured.complete_validated`` (self-correcting on a
parse/validation failure), others just call ``complete_json`` and build their
result model by hand from the raw dict with no validation or retry at all.

``run_single_shot_review`` collapses both steps into one call and supports
both JSON-handling modes so agents not yet migrated to a schema aren't forced
to define one before they can adopt the resolution half of this helper:

- ``schema=<a BaseModel subclass>``: routes through ``complete_validated`` —
  the reply is parsed, validated against ``schema``, and self-corrected up to
  ``correction_attempts`` times on failure. Returns a ``schema`` instance.
- ``schema=None`` (default): routes through ``client.complete_json`` directly
  — no schema validation or self-correction. Returns the raw JSON dict, for
  callers that build their own result model by hand.

Usage::

    from software_engineering_team.shared.single_shot_review import run_single_shot_review

    # Schema-validated mode
    result = run_single_shot_review(
        llm_client,
        agent_key="devops",
        prompt=DEVSECOPS_REVIEW_PROMPT + "\\n\\n---\\n\\n" + context,
        system_prompt="You are a DevSecOps reviewer.",
        schema=DevSecOpsReviewLLMResponse,
    )  # -> DevSecOpsReviewLLMResponse

    # Plain-JSON mode (no schema yet)
    data = run_single_shot_review(
        llm_client, agent_key="devops", prompt=prompt, system_prompt=system_prompt
    )  # -> dict
"""

from __future__ import annotations

from typing import Any, Optional, TypeVar

from pydantic import BaseModel

from llm_service import LLMClient, get_client
from llm_service.structured import complete_validated

T = TypeVar("T", bound=BaseModel)


def run_single_shot_review(
    llm_client: Optional[LLMClient],
    agent_key: str,
    prompt: str,
    system_prompt: Optional[str] = None,
    *,
    schema: Optional[type[T]] = None,
    objective: Optional[str] = None,
    temperature: float = 0.0,
    correction_attempts: int = 1,
    think: "bool | str | None" = False,
    context: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> Any:
    """Resolve a client and make one single-shot review call.

    Preconditions:
        ``agent_key`` and ``prompt`` are non-empty (non-whitespace) strings.
        ``llm_client`` is a pre-resolved ``LLMClient`` or ``None``.

    Postconditions:
        The client used is ``llm_client`` when given, else
        ``get_client(agent_key)`` — ``agent_key`` is always forwarded to
        ``get_client`` for attribution even when it is not needed for
        resolution. When ``schema`` is given, returns a validated instance of
        ``schema`` (see ``llm_service.structured.complete_validated`` for the
        self-correction-retry and error semantics on a parse/validation
        failure). When ``schema`` is ``None``, returns the raw dict from
        ``client.complete_json`` with no validation or retry. ``objective``,
        when omitted, defaults to ``f"{agent_key} single-shot review"``.
        ``correction_attempts`` and ``context`` are forwarded only in
        schema-validated mode (``complete_json`` has no equivalent
        parameters). ``**kwargs`` is forwarded to the underlying call in
        either mode.
    """
    assert agent_key and agent_key.strip(), "agent_key must be non-empty"
    assert prompt and prompt.strip(), "prompt must be non-empty"

    client = llm_client if llm_client is not None else get_client(agent_key)
    call_objective = objective if objective is not None else f"{agent_key} single-shot review"

    if schema is not None:
        return complete_validated(
            client,
            prompt,
            schema=schema,
            objective=call_objective,
            system_prompt=system_prompt,
            temperature=temperature,
            correction_attempts=correction_attempts,
            context=context,
            think=think,
            **kwargs,
        )

    return client.complete_json(
        prompt,
        objective=call_objective,
        system_prompt=system_prompt,
        temperature=temperature,
        think=think,
        **kwargs,
    )


__all__ = ["run_single_shot_review"]
