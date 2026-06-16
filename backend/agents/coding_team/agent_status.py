"""Derive a per-agent status roster for a coding-team job from its persisted state.

The coding-team status endpoint surfaces *which* agents exist and what each is doing, so the
UI can render per-agent cards (which agent is working now, a status per agent, what the team
is working on). The roster is derived — not stored — from four pieces of already-persisted
job state: the stack specs (the engineer roster), the agent->task map (who holds which
non-merged task), the task-graph snapshot (task titles + statuses), and the current_activity
(the single live review sub-step). Keeping the derivation here, as a pure function, keeps the
status endpoint thin and makes the logic unit-testable without a job store.

The Tech Lead is the coordinator: it is never present in ``agent_task_map`` (only Senior SWEs
are assigned tasks via ``TaskGraphService.assign_task_to_agent``), so its card is always
synthesized here regardless of the persisted state.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, NamedTuple, Optional

from coding_team.models import AgentStatusEntry

# The synthetic, stable id for the Tech Lead coordinator card (never a key in agent_task_map).
TECH_LEAD_AGENT_ID = "tech_lead"

# The ``current_activity.agent`` literal the Tech Lead's merge review reports under. The other
# literal in the system is ``"code_review"`` (a quality gate that runs on an engineer's task).
_TECH_LEAD_ACTIVITY_AGENT = "tech_lead_review"


class StackRosterEntry(NamedTuple):
    """One Senior SWE slot derived from a stack spec. ``agent_id == display_name`` (the
    agent_task_map key the orchestrator writes); ``tools_services`` is the stack's tool list."""

    agent_id: str
    display_name: str
    tools_services: List[str]


def derive_stack_roster(stacks_raw: List[Dict[str, Any]]) -> List[StackRosterEntry]:
    """Map raw stack specs to ``(agent_id, display_name, tools_services)``, one per stack.

    This MUST stay faithful to how the orchestrator names Senior SWE agents (the
    ``run_coding_team_orchestrator`` worker-build loop), because the returned ``agent_id`` is
    the exact key the orchestrator writes into ``agent_task_map``. A stack with no name falls
    back to ``f"stack_{i}"``, and that same value is the agent id — so the two sides cannot
    drift, the orchestrator and this module call this single helper.

    Preconditions:
        - ``stacks_raw`` is normally a list (possibly empty). Each entry is normally a dict that
          may carry ``name`` (str) and ``tools_services`` (list[str]); malformed/non-dict
          entries are tolerated and treated as empty.
    Postconditions:
        - Returns one entry per input, in order. ``display_name`` is the entry's ``name`` when
          truthy else ``f"stack_{i}"``. ``agent_id`` equals ``display_name`` unless that name was
          already used, in which case a ``_N`` suffix makes it unique (e.g. two ``"backend"``
          stacks yield ids ``"backend"`` and ``"backend_2"``), so distinct stacks never collide on
          one ``agent_task_map`` key. ``tools_services`` is always a list (a copy; empty when
          absent or malformed). A non-list ``stacks_raw`` yields an empty roster. Never raises.
    """
    if not isinstance(stacks_raw, list):
        return []
    roster: List[StackRosterEntry] = []
    used_ids: set[str] = set()
    for i, entry in enumerate(stacks_raw):
        spec = entry if isinstance(entry, dict) else {}
        name = spec.get("name") or f"stack_{i}"
        tools = spec.get("tools_services")
        tools = list(tools) if isinstance(tools, list) else []
        # Make the agent_id GLOBALLY unique so the orchestrator assigns each stack a distinct
        # agent_task_map entry (and the UI shows distinct cards) instead of one overwriting the
        # other. Bump the suffix until the id is free — this also covers a stack whose literal name
        # collides with a suffix generated for an earlier duplicate (e.g. "backend", "backend",
        # "backend_2" -> "backend", "backend_2", "backend_3"). The display name keeps the original.
        agent_id = name
        count = 1
        while agent_id in used_ids:
            count += 1
            agent_id = f"{name}_{count}"
        used_ids.add(agent_id)
        roster.append(StackRosterEntry(agent_id=agent_id, display_name=name, tools_services=tools))
    return roster


