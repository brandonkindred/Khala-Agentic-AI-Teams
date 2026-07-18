"""Best-effort ``planning_runs`` audit write for SE-embedded planning invocations.

``software_engineering_team`` invokes ``planning_team.orchestrator.run_workflow``
directly (thread mode: ``orchestrator.py``; Temporal mode: ``temporal/activities.py``),
bypassing planning_team's own job-store finalize hooks that normally trigger
``planning_team.postgres.writer.record_planning_run``. This module lets SE record the
same audit row using its own job_id, so a planning phase run through SE is not
invisible to the ``planning_runs`` audit table.

Both SE call sites call the monolithic ``run_workflow()`` directly and share one
result shape, so one helper covers both — unlike planning_team's own Temporal path,
which decomposes into per-phase activities and derives its audit fields from a
live job-store read instead.
"""

from __future__ import annotations

from typing import Any, Dict


def record_se_planning_run(job_id: str, planning_result: Dict[str, Any]) -> bool:
    """Write a ``planning_runs`` audit row for a successful SE-embedded planning run.

    Preconditions:
        - ``job_id`` is software_engineering_team's own non-empty job id.
        - ``planning_result`` is the dict returned by a successful call to
          ``planning_team.orchestrator.run_workflow`` (``planning_result["success"]``
          is ``True``). Callers must not invoke this for a failed planning run.
    Postconditions:
        - Returns ``True`` if the audit row was written or already existed (the
          underlying write is idempotent per job_id), ``False`` if Postgres is
          disabled or the write failed operationally.
    Raises:
        - ``ValueError`` if ``job_id`` is blank, propagated from
          ``record_planning_run`` — a caller contract violation.
    """
    from planning_team.postgres.writer import record_planning_run

    handoff = planning_result.get("handoff_package") or {}
    # open_questions/resolved_questions come from planning_result's own top-level
    # keys, not handoff_package's copies (deliberately left empty so a non-empty
    # handoff doesn't pause every SE-driven run) — see planning_team/orchestrator.py.
    return record_planning_run(
        job_id,
        client_name=None,
        summary=planning_result.get("summary") or "",
        handoff_summary=handoff.get("summary") or "",
        open_questions=planning_result.get("open_questions") or [],
        resolved_questions=planning_result.get("resolved_questions") or [],
    )


__all__ = ["record_se_planning_run"]
