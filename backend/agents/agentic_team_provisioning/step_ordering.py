"""Pure, dependency-free process-step ordering.

Extracted so both the daemon-thread runner (``runtime/pipeline_runner.py``) and the
Temporal workflow (``temporal/workflows.py``) share one topological-sort implementation
without the workflow having to import the heavyweight runtime module. This module
imports nothing beyond the stdlib, so it is safe to import inside the temporalio
workflow sandbox (which replays it during workflow registration).
"""

from __future__ import annotations


def order_step_ids(steps: list[tuple[str, list[str]]]) -> list[str]:
    """Order step ids by a breadth-first walk of the ``next_steps`` edges.

    Preconditions:
        - ``steps`` is a list of ``(step_id, next_steps)`` pairs. ``step_id`` is a
          non-empty str; ``next_steps`` is a (possibly empty) list of str ids. Order of
          ``steps`` is the caller's original declaration order.

    Postconditions:
        - Returns a list containing each input ``step_id`` exactly once. Entry points
          (ids referenced by no other step's ``next_steps``) seed a breadth-first walk;
          when no entry point exists (a cycle among all steps), the first step seeds it.
          Any step unreachable from the entry points is appended at the end in the
          original input order. Ids appearing in ``next_steps`` but not present as a
          ``step_id`` are ignored (dangling edges never invent a step).
    """
    if not steps:
        return []

    ids = [sid for sid, _ in steps]
    id_set = set(ids)
    all_next: set[str] = set()
    for _, nexts in steps:
        all_next.update(nexts or [])

    entry_ids = [sid for sid in ids if sid not in all_next]
    if not entry_ids:
        entry_ids = [ids[0]]

    next_map = {sid: (nexts or []) for sid, nexts in steps}
    visited: set[str] = set()
    ordered: list[str] = []
    queue = list(entry_ids)
    while queue:
        sid = queue.pop(0)
        if sid in visited or sid not in id_set:
            continue
        visited.add(sid)
        ordered.append(sid)
        queue.extend(next_map.get(sid, []))

    for sid in ids:
        if sid not in visited:
            ordered.append(sid)
    return ordered
