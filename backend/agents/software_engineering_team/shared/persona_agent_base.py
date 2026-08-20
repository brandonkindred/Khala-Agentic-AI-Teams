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

Decision record: ``devops_team``'s single-shot JSON agents standardize on
``complete_json_with_continuation`` (``software_engineering_team.shared.llm``)
instead, since all of them already called it before any standardization
effort began — no helper migration is needed there. ``run_structured_persona``
stays the pattern only for the four callers named above. Moving
``devops_team`` onto ``run_structured_persona`` instead was considered and
deferred: it would require defining a ``fallback_factory`` per devops agent,
plus verifying Strands' ``structured_output_model`` mechanism against the
nested models several devops outputs carry (``DevOpsCompletionPackage``,
``IaCExecutionError``, ``ReviewFinding``) — work that is out of scope for the
devops template standardization this module supports.
"""

from __future__ import annotations

from typing import Any, Callable, List, TypeVar

from software_engineering_team.shared.system_prompt_assembly import (
    build_system_prompt_with_content,
)

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
    system_prompt_content: List[Any] | None = None,
) -> OutputT:
    """Run a one-shot structured-output Strands ``Agent`` call with a safe fallback.

    Preconditions:
        ``agent_factory(model=model, system_prompt=system_prompt)`` returns a
        callable Strands ``Agent``; ``fallback_factory(exc)`` returns a valid,
        already-final instance of ``output_model`` (e.g. ``approved=False``)
        and may itself log a warning; ``on_success``, if given, returns a
        valid instance of ``output_model``. ``system_prompt_content``, when
        given, is a list of system-content segments (``CacheBreakpoint``
        instances, dict blocks, or strings) attached to the ``Agent``'s
        system prompt — restricted to **trusted** metadata (spec excerpts,
        architecture overviews) that is safe to elevate to system level.
        Untrusted content (code under review, repository-controlled text)
        must remain in ``user_prompt``.
    Postconditions:
        On a successful call whose ``structured_output`` is an instance of
        ``output_model``, returns ``on_success(result)`` (or ``result``
        unchanged if ``on_success`` is ``None``). On any failure — building
        the agent, calling it, or an unexpected ``structured_output`` type —
        returns ``fallback_factory(exc)`` **without** passing it through
        ``on_success``: callers derive an approval/pass flag from the
        *reported findings* in ``on_success`` (e.g. "no critical/high
        severities"), and an empty findings list from the safe fallback must
        not be reinterpreted as a clean approval. Does not itself raise for
        agent/LLM/validation failures or ``on_success`` errors (those are
        caught and passed to ``fallback_factory`` as the failure cause).
        Exceptions raised by ``fallback_factory`` propagate to the caller.
    """
    try:
        composed_prompt = build_system_prompt_with_content(system_prompt, system_prompt_content)
        agent = agent_factory(model=model, system_prompt=composed_prompt)
        agent_result = agent(user_prompt, structured_output_model=output_model)
        result = agent_result.structured_output
        if not isinstance(result, output_model):
            raise TypeError(
                f"Expected {output_model.__name__}, got {type(result).__name__ if result else 'None'}"
            )
        return on_success(result) if on_success is not None else result
    except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
        return fallback_factory(exc)
