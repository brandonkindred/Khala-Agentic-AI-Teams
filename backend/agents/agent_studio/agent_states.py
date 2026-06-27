"""Canonical operating "states of being" seeded onto every authored Studio agent.

Each agent the Studio creates is born with three behavioral states — **planning**,
**executing**, and **researching** — each carrying its own system prompt that
constrains how the agent behaves while in that state:

* **Planning** — produce a plan for the task/problem; do not execute it.
* **Executing** — perform actions / use tools / leverage skills to implement a plan.
* **Researching** — search for, evaluate, and collate relevant information.

These are *seeded but editable*: the three keys are fixed, but a state's
``system_prompt`` can be refined through the build conversation. They are inert
declarative metadata — nothing reads them at invoke time yet (runtime binding is
the same deferred follow-up the saved ``role`` / ``system_prompt`` already carry).

Invariants:
    * ``STATE_ORDER`` is the single source of truth for which keys exist and in
      what order; ``STATE_LABELS`` and ``DEFAULT_STATE_PROMPTS`` have exactly the
      same key set (asserted at import).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from .models import AgentState

# Stable state keys, in seed/display order. The single source of truth for the
# state set — `AgentStateKey` in models.py mirrors these exact literals.
PLANNING_KEY = "planning"
EXECUTING_KEY = "executing"
RESEARCHING_KEY = "researching"

STATE_ORDER: tuple[str, ...] = (PLANNING_KEY, EXECUTING_KEY, RESEARCHING_KEY)

STATE_LABELS: dict[str, str] = {
    PLANNING_KEY: "Planning",
    EXECUTING_KEY: "Executing",
    RESEARCHING_KEY: "Researching",
}

DEFAULT_STATE_PROMPTS: dict[str, str] = {
    PLANNING_KEY: (
        "You are operating in PLANNING mode. Your sole job is to produce a clear, "
        "ordered plan for the given task or problem — you do NOT execute it. Break "
        "the work into concrete, verifiable steps; call out dependencies, "
        "assumptions, the inputs you need, and the risks. Do not call tools to "
        "perform the work or make changes; if information is missing, record it as "
        "an open question in the plan rather than going to fetch it. Output the "
        "structured plan and stop."
    ),
    EXECUTING_KEY: (
        "You are operating in EXECUTING mode. You are given an agreed plan; carry "
        "it out. Use your available tools and skills to perform each step in order "
        "and produce the concrete artifacts the plan calls for. Stay within the "
        "plan's scope — do not redesign it; if a step is blocked or the plan is "
        "wrong, report the blocker and what you changed rather than silently "
        "improvising. Report your progress and the final result of the actions you "
        "took."
    ),
    RESEARCHING_KEY: (
        "You are operating in RESEARCHING mode. Your job is to find, evaluate, and "
        "collate information relevant to the given topic or problem — not to act on "
        "it or implement anything. Gather from the available sources, assess "
        "credibility and relevance, reconcile conflicting findings, and synthesize "
        "a concise, well-organized summary with citations or source references. "
        "Distinguish established facts from inference, and surface gaps. Output the "
        "collated findings and stop."
    ),
}

# Fail loud at import if the three maps ever drift out of sync — they are hand-
# maintained constants, and a missing label/prompt would seed a malformed state.
assert set(STATE_LABELS) == set(STATE_ORDER), "STATE_LABELS keys must match STATE_ORDER"
assert set(DEFAULT_STATE_PROMPTS) == set(STATE_ORDER), (
    "DEFAULT_STATE_PROMPTS keys must match STATE_ORDER"
)


def default_agent_states() -> list[AgentState]:
    """Return a fresh list of the three default operating states.

    A factory (not a module-level constant) so every ``AgentDefinition`` gets its
    own independent ``AgentState`` instances — editing one definition's state
    prompt must never mutate another's.

    Postconditions:
        * Returns exactly three states whose keys equal ``STATE_ORDER`` in order,
          each with the canonical label and a non-empty default ``system_prompt``.
    """
    # Imported lazily to avoid a circular import (models.py imports this module).
    from .models import AgentState

    return [
        AgentState(key=key, label=STATE_LABELS[key], system_prompt=DEFAULT_STATE_PROMPTS[key])
        for key in STATE_ORDER
    ]


def normalize_agent_states(states: list[AgentState]) -> list[AgentState]:
    """Coerce an arbitrary state list into exactly the three fixed states.

    The per-item ``Literal`` key locks each state's identity, but a supplied
    ``list[AgentState]`` is otherwise unconstrained: a client or LLM could send
    ``[]``, a partial list (missing ``executing`` / ``researching``), or duplicate
    keys. This normalizes any such list so the fixed-key-set invariant holds before
    the value is ever persisted — without raising, so neither the save API nor the
    authoring conversation breaks on a partial emission.

    For each canonical key in ``STATE_ORDER`` it keeps the supplied state's
    ``system_prompt`` (last wins on duplicates), or the default prompt when absent,
    and always stamps the canonical label (labels are display-only, not editable).

    Postconditions:
        * Returns exactly ``len(STATE_ORDER)`` states whose keys equal
          ``STATE_ORDER`` in order — one of each fixed key, no duplicates.
    """
    from .models import AgentState

    # Last occurrence of a key wins, so a duplicate edit collapses to one state.
    supplied_prompts = {s.key: s.system_prompt for s in states}
    return [
        AgentState(
            key=key,
            label=STATE_LABELS[key],
            system_prompt=supplied_prompts.get(key, DEFAULT_STATE_PROMPTS[key]),
        )
        for key in STATE_ORDER
    ]
