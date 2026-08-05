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

- ``schema=<a BaseModel subclass>``: delegates to
  ``llm_service.api.generate_structured`` — the reply is parsed, validated
  against ``schema``, and self-corrected up to ``correction_attempts`` times
  on failure. Returns a ``schema`` instance.
- ``schema=None`` (default): routes through ``client.complete_json`` directly
  — no schema validation or self-correction, and no canonical entrypoint
  exists for this mode. Returns the raw JSON dict, for callers that build
  their own result model by hand.

Architecture note (see ``docs/LLM_CALLING_PATTERN_DECISION.md``): this
module is documented there as a narrow, justified exception to that
decision's Pattern 1 (``LlmToolAgentBase``) default for *new agents* — it
is a plain function, not an agent, and exists to give already-hand-rolled
(Pattern 5) call sites one shared implementation of the resolve+call
boilerplate they already duplicate, not a sixth agent-shape choice. The
schema-validated branch delegates to ``generate_structured`` (the canonical
public entrypoint for that resolve+validate happy path) rather than
re-implementing it; the plain-JSON branch has no canonical entrypoint to
delegate to, so it resolves a client and calls ``complete_json`` itself.

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

from typing import Any, Optional, TypeVar, overload

from pydantic import BaseModel

from llm_service import LLMClient, generate_structured, get_client

T = TypeVar("T", bound=BaseModel)


@overload
def run_single_shot_review(
    llm_client: Optional[LLMClient],
    agent_key: str,
    prompt: str,
    system_prompt: Optional[str] = None,
    *,
    schema: type[T],
    objective: Optional[str] = None,
    temperature: float = 0.0,
    correction_attempts: int = 1,
    think: bool | str | None = False,
    context: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> T: ...


@overload
def run_single_shot_review(
    llm_client: Optional[LLMClient],
    agent_key: str,
    prompt: str,
    system_prompt: Optional[str] = None,
    *,
    schema: None = None,
    objective: Optional[str] = None,
    temperature: float = 0.0,
    correction_attempts: int = 1,
    think: bool | str | None = False,
    context: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> dict[str, Any]: ...


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
    think: bool | str | None = False,
    context: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> T | dict[str, Any]:
    """Resolve a client and make one single-shot review call.

    Preconditions:
        ``agent_key`` and ``prompt`` are non-empty (non-whitespace) strings —
        violation raises ``ValueError``. ``llm_client`` is a pre-resolved
        ``LLMClient`` or ``None``.

    Postconditions:
        The client used is ``llm_client`` when given, else
        ``get_client(agent_key)`` — only that second branch forwards
        ``agent_key`` to ``get_client`` (for that client's
        attribution/telemetry); an injected ``llm_client`` keeps whatever
        attribution it already carries, unaffected by ``agent_key``.
        Independent of which branch resolved the client, ``objective``, when
        omitted, defaults to ``f"{agent_key} single-shot review"``. When
        ``schema`` is given, returns a validated instance of ``schema`` (see
        ``llm_service.api.generate_structured`` for the self-correction-retry
        and error semantics on a parse/validation failure). When ``schema``
        is ``None``, returns the raw dict from ``client.complete_json`` with
        no validation or retry. ``correction_attempts`` and ``context`` are
        forwarded only in schema-validated mode (``complete_json`` has no
        equivalent parameters). ``**kwargs`` is forwarded to the underlying
        call in either mode.

    Raises:
        ValueError: ``agent_key`` or ``prompt`` is empty/whitespace-only.
    """
    if not agent_key or not agent_key.strip():
        raise ValueError("agent_key must be a non-empty string")
    if not prompt or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    call_objective = objective if objective is not None else f"{agent_key} single-shot review"

    if schema is not None:
        return generate_structured(
            prompt,
            schema=schema,
            objective=call_objective,
            system_prompt=system_prompt,
            temperature=temperature,
            agent_key=agent_key,
            correction_attempts=correction_attempts,
            llm_client=llm_client,
            think=think,
            context=context,
            **kwargs,
        )

    client = llm_client if llm_client is not None else get_client(agent_key)
    return client.complete_json(
        prompt,
        objective=call_objective,
        system_prompt=system_prompt,
        temperature=temperature,
        think=think,
        **kwargs,
    )


__all__ = ["run_single_shot_review"]
