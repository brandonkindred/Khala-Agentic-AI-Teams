"""Team form-record (database) domain logic for agentic team provisioning HTTP.

Preconditions: callers pass the same request models / ids the former ``main``
    handlers accepted.
Postconditions: behavior matches the pre-split handlers (status codes, bodies).
    Collaborators are read from ``api.main`` at call time so tests can
    ``monkeypatch.setattr(main, …)``.
"""

from __future__ import annotations

from fastapi import HTTPException

from agent_team_studio.agentic_team_provisioning.models import (
    CreateFormRecordRequest,
    FormRecord,
    UpdateFormRecordRequest,
)


def list_team_form_keys(team_id: str):
    """List distinct form keys that have records.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with the distinct ``form_key`` values that have at
        least one record (empty if none); ``404`` if the team is not found.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    infra = _main._get_infra_or_404(team_id)
    return infra.form_store.list_form_keys()


def list_team_form_records(team_id: str, form_key: str):
    """Get all records for a form key.

    Preconditions: ``team_id`` and ``form_key`` are non-empty strings.
    Postconditions: ``200`` with a ``FormRecord`` per stored record for
        ``form_key`` (empty list, not 404, if the key has no records);
        ``404`` if the team is not found.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    infra = _main._get_infra_or_404(team_id)
    rows = infra.form_store.get_records(form_key)
    return [FormRecord(**r) for r in rows]


def create_team_form_record(team_id: str, form_key: str, req: CreateFormRecordRequest):
    """Create a new form record.

    Preconditions: ``team_id`` and ``form_key`` are non-empty strings; ``req``
        carries the record's field data.
    Postconditions: ``201`` with the newly created ``FormRecord`` (a fresh
        ``record_id`` assigned by the store); ``404`` if the team is not
        found (no record created).
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    infra = _main._get_infra_or_404(team_id)
    record = infra.form_store.create_record(form_key, req.data)
    return FormRecord(**record)


def update_team_form_record(
    team_id: str, form_key: str, record_id: str, req: UpdateFormRecordRequest
):
    """Update an existing form record.

    Preconditions: ``team_id``, ``form_key``, and ``record_id`` are non-empty
        strings; ``req`` carries the field data to write.
    Postconditions: ``200`` with the updated ``FormRecord`` re-read from the
        store; ``404`` if the team is not found, if no record with
        ``record_id`` exists under ``form_key`` (update is a no-op in that
        case), or in the narrow race where the record is deleted between the
        update and the follow-up read.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    infra = _main._get_infra_or_404(team_id)
    if not infra.form_store.update_record(form_key, record_id, req.data):
        raise HTTPException(status_code=404, detail="Record not found")
    record = infra.form_store.get_record(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found after update")
    return FormRecord(**record)


def delete_team_form_record(team_id: str, form_key: str, record_id: str):
    """Delete a form record.

    Preconditions: ``team_id``, ``form_key``, and ``record_id`` are non-empty
        strings.
    Postconditions: ``204`` with the record removed when it existed under
        ``form_key``; ``404`` if the team is not found, or no such record
        exists (store unchanged in that case).
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    infra = _main._get_infra_or_404(team_id)
    if not infra.form_store.delete_record(form_key, record_id):
        raise HTTPException(status_code=404, detail="Record not found")
