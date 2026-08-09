"""Pending-question domain logic for agentic team provisioning HTTP.

Preconditions: callers pass the same request models / ids the former ``main``
    handlers accepted.
Postconditions: behavior matches the pre-split handlers (status codes, bodies).
    Collaborators are read from ``api.main`` at call time so tests can
    ``monkeypatch.setattr(main, …)``.
"""

from __future__ import annotations

from typing import List

from fastapi import HTTPException

from agent_team_studio.agentic_team_provisioning.models import (
    SubmitTeamAnswersRequest,
    TeamPendingQuestion,
)


def list_team_questions(team_id: str):
    """Collect pending questions from all active jobs for a team.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with a ``TeamPendingQuestion`` per pending question
        across every job in ``pending``/``running`` status (empty if none, or
        if the team has no active jobs); ``404`` if the team is not found.
        Jobs outside those two statuses are not queried.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    infra = _main._get_infra_or_404(team_id)
    active_jobs = infra.job_client.list_jobs(statuses=["pending", "running"]) or []
    result: List[TeamPendingQuestion] = []
    for j in active_jobs:
        jid = j.get("job_id", "")
        for q in j.get("pending_questions", []):
            result.append(TeamPendingQuestion(job_id=jid, question=q))
    return result


def submit_team_answers(team_id: str, job_id: str, req: SubmitTeamAnswersRequest):
    """Submit answers to pending questions for a job.

    Preconditions: ``team_id`` and ``job_id`` are non-empty strings; ``req``
        carries the answers to append.
    Postconditions: ``200`` with ``{"job_id", "message"}`` and the job's
        record updated — ``pending_questions`` cleared, ``waiting_for_answers``
        set to ``False``, and ``req.answers`` appended to
        ``submitted_answers`` — via an atomic update; ``404`` if the team or
        the job is not found (job unchanged in that case).
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    infra = _main._get_infra_or_404(team_id)
    job = infra.job_client.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    infra.job_client.atomic_update(
        job_id,
        merge_fields={"pending_questions": [], "waiting_for_answers": False},
        append_to={"submitted_answers": req.answers},
    )
    return {"job_id": job_id, "message": "Answers submitted"}
