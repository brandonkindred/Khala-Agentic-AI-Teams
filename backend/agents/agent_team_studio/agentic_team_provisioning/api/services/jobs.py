"""Team job-status domain logic for agentic team provisioning HTTP.

Preconditions: callers pass the same request models / ids the former ``main``
    handlers accepted.
Postconditions: behavior matches the pre-split handlers (status codes, bodies).
    Collaborators are read from ``api.main`` at call time so tests can
    ``monkeypatch.setattr(main, …)``.
"""

from __future__ import annotations

from fastapi import HTTPException

from agent_team_studio.agentic_team_provisioning.models import TeamJobDetail, TeamJobSummary


def list_team_jobs(team_id: str):
    """List all jobs for a provisioned team.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with a ``TeamJobSummary`` per job known to the
        team's job client (empty if none exist); ``404`` if the team is not
        found (infrastructure is provisioned on first access via
        ``_get_infra_or_404``, so a 404 here means the team itself is
        unknown, not a missing/failed infra).
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    infra = _main._get_infra_or_404(team_id)
    raw_jobs = infra.job_client.list_jobs() or []
    return [
        TeamJobSummary(
            job_id=j.get("job_id", ""),
            status=j.get("status", "unknown"),
            created_at=j.get("created_at", ""),
            updated_at=j.get("updated_at", ""),
        )
        for j in raw_jobs
    ]


def get_team_job(team_id: str, job_id: str):
    """Get a single job's detail.

    Preconditions: ``team_id`` and ``job_id`` are non-empty strings.
    Postconditions: ``200`` with a ``TeamJobDetail`` wrapping the raw job
        record; ``404`` if the team is not found, or the team is found but
        has no job with ``job_id``.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    infra = _main._get_infra_or_404(team_id)
    job = infra.job_client.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return TeamJobDetail(
        job_id=job.get("job_id", job_id),
        status=job.get("status", "unknown"),
        data=job,
    )
