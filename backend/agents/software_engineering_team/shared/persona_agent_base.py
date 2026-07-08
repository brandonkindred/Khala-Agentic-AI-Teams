"""
Shared structured-output call for the top-level persona agents.

``security_agent``, ``qa_agent``, ``accessibility_agent``, and
``integration_team`` each build a fresh Strands ``Agent`` per ``run()`` call
(required — reusing one instance across calls breaks
``structured_output_model``'s forced tool choice, since Strands accumulates
message history), call it with ``structured_output_model=<their Output type>``,
and fall back to a safe default whenever the model/validation fails. That
three-step incantation — build, call, coerce-or-fallback — was duplicated
verbatim across all four; this module collapses it to one definition. Each
agent keeps its own log messages, system prompt (or per-mode prompt
selection), and fallback field values as call-site data.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

OutputT = TypeVar("OutputT")


def run_structured_persona(
    *,
    model: Any,
    system_prompt: str,
    user_prompt: str,
    output_model: type[OutputT],
    fallback_factory: Callable[[Exception], OutputT],
    agent_factory: Callable[..., Any],
    on_success: Callable[[OutputT], OutputT] | None = None,
) -> OutputT:
    """Run a one-shot structured-output Strands ``Agent`` call with a safe fallback.

    Preconditions:
        ``agent_factory(model=model, system_prompt=system_prompt)`` returns a
        callable Strands ``Agent``; ``fallback_factory(exc)`` returns a valid,
        already-final instance of ``output_model`` (e.g. ``approved=False``)
        and may itself log a warning; ``on_success``, if given, returns a
        valid instance of ``output_model``.
    Postconditions:
        On a successful call whose ``structured_output`` is an instance of
        ``output_model``, returns ``on_success(result)`` (or ``result``
        unchanged if ``on_success`` is ``None``). On any failure — building
        the agent, calling it, or an unexpected ``structured_output`` type —
        returns ``fallback_factory(exc)`` **without** passing it through
        ``on_success``: callers derive an approval/pass flag from the
        *reported findings* in ``on_success`` (e.g. "no critical/high
        severities"), and an empty findings list from the safe fallback must
        not be reinterpreted as a clean approval. Never raises.
    """
    agent = agent_factory(model=model, system_prompt=system_prompt)
    try:
        agent_result = agent(user_prompt, structured_output_model=output_model)
        result = agent_result.structured_output
        if not isinstance(result, output_model):
            raise TypeError(
                f"Expected {output_model.__name__}, got {type(result).__name__ if result else 'None'}"
            )
        return on_success(result) if on_success is not None else result
    except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
        return fallback_factory(exc)