def _coerce_fraction(value: Any) -> Optional[float]:
    """Return a float in the closed unit interval, or None for anything non-numeric.

    Postconditions: bools are rejected (``True`` is not a fraction); non-finite values (NaN, ±inf)
    are rejected — NaN in particular would survive the clamp and then fail the model's [0, 1]
    constraint, raising on the status endpoint; finite numeric values are clamped to [0.0, 1.0] so
    a corrupt record can never drive an out-of-range sub-bar.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    fraction = float(value)
    if not math.isfinite(fraction):
        return None
    return min(max(fraction, 0.0), 1.0)


def build_agent_statuses(
    stack_specs: List[Dict[str, Any]],
    agent_task_map: Dict[str, str],
    task_graph_snapshot: List[Dict[str, Any]],
    current_activity: Optional[Dict[str, Any]],
    phase: Optional[str],
) -> List[AgentStatusEntry]:
    """Derive the per-agent status roster for a coding-team job.

    Pure: reads only its arguments, performs no I/O, and never raises on malformed input — a
    corrupt job record must degrade the cards, not break the status endpoint. The roster is
    the Tech Lead (always, first) followed by one Senior SWE per entry in ``stack_specs``.

    Preconditions:
        - ``stack_specs`` is a list of stack dicts (may be empty — old or SE-pipeline records
          carry none, which yields a Tech-Lead-only roster). Engineer ids are derived from it
          via :func:`derive_stack_roster`, matching the orchestrator's assignment keys.
        - ``agent_task_map`` maps an engineer ``agent_id`` to the id of its current non-merged
          task (the only entries the orchestrator ever writes).
        - ``task_graph_snapshot`` is the list of task dicts (each with ``id``/``title``/``status``).
        - ``current_activity`` is the single live review sub-step dict, or None; only a dict is
          honoured.
        - ``phase`` is the job phase string, or None.
    Postconditions:
        - Returns ``[tech_lead, *engineers]`` in stack order. Each engineer's ``status`` is
          ``working`` (its task is in_progress), ``in_review`` (its task is in_review), or
          ``idle`` (no live task). The Tech Lead is ``planning`` during ``task_graph``,
          ``reviewing`` while a merge review runs or any task is in_review, else ``idle``.
        - The single ``current_activity`` is overlaid onto exactly one agent: the Tech Lead
          when it is a ``tech_lead_review`` (checked first — that activity also carries the
          engineer's task_id, which is still mapped while the task is in_review), otherwise the
          engineer that owns the activity's task.
    """
    # Coerce malformed inputs so the function never raises (derive_stack_roster guards stack_specs
    # itself). A corrupt record with e.g. task_graph_snapshot=None must degrade, not crash.
    activity = current_activity if isinstance(current_activity, dict) else None
    agent_task_map = agent_task_map if isinstance(agent_task_map, dict) else {}
    task_graph_snapshot = task_graph_snapshot if isinstance(task_graph_snapshot, list) else []
    tasks_by_id: Dict[Any, Dict[str, Any]] = {
        t.get("id"): t for t in task_graph_snapshot if isinstance(t, dict) and t.get("id")
    }
    activity_agent = activity.get("agent") if activity else None
    activity_task_id = activity.get("task_id") if activity else None

    # The single live activity is overlaid onto exactly one agent, and is passed into that agent's
    # constructor rather than mutated in afterwards. A tech_lead_review goes to the Tech Lead; any
    # other activity goes to the first engineer whose current task matches its task_id. Branching
    # on the agent first matters because an in_review task is still mapped to its engineer, so a
    # tech_lead_review carries that engineer's task_id too.
    overlay: Dict[str, Any] = (
        {
            "current_step": activity.get("step"),
            "activity_detail": activity.get("detail"),
            "activity_fraction": _coerce_fraction(activity.get("fraction")),
        }
        if activity is not None
        else {}
    )
    overlay_for_engineers = overlay if activity_agent != _TECH_LEAD_ACTIVITY_AGENT else {}

    # --- Engineer cards (one per stack) ------------------------------------------------
    engineers: List[AgentStatusEntry] = []
    overlay_used = False
    for agent_id, display_name, tools in derive_stack_roster(stack_specs):
        task_id = agent_task_map.get(agent_id)
        task = tasks_by_id.get(task_id) if task_id else None
        status = "idle"
        current_task_id: Optional[str] = None
        current_task_title: Optional[str] = None
        if task is not None:
            current_task_id = task.get("id")
            current_task_title = task.get("title")
            # The map only ever holds in_progress/in_review tasks (merge/fail frees the agent),
            # so anything that is not in_review is active implementation work.
            status = "in_review" if task.get("status") == "in_review" else "working"
        # Apply the overlay to the first engineer that owns the activity's task, once. The
        # ``activity_task_id`` truthy check keeps a task-less activity from matching an idle
        # engineer (whose current_task_id is None) via ``None == None``.
        fields: Dict[str, Any] = {}
        if (
            overlay_for_engineers
            and not overlay_used
            and activity_task_id
            and activity_task_id == current_task_id
        ):
            fields = overlay_for_engineers
            overlay_used = True
        engineers.append(
            AgentStatusEntry(
                agent_id=agent_id,
                role="senior_engineer",
                display_name=f"Senior Engineer — {display_name}",
                stack=display_name,
                tools_services=tools,
                status=status,
                current_task_id=current_task_id,
                current_task_title=current_task_title,
                **fields,
            )
        )

    # --- Tech Lead card (coordinator) --------------------------------------------------
    # Reuse the already-filtered/indexed tasks rather than re-scanning the raw snapshot.
    any_in_review = any(t.get("status") == "in_review" for t in tasks_by_id.values())
    if phase == "task_graph":
        tl_status = "planning"
    elif activity_agent == _TECH_LEAD_ACTIVITY_AGENT or any_in_review:
        tl_status = "reviewing"
    else:
        tl_status = "idle"
    tl_fields = overlay if activity_agent == _TECH_LEAD_ACTIVITY_AGENT else {}
    tech_lead = AgentStatusEntry(
        agent_id=TECH_LEAD_AGENT_ID,
        role="tech_lead",
        display_name="Tech Lead",
        stack=None,
        tools_services=[],
        status=tl_status,
        **tl_fields,
    )

    return [tech_lead, *engineers]
