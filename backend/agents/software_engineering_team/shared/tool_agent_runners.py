"""
Shared tool-agent-runner builder for the code-v2 team orchestrators.

``build_tool_runners`` was byte-identical between
``backend_code_v2_team.orchestrator.BackendDevelopmentAgent._build_tool_runners``
and ``frontend_code_v2_team.orchestrator.FrontendDevelopmentAgent._build_tool_runners``:
both turned a ``{ToolAgentKind: tool_agent_instance}`` map into a
``{ToolAgentKind: callable(ToolAgentInput) -> ToolAgentOutput}`` map for the
Execution phase, preferring ``.run`` over ``.execute``. Each team's registry
(the tool-agent instances themselves) stays per-team; only this binding step
was duplicated.
"""

from __future__ import annotations

from typing import Any, Callable, Dict


def build_tool_runners(tool_agents: Dict[Any, Any]) -> Dict[Any, Callable[[Any], Any]]:
    """Build run callables from tool agent instances (for the Execution phase).

    Preconditions:
        ``tool_agents`` maps a tool-agent-kind key to an instance exposing
        ``run`` and/or ``execute``.
    Postconditions:
        Returns a map of the same keys to whichever of ``.run``/``.execute``
        the instance exposes (``.run`` preferred); an instance with neither is
        omitted.
    """
    runners: Dict[Any, Callable[[Any], Any]] = {}
    for k, ag in tool_agents.items():
        if hasattr(ag, "run"):
            runners[k] = ag.run
        elif hasattr(ag, "execute"):
            runners[k] = ag.execute
    return runners
