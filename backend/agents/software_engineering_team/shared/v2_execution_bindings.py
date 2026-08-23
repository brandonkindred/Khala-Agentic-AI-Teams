"""
Shared execution-phase bindings for the code-v2 teams.

``backend_code_v2_team`` and ``frontend_code_v2_team`` each used to carry a
twin ``phases/execution.py`` that bound the same shared implementations
(``shared/phases/execution.py``'s ``run_execution_impl`` /
``run_gated_execution_impl`` / ``GatedExecutionConfig``) to that team's
``models`` module, prompt, and :class:`~software_engineering_team.shared.stack_profile.StackProfile`.
``build_execution_bindings`` performs that binding once, here — each team's
``phases/_profile.py`` calls it and re-exports the resulting
:class:`ExecutionBindings` attributes under their existing public names
(``run_execution``, ``run_execution_with_review_gates``, ``GATE_CONFIG``), so
no team-local wrapper module remains.

The one genuine per-team behavioural difference this module does *not*
collapse is the review-gate architecture itself: backend's
``run_code_review_gate``/``run_qa_gate``/``run_security_gate`` wrap three
separate shared phase functions (``run_{code_review,qa,security}_testing_phase``,
which already self-scope their ``tool_agents`` fan-out to a single
``ToolAgentKind`` internally), while frontend's wrap one unified
``run_microtask_review`` called three times and must therefore narrow
``tool_agents`` to a single kind — and forward the cross-gate
``tool_agent_cache`` — itself. That is an intentional per-team design choice
(frontend's code-review gate deliberately fans out to every wired tool-agent
kind), so the three gate adapters stay team-authored in each team's
``phases/_profile.py`` and are passed into :func:`build_execution_bindings`
as callables, exactly like ``GatedExecutionConfig`` already expects.
:func:`scope_tool_agents_by_kind` below is the shared, public, independently
tested replacement for what used to be frontend's private
``_scoped_tool_agents`` — the "config hook" for that plumbing is simply
*which* gate closures a team wires in: a team whose gates need per-kind
scoping calls this helper from inside its own gate closures; a team whose
gates already self-scope (backend) never calls it, so the backend path is
provably unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from strands import Agent

from llm_service import LLMClient
from llm_service.strands_model import LlmRunner, resolve_text_mode_strands_model
from shared.dev_models.models import SystemArchitecture, Task
from software_engineering_team.shared.phases.execution import (
    GatedExecutionConfig,
    GateOutcome,
    _run_general_microtask_impl,
    run_execution_impl,
)
from software_engineering_team.shared.stack_profile import PhaseModels, StackProfile


def _llm_runner() -> LlmRunner:
    """Build the LLM runner from this module's globals so tests can monkeypatch them.

    Preconditions: none.
    Postconditions: returns a freshly constructed ``LlmRunner`` bound to this
      module's current ``Agent`` / ``resolve_text_mode_strands_model``
      globals, looked up at call time (not cached).
    """
    return LlmRunner(agent_factory=Agent, resolve_model=resolve_text_mode_strands_model)


def scope_tool_agents_by_kind(
    tool_agents: Optional[Dict[Any, Any]], kind: Any
) -> Optional[Dict[Any, Any]]:
    """Filter a tool-agent mapping down to a single kind.

    Shared replacement for what used to be frontend's private
    ``_scoped_tool_agents``. A team whose review-gate architecture calls one
    unified review function per gate (rather than a gate-scoped shared phase
    function, as backend's ``run_qa_testing_phase``/``run_security_testing_phase``
    already do internally) calls this directly inside its own ``run_qa_gate``/
    ``run_security_gate`` closures so their tool-agent fan-out invokes only
    the one tool agent that matches the gate, not every wired kind. Teams
    whose gate functions already self-scope (backend) never call this.

    Preconditions: none -- ``tool_agents`` may be ``None`` or empty.
    Postconditions: returns ``{kind: tool_agents[kind]}`` when ``kind`` is
      wired in ``tool_agents``, else ``None``.
    """
    if not tool_agents or kind not in tool_agents:
        return None
    return {kind: tool_agents[kind]}


@dataclass(frozen=True)
class ExecutionBindings:
    """A team's execution-phase entry points, bound.

    Invariants:
        ``run_execution`` is a plain callable matching the code-v2 team
        public signature that used to live in that team's
        ``phases/execution.py``; ``gate_config`` is the ``GatedExecutionConfig``
        closed over the same team data.

    Deliberately excludes ``run_execution_with_review_gates``: that thin
    wrapper must stay defined directly in each team's ``phases/_profile.py``,
    referencing its own module-level ``GATE_CONFIG`` (built from
    ``gate_config`` below) by bare name at call time -- exactly the
    ``v2_review_bindings`` "resolved by bare name so this module stays the
    test patch surface" technique. A closure built here would instead capture
    this function's local ``gate_config`` variable once, so a test's
    ``monkeypatch.setattr(<team>.phases._profile, "GATE_CONFIG", replace(...))``
    would silently not take effect.
    """

    run_execution: Callable[..., Any]
    gate_config: GatedExecutionConfig


def build_execution_bindings(
    *,
    models: PhaseModels,
    profile: StackProfile,
    execution_prompt: str,
    parse_files_and_summary: Callable[[str], Dict[str, Any]],
    run_code_review_gate: Callable[..., GateOutcome],
    run_qa_gate: Callable[..., GateOutcome],
    run_security_gate: Callable[..., GateOutcome],
    run_batch_coding_fixes: Callable[..., Any],
    run_documentation_self_review: Callable[..., Any],
    run_dbc_self_review: Optional[Callable[..., Any]],
    status_code_review: Any,
    status_qa: Any,
    status_security: Any,
    status_qa_security: Any,
    max_total_cycles: Callable[[Any], int],
    code_review_retry_cap: Callable[[Any], int],
    max_cycles_requires_failing_gate: bool,
    startup_log_message: Callable[..., str],
    gate_issue_log_verb: str,
    parallelize_qa_security: bool,
) -> ExecutionBindings:
    """Bind a team's ``run_execution`` and ``GATE_CONFIG``.

    Preconditions:
        ``models`` satisfies ``PhaseModels``; ``profile`` is the team's
        ``StackProfile``; ``execution_prompt`` / ``parse_files_and_summary``
        are the team's prompt template and output parser; the remaining
        arguments are exactly the fields ``GatedExecutionConfig`` expects
        (``run_code_review_gate``/``run_qa_gate``/``run_security_gate`` are
        the team-authored gate adapters -- see module docstring for why this
        architecture fork is intentional and not collapsed here).
    Postconditions:
        Returns an ``ExecutionBindings`` whose ``run_execution`` delegates to
        ``run_execution_impl`` and whose ``gate_config`` is a
        ``GatedExecutionConfig`` closed over this team's
        ``models``/``execution_prompt``/``profile`` via a freshly built
        ``_run_general_microtask``. The caller (each team's ``phases/_profile.py``)
        assigns ``gate_config`` to its own module-level ``GATE_CONFIG`` and
        defines its own thin ``run_execution_with_review_gates`` wrapper
        referencing that name by bare lookup (see ``ExecutionBindings``'s
        docstring for why).
    """

    def _run_general_microtask(
        *,
        llm: LLMClient,
        microtask: Any,
        task: Task,
        language: str,
        existing_code: str,
        architecture: Optional[SystemArchitecture],
    ) -> Dict[str, str]:
        return _run_general_microtask_impl(
            llm=llm,
            microtask=microtask,
            task=task,
            language=language,
            existing_code=existing_code,
            architecture=architecture,
            execution_prompt=execution_prompt,
            parse_files_and_summary=parse_files_and_summary,
            profile=profile,
            runner=_llm_runner(),
        )

    def run_execution(
        *,
        llm: LLMClient,
        task: Task,
        planning_result: Any,
        repo_path: Path,
        architecture: Optional[SystemArchitecture] = None,
        existing_code: str = "",
        tool_runners: Optional[Dict[Any, Any]] = None,
        progress_callback: Optional[Callable[[int, int, int, str, str, str], None]] = None,
        only_microtask_ids: Optional[List[str]] = None,
    ) -> Any:
        return run_execution_impl(
            llm=llm,
            task=task,
            planning_result=planning_result,
            repo_path=repo_path,
            architecture=architecture,
            existing_code=existing_code,
            tool_runners=tool_runners,
            progress_callback=progress_callback,
            only_microtask_ids=only_microtask_ids,
            models=models,
            run_general_microtask=_run_general_microtask,
        )

    gate_config = GatedExecutionConfig(
        models=models,
        run_general_microtask=_run_general_microtask,
        run_code_review_gate=run_code_review_gate,
        run_qa_gate=run_qa_gate,
        run_security_gate=run_security_gate,
        run_batch_coding_fixes=run_batch_coding_fixes,
        run_documentation_self_review=run_documentation_self_review,
        run_dbc_self_review=run_dbc_self_review,
        status_code_review=status_code_review,
        status_qa=status_qa,
        status_security=status_security,
        status_qa_security=status_qa_security,
        max_total_cycles=max_total_cycles,
        code_review_retry_cap=code_review_retry_cap,
        max_cycles_requires_failing_gate=max_cycles_requires_failing_gate,
        startup_log_message=startup_log_message,
        gate_issue_log_verb=gate_issue_log_verb,
        parallelize_qa_security=parallelize_qa_security,
    )

    return ExecutionBindings(
        run_execution=run_execution,
        gate_config=gate_config,
    )
